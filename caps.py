"""Daily/weekly LinkedIn caps, persisted GLOBALLY (not per-run/session) in the
li_daily_counter table. Daily = today's count; weekly = sum of the last 7 days.

Ported from the proven lead_tool enrichment project (orchestrator/caps.py).

reserve() is the gate the LinkedIn lane calls before each profile: it returns
whether a profile may run now, and if not, when the cap resets. record_done()
increments only after a SUCCESSFUL profile, so a failed scrape never consumes
the budget.

SQL uses '?' placeholders: they run natively on sqlite (tests) and are
translated to %s by the app's PostgreSQL wrapper (db._PgConnection).
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS li_daily_counter ("
        "day TEXT PRIMARY KEY, profiles_done INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()


def daily_done(conn) -> int:
    row = conn.execute(
        "SELECT profiles_done FROM li_daily_counter WHERE day = ?", (_today(),)
    ).fetchone()
    return row["profiles_done"] if row else 0


def weekly_done(conn) -> int:
    since = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(profiles_done), 0) AS n FROM li_daily_counter WHERE day >= ?",
        (since,),
    ).fetchone()
    return row["n"] if row else 0


def next_daily_reset() -> str:
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat(timespec="minutes")


def check(conn, cfg_linkedin) -> dict:
    """Return current cap state without reserving."""
    d, w = daily_done(conn), weekly_done(conn)
    return {
        "daily_done": d, "daily_cap": cfg_linkedin.daily_cap,
        "weekly_done": w, "weekly_cap": cfg_linkedin.weekly_cap,
        "daily_ok": d < cfg_linkedin.daily_cap,
        "weekly_ok": w < cfg_linkedin.weekly_cap,
        "reset_at": next_daily_reset(),
    }


def reserve(conn, cfg_linkedin) -> tuple[bool, str | None, dict]:
    """Gate for one profile. Returns (allowed, reason, state).
    Does NOT increment — call record_done() after a successful profile so a
    failed scrape doesn't consume the cap."""
    state = check(conn, cfg_linkedin)
    if not state["daily_ok"]:
        return False, "daily_cap", state
    if not state["weekly_ok"]:
        return False, "weekly_cap", state
    return True, None, state


def record_done(conn) -> None:
    conn.execute(
        "INSERT INTO li_daily_counter (day, profiles_done) VALUES (?, 1) "
        "ON CONFLICT (day) DO UPDATE SET profiles_done = li_daily_counter.profiles_done + 1",
        (_today(),),
    )
    conn.commit()
