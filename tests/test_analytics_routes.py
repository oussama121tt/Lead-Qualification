"""Phase 4 analytics routes (Tasks 16–18) — offline Flask route tests.

Queries are stubbed at the analytics-module boundary (they are covered by
tests/test_analytics.py against the sqlite harness); here we verify the routes
render, the on-demand sync / channel-assignment POSTs behave, and the
per-session auth guard is enforced.
"""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import analytics as analyticsmod
import apollo_analytics as apollo_analyticsmod

TEST_CONFIG = SimpleNamespace(
    apollo_credit_price_usd=0.05,
    scrape_price_usd=0.01,
    operator_rate_usd_hour=30.0,
    review_minutes_per_lead=5.0,
)


def _canned_attribution(conn, session_id=None):
    return (
        [{"family": "segment", "value": "ai_solo_founder", "leads": 20, "sent": 12,
          "replies": 6, "reply_rate": 0.5, "lift": 1.5},
         {"family": "trigger", "value": "lp_uploaded", "leads": 2, "sent": 2,
          "replies": 0, "reply_rate": 0.0, "lift": None}],
        {"leads": 22, "sent": 14, "replies": 6, "reply_rate": 0.4286},
    )


def _canned_costs(conn, cfg=None):
    return {
        "recipes": [{"key": 1, "label": "Solo founders v3", "leads": 2, "sent": 1,
                     "replies": 1, "closed_won": 1, "revenue": 5000.0, "cost": 7.5,
                     "reply_rate": 1.0, "cost_per_reply": 7.5, "cost_per_close": 7.5,
                     "net_revenue": 4992.5}],
        "segments": [{"key": "unsegmented", "label": "unsegmented", "leads": 2, "sent": 1,
                      "replies": 1, "closed_won": 1, "revenue": 5000.0, "cost": 2.5,
                      "reply_rate": 1.0, "cost_per_reply": 2.5, "cost_per_close": 2.5,
                      "net_revenue": 4997.5}],
        "channels": [{"key": "cold_email", "label": "cold_email", "leads": 1, "sent": 1,
                      "replies": 1, "closed_won": 0, "revenue": 0.0, "cost": 1.0,
                      "reply_rate": 1.0, "cost_per_reply": 1.0, "cost_per_close": None,
                      "net_revenue": -1.0}],
        "totals": {"leads": 2, "sent": 1, "replies": 1, "closed_won": 1,
                   "cost": 7.5, "revenue": 5000.0, "net_revenue": 4992.5},
        "assumptions": {"apollo_credit_price_usd": 0.05, "scrape_price_usd": 0.01,
                        "operator_rate_usd_hour": 30.0, "review_minutes_per_lead": 5.0},
    }


def _canned_channels(conn, cfg=None):
    return [{"channel": "upwork", "leads": 3, "sent": 2, "replies": 1, "closed_won": 1,
             "revenue": 5000.0, "cost": 2.5, "reply_rate": 0.5, "cost_per_reply": 2.5,
             "cost_per_close": 2.5, "net_revenue": 4997.5, "cycle_days": 3.5}]


def _canned_sessions(conn, owner_id=None):
    return [{"id": 5, "label": "Alpha agency hunt", "created_at": "2026-09-01 10:00",
             "channel": "upwork"}]


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE analysis_sessions (id INTEGER PRIMARY KEY, channel TEXT)")
    c.commit()
    return c


@pytest.fixture()
def client(conn, monkeypatch):
    import app as appmod
    import runconfig as runconfigmod

    @contextmanager
    def _open():
        yield conn
    monkeypatch.setattr(appmod, "open_db", _open)
    monkeypatch.setattr(appmod, "_init_done", True)

    def _fake_get_user(c, uid):
        return {"id": uid, "email": "test@test.com", "role": "admin", "is_active": True}
    monkeypatch.setattr(appmod.dbmod, "get_user_by_id", _fake_get_user)

    def _fake_gas(c, sid):
        return {"id": sid, "label": f"sess-{sid}", "owner_id": None}
    monkeypatch.setattr(appmod.dbmod, "get_analysis_session", _fake_gas)

    # Stub the analytics query layer (unit-covered in test_analytics.py).
    monkeypatch.setattr(analyticsmod, "attribution", _canned_attribution)
    monkeypatch.setattr(analyticsmod, "cost_per_outcome", _canned_costs)
    monkeypatch.setattr(analyticsmod, "channel_comparison", _canned_channels)
    monkeypatch.setattr(analyticsmod, "session_channels", _canned_sessions)
    monkeypatch.setattr(analyticsmod, "last_sync_summary",
                        lambda c: {"month": "2026-08", "messages": 25, "opened": 12,
                                   "clicked": 5, "replied": 3})
    monkeypatch.setattr(apollo_analyticsmod, "sync_analytics_report",
                        lambda c, **kw: {"month": "2026-08", "messages": 5, "matched": 3,
                                         "replied": 2, "opened": 1, "clicked": 1,
                                         "unmatched": 2})
    monkeypatch.setattr(runconfigmod, "load_config",
                        lambda: SimpleNamespace(costs=TEST_CONFIG))

    appmod.app.config["TESTING"] = True
    appmod.app.config["WTF_CSRF_ENABLED"] = False
    c = appmod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "admin"
    return c


