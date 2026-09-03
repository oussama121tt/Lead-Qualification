"""Do-Not-Contact registry — every email and domain we have ever exported or
sent, checked automatically ON IMPORT (not just on export).

Why this exists: cross-batch dedup previously keyed on DOMAIN and was checked
only at export time, so a person re-imported in a new batch was not flagged
until export, and never at the email level. This nearly caused a 22-person
mid-sequence re-email. The DNC registry is the first-class fix: email-level,
permanent, checked before a lead ever enters a batch.

A lead whose email OR domain is on the list is marked is_duplicate with reason
'do_not_contact' at import — it stays visible (never silently dropped) but is
excluded from processing and the send queue.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS do_not_contact ("
        "email TEXT, domain TEXT, reason TEXT, added_at TEXT)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dnc_email ON do_not_contact(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dnc_domain ON do_not_contact(domain)")
    conn.commit()


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _norm_domain(domain: str | None) -> str:
    d = (domain or "").strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d


def add(conn, *, email: str | None = None, domain: str | None = None,
        reason: str = "manual") -> None:
    ensure_table(conn)
    e, d = _norm_email(email), _norm_domain(domain)
    if not e and not d:
        return
    # Avoid duplicate rows for the same email/domain+reason.
    exists = conn.execute(
        "SELECT 1 FROM do_not_contact WHERE COALESCE(email,'') = ? AND COALESCE(domain,'') = ?",
        (e, d),
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO do_not_contact (email, domain, reason, added_at) VALUES (?, ?, ?, ?)",
        (e or None, d or None, reason, _now()),
    )
    conn.commit()


def add_many_from_leads(conn, lead_rows: list[dict], reason: str) -> int:
    """Add every email + domain from a list of lead dicts (used when leads are
    exported or sent, so the next import catches them)."""
    ensure_table(conn)
    n = 0
    for lead in lead_rows:
        e = _norm_email(lead.get("email"))
        d = _norm_domain(lead.get("domain_normalized") or lead.get("email_domain"))
        if e or d:
            add(conn, email=e, domain=d, reason=reason)
            n += 1
    return n


def load_sets(conn) -> tuple[set, set]:
    """Return (emails, domains) currently on the registry — for a fast in-memory
    check over a whole import batch."""
    ensure_table(conn)
    rows = conn.execute("SELECT email, domain FROM do_not_contact").fetchall()
    emails = {r["email"] for r in rows if r["email"]}
    domains = {r["domain"] for r in rows if r["domain"]}
    return emails, domains


def check_lead(email: str | None, domain: str | None,
               dnc_emails: set, dnc_domains: set) -> str | None:
    """Return a reason string if this email/domain is on the registry, else None."""
    e, d = _norm_email(email), _norm_domain(domain)
    if e and e in dnc_emails:
        return "do_not_contact_email"
    if d and d in dnc_domains:
        return "do_not_contact_domain"
    return None


def flag_batch_on_import(conn, session_id: int) -> int:
    """Mark every lead in a freshly imported session that matches the DNC
    registry as is_duplicate (reason do_not_contact). Returns the count.
    Called right after ingestion, before dedup/processing."""
    dnc_emails, dnc_domains = load_sets(conn)
    if not dnc_emails and not dnc_domains:
        return 0
    rows = conn.execute(
        "SELECT id, email, domain_normalized FROM leads WHERE session_id = ? AND is_duplicate = 0",
        (session_id,),
    ).fetchall()
    flagged = 0
    for r in rows:
        reason = check_lead(r["email"], r["domain_normalized"], dnc_emails, dnc_domains)
        if reason:
            conn.execute(
                "UPDATE leads SET is_duplicate = 1, duplicate_reason = ?, status = 'SKIPPED' WHERE id = ?",
                (reason, r["id"]),
            )
            flagged += 1
    conn.commit()
    return flagged
