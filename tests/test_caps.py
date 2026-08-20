"""Daily/weekly cap gating, run offline against sqlite (the app's PG wrapper
translates the same '?' SQL at runtime)."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import caps


@dataclass
class _Cfg:
    daily_cap: int = 2
    weekly_cap: int = 3


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    caps.ensure_table(conn)
    return conn


def test_reserve_allows_until_daily_cap():
    conn = _conn()
    cfg = _Cfg()
    allowed, reason, _ = caps.reserve(conn, cfg)
    assert allowed
    caps.record_done(conn)
    caps.record_done(conn)
    allowed, reason, state = caps.reserve(conn, cfg)
    assert not allowed
    assert reason == "daily_cap"
    assert state["daily_done"] == 2


def test_failed_scrape_does_not_consume_cap():
    conn = _conn()
    cfg = _Cfg()
    # reserve() alone never increments — only record_done() does.
    for _ in range(10):
        allowed, _, _ = caps.reserve(conn, cfg)
        assert allowed
    assert caps.daily_done(conn) == 0


def test_weekly_cap_sums_last_seven_days():
    conn = _conn()
    cfg = _Cfg(daily_cap=100, weekly_cap=3)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO li_daily_counter (day, profiles_done) VALUES (?, 3)", (yesterday,))
    allowed, reason, _ = caps.reserve(conn, cfg)
    assert not allowed
    assert reason == "weekly_cap"
    # A day 8+ days old must NOT count toward the weekly window.
    conn.execute("DELETE FROM li_daily_counter")
    old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    conn.execute("INSERT INTO li_daily_counter (day, profiles_done) VALUES (?, 3)", (old,))
    allowed, _, _ = caps.reserve(conn, cfg)
    assert allowed
