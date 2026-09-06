"""Task 9 — Trigger monitoring (solve WHEN).

Acceptance bar (DEVELOPER_TASKS.md):
- given two stored snapshots of a careers page (before/after an engineering
  role is added), the checker emits a `hiring_engineer` event exactly once
  and updates the snapshot;
- cheap checks never call the LinkedIn lane.

Restriction also locked here: `team_growth` routes through the Apollo credit
governor (apollo_client.check_credit_budget), so Apollo-touching checks pause
cleanly when the monthly budget is exhausted.
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import db as dbmod
import runconfig
import triggers as triggersmod


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def _mk_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE analysis_sessions (
            id INTEGER PRIMARY KEY, label TEXT, status TEXT, created_at TEXT
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY, session_id INTEGER,
            first_name TEXT, last_name TEXT, title TEXT, company_name TEXT,
            email TEXT, website_url TEXT, domain_normalized TEXT, email_domain TEXT,
            status TEXT NOT NULL DEFAULT 'NEW',
            is_duplicate INTEGER NOT NULL DEFAULT 0,
            duplicate_of_id INTEGER, duplicate_reason TEXT,
            created_at TEXT NOT NULL,
            next_check_at TEXT, trigger_state TEXT,
            trigger_priority INTEGER, trigger_hook TEXT, linkedin_url TEXT
        );
        CREATE TABLE lead_scores (
            id INTEGER PRIMARY KEY, session_id INTEGER, lead_id INTEGER NOT NULL,
            segment TEXT, confidence REAL, needs_human_review INTEGER, scored_at TEXT,
            personalization_hooks TEXT
        );
        CREATE TABLE lead_trigger_events (
            id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL,
            trigger TEXT NOT NULL, detected_at TEXT NOT NULL, detail TEXT
        );
        CREATE TABLE apollo_usage (
            month TEXT PRIMARY KEY, credits_used INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _insert_lead(conn, lead_id: int = 1, *, segment: str = "ai_solo_founder",
                 confidence: float = 0.6, needs_human_review: int = 0,
                 website: str = "https://acme.example",
                 linkedin: str | None = None, status: str = "SCORED",
                 is_duplicate: int = 0, next_check_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO leads (id, session_id, company_name, website_url, status,"
        " is_duplicate, created_at, linkedin_url, next_check_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (lead_id, 1, f"Acme {lead_id}", website, status, is_duplicate,
         _now_iso(), linkedin, next_check_at),
    )
    conn.execute(
        "INSERT INTO lead_scores (session_id, lead_id, segment, confidence,"
        " needs_human_review, scored_at) VALUES (?,?,?,?,?,?)",
        (1, lead_id, segment, confidence, needs_human_review, _now_iso()),
    )
    conn.commit()


_HOME = {
    "ok": True, "status": 200,
    "text": ("Acme makes audit-ready sites for founders. The Careers and "
             "Pricing pages live in our nav."),
    "html": (
        "<html><head><meta property='og:url' content='https://acme.example/'/>"
        "</head><body><a href='/careers'>Careers</a><a href='/pricing'>Pricing</a>"
        "</body></html>"
    ),
    "links": ["https://acme.example/careers", "https://acme.example/pricing"],
    "js_heavy": False,
}

_PRICING = {
    "ok": True, "status": 200,
    "text": "Pricing: pick the Starter plan or book a demo. Prices in USD.",
    "html": "<div>pricing</div>", "links": [], "js_heavy": False,
}


def _careers(with_engineer: bool) -> dict:
    text = (
        "We're hiring a backend engineer who will own our core audit engine."
        if with_engineer else
        "We're hiring a content writer to keep the docs fresh."
    )
    return {"ok": True, "status": 200, "text": text, "html": "<div>jobs</div>",
            "links": [], "js_heavy": False}


def _fake_fetch(pages: dict):
    """Seam factory: serves a fixed mapping of URL -> page dict."""
    def fetch(url, timeout=15.0, per_domain_delay=1.0):
        return pages.get(url.rstrip("/"), {"ok": False, "status": None, "text": ""})
    return fetch


def test_hiring_engineer_fires_once_and_updates_snapshot():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.9, linkedin="https://linkedin.com/in/founder")
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(with_engineer=False),
        "https://acme.example/pricing": _PRICING,
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": [], "notes": []},
    }
    lead1 = dbmod.get_leads(conn)[0]

    # Run 1 — baseline: careers page WITHOUT an engineering role. No event.
    fired1 = triggersmod.run_checks_for_lead(conn, lead1, cfg, now=_now_iso(), **seams)
    assert fired1 == []
    row1 = dbmod.get_leads(conn)[0]
    state1 = json.loads(row1["trigger_state"])
    assert state1["hiring_engineer"]["hiring_technical"] is False
    assert state1["hiring_engineer"]["has_careers_page"] is True
    assert row1["next_check_at"] is not None
    assert dbmod.get_lead_trigger_events(conn, 1) == []

    # Run 2 — careers page GAINS an engineering role -> exactly one event.
    pages["https://acme.example/careers"] = _careers(with_engineer=True)
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    hiring = [ev for ev in fired2 if ev["trigger"] == "hiring_engineer"]
    assert len(hiring) == 1
    ev = dbmod.get_lead_trigger_events(conn, 1)
    assert len(ev) == 1
    assert ev[0]["trigger"] == "hiring_engineer"
    assert lead2["id"] in (ev[0]["lead_id"],)
    row2 = dbmod.get_leads(conn)[0]
    state2 = json.loads(row2["trigger_state"])
    assert state2["hiring_engineer"]["hiring_technical"] is True

    # Run 3 — snapshot unchanged -> no repeat event (fires exactly once).
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    assert [ev for ev in fired3 if ev["trigger"] == "hiring_engineer"] == []
    assert len(dbmod.get_lead_trigger_events(conn, 1)) == 1


def test_cheap_checks_never_call_the_linkedin_lane():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6, linkedin="https://linkedin.com/in/founder")
    cfg = runconfig.load_config()

    def _forbidden_linkedin(*args, **kwargs):
        raise AssertionError("cheap checks must never call the LinkedIn lane")

    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(with_engineer=False),
        "https://acme.example/pricing": _PRICING,
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": _forbidden_linkedin,
    }
    lead = dbmod.get_leads(conn)[0]
    fired = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    # Mid-score lead: founder_posts_pain is NOT in the check set, so the
    # LinkedIn lane is never touched — the spy above would have failed.
    assert all(ev["trigger"] != "founder_posts_pain" for ev in fired)


def test_team_growth_hooks_into_credit_governor_and_stops_when_over_budget():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    calls = {"apollo": 0}

    def apollo_search(filters):
        calls["apollo"] += 1
        return [{"organization": {"estimated_num_employees": 12}}]

    pages = {"https://acme.example": _HOME, "https://acme.example/careers": _careers(False),
             "https://acme.example/pricing": _PRICING}
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": apollo_search,
        "web_search": lambda company, founder_name=None, **kw: {},
    }
    lead = dbmod.get_leads(conn)[0]

    # Under budget: the search runs (governor permits) and baselines headcount.
    fired = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert calls["apollo"] == 1
    state = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state["team_growth"]["headcount"] == 12
    assert state["apollo_budget_reached"] is False

    # Over the monthly budget: the governor raises and the search is skipped.
    # Only apollo_budget_reached trips — the SGAI lane (funding/product_hunt)
    # is untouched and its flag stays False: proof the budgets are independent.
    lead2 = dbmod.get_leads(conn)[0]
    import apollo_client
    apollo_client.ensure_usage_table(conn)
    apollo_client.record_credits(conn, 99999)  # used >> cap
    shots_before = calls["apollo"]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert calls["apollo"] == shots_before  # governor stopped the search
    assert all(ev["trigger"] != "team_growth" for ev in fired2)
    state2 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state2["apollo_budget_reached"] is True
    assert state2["sgai_budget_reached"] is False


def test_funding_and_product_hunt_hook_into_credit_governor_and_stop_when_over_budget():
    """funding_announced + product_hunt run a PAID SGAI web search for every
    due lead on every run. They are gated by the SGAI monthly cap — SEPARATE
    from the Apollo cap that gates team_growth: under SGAI budget the single
    per-run search runs, over the SGAI cap every search is skipped and the
    prior snapshot is preserved — but Apollo being over budget NEVER throttles
    them. Each run's trigger_state records which vendor tripped
    (apollo_budget_reached / sgai_budget_reached).
    """
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    calls = {"web": 0}

    def web_search(company_name, founder_name=None, **kwargs):
        calls["web"] += 1
        # Empty result set: both checks baseline (no fire) but store snapshots.
        return {}

    pages = {"https://acme.example": _HOME, "https://acme.example/careers": _careers(False),
             "https://acme.example/pricing": _PRICING}
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": web_search,
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # Under BOTH budgets: the SGAI search runs exactly once per run (both
    # checks share the single per-run websearch cache) and baseline snapshots
    # store; no vendor was over budget this run.
    fired = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert calls["web"] == 1
    assert fired == []
    state = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert "funding_announced" in state
    assert "product_hunt" in state
    assert state["apollo_budget_reached"] is False
    assert state["sgai_budget_reached"] is False

    # APOLLO over ITS cap, SGAI NOT: the web searches still run normally for
    # every lead (proves the budgets are independent — Apollo cannot throttle
    # the SGAI lane), and only apollo_budget_reached trips.
    lead2 = dbmod.get_leads(conn)[0]
    import apollo_client
    apollo_client.ensure_usage_table(conn)
    apollo_client.record_credits(conn, 99999)  # used >> apollo cap
    shots_before = calls["web"]
    fired_apollo_over = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert calls["web"] == shots_before + 1  # the SGAI search ran anyway
    # Apollo over budget: team_growth is skipped, and nothing spurious fires.
    assert fired_apollo_over == []
    state_a = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state_a["apollo_budget_reached"] is True
    assert state_a["sgai_budget_reached"] is False

    # SGAI over ITS cap: both web-searched checks skip entirely, the prior
    # snapshots are preserved, and sgai_budget_reached is what tripped.
    lead3 = dbmod.get_leads(conn)[0]
    import sgai_client
    sgai_client.ensure_usage_table(conn)
    sgai_client.record_credits(conn, 99999)  # used >> sgai cap
    shots_before = calls["web"]
    fired_sgai_over = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    assert calls["web"] == shots_before  # governor stopped the SGAI searches
    assert all(ev["trigger"] not in ("funding_announced", "product_hunt") for ev in fired_sgai_over)
    state_b = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state_b["apollo_budget_reached"] is True
    assert state_b["sgai_budget_reached"] is True
    assert state_b["funding_announced"] == state_a["funding_announced"]
    assert state_b["product_hunt"] == state_a["product_hunt"]

    # SGAI budget recovers: the next run searches again and clears the flag
    # (Apollo is still over its cap, so apollo_budget_reached stays True).
    conn.execute("DELETE FROM sgai_usage")
    conn.commit()
    lead4 = dbmod.get_leads(conn)[0]
    shots_before = calls["web"]
    fired_recovered = triggersmod.run_checks_for_lead(conn, lead4, cfg, now=_now_iso(), **seams)
    assert calls["web"] == shots_before + 1  # the SGAI lane works again
    state_c = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state_c["apollo_budget_reached"] is True
    assert state_c["sgai_budget_reached"] is False


def test_due_leads_selection():
    conn = _mk_conn()
    _insert_lead(conn, 1, next_check_at=None)                       # due (never scheduled)
    _insert_lead(conn, 2, next_check_at=_future_iso(30))            # not due yet
    _insert_lead(conn, 3, next_check_at=None, is_duplicate=1)       # duplicate -> excluded
    _insert_lead(conn, 4, next_check_at=None, status="SCORE_FAILED")  # unscored -> excluded
    due = dbmod.get_due_leads(conn, now=_now_iso())
    assert [l["id"] for l in due] == [1]


def test_triggered_leads_sort_to_top_of_queue():
    from app import _categorize_leads

    low = {
        "id": 1, "status": "SCORED", "segment": "ai_solo_founder",
        "needs_human_review": 0, "trigger_priority": None, "trigger_hook": None,
    }
    hot = {
        "id": 2, "status": "SCORED", "segment": "ai_solo_founder",
        "needs_human_review": 0, "trigger_priority": 4,
        "trigger_hook": "Saw you're hiring an engineer",
    }
    categories = _categorize_leads([low, hot])
    assert categories["approved"] == [hot, low]
    assert categories["approved"][0]["trigger_hook"] == "Saw you're hiring an engineer"


def test_reorder_queue_false_keeps_queue_order_even_when_trigger_fired():
    from app import _categorize_leads

    low = {
        "id": 1, "status": "SCORED", "segment": "ai_solo_founder",
        "needs_human_review": 0, "trigger_priority": None, "trigger_hook": None,
    }
    hot = {
        "id": 2, "status": "SCORED", "segment": "ai_solo_founder",
        "needs_human_review": 0, "trigger_priority": 4,
        "trigger_hook": "Saw you're hiring an engineer",
    }
    inserted_order = [low, hot]
    # reorder_queue=false: the trigger still fired/logged/set the hook (the
    # hot lead carries trigger_priority + trigger_hook) — only the ORDER BY
    # change is skipped, so the queue keeps its insertion order.
    categories = _categorize_leads(inserted_order, reorder_queue=False)
    assert [l["id"] for l in categories["approved"]] == [1, 2]
    assert categories["approved"][1]["trigger_hook"] == "Saw you're hiring an engineer"
    # sanity: the default (true) still reorders.
    categories = _categorize_leads(inserted_order, reorder_queue=True)
    assert [l["id"] for l in categories["approved"]] == [2, 1]


def _latest_lead_score_hooks(conn, lead_id):
    row = conn.execute(
        "SELECT personalization_hooks FROM lead_scores "
        "WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    if row is None or not row["personalization_hooks"]:
        return []
    try:
        parsed = json.loads(row["personalization_hooks"])
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _trigger_sourced_hooks(conn, lead_id):
    """The injected hooks (dicts carrying source == "trigger") in the latest
    lead_scores.personalization_hooks — the field both outreach paths read."""
    return [
        h for h in _latest_lead_score_hooks(conn, lead_id)
        if isinstance(h, dict) and h.get("source") == "trigger"
    ]


_HOME_APPSTORE = {
    "ok": True, "status": 200,
    "text": _HOME["text"],
    "html": "<div>app</div>",
    "links": list(_HOME["links"]) + ["https://apps.apple.com/app/acme"],
    "js_heavy": False,
}

_TERMS_OK = {
    "ok": True, "status": 200,
    "text": "Our Privacy Policy, DPA and Terms. SOC 2 Type II report available.",
    "html": "<div>terms</div>", "links": [], "js_heavy": False,
}


def test_trigger_hook_reaches_outreach_exactly_once_after_two_firings():
    """After two consecutive trigger firings for the SAME lead, exactly ONE
    active hook reaches the outreach paths (email prompt + Instantly export),
    not two stacked, and not zero — and it is the most recent one."""
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(with_engineer=False),
        # pricing confirmed ABSENT for runs 1-2, so run 3's appearance fires.
        "https://acme.example/pricing": {"ok": False, "status": 404, "text": ""},
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }

    # Run 1 — baseline, no fire, no injected hook -> "not zero" starts clean.
    lead = dbmod.get_leads(conn)[0]
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []
    assert _trigger_sourced_hooks(conn, 1) == []

    # Run 2 — careers page gains an engineer: firing #1.
    pages["https://acme.example/careers"] = _careers(with_engineer=True)
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert any(ev["trigger"] == "hiring_engineer" for ev in fired2)
    hooks_after_first = _trigger_sourced_hooks(conn, 1)
    assert len(hooks_after_first) == 1
    assert hooks_after_first[0]["hook"] == "Saw you're hiring an engineer"

    # Run 3 — a pricing page appears: firing #2 for the same lead.
    pages["https://acme.example/pricing"] = _PRICING
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    assert any(ev["trigger"] == "pricing_introduced" for ev in fired3)

    # EXACTLY one active hook, the newest one — the other was replaced.
    hooks_after_second = _trigger_sourced_hooks(conn, 1)
    assert len(hooks_after_second) == 1
    assert hooks_after_second[0]["hook"] == "Saw you added a pricing page"

    # The outreach renderers see exactly that one hook.
    from emailer import _as_text
    from export import _flatten
    stored = _latest_lead_score_hooks(conn, 1)
    text = _as_text(stored)
    flat = _flatten(stored)
    assert "Saw you added a pricing page" in text
    assert "Saw you're hiring an engineer" not in text
    assert "Saw you added a pricing page" in flat
    assert "Saw you're hiring an engineer" not in flat


def test_pricing_introduced_refires_on_true_false_true():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/pricing": _PRICING,
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # True: pricing page present -> snapshot True, no fire (baseline-first).
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []
    state1 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state1["pricing_introduced"]["has_pricing_page"] is True

    # False: pricing page CONFIRMED absent (404) -> snapshot flips to False.
    pages["https://acme.example/pricing"] = {"ok": False, "status": 404, "text": ""}
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired2 if ev["trigger"] == "pricing_introduced"] == []
    state2 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state2["pricing_introduced"]["has_pricing_page"] is False

    # True again -> real transition, fires (a total of one event, not twice).
    pages["https://acme.example/pricing"] = _PRICING
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    pricing_events = [ev for ev in fired3 if ev["trigger"] == "pricing_introduced"]
    assert len(pricing_events) == 1
    events = dbmod.get_lead_trigger_events(conn, 1)
    assert [ev["trigger"] for ev in events].count("pricing_introduced") == 1


def test_compliance_page_refires_on_true_false_true():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/terms": _TERMS_OK,
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # True: /terms serves compliance content -> snapshot True, no fire.
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []
    state1 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state1["compliance_page"]["compliance_page"] is True

    # False: /terms confirmed absent (404) -> snapshot flips to False.
    pages["https://acme.example/terms"] = {"ok": False, "status": 404, "text": ""}
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired2 if ev["trigger"] == "compliance_page"] == []
    state2 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state2["compliance_page"]["compliance_page"] is False

    # True again -> real transition, fires exactly once.
    pages["https://acme.example/terms"] = _TERMS_OK
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    compliance_events = [ev for ev in fired3 if ev["trigger"] == "compliance_page"]
    assert len(compliance_events) == 1
    events = dbmod.get_lead_trigger_events(conn, 1)
    assert [ev["trigger"] for ev in events].count("compliance_page") == 1


def test_app_store_launch_refires_on_true_false_true():
    """Homepage-driven: no store link (baseline) -> link appears (fires) ->
    link removed (back to no-fire) -> link reappears (fires AGAIN)."""
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/pricing": {"ok": False, "status": 404, "text": ""},
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # Baseline: no App Store link -> stored [], no fire.
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []

    # App Store listing appears -> fires once.
    pages["https://acme.example"] = _HOME_APPSTORE
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired2 if ev["trigger"] == "app_store_launch"] == ["app_store_launch"]

    # Listing removed -> stored [] again, no fire.
    pages["https://acme.example"] = _HOME
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired3 if ev["trigger"] == "app_store_launch"] == []

    # Listing reappears -> fires a SECOND time (True->False->True).
    pages["https://acme.example"] = _HOME_APPSTORE
    lead4 = dbmod.get_leads(conn)[0]
    fired4 = triggersmod.run_checks_for_lead(conn, lead4, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired4 if ev["trigger"] == "app_store_launch"] == ["app_store_launch"]
    events = dbmod.get_lead_trigger_events(conn, 1)
    assert [ev["trigger"] for ev in events].count("app_store_launch") == 2


def test_hiring_engineer_refires_on_true_false_true():
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/pricing": {"ok": False, "status": 404, "text": ""},
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # Baseline: careers page without an engineer -> snapshot stored, no fire.
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []

    # Engineer added -> fires once.
    pages["https://acme.example/careers"] = _careers(True)
    lead2 = dbmod.get_leads(conn)[0]
    fired2 = triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired2 if ev["trigger"] == "hiring_engineer"] == ["hiring_engineer"]

    # Careers confirmed ABSENT (404) -> snapshot flips to False, no fire.
    pages["https://acme.example/careers"] = {"ok": False, "status": 404, "text": ""}
    lead3 = dbmod.get_leads(conn)[0]
    fired3 = triggersmod.run_checks_for_lead(conn, lead3, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired3 if ev["trigger"] == "hiring_engineer"] == []
    state3 = json.loads(dbmod.get_leads(conn)[0]["trigger_state"])
    assert state3["hiring_engineer"]["has_careers_page"] is False

    # Engineer reappears -> fires a SECOND time (True->False->True).
    pages["https://acme.example/careers"] = _careers(True)
    lead4 = dbmod.get_leads(conn)[0]
    fired4 = triggersmod.run_checks_for_lead(conn, lead4, cfg, now=_now_iso(), **seams)
    assert [ev["trigger"] for ev in fired4 if ev["trigger"] == "hiring_engineer"] == ["hiring_engineer"]
    events = dbmod.get_lead_trigger_events(conn, 1)
    assert [ev["trigger"] for ev in events].count("hiring_engineer") == 2


def test_run_checks_for_lead_rolls_back_on_failure():
    """The per-lead read-check-write cycle is one transaction: a mid-run
    write failure rolls back EVERYTHING (events, hook injection, snapshot),
    leaving the lead exactly as it was — no partial writes."""
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/pricing": {"ok": False, "status": 404, "text": ""},
    }
    seams = {
        "fetch": _fake_fetch(pages),
        "apollo_search": lambda filters: [],
        "web_search": lambda company, founder_name=None, **kw: {},
        "linkedin_harvest": lambda url, lcfg, conn: {"status": "ok", "hits": []},
    }
    lead = dbmod.get_leads(conn)[0]

    # Baseline run commits a snapshot (with the pricing page confirmed absent).
    fired1 = triggersmod.run_checks_for_lead(conn, lead, cfg, now=_now_iso(), **seams)
    assert fired1 == []
    state_before = dbmod.get_leads(conn)[0]["trigger_state"]

    # Run 2 fires (engineer added) but a mid-transaction write fails -> the
    # event already inserted is rolled back along with everything else.
    pages["https://acme.example/careers"] = _careers(True)

    saved = dbmod.update_lead_trigger_fields

    def _raiser(*args, **kwargs):
        raise RuntimeError("simulated mid-transaction write failure")

    dbmod.update_lead_trigger_fields = _raiser
    lead2 = dbmod.get_leads(conn)[0]
    try:
        with pytest.raises(RuntimeError):
            triggersmod.run_checks_for_lead(conn, lead2, cfg, now=_now_iso(), **seams)
    finally:
        dbmod.update_lead_trigger_fields = saved

    # Nothing from the failed run is visible: no events, no injected hook,
    # and the snapshot is byte-for-byte the pre-run state.
    assert dbmod.get_lead_trigger_events(conn, 1) == []
    assert _trigger_sourced_hooks(conn, 1) == []
    assert dbmod.get_leads(conn)[0]["trigger_state"] == state_before


def test_lock_is_not_held_during_network_fetches(monkeypatch):
    """The per-lead row lock (SELECT ... FOR UPDATE) scopes ONLY the final
    re-read-compare-write, never the network work.

    Proof by ordering: every network fetch of a run must complete BEFORE the
    lock is taken, and no fetch may happen after it. A slow fetcher that 20ms
    per URL makes a fetch-inside-the-lock regression obvious (the lock would
    then be recorded while fetches are still running / after they started).
    """
    conn = _mk_conn()
    _insert_lead(conn, 1, confidence=0.6)
    cfg = runconfig.load_config()
    pages = {
        "https://acme.example": _HOME,
        "https://acme.example/careers": _careers(False),
        "https://acme.example/pricing": _PRICING,
    }

    order: list = []
    real_lock = dbmod.lock_lead_trigger_row

    def recording_lock(c, lead_id):
        order.append(("lock", time.monotonic()))
        return real_lock(c, lead_id)

    monkeypatch.setattr(dbmod, "lock_lead_trigger_row", recording_lock)

    def slow_fetch(url, timeout=15.0, per_domain_delay=1.0):
        order.append(("fetch", time.monotonic(), url))
        time.sleep(0.02)
        return pages.get(url.rstrip("/"), {"ok": False, "status": None, "text": ""})

    lead = dbmod.get_leads(conn)[0]
    fired = triggersmod.run_checks_for_lead(
        conn, lead, cfg, now=_now_iso(),
        fetch=slow_fetch,
        apollo_search=lambda filters: [],
        web_search=lambda company, founder_name=None, **kw: {},
        linkedin_harvest=lambda url, lcfg, conn: {"status": "ok", "hits": []},
    )
    assert fired == []  # baseline run, committed normally

    fetch_ts = [ts for kind, ts, *_ in order if kind == "fetch"]
    lock_idx = [i for i, (kind, *_) in enumerate(order) if kind == "lock"]
    assert fetch_ts, "the run must have made network fetches"
    assert len(lock_idx) == 1

    # The lock is acquired ONLY after every fetch completed...
    assert max(fetch_ts) < order[lock_idx[0]][1]
    # ...and nothing after it is a fetch (phase B diffed against the memoized
    # observations, so the locked transaction does zero HTTP).
    after_lock = order[lock_idx[0] + 1:]
    assert all(kind != "fetch" for kind, *_ in after_lock)