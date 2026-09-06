"""Cost estimation, logging, and the session budget cap (FR-7)."""
import sqlite3

import pytest

import costlog


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    costlog.ensure_table(conn)
    return conn


def test_estimate_known_model():
    # llama-3.3-70b: $0.59/M in, $0.79/M out
    cost = costlog.estimate_cost("llama-3.3-70b-versatile", 1_000_000, 1_000_000)
    assert abs(cost - (0.59 + 0.79)) < 1e-9


def test_estimate_unknown_model_is_none_not_zero():
    assert costlog.estimate_cost("mystery-model", 1000, 1000) is None


def test_log_and_session_spend():
    conn = _conn()
    costlog.log_call(conn, session_id=1, lead_id=10, purpose="score",
                     provider="groq", model="llama-3.3-70b-versatile",
                     tokens_in=10_000, tokens_out=1_000, latency_ms=800,
                     created_at="2026-08-20T00:00:00")
    costlog.log_call(conn, session_id=1, lead_id=11, purpose="email",
                     provider="groq", model="llama-3.3-70b-versatile",
                     tokens_in=2_000, tokens_out=500, latency_ms=400,
                     created_at="2026-08-20T00:01:00")
    spend = costlog.session_spend(conn, 1)
    assert spend["calls"] == 2
    assert spend["tokens_in"] == 12_000
    assert spend["cost_usd"] > 0
    # Another session is isolated.
    assert costlog.session_spend(conn, 2)["calls"] == 0


def test_budget_cap_raises_and_zero_disables():
    conn = _conn()
    costlog.log_call(conn, session_id=1, lead_id=1, purpose="score",
                     provider="groq", model="llama-3.3-70b-versatile",
                     tokens_in=10_000_000, tokens_out=1_000_000, latency_ms=1,
                     created_at="2026-08-20T00:00:00")
    with pytest.raises(costlog.BudgetExceeded):
        costlog.check_budget(conn, 1, cap_usd=1.0)
    # cap 0 = disabled — never raises
    costlog.check_budget(conn, 1, cap_usd=0)
