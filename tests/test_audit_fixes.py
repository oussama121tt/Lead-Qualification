"""Regression tests for the defects found in the Sept 2026 branch audit:

- Apollo outcomes sync: the plain search payload has no opened/clicked fields
  (verified live), so engagement must come from the stats-filtered sweeps.
- Trigger scheduler: a lead whose run throws is COUNTED and NAMED, not skipped
  silently.
- SGAI governor: at exactly the monthly cap the paid lane pauses (no overshoot).
- Analytics: a reply implies a send (reply-without-send never inflates rates).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analytics as az
import apollo_analytics as aa
import runconfig
import sgai_client
import triggers as triggersmod
from test_apollo_analytics import _sqlite_conn as _aa_conn
from test_triggers import _mk_conn, _insert_lead, _fake_fetch, _HOME, _PRICING, _careers


# --- Apollo engagement enrichment ------------------------------------------

def _fake_apollo(plain, by_stat):
    """A _get seam that honours the emailer_message_stats[] filter."""
    calls = []

    def _get(path, params, key):
        calls.append(dict(params))
        stats = params.get("emailer_message_stats[]")
        page = int(params.get("page", 1))
        if stats:
            msgs = by_stat.get(stats[0], []) if page == 1 else []
        else:
            msgs = plain if page == 1 else []
        return {"emailer_messages": msgs}
    return _get, calls


def test_sync_enriches_opened_clicked_bounced_from_stats_sweeps(monkeypatch):
    conn = _aa_conn()
    conn.execute("INSERT INTO leads (id, session_id, email) VALUES (1, 1, 'alice@co.com')")
    conn.commit()
    monkeypatch.setattr(aa, "ensure_report_table", lambda c: None)
    plain = [  # what the live endpoint returns: no opened/clicked keys at all
        {"id": "m1", "to_email": "alice@co.com", "status": "completed",
         "created_at": "2026-09-08T10:00:00Z", "replied": None},
        {"id": "m2", "to_email": "bob@co.com", "status": "completed",
         "created_at": "2026-09-08T10:00:00Z", "replied": None},
    ]
    by_stat = {"opened": [{"id": "m1"}, {"id": "m2"}], "clicked": [{"id": "m1"}],
               "replied": [{"id": "m1", "replied": True}], "bounced": [{"id": "m2"}]}
    _get, calls = _fake_apollo(plain, by_stat)

    result = aa.sync_analytics_report(conn, key="k", days=7, _get=_get)
    assert result["opened"] == 2
    assert result["clicked"] == 1
    assert result["replied"] == 1
    assert result["bounced"] == 1
    assert result["stats_errors"] == {}
    # Every engagement state was swept (page 1 with rows, page 2 empty -> stop).
    swept = [c["emailer_message_stats[]"][0] for c in calls if c.get("emailer_message_stats[]")]
    assert list(dict.fromkeys(swept)) == list(aa.ENGAGEMENT_STATS)
    assert all(swept.count(st) == 2 for st in aa.ENGAGEMENT_STATS)
    outcome = conn.execute("SELECT * FROM lead_outcomes WHERE lead_id = 1").fetchone()
    assert outcome["opened"] == 1 and outcome["clicked"] == 1 and outcome["replied"] == 1
    report = {r["apollo_message_id"]: r for r in conn.execute("SELECT * FROM apollo_analytics_sync_report")}
    assert report["m2"]["status"] == "bounced"


def test_sync_reports_a_failed_sweep_instead_of_hiding_it(monkeypatch):
    conn = _aa_conn()
    monkeypatch.setattr(aa, "ensure_report_table", lambda c: None)
    plain = [{"id": "m1", "to_email": "a@co.com", "status": "completed",
              "created_at": "2026-09-08T10:00:00Z"}]

    def _get(path, params, key):
        stats = params.get("emailer_message_stats[]")
        if stats and stats[0] == "clicked":
            raise RuntimeError("HTTP 500")
        if stats:
            return {"emailer_messages": [{"id": "m1"}] if int(params["page"]) == 1 else []}
        return {"emailer_messages": plain if int(params["page"]) == 1 else []}

    result = aa.sync_analytics_report(conn, key="k", days=7, _get=_get)
    assert result["opened"] == 1          # the other sweeps still landed
    assert "clicked" in result["stats_errors"]
    assert "HTTP 500" in result["stats_errors"]["clicked"]


# --- Trigger scheduler: failures are counted and named ----------------------

def test_run_due_leads_counts_and_names_failed_leads():
    conn = _mk_conn()
    _insert_lead(conn, 1, website="https://acme.example")
    _insert_lead(conn, 2, website="https://broken.example")
    cfg = runconfig.load_config()
    leads = [dict(r) for r in conn.execute(
        "SELECT l.*, s.segment, s.confidence, s.needs_human_review FROM leads l "
        "JOIN lead_scores s ON s.lead_id = l.id ORDER BY l.id").fetchall()]
    pages = {"https://acme.example": _HOME, "https://acme.example/pricing": _PRICING,
             "https://acme.example/careers": _careers(False)}

    real_run = triggersmod.run_checks_for_lead

    def exploding(conn_, lead, cfg_, **seams):
        if lead["id"] == 2:
            raise RuntimeError("db went away")
        return real_run(conn_, lead, cfg_, **seams)

    triggersmod.run_checks_for_lead = exploding
    try:
        summary = triggersmod.run_due_leads(
            conn, leads, cfg, fetch=_fake_fetch(pages), apollo_search=lambda *a, **k: [],
            web_search=lambda *a, **k: {}, linkedin_harvest=lambda *a, **k: {"hits": []})
    finally:
        triggersmod.run_checks_for_lead = real_run
    assert summary["checked"] == 1
    assert summary["failed"] == 1
    assert summary["errors"][0]["lead_id"] == 2
    assert "db went away" in summary["errors"][0]["error"]


# --- SGAI governor: no overshoot at the cap ---------------------------------

def test_sgai_lane_pauses_at_exactly_the_cap():
    conn = _mk_conn()
    _insert_lead(conn, 1)
    cfg = runconfig.load_config()
    sgai_client.ensure_usage_table(conn)
    cap = cfg.triggers.sgai_monthly_credit_cap
    conn.execute("INSERT OR REPLACE INTO sgai_usage VALUES (?, ?)", (sgai_client._this_month(), cap))
    conn.commit()
    calls = []

    def ws(company, founder_name=None):
        calls.append(company)
        return {}
    lead = dict(conn.execute(
        "SELECT l.*, s.segment, s.confidence, s.needs_human_review FROM leads l "
        "JOIN lead_scores s ON s.lead_id = l.id WHERE l.id = 1").fetchone())
    pages = {"https://acme.example": _HOME}
    triggersmod.run_checks_for_lead(conn, lead, cfg, fetch=_fake_fetch(pages),
                                    apollo_search=lambda *a, **k: [], web_search=ws,
                                    linkedin_harvest=lambda *a, **k: {"hits": []})
    assert calls == []                                            # paused, not run
    assert sgai_client.credits_used_this_month(conn) == cap       # never 1001
    snap = triggersmod._load_snapshot(
        dict(conn.execute("SELECT trigger_state FROM leads WHERE id = 1").fetchone()))
    assert snap["sgai_budget_reached"] is True


# --- Analytics: reply implies send -----------------------------------------

def test_reply_without_send_marker_counts_as_sent():
    row = {"outcome_sent_at": None, "email_sent_at": None, "replied": 1}
    assert az._is_sent(row) is True
    assert az._replied(row) is True
    assert az._is_sent({"outcome_sent_at": None, "email_sent_at": None, "replied": 0}) is False


# --- Postgres cursor wrapper must behave like sqlite's ----------------------

def test_pg_cursor_wrapper_is_iterable_like_sqlite():
    """Regression: analytics/_fold_into_outcomes iterate `conn.execute(...)`
    directly. sqlite cursors allow that; the PG wrapper crashed with
    "'_PgCursor' object is not iterable" in production only."""
    import db as dbmod

    class _RawCur:
        description = None
        rowcount = 2

        def __init__(self):
            self._rows = [{"id": 1, "email": "a@x.com"}, {"id": 2, "email": "b@x.com"}]

        def __iter__(self):
            return iter(self._rows)

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0]

        def fetchmany(self, size=1):
            return self._rows[:size]

    cur = dbmod._PgCursor(_RawCur())
    assert [r["id"] for r in cur] == [1, 2]
    assert [r["email"] for r in cur.fetchall()] == ["a@x.com", "b@x.com"]
    assert cur.fetchmany(1)[0]["id"] == 1
    assert cur.rowcount == 2
