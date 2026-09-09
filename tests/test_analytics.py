"""analytics.py (Tasks 16–18) — signal attribution, cost-per-outcome and
channel comparison, all against a sqlite harness (no PG, no ensure_table)."""
import sqlite3
from types import SimpleNamespace

import pytest

import analytics as az
import db as dbmod


def _sqlite_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE analysis_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, created_at TEXT,
            channel TEXT, owner_id INTEGER
        );
        CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, email TEXT,
            email_sent_at TEXT, review_status TEXT
        );
        CREATE TABLE lead_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, segment TEXT,
            budget_signal TEXT, technical_signals TEXT, pain_signals TEXT,
            sensitive_data_categories TEXT
        );
        CREATE TABLE lead_technical_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER,
            app_builder_fingerprint TEXT, site_builder_fingerprint TEXT,
            on_builder_subdomain INTEGER
        );
        CREATE TABLE lead_outcomes (
            lead_id INTEGER PRIMARY KEY, email TEXT, channel TEXT, recipe_id INTEGER,
            sent_at TEXT, opened INTEGER, clicked INTEGER, replied INTEGER,
            reply_sentiment TEXT, meeting_booked INTEGER, closed_won INTEGER,
            revenue REAL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE lead_trigger_events (lead_id INTEGER, trigger TEXT, session_id INTEGER);
        CREATE TABLE lead_content (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, lead_id INTEGER, content TEXT);
        CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, lead_id INTEGER, cost_usd REAL);
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, recipe_id INTEGER);
        CREATE TABLE apollo_recipes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, enriched INTEGER);
        CREATE TABLE apollo_usage (month TEXT UNIQUE, credits_used INTEGER);
        CREATE TABLE apollo_analytics_sync_report (
            month TEXT, fetched_at TEXT, opened INTEGER, clicked INTEGER, replied INTEGER
        );
    """)
    return conn


def _cfg(**over):
    base = dict(apollo_credit_price_usd=0.05, scrape_price_usd=0.01,
                operator_rate_usd_hour=30.0, review_minutes_per_lead=5.0)
    base.update(over)
    return SimpleNamespace(**base)


def test_signal_families_reconciles_both_signal_shapes():
    row = {
        "segment": "ai_solo_founder",
        "budget_signal": "1000",
        "technical_signals": '["bubbleapp", "wix"]',
        "pain_signals": None,
        "sensitive_data_categories": '["pii", "none"]',
        "app_builder_fingerprint": "bubble",   # naming drift: same signal, wide col
        "site_builder_fingerprint": "webflow",
        "on_builder_subdomain": 1,
    }
    fams = set(az.signal_families(row))
    assert ("segment", "ai_solo_founder") in fams
    assert ("budget", "1000") in fams
    assert ("technical", "bubbleapp") in fams
    assert ("technical", "wix") in fams
    assert ("technical", "app_builder:bubble") in fams   # wide col folds as sub-value
    assert ("technical", "site_builder:webflow") in fams
    assert ("technical", "on_builder_subdomain:yes") in fams
    assert ("sensitive_data", "pii") in fams
    assert ("sensitive_data", "none") not in fams


def test_signal_families_skips_budget_none():
    assert az.signal_families({"segment": "", "budget_signal": "none",
                               "sensitive_data_categories": None}) == []


def test_attribution_lift_and_baseline():
    conn = _sqlite_conn()
    conn.executescript("""
        INSERT INTO analysis_sessions (id, label, created_at, channel) VALUES (1, 'A', '2026-09-01', NULL);
        INSERT INTO leads (id, session_id, email, email_sent_at, review_status) VALUES
            (1, 1, 'a@x.com', '2026-09-02', 'reviewed'),
            (2, 1, 'b@x.com', '2026-09-02', NULL),
            (3, 1, 'c@x.com', '2026-09-02', NULL);
        INSERT INTO lead_scores (id, lead_id, segment) VALUES
            (1, 1, 'ai_solo_founder'), (2, 2, 'ai_solo_founder'), (3, 3, 'technical_founder');
        INSERT INTO lead_outcomes (lead_id, email, replied, sent_at) VALUES
            (1, 'a@x.com', 1, '2026-09-03'), (2, 'b@x.com', 0, '2026-09-03'), (3, 'c@x.com', 0, '2026-09-03');
        INSERT INTO lead_trigger_events (lead_id, trigger, session_id) VALUES (1, 'lp_uploaded', 1);
    """)
    rows, baseline = az.attribution(conn)
    assert baseline == {"leads": 3, "sent": 3, "replies": 1, "reply_rate": 0.3333}

    by = {(r["family"], r["value"]): r for r in rows}
    seg = by[("segment", "ai_solo_founder")]
    assert seg["leads"] == 2 and seg["sent"] == 2 and seg["replies"] == 1
    # rates are computed even below the lift threshold; lift is withheld.
    assert seg["reply_rate"] == 0.5
    assert seg["lift"] is None

    tech = by[("segment", "technical_founder")]
    assert tech["replies"] == 0 and tech["reply_rate"] == 0.0 and tech["lift"] is None

    trig = by[("trigger", "lp_uploaded")]
    assert trig["leads"] == 1 and trig["replies"] == 1
    assert trig["reply_rate"] == 1.0 and trig["lift"] is None


def _seed_sent_segment(conn, start_id, segment, n, replies):
    """n sent leads in `segment`, first `replies` of them replied."""
    for i in range(n):
        lid = start_id + i
        conn.execute(
            "INSERT INTO leads (id, session_id, email, email_sent_at) VALUES (?, 1, ?, '2026-09-02')",
            (lid, f"u{lid}@x.com"))
        conn.execute("INSERT INTO lead_scores (id, lead_id, segment) VALUES (?, ?, ?)",
                     (lid, lid, segment))
        if i < replies:
            conn.execute("INSERT INTO lead_outcomes (lead_id, email, replied) VALUES (?, ?, 1)",
                         (lid, f"u{lid}@x.com"))
    conn.commit()


def test_attribution_lift_requires_minimum_sends():
    conn = _sqlite_conn()
    # small:  9 sent, 0 replies  -> rate 0.0, lift withheld (< MIN_SENT_FOR_LIFT)
    # mid:    5 sent, 3 replies  -> rate 0.6, lift STILL withheld (below threshold)
    # big:   10 sent, 4 replies  -> rate 0.4, at threshold -> lift computed
    _seed_sent_segment(conn, 1, "small", 9, 0)
    _seed_sent_segment(conn, 100, "mid", 5, 3)
    _seed_sent_segment(conn, 200, "big", 10, 4)

    rows, baseline = az.attribution(conn)
    # baseline: 24 sent, 7 replied -> 0.2917
    assert baseline["sent"] == 24 and baseline["replies"] == 7

    by = {(r["family"], r["value"]): r for r in rows}
    small = by[("segment", "small")]
    assert small["sent"] == 9 and small["reply_rate"] == 0.0 and small["lift"] is None

    mid = by[("segment", "mid")]
    assert mid["sent"] == 5 and mid["reply_rate"] == 0.6
    assert mid["lift"] is None  # computable rate, but below threshold

    big = by[("segment", "big")]
    assert big["sent"] == 10 and big["replies"] == 4
    assert big["reply_rate"] == 0.4
    assert big["lift"] == 1.37  # 0.4 / (7/24) = 1.3714... rounded to 2 dp


def test_attribution_single_session_filter():
    conn = _sqlite_conn()
    conn.executescript("""
        INSERT INTO analysis_sessions (id, label, channel) VALUES (1, 'A', 'upwork');
        INSERT INTO leads (id, session_id, email, email_sent_at) VALUES
            (1, 1, 'a@x.com', '2026-09-02'), (2, 2, 'b@x.com', '2026-09-02');
        INSERT INTO lead_scores (id, lead_id, segment) VALUES (1, 1, 'solo'), (2, 2, 'agency');
        INSERT INTO lead_outcomes (lead_id, email, replied) VALUES (1, 'a@x.com', 1), (2, 'b@x.com', 0);
    """)
    rows, baseline = az.attribution(conn, session_id=1)
    assert baseline["leads"] == 1 and baseline["replies"] == 1
    assert all(r["family"] == "segment" for r in rows)


def test_cost_per_outcome_recipe_apollo_and_totals():
    conn = _sqlite_conn()
    cfg = _cfg()
    conn.executescript("""
        INSERT INTO apollo_recipes (id, name, enriched) VALUES (1, 'Solo founders v3', 100);
        INSERT INTO apollo_usage (month, credits_used) VALUES ('2026-08', 100);
        INSERT INTO analysis_sessions (id, label, created_at, channel) VALUES
            (1, 'A', '2026-09-01', 'upwork'), (2, 'B', '2026-08-20', NULL);
        INSERT INTO campaigns (id, session_id, recipe_id) VALUES (1, 1, 1);
        INSERT INTO leads (id, session_id, email, email_sent_at, review_status) VALUES
            (1, 1, 'a@x.com', '2026-09-02', 'reviewed'),
            (2, 1, 'b@x.com', '2026-09-02', NULL),
            (3, 2, 'c@x.com', NULL, NULL),
            (4, 2, 'd@x.com', '2026-09-01', NULL);
        INSERT INTO lead_scores (id, lead_id, segment) VALUES
            (1, 1, 'ai_solo_founder'), (2, 2, 'ai_solo_founder'), (3, 3, NULL);
        INSERT INTO lead_outcomes (lead_id, email, replied, closed_won, revenue, sent_at) VALUES
            (1, 'a@x.com', 1, 1, 5000, '2026-09-03'),
            (2, 'b@x.com', 0, 0, 0, '2026-09-03'),
            (4, 'd@x.com', 1, 0, 0, '2026-09-02');
    """)
    result = az.cost_per_outcome(conn, cfg=cfg)

    by_rid = {r["key"]: r for r in result["recipes"]}
    r1 = by_rid[1]
    # apollo 100*0.05 + L1 operator time 5/60*30
    assert r1["cost"] == 7.5
    assert r1["leads"] == 2 and r1["sent"] == 2 and r1["replies"] == 1
    assert r1["reply_rate"] == 0.5 and r1["cost_per_reply"] == 7.5
    assert r1["closed_won"] == 1 and r1["revenue"] == 5000

    nr = by_rid["no_recipe"]
    assert nr["leads"] == 2 and nr["sent"] == 1 and nr["replies"] == 1
    assert nr["cost"] == 0.0

    assert result["totals"]["cost"] == 7.5
    assert result["totals"]["leads"] == 4 and result["totals"]["sent"] == 3
    assert result["totals"]["replies"] == 2 and result["totals"]["closed_won"] == 1
    assert result["totals"]["revenue"] == 5000
    assert result["assumptions"]["apollo_credit_price_usd"] == 0.05

    # channel + segment share the total apollo spend 5.0 by lead-count share
    # (on top of llm/scrape/operator time already bucketed per lead).
    ch = {r["label"]: r for r in result["channels"]}
    assert ch["upwork"]["leads"] == 2 and ch["upwork"]["cost"] == 5.0  # 2.5 operator + 2.5 apollo
    assert ch["cold_email"]["cost"] == 2.5                             # 2/4 share of the 5.0 credits
    seg = {r["label"]: r for r in result["segments"]}
    assert seg["ai_solo_founder"]["cost"] == 5.0
    assert seg["unsegmented"]["cost"] == 2.5


def test_channel_comparison_apollo_apportionment_matches_cost_page():
    """/analytics/channels and /analytics/costs must agree on a channel's cost:
    channel_comparison apportions Apollo credits by lead-count share exactly
    like cost_per_outcome's channel split."""
    conn = _sqlite_conn()
    conn.executescript("""
        INSERT INTO apollo_recipes (id, name, enriched) VALUES (1, 'Solo founders v3', 100);
        INSERT INTO apollo_usage (month, credits_used) VALUES ('2026-08', 100);
        INSERT INTO analysis_sessions (id, label, created_at, channel) VALUES
            (1, 'A', '2026-09-01', 'upwork'), (2, 'B', '2026-08-20', NULL);
        INSERT INTO campaigns (id, session_id, recipe_id) VALUES (1, 1, 1);
        INSERT INTO leads (id, session_id, email, email_sent_at, review_status) VALUES
            (1, 1, 'a@x.com', '2026-09-02', 'reviewed'),
            (2, 1, 'b@x.com', '2026-09-02', NULL),
            (3, 2, 'c@x.com', NULL, NULL),
            (4, 2, 'd@x.com', '2026-09-01', NULL);
        INSERT INTO lead_outcomes (lead_id, email, replied, closed_won, revenue) VALUES
            (1, 'a@x.com', 1, 1, 5000), (2, 'b@x.com', 0, 0, 0), (4, 'd@x.com', 1, 0, 0);
    """)
    cfg = _cfg()

    costs = {r["label"]: r for r in az.cost_per_outcome(conn, cfg=cfg)["channels"]}
    channels = {r["channel"]: r for r in az.channel_comparison(conn, cfg=cfg)}

    for name in ("cold_email", "upwork"):
        assert costs[name]["cost"] == channels[name]["cost"], (
            f"channel '{name}' cost differs across screens: "
            f"costs={costs[name]['cost']} vs channels={channels[name]['cost']}")
    assert channels["upwork"]["cost"] == 5.0   # 2.5 operator time + 2.5 apollo share
    assert channels["cold_email"]["cost"] == 2.5