# ──────────────────────────────────────────────────────────────────────
# GET pages
# ──────────────────────────────────────────────────────────────────────

def test_analytics_page_renders_attribution(client):
    r = client.get("/analytics")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Signal" in body
    assert "ai_solo_founder" in body
    assert "1.5" in body                                  # real lift rendered (>= MIN_SENT_FOR_LIFT sends)
    assert "not enough data" in body                      # below-threshold lift withheld, not blank
    assert "42.86%" in body or "42.9%" in body            # baseline reply rate formatted
    assert "2026-08" in body                              # last sync summary


def test_analytics_sync_renders_without_sync_data(client, monkeypatch):
    monkeypatch.setattr(analyticsmod, "last_sync_summary", lambda c: None)
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "No sync yet" in r.get_data(as_text=True) or "sync" in r.get_data(as_text=True).lower()


def test_analytics_costs_page_renders(client):
    r = client.get("/analytics/costs")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Cost per outcome" in body
    assert "Solo founders v3" in body
    assert "$7.50" in body                       # recipe cost formatted
    assert "0.0500" in body                      # apollo credit assumption


def test_analytics_channels_page_renders(client):
    r = client.get("/analytics/channels")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "upwork" in body
    assert "Alpha agency hunt" in body             # per-session assignment rows
    assert "/analytics/sessions/5/channel" in body


# ──────────────────────────────────────────────────────────────────────
# On-demand Apollo sync
# ──────────────────────────────────────────────────────────────────────

def test_analytics_sync_runs_now(client, monkeypatch):
    seen = {}
    def _fake_sync(conn, **kw):
        seen["days"] = kw.get("days")
        return {"month": "2026-08", "messages": 5, "matched": 3, "replied": 2,
                "opened": 1, "clicked": 1, "unmatched": 2}
    monkeypatch.setattr(apollo_analyticsmod, "sync_analytics_report", _fake_sync)
    r = client.post("/analytics/sync", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert seen["days"] == 1                    # matches tools/run_outcomes_sync.py --days 1
    assert "3/5" in body and "Analytics sync" in body


def test_analytics_sync_failure_flashes(client, monkeypatch):
    def _boom(conn, **kw):
        raise apollo_analyticsmod.ApolloAnalyticsError("no API key configured")
    monkeypatch.setattr(apollo_analyticsmod, "sync_analytics_report", _boom)
    r = client.post("/analytics/sync", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Analytics sync failed" in body
    assert "no API key configured" in body


# ──────────────────────────────────────────────────────────────────────
# Per-session channel assignment
# ──────────────────────────────────────────────────────────────────────

def test_analytics_set_channel_persists(client, conn):
    conn.execute("INSERT INTO analysis_sessions (id, channel) VALUES (5, NULL)")
    conn.commit()
    r = client.post("/analytics/sessions/5/channel",
                    data={"channel": " Upwork "}, follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "channel set to upwork" in body
    st = conn.execute("SELECT channel FROM analysis_sessions WHERE id = 5").fetchone()
    assert st["channel"] == "upwork"


def test_analytics_set_channel_rejects_unknown(client):
    r = client.post("/analytics/sessions/5/channel",
                    data={"channel": "telepathy"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Unknown channel: telepathy" in body


def test_analytics_set_channel_session_not_found(client, monkeypatch):
    import app as appmod
    monkeypatch.setattr(appmod.dbmod, "get_analysis_session", lambda c, sid: None)
    r = client.post("/analytics/sessions/404/channel",
                    data={"channel": "upwork"}, follow_redirects=True)
    assert "Session not found." in r.get_data(as_text=True)


def test_analytics_set_channel_forbids_legacy_session_to_member(client):
    with client.session_transaction() as sess:
        sess["role"] = "member"
    r = client.post("/analytics/sessions/5/channel",
                    data={"channel": "upwork"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Access not authorized to this analysis" in body
    assert r.status_code == 200