"""Phase 2 campaign flow — offline route tests + hook override + capacity scheduling.

Keyboard sequence is tested at the route layer (Flask test client) by
monkeypatching the DB context manager to point at a persistent sqlite in-memory
connection. No Playwright / no real browser is required for these; a separate
playwright-gated test covers the live JS wiring.
"""
import csv
import io
import json
import sqlite3
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

from capacity import Mailbox, SequenceSchedule
import capacity as capacitymod
import campaigns as campaignsmod
import db as dbmod
import export as exportmod
import emailer
from flask import render_template


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

def _make_leads_schema(conn):
    """Bare sqlite schema for leads — just enough for the route tests."""
    conn.execute("""
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER, company_name TEXT, first_name TEXT, last_name TEXT,
            email TEXT, website_url TEXT, linkedin_url TEXT, title TEXT, job_title TEXT,
            status TEXT, email_subject TEXT, email_body TEXT, email_sent_at TEXT,
            personalization_hooks TEXT, trigger_hook TEXT, hook_override TEXT,
            review_status TEXT, review_segment_override TEXT, reviewed_at TEXT,
            is_duplicate INTEGER DEFAULT 0, domain_normalized TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE lead_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, needs_human_review INTEGER DEFAULT 0
        )
    """)
    conn.commit()


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _make_leads_schema(c)
    return c


@pytest.fixture()
def client(conn, monkeypatch):
    """Flask test client with DB pointed at the persistent in-memory sqlite."""
    import app as appmod

    # Stub out ensure_table / complex DB helpers that hit PG
    monkeypatch.setattr(campaignsmod, "ensure_table", lambda c: None)

    # Patch open_db to yield our sqlite conn
    from contextlib import contextmanager
    @contextmanager
    def _open():
        yield conn
    monkeypatch.setattr(appmod, "open_db", _open)

    # Skip schema init (needs PG)
    monkeypatch.setattr(appmod, "_init_done", True)

    # get_leads_with_scores: SELECT * FROM leads (sqlite)
    def _fake_glws(c, session_id=None, owner_id=None):
        rows = conn.execute("SELECT * FROM leads").fetchall()
        return [dict(r) for r in rows]
    monkeypatch.setattr(appmod.dbmod, "get_leads_with_scores", _fake_glws)

    # get_analysis_session: return a dummy row for _require_session
    def _fake_gas(c, sid):
        return {"id": sid, "label": f"sess-{sid}", "owner_id": None}
    monkeypatch.setattr(appmod.dbmod, "get_analysis_session", _fake_gas)

    # Disable DNC + export-history side effects in ship route
    monkeypatch.setattr(appmod.dncmod, "add_many_from_leads", lambda *a, **kw: None)
    monkeypatch.setattr(appmod.dbmod, "record_export", lambda *a, **kw: None)

    # Stub user lookup for _require_login (queries users table which doesn't exist in sqlite)
    def _fake_get_user(c, uid):
        return {"id": uid, "email": "test@test.com", "role": "admin", "is_active": True}
    monkeypatch.setattr(appmod.dbmod, "get_user_by_id", _fake_get_user)

    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False
    c = appmod.app.test_client()
    # Inject an authenticated session so before_request doesn't redirect to login
    with c.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "admin"
    return c


def _insert_lead(conn, session_id, **kw):
    cols = "session_id, company_name, first_name, last_name, email, website_url, title"
    vals = [session_id, kw.get("company", "Co"), kw.get("first", "A"), kw.get("last", "B"),
            kw.get("email", "a@co.com"), kw.get("website", "https://co.com"), kw.get("title", "")]
    conn.execute(f"INSERT INTO leads ({cols}) VALUES (?,?,?,?,?,?,?)", vals)
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_leads(conn, session_id, n, **kw):
    ids = []
    for i in range(n):
        lid = _insert_lead(conn, session_id,
                           company=f"Co{i}", first=f"First{i}", email=f"{i}@co.com",
                           website=f"https://co{i}.com", **kw)
        ids.append(lid)
    return ids