def test_channel_comparison_cycle_length_and_nulls():
    conn = _sqlite_conn()
    conn.executescript("""
        INSERT INTO analysis_sessions (id, label, created_at, channel) VALUES
            (1, 'A', '2026-09-01', 'upwork'), (2, 'B', '2026-10-01', NULL);
        INSERT INTO leads (id, session_id, email, email_sent_at, review_status) VALUES
            (1, 1, 'a@x.com', '2026-09-04', 'reviewed'),  -- 3 days after session
            (2, 1, 'b@x.com', '2026-09-05', NULL),        -- 4 days
            (3, 2, 'c@x.com', NULL, NULL),                -- never sent
            (4, 2, 'd@x.com', '2026-09-03', NULL);        -- sent before session date -> 0
        INSERT INTO lead_outcomes (lead_id, email, replied, closed_won) VALUES
            (1, 'a@x.com', 1, 1), (4, 'd@x.com', 1, 0);
    """)
    rows = {r["channel"]: r for r in az.channel_comparison(conn, cfg=_cfg())}
    up = rows["upwork"]
    ce = rows["cold_email"]
    assert up["leads"] == 2 and up["sent"] == 2 and up["replies"] == 1
    assert up["cycle_days"] == 3.5  # (3 + 4) / 2
    assert up["cost"] == 2.5        # operator time on the reviewed lead only
    assert up["reply_rate"] == 0.5
    # default channel fills NULL, unsent lead excluded from cycle length.
    assert ce["leads"] == 2 and ce["sent"] == 1 and ce["replies"] == 1
    assert ce["cycle_days"] == 0.0


