"""Send-time DNC gate: a lead that entered the do-not-contact registry is
blocked in the email-send loop (never handed to gmail_sender.send_email) and
surfaced with a visible do_not_contact status + reason.

Task 12 acceptance: "a lead whose email is in the registry is blocked from the
send queue with a visible reason." There is no persisted send queue in this
app — sends run immediately in _background_send_emails — so the gate lives in
that loop, re-checking the registry at send time (not just at import).
"""
import sqlite3

import app
import db as dbmod


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, session_id INTEGER, "
                 "domain_normalized TEXT)")
    return conn


def _install(monkeypatch, *, dnc_emails, dnc_domains, sent_rows):
    calls = {"updated": [], "sent_to": []}

    class _NoSend:
        @staticmethod
        def __call__(email, subject, body):
            calls["sent_to"].append(email)

    conn = _conn()
    monkeypatch.setattr(app.dbmod, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(
        app.dbmod, "get_leads_with_scores",
        lambda c, session_id=None: [
            {"id": r["id"], "email": r["email"], "domain_normalized": r["domain"],
             "email_status": r.get("email_status"), "email_subject": r.get("email_subject", ""),
             "email_body": r.get("email_body", "")}
            for r in sent_rows
        ],
    )
    monkeypatch.setattr(app.dncmod, "load_sets", lambda c: (dnc_emails, dnc_domains))
    monkeypatch.setattr(app.dncmod, "add", lambda *a, **k: None)
    monkeypatch.setattr(app, "_set_email_job", lambda *a, **k: None)
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(app.dbmod, "update_lead_email_status",
                        lambda c, lead_id, **kw: calls["updated"].append((lead_id, kw)))
    monkeypatch.setattr("gmail_sender.send_email", _NoSend())
    return calls, conn


def _payload(rows):
    return [{"lead_id": r["id"], "subject": r["email_subject"], "body": r["email_body"]}
            for r in rows]


def test_dnc_lead_is_not_sent_and_gets_visible_reason(monkeypatch):
    rows = [
        {"id": 1, "email": "ok@fresh.com", "domain": "fresh.com",
         "email_status": "sending", "email_subject": "Hi", "email_body": "Body"},
        {"id": 2, "email": "blocked@acme.com", "domain": "acme.com",
         "email_status": "sending", "email_subject": "Hi", "email_body": "Body"},
    ]
    calls, conn = _install(monkeypatch, dnc_emails={"blocked@acme.com"}, dnc_domains=set(),
                           sent_rows=rows)

    app._background_send_emails(9, _payload(rows))

    # Only the non-DNC lead reached send_email.
    assert calls["sent_to"] == ["ok@fresh.com"]
    # The DNC lead was skipped with a visible status + reason.
    dnc_updates = [u for u in calls["updated"] if u[1].get("status") == "do_not_contact"]
    assert dnc_updates == [(2, {"status": "do_not_contact", "error": "do_not_contact_email"})]


def test_dnc_by_domain_blocks_send(monkeypatch):
    """Domain-level DNC also blocks at send time."""
    rows = [
        {"id": 1, "email": "anyone@acme.com", "domain": "acme.com",
         "email_status": "sending", "email_subject": "Hi", "email_body": "Body"},
    ]
    calls, conn = _install(monkeypatch, dnc_emails=set(), dnc_domains={"acme.com"},
                           sent_rows=rows)

    app._background_send_emails(9, _payload(rows))

    assert calls["sent_to"] == []
    assert calls["updated"] == [(1, {"status": "do_not_contact", "error": "do_not_contact_domain"})]


def test_already_sent_and_missing_leads_untouched_by_dnc(monkeypatch):
    """Already-sent leads are skipped as before; a DNC lead is still blocked."""
    rows = [
        {"id": 1, "email": "done@x.com", "domain": "x.com",
         "email_status": "sent", "email_subject": "Hi", "email_body": "Body"},
        {"id": 2, "email": "blocked@acme.com", "domain": "acme.com",
         "email_status": "sending", "email_subject": "Hi", "email_body": "Body"},
    ]
    calls, conn = _install(monkeypatch, dnc_emails={"blocked@acme.com"}, dnc_domains=set(),
                           sent_rows=rows)

    app._background_send_emails(9, _payload(rows))

    assert calls["sent_to"] == []
    # lead 1 was already sent → skipped with no DNC write; lead 2 → DNC block.
    assert calls["updated"] == [(2, {"status": "do_not_contact", "error": "do_not_contact_email"})]