# ──────────────────────────────────────────────────────────────────────
# Emailer hook override (build_prompt)
# ──────────────────────────────────────────────────────────────────────

def test_build_prompt_uses_hook_override_when_set():
    lead = {
        "company_name": "ACME",
        "first_name": "Joe",
        "personalization_hooks": '[{"hook":"AI scoring"}]',
        "hook_override": "Saw you spoke at HealthTech Summit",
    }
    prompt = emailer.build_prompt(lead, "")
    assert "Saw you spoke at HealthTech Summit" in prompt
    assert "AI scoring" not in prompt


def test_build_prompt_falls_back_to_personalization_hooks():
    lead = {
        "company_name": "ACME",
        "first_name": "Joe",
        "personalization_hooks": '[{"hook":"AI scoring"}]',
        "hook_override": None,
    }
    prompt = emailer.build_prompt(lead, "")
    assert "AI scoring" in prompt


# ──────────────────────────────────────────────────────────────────────
# DB hook override helper
# ──────────────────────────────────────────────────────────────────────

def test_update_lead_hook_override_persists(conn):
    lid = _insert_lead(conn, 1)
    assert conn.execute("SELECT hook_override FROM leads WHERE id=?", (lid,)).fetchone()[0] is None
    dbmod.update_lead_hook_override(conn, lid, "Override text")
    assert conn.execute("SELECT hook_override FROM leads WHERE id=?", (lid,)).fetchone()[0] == "Override text"
    dbmod.update_lead_hook_override(conn, lid, None)
    assert conn.execute("SELECT hook_override FROM leads WHERE id=?", (lid,)).fetchone()[0] is None


# ──────────────────────────────────────────────────────────────────────
# Keyboard sequence — 20 decisions via POST
# ──────────────────────────────────────────────────────────────────────

def test_campaign_keyboard_sequence_20_decisions(conn, client, monkeypatch):
    monkeypatch.setattr(campaignsmod, "create", lambda conn, rid, name=None: 1)
    monkeypatch.setattr(campaignsmod, "get", lambda conn, cid: {
        "id": cid, "recipe_id": 1, "session_id": 99, "status": "reviewing",
        "name": "test", "created_at": None, "shipped_at": None, "ship_plan": None,
    })

    ids = _insert_leads(conn, 99, n=20)
    for lid in ids:
        assert conn.execute("SELECT review_status FROM leads WHERE id=?", (lid,)).fetchone()[0] is None

    for i, lid in enumerate(ids):
        decision = "APPROVED" if i % 2 == 0 else "REJECTED"
        resp = client.post(f"/campaign/1/review", data={
            "lead_id": lid, "decision": decision,
        })
        body = resp.get_json()
        assert body["ok"], body
        assert body["remaining"] == 19 - i

    for lid in ids:
        rs = conn.execute("SELECT review_status FROM leads WHERE id=?", (lid,)).fetchone()[0]
        assert rs in dbmod.VALID_REVIEW_STATUSES


def test_campaign_review_lead_unknown_lead_returns_404(conn, client, monkeypatch):
    monkeypatch.setattr(campaignsmod, "get", lambda conn, cid: {
        "id": 1, "session_id": 99, "status": "reviewing",
        "recipe_id": 1, "name": "", "created_at": None, "shipped_at": None, "ship_plan": None,
    })
    resp = client.post("/campaign/1/review", data={"lead_id": 9999, "decision": "APPROVED"})
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Ship route — schedule + plan + DNC + export recording
# ──────────────────────────────────────────────────────────────────────

