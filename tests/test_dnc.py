"""Do-not-contact registry — email + domain, checked on import."""
import sqlite3

import dnc


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dnc.ensure_table(conn)
    # minimal leads table for flag_batch_on_import
    conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, "
                 "email TEXT, domain_normalized TEXT, is_duplicate INTEGER DEFAULT 0, "
                 "duplicate_reason TEXT, status TEXT)")
    return conn


def test_add_and_check_email():
    conn = _conn()
    dnc.add(conn, email="Jane@Acme.com", reason="sent")
    emails, domains = dnc.load_sets(conn)
    assert dnc.check_lead("jane@acme.com", None, emails, domains) == "do_not_contact_email"
    assert dnc.check_lead("other@x.com", None, emails, domains) is None


def test_add_and_check_domain():
    conn = _conn()
    dnc.add(conn, domain="www.Acme.com", reason="export")
    emails, domains = dnc.load_sets(conn)
    assert dnc.check_lead(None, "acme.com", emails, domains) == "do_not_contact_domain"


def test_no_duplicate_rows():
    conn = _conn()
    dnc.add(conn, email="a@b.com")
    dnc.add(conn, email="a@b.com")
    n = conn.execute("SELECT COUNT(*) AS c FROM do_not_contact").fetchone()["c"]
    assert n == 1


def test_flag_batch_on_import_marks_and_skips():
    conn = _conn()
    dnc.add(conn, email="known@acme.com", reason="sent")
    conn.execute("INSERT INTO leads (session_id, email, domain_normalized) VALUES (1, 'known@acme.com', 'acme.com')")
    conn.execute("INSERT INTO leads (session_id, email, domain_normalized) VALUES (1, 'new@fresh.com', 'fresh.com')")
    conn.commit()
    flagged = dnc.flag_batch_on_import(conn, 1)
    assert flagged == 1
    row = conn.execute("SELECT is_duplicate, duplicate_reason, status FROM leads WHERE email='known@acme.com'").fetchone()
    assert row["is_duplicate"] == 1
    assert row["duplicate_reason"] == "do_not_contact_email"
    assert row["status"] == "SKIPPED"
    fresh = conn.execute("SELECT is_duplicate FROM leads WHERE email='new@fresh.com'").fetchone()
    assert fresh["is_duplicate"] == 0
