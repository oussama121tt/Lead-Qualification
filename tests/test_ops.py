"""/ops dashboard snapshot on the sqlite harness (no vendor calls)."""
import sqlite3

import ops


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE analysis_sessions (id INTEGER PRIMARY KEY, label TEXT, status TEXT, created_at TEXT);
        CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, company_name TEXT, status TEXT,
                            is_duplicate INTEGER DEFAULT 0, review_status TEXT, email_body TEXT, email_status TEXT);
        CREATE TABLE lead_scores (id INTEGER PRIMARY KEY, lead_id INTEGER, segment TEXT, confidence REAL,
                                  founder_profile TEXT, build_evidence TEXT, needs_human_review INTEGER,
                                  disqualify_reason TEXT, scored_at TEXT);
        CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, provider TEXT, model TEXT, tokens_in INTEGER,
                                tokens_out INTEGER, cost_usd REAL, created_at TEXT);
        CREATE TABLE apollo_usage (month TEXT PRIMARY KEY, credits_used INTEGER);
        CREATE TABLE sgai_usage (month TEXT PRIMARY KEY, credits_used INTEGER);
        CREATE TABLE do_not_contact (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE export_history (id INTEGER PRIMARY KEY, lead_id INTEGER);
        CREATE TABLE lead_outcomes (lead_id INTEGER PRIMARY KEY, opened INTEGER, replied INTEGER);
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, name TEXT, status TEXT, session_id INTEGER);
        CREATE TABLE lead_trigger_events (id INTEGER PRIMARY KEY, lead_id INTEGER);
    """)
    conn.executescript("""
        INSERT INTO analysis_sessions VALUES (1, 's', 'running', '2026-09-11T10:00:00');
        INSERT INTO leads (id, session_id, company_name, status) VALUES (1, 1, 'A', 'RESCORE_PENDING'), (2, 1, 'B', 'SCORED'), (3, 1, 'C', 'NEW');
        INSERT INTO leads (id, session_id, company_name, status, is_duplicate) VALUES (4, 1, 'D', 'SKIPPED', 1);
        INSERT INTO lead_scores (lead_id, segment, confidence, founder_profile, build_evidence, needs_human_review, disqualify_reason, scored_at)
            VALUES (2, 'unclear', 0.2, 'unknown', 'unknown', 1, 'ungrounded_evidence_quotes_removed: 1', '2020-01-01T00:00:00'),
                   (2, 'ai_solo_founder', 0.85, 'non_technical', 'ai_built', 0, NULL, '2099-01-01T00:00:00');
        INSERT INTO llm_calls (provider, model, tokens_in, tokens_out, cost_usd, created_at)
            VALUES ('anthropic', 'claude-sonnet-5', 1000, 200, 0.004, '2099-01-01T00:00:00');
        INSERT INTO do_not_contact (email) VALUES ('x@y.z');
    """)
    return conn


def test_snapshot_shape_without_vendor_calls():
    s = ops.snapshot(_conn(), external=False)
    p = s["pipeline"]
    assert p["total"] == 4 and p["duplicates"] == 1
    assert p["pending"] == 2                      # RESCORE_PENDING + NEW
    assert p["by_status"]["SCORED"] == 1
    assert p["running_sessions"][0]["id"] == 1
    # latest verdict per lead wins for the quality block
    assert s["quality"]["segment"] == {"ai_solo_founder": 1}
    assert s["quality"]["founder_profile"] == {"non_technical": 1}
    assert s["quality"]["needs_review"] == 0
    assert s["outreach"]["dnc"] == 1
    assert "apollo" not in s["vendors"] and "sgai" not in s["vendors"]


def test_snapshot_survives_missing_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("CREATE TABLE leads (id INTEGER PRIMARY KEY, status TEXT, is_duplicate INTEGER DEFAULT 0);")
    s = ops.snapshot(conn, external=False)
    assert s["pipeline"]["total"] == 0
    assert s["llm"]["today_usd"] == 0


def test_vendor_errors_are_reported_not_raised(monkeypatch):
    ops._CACHE.clear()
    monkeypatch.setattr(ops, "apollo_credits", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(ops, "sgai_credits", lambda: {"per_key": [], "total_remaining": 0, "n_keys": 0})
    s = ops.snapshot(_conn(), external=True)
    assert "down" in s["vendors"]["apollo"]["error"]
    assert s["vendors"]["sgai"]["n_keys"] == 0
    ops._CACHE.clear()


def test_provider_status_roundtrip():
    conn = _conn()
    ops.record_provider_status(conn, "anthropic", {"input_tokens_remaining": "123"})
    s = ops.snapshot(conn, external=False)
    assert s["llm"]["provider_status"]["anthropic"]["input_tokens_remaining"] == "123"