def test_channel_comparison_default_channel_and_empty():
    conn = _sqlite_conn()
    assert az.channel_comparison(conn) == []
    conn.execute("INSERT INTO analysis_sessions (id, label) VALUES (1, 'x')")
    conn.execute("INSERT INTO leads (id, session_id, email) VALUES (1, 1, 'a@x.com')")
    conn.commit()
    rows = az.channel_comparison(conn)
    assert len(rows) == 1 and rows[0]["channel"] == "cold_email"


def test_session_channels_owner_filter():
    conn = _sqlite_conn()
    conn.executescript("""
        INSERT INTO analysis_sessions (id, label, created_at, channel, owner_id) VALUES
            (1, 'A', '2026-09-01', 'upwork', 10),
            (2, 'B', '2026-09-02', NULL, 10),
            (3, 'C', '2026-09-03', 'discord', 20);
    """)
    rows = az.session_channels(conn, owner_id=10)
    assert len(rows) == 2
    assert rows[0]["channel"] == "cold_email"  # COALESCE default
    assert [r["id"] for r in rows] == [2, 1]   # ORDER BY id DESC


def test_last_sync_summary_empty_and_populated():
    conn = _sqlite_conn()
    assert az.last_sync_summary(conn) is None
    conn.execute("INSERT INTO apollo_analytics_sync_report (month, fetched_at, opened, clicked, replied) "
                 "VALUES ('2026-08', '2026-09-01 04:00', 3, 1, 2)")
    conn.commit()
    s = az.last_sync_summary(conn)
    assert s["month"] == "2026-08"
    assert s["messages"] == 1
    assert s["opened"] == 3 and s["clicked"] == 1 and s["replied"] == 2


