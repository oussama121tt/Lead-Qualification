"""Instantly/Smartlead export — approval-gated, {{first_line}} populated."""
import export


def test_first_line_prefers_email_body_opener():
    lead = {"email_body": "Hi Jane,\n\nSaw you're hiring 3 engineers.\n\nBest,\nWael"}
    assert "hiring 3 engineers" in export._first_line_from(lead)


def test_exported_body_carries_first_line_tag_not_the_literal_opener(monkeypatch):
    """Phase 1 export contract: the exported email body holds the {{first_line}}
    merge tag where the opener sat (so the sending tool substitutes it, and the
    opener is not emitted twice), while the `first_line` column keeps the real
    value. Re-substituting the value must reproduce the original drafted body."""
    import db as dbmod

    lead = {
        "id": 1, "segment": "ai_solo_founder", "needs_human_review": 0,
        "email": "a@x.com", "is_duplicate": 0, "review_status": None,
        "first_name": "Jane", "last_name": "", "company_name": "Acme",
        "website_url": "", "linkedin_url": "", "email_subject": "Quick question",
        "email_body": "Hi Jane,\n\nSaw you're hiring 3 engineers.\n\nBest,\nWael",
        "personalization_hooks": None,
    }
    monkeypatch.setattr(dbmod, "get_leads_with_scores", lambda conn, session_id=None: [lead])
    row = next(export._iter_instantly_rows(None, session_id=1, approved_only=True))
    assert row["first_line"] == "Saw you're hiring 3 engineers."
    assert "{{first_line}}" in row["email_body"]
    assert "Saw you're hiring 3 engineers." not in row["email_body"]
    # Tag substitution round-trips to the exact original drafted body.
    assert row["email_body"].replace("{{first_line}}", row["first_line"]) == \
        "Hi Jane,\n\nSaw you're hiring 3 engineers.\n\nBest,\nWael"


def test_instant_csv_string_writes_first_line_tag_and_omits_internal_id(monkeypatch):
    """The CSV writer must survive the internal "id" key (latent bug: DictWriter
    defaults to extrasaction='raise' and the row carries id for the download
    route's DNC/export-history recording) and emit the tag + value columns."""
    import io
    import csv
    import db as dbmod

    rows_db = [
        {"id": 1, "segment": "ai_solo_founder", "needs_human_review": 0,
         "email": "a@x.com", "email_subject": "Subject",
         "email_body": "Hi Jane,\n\nYou moved off Lovable this month.\n\nBest,\nWael",
         "personalization_hooks": None, "is_duplicate": 0, "review_status": None},
    ]
    monkeypatch.setattr(dbmod, "get_leads_with_scores", lambda conn, session_id=None: rows_db)
    reader = csv.DictReader(io.StringIO(export.instantly_csv_string(None, session_id=1)))
    out = next(iter(reader))
    assert "id" not in out  # internal key never leaks into the CSV
    assert out["first_line"] == "You moved off Lovable this month."
    assert out["email_body"] == "Hi Jane,\n\n{{first_line}}\n\nBest,\nWael"


def test_with_first_line_tag_unchanged_when_opener_absent_or_body_empty():
    assert export._with_first_line_tag("", "Hi") == ""
    assert export._with_first_line_tag("Hi,\n\nProd", "") == "Hi,\n\nProd"
    # Opener not present verbatim (e.g. LLM rewrote it) -> body untouched,
    # first_line column still carries the extracted value.
    body = "Hi Jane,\n\nHow are things?\n\nBest,\nWael"
    assert export._with_first_line_tag(body, "Saw you're hiring") == body


def test_with_first_line_tag_replaces_only_the_opener_occurrence():
    """Plain str.find + slicing (no regex): only the FIRST occurrence — the
    opener's own position — becomes the tag even when that sentence appears
    again later in the body, and the round-trip still reproduces the original."""
    opener = "Saw you're hiring 3 engineers."
    body = (f"Hi Jane,\n\n{opener}\n\nWe help teams like yours that are "
            f"{opener} We can talk through it in 15 minutes.\nBest,\nWael")
    assert body.count(opener) == 2
    tagged = export._with_first_line_tag(body, opener)
    assert tagged.count(export.FIRST_LINE_TAG) == 1
    assert tagged.count(opener) == 1
    assert tagged.replace(export.FIRST_LINE_TAG, opener) == body


def test_first_line_falls_back_to_hook():
    lead = {"email_body": "", "personalization_hooks": [{"hook": "you moved off Lovable", "based_on": "x"}]}
    assert export._first_line_from(lead) == "you moved off Lovable"


def test_iter_instantly_only_approved(monkeypatch):
    rows_db = [
        {"id": 1, "segment": "ai_solo_founder", "needs_human_review": 0, "email": "a@x.com",
         "is_duplicate": 0, "review_status": None, "email_body": "Hi,\n\nGreat product.", "personalization_hooks": None},
        {"id": 2, "segment": "unclear", "needs_human_review": 1, "email": "b@x.com",
         "is_duplicate": 0, "review_status": None, "email_body": "", "personalization_hooks": None},
        {"id": 3, "segment": "too_big", "needs_human_review": 0, "email": "c@x.com",
         "is_duplicate": 0, "review_status": "APPROVED", "email_body": "", "personalization_hooks": None},
        {"id": 4, "segment": "ai_solo_founder", "needs_human_review": 0, "email": "d@x.com",
         "is_duplicate": 0, "review_status": "REJECTED", "email_body": "", "personalization_hooks": None},
    ]
    import db as dbmod
    monkeypatch.setattr(dbmod, "get_leads_with_scores", lambda conn, session_id=None: rows_db)

    got = export.instantly_rows(None, session_id=1, approved_only=True)
    emails = {r["email"] for r in got}
    assert emails == {"a@x.com", "c@x.com"}   # target+clean, and explicit APPROVED
    assert "b@x.com" not in emails            # needs review → excluded
    assert "d@x.com" not in emails            # REJECTED → excluded
    # Lead id is carried on the row dict (not written to the CSV) so the
    # download route can record export_history entries for dedup.
    assert {r["id"] for r in got} == {1, 3}


