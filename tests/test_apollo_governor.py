"""Apollo credit governor — never enrich past the monthly cap. Offline."""
import sqlite3

import pytest

import apollo_client


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apollo_client.ensure_usage_table(conn)
    return conn


def test_records_and_sums_month():
    conn = _conn()
    apollo_client.record_credits(conn, 10)
    apollo_client.record_credits(conn, 5)
    assert apollo_client.credits_used_this_month(conn) == 15


def test_budget_check_blocks_over_cap():
    conn = _conn()
    apollo_client.record_credits(conn, 90)
    with pytest.raises(apollo_client.ApolloCreditCapReached):
        apollo_client.check_credit_budget(conn, needed=20, cap=100)  # 90+20 > 100
    # exactly at cap is allowed
    apollo_client.check_credit_budget(conn, needed=10, cap=100)


def test_cap_zero_disables():
    conn = _conn()
    apollo_client.record_credits(conn, 10_000)
    apollo_client.check_credit_budget(conn, needed=999, cap=0)  # no raise


def test_enrich_respects_cap_and_records(monkeypatch):
    conn = _conn()
    apollo_client.record_credits(conn, 99)

    # Cap is 100, we already used 99 → enriching 3 people must be blocked
    # BEFORE any HTTP call. Patch _post to explode if it's ever reached.
    monkeypatch.setattr(apollo_client, "_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API")))
    people = [{"first_name": "A", "last_name": "B", "company_name": "C"} for _ in range(3)]
    with pytest.raises(apollo_client.ApolloCreditCapReached):
        apollo_client.enrich_people(conn, people, monthly_cap=100)


def test_enrich_maps_and_records_credits(monkeypatch):
    conn = _conn()

    def fake_post(path, payload, timeout=45.0):
        # one match per detail sent
        return {"matches": [{"first_name": "Jane", "last_name": "Doe",
                             "email": "jane@acme.com", "title": "Founder",
                             "organization": {"name": "Acme", "primary_domain": "acme.com"}}
                            for _ in payload["details"]]}
    monkeypatch.setattr(apollo_client, "_post", fake_post)

    people = [{"first_name": "Jane", "last_name": "Doe", "company_name": "Acme"}]
    out = apollo_client.enrich_people(conn, people, monthly_cap=1000)
    assert out["credits"] == 1
    assert apollo_client.credits_used_this_month(conn) == 1
    row = apollo_client.person_to_lead_row(out["enriched"][0])
    assert row["email"] == "jane@acme.com"
    assert "acme.com" in row["website_url"]