def test_campaign_ship_schedule_and_plan(conn, client, monkeypatch):
    monkeypatch.setattr(campaignsmod, "create", lambda conn, rid, name=None: 1)
    _camp = {
        "id": 1, "recipe_id": 1, "session_id": 99, "status": "reviewing",
        "name": "ship-test", "created_at": None, "shipped_at": None, "ship_plan": None,
    }
    def _get(c, cid):
        return _camp
    monkeypatch.setattr(campaignsmod, "get", _get)
    monkeypatch.setattr(campaignsmod, "mark_shipped", lambda c, cid, ship_plan=None: _camp.update({"status": "shipped", "ship_plan": ship_plan}))

    _insert_leads(conn, 99, n=5)
    # Mark first 3 as APPROVED
    for lid in conn.execute("SELECT id FROM leads LIMIT 3").fetchall():
        conn.execute("UPDATE leads SET review_status='APPROVED' WHERE id=?", (lid[0],))
    conn.commit()

    # Provide fleet capacity
    from app import _fleet_mailboxes, _sent_contacts
    monkeypatch.setattr("app._fleet_mailboxes", lambda cfg: [Mailbox("box", 30)])
    monkeypatch.setattr("app._sent_contacts", lambda c: [])

    resp = client.post("/campaign/1/ship", follow_redirects=False)
    assert resp.status_code == 302  # redirect after ship
    assert _camp["status"] == "shipped"
    plan = json.loads(_camp["ship_plan"])
    assert len(plan["batches"]) == 1
    assert len(plan["batches"][0][1]) == 3  # 3 approved leads


def test_schedule_sees_shipped_leads_from_prior_campaign(conn, monkeypatch):
    """Regression for the cross-campaign capacity bug: shipping campaign A
    commits T1/T2/T3 slots for its leads on their batch dates. When campaign B
    is scheduled the same day it must SEE A's reservations, else each campaign
    looks capacity-safe in isolation while the two together bust the cap.

    (This test asserts the shipped-plan reservation is load-bearing: with it
    removed, B gets 10 leads on `start` instead of 0.)"""
    from app import _sent_contacts
    monkeypatch.setattr(campaignsmod, "ensure_table", lambda c: None)
    seq = capacitymod.SequenceSchedule(offsets=(0, 3, 7))
    start = date(2026, 9, 7)
    mb = Mailbox("box", 30)

    # Ship one campaign previously: 10 leads reserved on `start`.
    conn.execute(
        "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, session_id INTEGER, "
        "status TEXT, ship_plan TEXT)"
    )
    conn.execute(
        "INSERT INTO campaigns (status, ship_plan) VALUES ('shipped', ?)",
        (json.dumps({"batches": [[start.isoformat(), list(range(1, 11))]]}),),
    )
    conn.commit()

    contacts = _sent_contacts(conn)
    shipped = [c for c in contacts if c.get("touches_sent") == 0]
    assert len(shipped) == 10
    assert all(c["added_on"] == start for c in shipped)

    # Now schedule campaign B's 2 leads on the same day.
    batches = capacitymod.schedule_by_capacity([mb], seq, contacts, start, [1001, 1002])
    first_day = [lid for d, ls in batches if d == start for lid in ls]
    assert first_day == [], f"expected B pushed off day {start}, got {first_day}"
    # Both land on the next day, which still has headroom.
    second_day = {d.isoformat(): ls for d, ls in batches}
    assert len(second_day[(start + timedelta(days=1)).isoformat()]) == 2


