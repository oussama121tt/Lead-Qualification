"""Instantly/Smartlead export — approval-gated, {{first_line}} populated."""
import export


def test_first_line_prefers_email_body_opener():
    lead = {"email_body": "Hi Jane,\n\nSaw you're hiring 3 engineers.\n\nBest,\nWael"}
    assert "hiring 3 engineers" in export._first_line_from(lead)


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
