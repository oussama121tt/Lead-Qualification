"""LLM call logging & cost control (FR-7 of the original spec).

Every LLM call (scoring, email generation) is logged in the llm_calls table
with token counts, latency, and an estimated USD cost. A per-session hard cap
(config.toml [budget].session_cap_usd) stops a batch that runs over budget —
the check raises BudgetExceeded BEFORE the call that would exceed it.

Prices are per 1M tokens (input, output), in USD. When a model is missing from
the table the call is still logged with cost NULL — never silently dropped —
and a coverage note should flag the unknown model.
"""
from __future__ import annotations

# USD per 1M tokens: {model: (input, output)}
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Groq
    "llama-3.3-70b-versatile": (0.59, 0.79),
    # Anthropic (first-party API rates)
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}


class BudgetExceeded(RuntimeError):
    """The session's LLM spend reached the configured hard cap."""

    def __init__(self, spent: float, cap: float):
        super().__init__(f"session LLM budget exceeded: ${spent:.4f} spent, cap ${cap:.2f}")
        self.spent = spent
        self.cap = cap


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Estimated USD cost of one call, or None for an unknown model."""
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None
    return (tokens_in * prices[0] + tokens_out * prices[1]) / 1_000_000


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_calls ("
        "id INTEGER, "
        "session_id INTEGER, "
        "lead_id INTEGER, "
        "purpose TEXT, "
        "provider TEXT, "
        "model TEXT, "
        "tokens_in INTEGER, "
        "tokens_out INTEGER, "
        "cost_usd REAL, "
        "latency_ms INTEGER, "
        "created_at TEXT)"
    )
    conn.commit()


def log_call(conn, *, session_id: int | None, lead_id: int | None, purpose: str,
             provider: str, model: str, tokens_in: int, tokens_out: int,
             latency_ms: int, created_at: str) -> float | None:
    """Inserts one llm_calls row. Returns the estimated cost (None if the model
    has no price entry — the row is still written)."""
    cost = estimate_cost(model, tokens_in, tokens_out)
    conn.execute(
        "INSERT INTO llm_calls (session_id, lead_id, purpose, provider, model, "
        "tokens_in, tokens_out, cost_usd, latency_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, lead_id, purpose, provider, model,
         int(tokens_in or 0), int(tokens_out or 0), cost, int(latency_ms), created_at),
    )
    conn.commit()
    return cost


def session_spend(conn, session_id: int) -> dict:
    """Total logged spend for one session: {calls, tokens_in, tokens_out, cost_usd}."""
    row = conn.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in), 0) AS tin, "
        "COALESCE(SUM(tokens_out), 0) AS tout, COALESCE(SUM(cost_usd), 0.0) AS cost "
        "FROM llm_calls WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return {
        "calls": row["calls"] if row else 0,
        "tokens_in": row["tin"] if row else 0,
        "tokens_out": row["tout"] if row else 0,
        "cost_usd": float(row["cost"]) if row else 0.0,
    }


def check_budget(conn, session_id: int, cap_usd: float) -> float:
    """Raises BudgetExceeded when the session's spend has reached cap_usd.
    cap_usd <= 0 disables the check. Returns the current spend."""
    spent = session_spend(conn, session_id)["cost_usd"]
    if cap_usd > 0 and spent >= cap_usd:
        raise BudgetExceeded(spent, cap_usd)
    return spent