def test_sent_contacts_dedups_shipped_and_sent(conn, monkeypatch):
    """A lead that appears in a shipped plan AND has email_sent_at set must be
    counted once — email_sent_at wins (touches_sent=1), the plan entry (0) is
    skipped, or T2/T3 would be reserved twice."""
    from app import _sent_contacts
    monkeypatch.setattr(campaignsmod, "ensure_table", lambda c: None)
    start = date(2026, 9, 7)
    conn.execute(
        "CREATE TABLE campaigns (id INTEGER PRIMARY KEY, session_id INTEGER, "
        "status TEXT, ship_plan TEXT)"
    )
    conn.execute(
        "INSERT INTO campaigns (status, ship_plan) VALUES ('shipped', ?)",
        (json.dumps({"batches": [[start.isoformat(), [1, 2]]]}),),
    )
    # Lead 1 was actually sent; lead 2 is shipped-but-not-sent.
    conn.execute(
        "INSERT INTO leads (id, session_id, email_sent_at) VALUES (1, 1, '2026-09-05T10:00:00Z')"
    )

    contacts = _sent_contacts(conn)
    assert len(contacts) == 2
    done = {c["touches_sent"] for c in contacts}
    assert done == {0, 1}
    sent1 = [c for c in contacts if c["touches_sent"] == 1]
    assert sent1[0]["added_on"] == date(2026, 9, 5)


# ──────────────────────────────────────────────────────────────────────
# Batch download
# ──────────────────────────────────────────────────────────────────────

def test_campaign_batch_download_regenerates_csv(conn, client, monkeypatch):
    monkeypatch.setattr(campaignsmod, "ensure_table", lambda c: None)
    monkeypatch.setattr(campaignsmod, "get", lambda c, cid: {
        "id": cid, "session_id": 99, "status": "shipped",
        "recipe_id": 1, "name": "", "created_at": None, "shipped_at": None,
        "ship_plan": json.dumps({"batches": [["2026-09-07", [101, 102]]]})
    })

    conn.execute(
        "INSERT INTO leads (id, session_id, email, first_name, company_name) VALUES (101, 99, 'a@x.com', 'A', 'Co1')"
    )
    conn.execute(
        "INSERT INTO leads (id, session_id, email, first_name, company_name) VALUES (102, 99, 'b@x.com', 'B', 'Co2')"
    )
    conn.commit()

    resp = client.get("/campaign/1/batch/0/download")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/csv")
    text = resp.get_data(as_text=True)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) == 2
    assert {r["email"] for r in rows} == {"a@x.com", "b@x.com"}


# ──────────────────────────────────────────────────────────────────────
# View counts
# ──────────────────────────────────────────────────────────────────────

def test_campaign_view_shows_counts(conn, client, monkeypatch):
    monkeypatch.setattr(campaignsmod, "get", lambda c, cid: {
        "id": cid, "session_id": 99, "status": "reviewing",
        "recipe_id": 1, "name": "counts", "created_at": None, "shipped_at": None, "ship_plan": None,
    })
    ids = _insert_leads(conn, 99, n=5)
    conn.execute("UPDATE leads SET review_status='APPROVED' WHERE id=?", (ids[0],))
    conn.execute("UPDATE leads SET review_status='REJECTED' WHERE id=?", (ids[1],))
    conn.commit()

    resp = client.get("/campaign/1")
    text = resp.get_data(as_text=True)
    assert "Approved:" in text and "Rejected:" in text


# ──────────────────────────────────────────────────────────────────────
# Playwright-gated live keyboard test
# ──────────────────────────────────────────────────────────────────────

def test_live_keyboard_wiring():
    """Offline guard: the queue page template wires the JS endpoints and key
    handlers correctly (asserted on the raw HTML string). A live-browser test
    requires Playwright and is guarded separately."""
    import app as appmod
    with appmod.app.test_request_context("/campaign/77/review"):
        html = render_template("campaign_queue.html", camp={"id": 77}, cards=[
            {"id": 1, "company": "Co1", "name": "A B", "email": "a@x.com",
             "title": "CEO", "segment": "good", "offer": "free",
             "hooks": ["hook1"], "trigger_hook": "T", "hook_override": "", "website": "https://x.com"},
            {"id": 2, "company": "Co2", "name": "C D", "email": "b@x.com",
             "title": "CTO", "segment": "good", "offer": "paid",
             "hooks": [], "trigger_hook": "", "hook_override": "", "website": ""},
        ], total=2)
    assert "window.location.href" in html          # POSTs to same URL
    assert "campaign/77/review" not in html        # uses current href, not hardcoded
    assert "QUEUE" in html  # JS array injected
    assert "addEventListener" in html
    assert "'a'" in html and "'A'" in html          # A key mapped
    assert "'x'" in html and "'X'" in html          # X key mapped
    assert "'e'" in html and "'E'" in html          # E key mapped


