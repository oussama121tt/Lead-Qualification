"""
Deduplication — never deletes, only sets an `is_duplicate` flag.

3 levels, in this order:
1. Exact email
2. Identical normalized domain (www.acme.io/pricing -> acme.io)
3. Fuzzy matching on the company name (RapidFuzz), adjustable threshold (default 90)

A lead is never compared against a lead already marked as a duplicate (we
always compare against the remaining "original" leads), to avoid chains of
duplicates pointing at each other.
"""

from rapidfuzz import fuzz
import db as dbmod


def run_dedup(conn, fuzzy_threshold: int = 90, session_id: int | None = None) -> dict:
    leads = dbmod.get_leads(conn, include_duplicates=True, session_id=session_id)
    # Only process leads not yet marked as duplicates, in insertion order:
    # the first one seen for a given email/domain/name stays "the original".
    seen_emails = {}
    seen_domains = {}
    originals_for_fuzzy = []  # list of (lead_id, company_name_normalized)

    stats = {"exact_email": 0, "domain": 0, "fuzzy_company": 0, "kept_original": 0}
    duplicate_updates = []

    for lead in leads:
        if lead["is_duplicate"]:
            continue  # already handled in a previous pass, do not touch it again

        lead_id = lead["id"]
        email = (lead["email"] or "").strip().lower()
        domain = (lead["domain_normalized"] or "").strip().lower()
        company = (lead["company_name"] or "").strip().lower()

        # --- Level 1: exact email ---
        if email and email in seen_emails:
            duplicate_updates.append((seen_emails[email], "exact_email", lead_id))
            stats["exact_email"] += 1
            continue

        # --- Level 2: normalized domain ---
        if domain and domain in seen_domains:
            duplicate_updates.append((seen_domains[domain], "domain_match", lead_id))
            stats["domain"] += 1
            continue

        # --- Level 3: fuzzy company name ---
        duplicate_of = None
        if company:
            for other_id, other_company in originals_for_fuzzy:
                score = fuzz.token_sort_ratio(company, other_company)
                if score >= fuzzy_threshold:
                    duplicate_of = other_id
                    break

        if duplicate_of is not None:
            duplicate_updates.append((duplicate_of, "fuzzy_company_name", lead_id))
            stats["fuzzy_company"] += 1
            continue

        # No duplicate found: this lead becomes an "original" for the rest
        stats["kept_original"] += 1
        if email:
            seen_emails[email] = lead_id
        if domain:
            seen_domains[domain] = lead_id
        if company:
            originals_for_fuzzy.append((lead_id, company))

    if duplicate_updates:
        conn.executemany(
            "UPDATE leads SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ? WHERE id = ?",
            duplicate_updates,
        )
        conn.commit()

    return stats


def check_against_export_history(conn, exported_domains: set) -> int:
    """
    Cross-batch check (dedup against all past exports).
    `exported_domains` = set of domains already exported.
    Returns the number of newly flagged leads.
    """
    leads = dbmod.get_leads(conn, include_duplicates=False)
    flagged_ids = []
    for lead in leads:
        domain = (lead["domain_normalized"] or "").strip().lower()
        if domain and domain in exported_domains:
            flagged_ids.append(lead["id"])
    if flagged_ids:
        conn.executemany(
            "UPDATE leads SET is_duplicate = 1, duplicate_of_id = NULL, duplicate_reason = 'already_exported_previous_batch' WHERE id = ?",
            [(lead_id,) for lead_id in flagged_ids],
        )
        conn.commit()
    return len(flagged_ids)


def run_export_dedup(conn, session_id: int | None = None) -> int:
    """
    Cross-batch deduplication: flags as duplicate any lead whose domain
    was already exported during a previous session.
    """
    exported_domains = dbmod.get_exported_domains(conn)
    return check_against_export_history(conn, exported_domains)