def test_empty_db_queries_are_safe():
    conn = _sqlite_conn()
    assert az.last_sync_summary(conn) is None
    assert az.cost_per_outcome(conn)["totals"]["leads"] == 0
    assert az.channel_comparison(conn) == []


def test_last_sync_summary_tolerates_missing_table():
    conn = sqlite3.connect(":memory:")  # no analytics tables at all
    assert az.last_sync_summary(conn) is None


def test_upsert_lead_outcome_upserts_then_updates():
    """db.upsert_lead_outcome must bind its OWN 14 placeholders exactly (regression
    for the 'updated_at = ?' 15th-placeholder bug) and coalesce non-None params."""
    conn = _sqlite_conn()
    dbmod.upsert_lead_outcome(conn, 1, email="a@x.com", replied=1, revenue=5000)
    row = conn.execute("SELECT * FROM lead_outcomes WHERE lead_id = 1").fetchone()
    assert row["replied"] == 1 and row["revenue"] == 5000
    assert row["email"] == "a@x.com"

    # Second call with changed + None params: row updates in place, no new row,
    # None keeps the stored value (COALESCE semantics).
    dbmod.upsert_lead_outcome(conn, 1, replied=0, revenue=9000, email=None)
    row = conn.execute("SELECT * FROM lead_outcomes WHERE lead_id = 1").fetchone()
    assert row["replied"] == 0 and row["revenue"] == 9000
    assert row["email"] == "a@x.com"
    assert conn.execute("SELECT COUNT(*) FROM lead_outcomes").fetchone()[0] == 1