# ──────────────────────────────────────────────────────────────────────
# Playwright live browser test (gated)
# ──────────────────────────────────────────────────────────────────────

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
def test_live_keyboard_playwright():
    """Requires `pip install playwright && playwright install chromium`.
    Verifies the A/X/E/Enter key wiring drives the actual JS + POST cycle."""
    from app import app as flask_app
    import app as appmod
    import threading

    # Build a tiny sqlite DB with 3 leads
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.row_factory = sqlite3.Row
    _make_leads_schema(mem)
    for i in range(1, 4):
        mem.execute(
            "INSERT INTO leads (id, session_id, email, first_name, company_name, website_url) "
            "VALUES (?, 1, ?, 'F', 'Co', 'https://co.com')",
            (i, f"{i}@co.com"),
        )
    mem.commit()

    from contextlib import contextmanager
    @contextmanager
    def _open():
        yield mem

    # Patch
    flask_app.config["TESTING"] = True
    orig_open = getattr(appmod, "open_db")
    orig_glws = getattr(appmod.dbmod, "get_leads_with_scores")
    orig_gas = getattr(appmod.dbmod, "get_analysis_session")
    try:
        appmod.open_db = _open

        def _glws(c, session_id=None, owner_id=None):
            return [dict(r) for r in mem.execute("SELECT * FROM leads").fetchall()]
        appmod.dbmod.get_leads_with_scores = _glws

        def _gas(c, sid):
            return {"id": sid, "label": "s", "owner_id": None}
        appmod.dbmod.get_analysis_session = _gas

        campaignsmod.ensure_table = lambda c: None

        # Campaign in context
        camp_store = {"id": 1, "session_id": 1, "status": "reviewing",
                      "recipe_id": 1, "name": "", "created_at": None, "shipped_at": None, "ship_plan": None}
        def _cget(c, cid): return dict(camp_store)
        campaignsmod.get = _cget

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            # Run Flask in background thread
            server = threading.Thread(
                target=lambda: flask_app.run(port=5555, use_reloader=False), daemon=True)
            server.start()
            page.goto("http://localhost:5555/campaign/1/review")
            page.wait_for_selector(".q-card.active")
            # A → approve lead 1
            page.keyboard.press("a")
            page.wait_for_function("document.getElementById('lead-counter').textContent.includes('Remaining: 2')")
            # E → type hook + Enter
            page.keyboard.press("e")
            page.fill("#hook-1", "test hook")
            page.keyboard.press("Enter")
            page.wait_for_function("document.getElementById('lead-counter').textContent.includes('Remaining: 1')")
            # X → reject
            page.keyboard.press("x")
            page.wait_for_function("document.getElementById('lead-counter').textContent.includes('Remaining: 0')")
            browser.close()

        # Verify DB state
        for lid, expected in [(1, "APPROVED"), (2, "REJECTED"), (3, None)]:
            rs = mem.execute("SELECT review_status FROM leads WHERE id=?", (lid,)).fetchone()[0]
            assert rs == expected, f"lead {lid}: expected {expected}, got {rs}"
        hook = mem.execute("SELECT hook_override FROM leads WHERE id=2").fetchone()[0]
        assert hook == "test hook"
    finally:
        appmod.open_db = orig_open
        appmod.dbmod.get_leads_with_scores = orig_glws
        appmod.dbmod.get_analysis_session = orig_gas
        campaignsmod.get = _cget if "_cget" in dir() else campaignsmod.get
        campaignsmod.ensure_table = campaignsmod.ensure_table
        mem.close()
