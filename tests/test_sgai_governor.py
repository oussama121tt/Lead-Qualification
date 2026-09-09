"""SGAI credit governor — never run the PAID web-search lane past its monthly
cap, and independent of the Apollo governor. Offline."""
import sqlite3

import pytest

import sgai_client


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sgai_client.ensure_usage_table(conn)
    return conn


def test_records_and_sums_month():
    conn = _conn()
    sgai_client.record_credits(conn, 10)
    sgai_client.record_credits(conn, 5)
    assert sgai_client.credits_used_this_month(conn) == 15


def test_budget_check_blocks_over_cap():
    conn = _conn()
    sgai_client.record_credits(conn, 90)
    with pytest.raises(sgai_client.SgaiCreditCapReached):
        sgai_client.check_credit_budget(conn, needed=20, cap=100)  # 90+20 > 100
    # exactly at cap is allowed
    sgai_client.check_credit_budget(conn, needed=10, cap=100)


def test_cap_zero_disables():
    conn = _conn()
    sgai_client.record_credits(conn, 10_000)
    sgai_client.check_credit_budget(conn, needed=999, cap=0)  # no raise


def test_sgai_is_independent_from_apollo_tables():
    """The two governors never share a row: Apollo spend does not appear in the
    SGAI counter and vice versa."""
    conn = _conn()
    import apollo_client

    apollo_client.ensure_usage_table(conn)
    assert sgai_client.credits_used_this_month(conn) == 0
    sgai_client.record_credits(conn, 7)
    apollo_client.record_credits(conn, 3)
    assert sgai_client.credits_used_this_month(conn) == 7
    assert apollo_client.credits_used_this_month(conn) == 3


def test_month_boundary_resets_usage(monkeypatch):
    """Monthly rollover, not a lifetime counter: usage keyed by 'YYYY-MM', so a
    new month has no row and the lane is never wedged permanently shut after
    one busy month. Proves exactly the apollo_client boundary semantics."""
    real_this_month = sgai_client._this_month
    months = iter(["2026-08", "2026-08", "2026-09", "2026-09"])
    monkeypatch.setattr(sgai_client, "_this_month", lambda: next(months))

    conn = _conn()
    # Busy August: blasts the whole cap.
    sgai_client.record_credits(conn, 50)
    with pytest.raises(sgai_client.SgaiCreditCapReached):
        sgai_client.check_credit_budget(conn, needed=1, cap=50)

    # September rolls over: the counter starts at zero again and the lane
    # unblocks — the exhausted month does not leak into the next one.
    assert sgai_client.credits_used_this_month(conn) == 0
    sgai_client.check_credit_budget(conn, needed=49, cap=50)