def test_first_line_blank_when_no_body_and_no_hooks():
    lead = {"email_body": "", "personalization_hooks": None}
    assert export._first_line_from(lead) == ""


def test_approved_without_draft_count_matches_blank_first_line(monkeypatch):
    """Leads counted in summary.approved_without_draft are exactly those whose
    exported first_line would be blank — and they're still exportable."""
    rows_db = [
        {"id": 1, "segment": "ai_solo_founder", "needs_human_review": 0, "email": "a@x.com",
         "is_duplicate": 0, "review_status": None, "email_body": "Hi,\n\nGreat product.",
         "personalization_hooks": None},
        {"id": 2, "segment": "ai_solo_founder", "needs_human_review": 0, "email": "b@x.com",
         "is_duplicate": 0, "review_status": None, "email_body": "", "personalization_hooks": None},
        {"id": 3, "segment": "ai_solo_founder", "needs_human_review": 0, "email": "c@x.com",
         "is_duplicate": 0, "review_status": None, "email_body": "",
         "personalization_hooks": [{"hook": "you moved off Lovable", "based_on": "x"}]},
    ]
    import db as dbmod
    monkeypatch.setattr(dbmod, "get_leads_with_scores", lambda conn, session_id=None: rows_db)

    got = export.instantly_rows(None, session_id=1, approved_only=True)
    # CSV first_line is computed from the source lead (hook fallback works),
    # while the summary.approved_without_draft banner counts source leads whose
    # first_line comes out blank.
    lead_map = {l["id"]: l for l in rows_db}
    exported_ids = [r["id"] for r in got]
    without_draft = [lid for lid in exported_ids if not export._first_line_from(lead_map[lid]).strip()]
    assert len(got) == 3 and without_draft == [2]
    first_lines = {r["id"]: r["first_line"] for r in got}
    assert first_lines[1] == "Hi,"
    assert first_lines[3] == "you moved off Lovable"


def _approve_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, "
                 "company_name TEXT, status TEXT, review_status TEXT, "
                 "review_segment_override TEXT, reviewed_at TEXT)")
    conn.execute("CREATE TABLE lead_scores (lead_id INTEGER, needs_human_review INTEGER)")
    return conn


def test_approve_lead_clears_flag_and_records_review_status():
    """mark_lead_approved (the single approve path used by approve_lead,
    bulk_approve AND review_lead with decision=APPROVED) records the full
    approved state — reviewed_at + segment_override included — so every
    approve route lands on identical final DB rows."""
    import db as dbmod
    conn = _approve_conn()
    conn.execute("INSERT INTO leads (id, session_id, company_name, status, review_segment_override) "
                 "VALUES (1, 9, 'Acme', 'NEEDS_REVIEW', NULL)")
    conn.execute("INSERT INTO lead_scores (lead_id, needs_human_review) VALUES (1, 1)")
    conn.commit()

    dbmod.mark_lead_approved(conn, 1, segment_override="target_cluster_a")
    conn.commit()

    lead = conn.execute("SELECT status, review_status, review_segment_override, reviewed_at "
                        "FROM leads WHERE id=1").fetchone()
    assert lead["status"] == "SCORED"
    assert lead["review_status"] == "APPROVED"
    assert lead["review_segment_override"] == "target_cluster_a"
    assert lead["reviewed_at"]  # timestamp recorded so the decision is auditable
    score = conn.execute("SELECT needs_human_review FROM lead_scores WHERE lead_id=1").fetchone()
    assert score["needs_human_review"] == 0


def test_record_export_writes_export_history_for_dedup():
    """The instantly download route records exported leads in export_history so
    get_exported_domains() (next-batch dedup) sees them as already exported."""
    import sqlite3
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, domain_normalized TEXT)")
    conn.execute("CREATE TABLE export_history (session_id INTEGER, lead_id INTEGER, "
                 "domain_normalized TEXT, exported_at TEXT)")

    conn.execute("INSERT INTO leads (id, session_id, domain_normalized) VALUES (1, 9, 'acme.com')")
    conn.execute("INSERT INTO leads (id, session_id, domain_normalized) VALUES (2, 9, NULL)")
    conn.execute("INSERT INTO leads (id, session_id, domain_normalized) VALUES (3, 9, 'fresh.io')")
    conn.commit()

    n = dbmod.record_export(conn, [1, 2], session_id=9)
    assert n == 1  # lead 2 (no domain) is not recorded
    assert dbmod.get_exported_domains(conn) == {"acme.com"}

    dbmod.record_export(conn, [3], session_id=9)
    assert dbmod.get_exported_domains(conn) == {"acme.com", "fresh.io"}
    assert dbmod.record_export(conn, [], session_id=9) == 0
