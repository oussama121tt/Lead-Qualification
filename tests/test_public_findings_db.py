"""DB hard rule for public-surface findings: a row is written ONLY when the
finding is verified AND carries an evidence excerpt. Everything else is dropped
so a finding without proof never reaches the store."""
import sqlite3

import db as dbmod


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE lead_public_findings ("
        "  id INTEGER PRIMARY KEY, session_id INTEGER, lead_id INTEGER NOT NULL,"
        "  check_name TEXT, severity TEXT, evidence_url TEXT, evidence_excerpt TEXT,"
        "  verified INTEGER NOT NULL DEFAULT 0, verified_at TEXT)"
    )
    conn.execute("INSERT INTO leads (id, session_id, status) VALUES (1, 1, 'SCORED')")
    conn.commit()
    return conn


def test_unverified_finding_is_dropped():
    conn = _conn()
    written = dbmod.save_lead_public_findings(conn, 1, [
        {"check_name": "cors_wildcard", "severity": "medium",
         "evidence_url": "https://example.com/", "evidence_excerpt": "proof",
         "verified": 0},   # not verified -> dropped
    ])
    assert written == 0
    assert dbmod.get_lead_public_findings(conn, 1) == []


def test_finding_without_excerpt_is_dropped():
    conn = _conn()
    written = dbmod.save_lead_public_findings(conn, 1, [
        {"check_name": "security_headers", "severity": "low",
         "evidence_url": "https://example.com/", "verified": 1},
    ])
    assert written == 0
    assert dbmod.get_lead_public_findings(conn, 1) == []


def test_only_verified_with_excerpt_is_persisted():
    conn = _conn()
    written = dbmod.save_lead_public_findings(conn, 1, [
        {"check_name": "exposed_dotfiles", "severity": "medium",
         "evidence_url": "https://example.com/.git/config", "verified": 1,
         "evidence_excerpt": "[core] served with HTTP 200."},
        {"check_name": "cors_wildcard", "severity": "medium",
         "evidence_url": "https://example.com/", "verified": 0, "evidence_excerpt": "dropped"},
    ])
    assert written == 1
    rows = dbmod.get_lead_public_findings(conn, 1)
    assert len(rows) == 1
    assert rows[0]["check_name"] == "exposed_dotfiles"
    assert rows[0]["verified"] == 1
    assert dbmod.get_lead_public_findings(conn, 2) == []  # other lead unaffected