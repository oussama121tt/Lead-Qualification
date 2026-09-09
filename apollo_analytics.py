"""Task 13 — Apollo OUTBOUND analytics sync (nightly replies/opens pull).

Two grounded Apollo endpoints:

  * Search for Outreach Emails   GET /api/v1/emailer_messages/search
      Per-message outreach records (to_email, status, opens/clicks/replies,
      reply_class sentiment). This is the per-lead source for Task 16 — a
      nightly sweep writes one raw row per message into
      ``apollo_analytics_sync_report``, then folds each into ``lead_outcomes``
      by matching leads.email.
  * Query Analytics Report       POST /api/v1/reports/sync_report
      Aggregate email metrics (num_emails_sent/opened/clicked/replied),
      0 credits, for monthly roll-ups. Mirrored here as build_sync_report_
      payload() + pull_sync_report() + parse_sync_report() (offline-testable)
      for a future campaign-level cost/volume screen.

Requires APOLLO_API_KEY in .env (the user adds it manually; no key lives in
the repo). The HTTP layer is isolated in _get so tests monkeypatch it without
a key.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import requests

APOLLO_BASE = "https://api.apollo.io/api/v1"
SEARCH_EMAILS_PATH = "/emailer_messages/search"
SYNC_REPORT_PATH = "/reports/sync_report"


class ApolloAnalyticsError(RuntimeError):
    pass


# Message statuses where nothing was actually sent yet — dropped from the
# sweep (draft/scheduled work still in Apollo; not an outcome).
_UNSENT_STATUSES = {"drafted", "scheduled", "started", "in_progress", "queueing"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _api_key(key: str | None = None) -> str:
    k = (key or os.getenv("APOLLO_API_KEY", "")).strip()
    if not k:
        raise ApolloAnalyticsError("APOLLO_API_KEY not set in .env")
    return k


def _get(path: str, params: dict, key: str, timeout: float = 45.0) -> dict:
    """Single GET to the Apollo API. Isolated for testability (no key needed
    when monkeypatched)."""
    resp = requests.get(
        f"{APOLLO_BASE}{path}",
        params=params,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": key,
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ApolloAnalyticsError(f"Apollo HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as e:
        raise ApolloAnalyticsError(f"Apollo returned non-JSON: {e}")


def _post(path: str, payload: dict, key: str, timeout: float = 45.0) -> dict:
    """Single POST to the Apollo API. Isolated for testability."""
    resp = requests.post(
        f"{APOLLO_BASE}{path}",
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": key,
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ApolloAnalyticsError(f"Apollo HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as e:
        raise ApolloAnalyticsError(f"Apollo returned non-JSON: {e}")


# ---------------------------------------------------------------------------
# Per-message sweep (Search for Outreach Emails)
# ---------------------------------------------------------------------------

def search_outreach_emails(*, page: int = 1, per_page: int = 25, statuses: list | None = None,
                           key: str | None = None, _get=_get) -> dict:
    """One page of Apollo outreach emails. `statuses` are emailer_message_stats
    values (delivered/opened/clicked/replied/bounced/...). Returns the raw
    response (emailer_messages[] + pagination)."""
    params = {"page": str(page), "per_page": str(per_page)}
    if statuses:
        params["emailer_message_stats[]"] = list(statuses)
    return _get(SEARCH_EMAILS_PATH, params, _api_key(key))


def _message_skippable(msg: dict) -> bool:
    status = str(msg.get("status") or "").strip().lower()
    return status in _UNSENT_STATUSES


def _date_str(value) -> str | None:
    """First 10 chars (YYYY-MM-DD) of any ISO-ish timestamp."""
    if not value:
        return None
    s = str(value)[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def parse_outreach_email(msg: dict) -> dict:
    """Maps one Apollo emailer_message into the normalized outcome row shape
    (Task 16 columns + the ids needed for reconciliation)."""
    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    to_email = (msg.get("to_email") or "").strip().lower()
    if not to_email:
        recipients = msg.get("recipients") or []
        if recipients:
            first = recipients[0] or {}
            to_email = (first.get("email") or "").strip().lower()
    opened = 1 if (msg.get("opened_at") or _int(msg.get("opens"))) else 0
    clicked = 1 if (msg.get("clicked_at") or _int(msg.get("clicks"))) else 0
    replied = 1 if (msg.get("replied") or msg.get("replied_at") or _int(msg.get("replies"))) else 0
    return {
        "apollo_message_id": msg.get("id") or "",
        "email": to_email,
        "campaign_id": msg.get("emailer_campaign_id") or "",
        "sent_at": msg.get("sent_at") or msg.get("created_at") or msg.get("completed_at"),
        "status": msg.get("status") or "",
        "opened": opened,
        "clicked": clicked,
        "replied": replied,
        "reply_sentiment": msg.get("reply_class") or None,
    }


# ---------------------------------------------------------------------------
# Raw report table (Task 13 artifact: apollo_analytics_sync_report)
# ---------------------------------------------------------------------------

def ensure_report_table(conn) -> None:
    """PG-identity DDL for apollo_analytics_sync_report — COURSE-GUARDED: the PG
    wrapper can't translate SQLite's AUTOINCREMENT idiom, so this must stay
    PG-identity only; sqlite (test harness) stubs ensure_report_table and
    builds its own compat table (same pattern as campaigns.ensure_table)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS apollo_analytics_sync_report ("
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
        "month TEXT NOT NULL, fetched_at TEXT NOT NULL, "
        "apollo_message_id TEXT, to_email TEXT, emailer_campaign_id TEXT, "
        "sent_at TIMESTAMPTZ, status TEXT, "
        "opened INTEGER NOT NULL DEFAULT 0, clicked INTEGER NOT NULL DEFAULT 0, "
        "replied INTEGER NOT NULL DEFAULT 0, reply_sentiment TEXT, "
        "raw_json TEXT)"
    )
    conn.commit()


def _save_report_rows(conn, month: str, rows: list[dict], raw_messages: list[dict]) -> None:
    now = _now()
    payload = []
    for i, r in enumerate(rows):
        raw = json.dumps(raw_messages[i], ensure_ascii=False) if i < len(raw_messages) else None
        payload.append((
            month, now, r["apollo_message_id"], r["email"], r["campaign_id"],
            r["sent_at"], r["status"], r["opened"], r["clicked"], r["replied"],
            r["reply_sentiment"], raw,
        ))
    conn.executemany(
        "INSERT INTO apollo_analytics_sync_report "
        "(month, fetched_at, apollo_message_id, to_email, emailer_campaign_id, "
        " sent_at, status, opened, clicked, replied, reply_sentiment, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (month, apollo_message_id) DO NOTHING",
        payload,
    )
    conn.commit()


def _fold_into_outcomes(conn, rows: list[dict]) -> tuple[int, int]:
    """Maps fetched message rows to leads by email and rows lead_outcomes
    (one row per lead, idempotent over the month). Returns (matched, unmatched).
    A single executemany + commit keeps a nightly sweep cheap on Neon."""
    emails = sorted({r["email"] for r in rows if r.get("email")})
    lead_map: dict[str, int] = {}
    if emails:
        for i in range(0, len(emails), 400):
            chunk = emails[i:i + 400]
            placeholders = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT id, email FROM leads WHERE LOWER(email) IN ({placeholders})", chunk
            ):
                lead_map[(r["email"] or "").strip().lower()] = r["id"]

    params = []
    seen: set[int] = set()
    now = _now()
    for r in rows:
        lid = lead_map.get(r["email"])
        if lid is None or lid in seen:
            continue
        seen.add(lid)
        params.append((
            lid, r["email"], r["sent_at"], r["opened"], r["clicked"], r["replied"],
            r["reply_sentiment"], now, now,
        ))
    if params:
        conn.executemany(
            "INSERT INTO lead_outcomes "
            "(lead_id, email, sent_at, opened, clicked, replied, reply_sentiment, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (lead_id) DO UPDATE SET "
            "email = COALESCE(EXCLUDED.email, lead_outcomes.email), "
            "sent_at = COALESCE(EXCLUDED.sent_at, lead_outcomes.sent_at), "
            "opened = COALESCE(EXCLUDED.opened, lead_outcomes.opened), "
            "clicked = COALESCE(EXCLUDED.clicked, lead_outcomes.clicked), "
            "replied = COALESCE(EXCLUDED.replied, lead_outcomes.replied), "
            "reply_sentiment = COALESCE(EXCLUDED.reply_sentiment, lead_outcomes.reply_sentiment), "
            "updated_at = EXCLUDED.updated_at ",
            params,
        )
        conn.commit()
    return len(seen), len(rows) - len(seen)


# Engagement states the search endpoint can filter on (emailer_message_stats[]).
ENGAGEMENT_STATS = ("opened", "clicked", "replied", "bounced")


def sync_analytics_report(conn, *, key: str | None = None, days: int = 7,
                          max_emails: int = 5000, _get=_get,
                          stats=ENGAGEMENT_STATS) -> dict:
    """Task 13 nightly entrypoint: pull Apollo per-message outreach outcomes
    for the last `days`, save raw rows to apollo_analytics_sync_report, then
    fold them into lead_outcomes by email.

    Returns a summary dict {messages, matched, unmatched, opened, clicked,
    replied, month}. Raises ApolloAnalyticsError when APOLLO_API_KEY is not
    set (the user adds it to .env manually)."""
    api_key = _api_key(key)
    ensure_report_table(conn)

    start = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    rows: list[dict] = []
    raw_messages: list[dict] = []
    page, fetched = 1, 0
    while fetched < max_emails:
        data = search_outreach_emails(page=page, per_page=100, key=api_key, _get=_get)
        messages = data.get("emailer_messages") or []
        if not messages:
            break
        for m in messages:
            if _message_skippable(m):
                continue
            parsed = parse_outreach_email(m)
            sent_date = _date_str(parsed["sent_at"])
            if sent_date and sent_date < start:
                continue  # older than the window (emailer_messages/search has no date filter)
            if not parsed["apollo_message_id"]:
                continue
            rows.append(parsed)
            raw_messages.append(m)
            fetched += 1
            if fetched >= max_emails:
                break
        pagination = data.get("pagination") or {}
        total_pages = pagination.get("total_pages")
        if total_pages and page >= int(total_pages):
            break
        page += 1

    # Engagement enrichment. The plain search payload carries NO opened /
    # clicked fields (verified live: only `replied` is present, and only on
    # replied messages), so without this pass opens and clicks would stay 0
    # forever. The same endpoint filtered by emailer_message_stats[] returns
    # exactly the messages in each engagement state; we sweep each state and
    # flag the rows by message id. A failing sweep is reported, never hidden.
    stats_errors = _enrich_engagement(rows, key=api_key, _get=_get,
                                      max_pages=max(1, max_emails // 100),
                                      stats=stats)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    _save_report_rows(conn, month, rows, raw_messages)
    matched, unmatched = _fold_into_outcomes(conn, rows)

    return {
        "messages": len(rows),
        "matched": matched,
        "unmatched": unmatched,
        "opened": sum(1 for r in rows if r["opened"]),
        "clicked": sum(1 for r in rows if r["clicked"]),
        "replied": sum(1 for r in rows if r["replied"]),
        "bounced": sum(1 for r in rows if r["status"] == "bounced"),
        "stats_errors": stats_errors,
        "month": month,
    }


def _enrich_engagement(rows: list[dict], *, key: str, _get=_get, max_pages: int = 50,
                       stats=ENGAGEMENT_STATS) -> dict:
    """Flags `rows` (in place) with opened/clicked/replied/bounced by sweeping
    the stats-filtered search for each state. Returns {stat: error_message}
    for sweeps that failed (empty dict when every sweep succeeded)."""
    if not rows or not stats:
        return {}
    by_id = {r["apollo_message_id"]: r for r in rows if r.get("apollo_message_id")}
    errors: dict[str, str] = {}
    for stat in stats:
        page = 1
        try:
            while page <= max_pages:
                data = search_outreach_emails(page=page, per_page=100, statuses=[stat],
                                              key=key, _get=_get)
                messages = data.get("emailer_messages") or []
                if not messages:
                    break
                for m in messages:
                    row = by_id.get(m.get("id"))
                    if row is None:
                        continue
                    if stat == "bounced":
                        row["status"] = "bounced"
                    else:
                        row[stat] = 1
                pagination = data.get("pagination") or {}
                total_pages = pagination.get("total_pages")
                if total_pages and page >= int(total_pages):
                    break
                page += 1
        except Exception as exc:  # one failed sweep must not lose the others
            errors[stat] = f"{exc.__class__.__name__}: {exc}"[:200]
    return errors


# ---------------------------------------------------------------------------
# Query Analytics Report (aggregates; 0 credits) — supporting roll-ups
# ---------------------------------------------------------------------------

def build_sync_report_payload(*, start: str | None = None, end: str | None = None,
                              group_by: str | None = None) -> dict:
    """Payload for POST /reports/sync_report: email engagement totals, grouped
    by one dimension (e.g. emailer_campaign_id) or flat. Custom window => the
    smart_datetime_range filter + custom_range modality."""
    metrics = [
        {"value": name, "smart_datetime_reference": "emailer_message__sent_at",
         "smart_user_id_reference": "emailer_message__user_id"}
        for name in ("num_emails_sent", "num_emails_opened", "num_emails_clicked", "num_emails_replied")
    ]
    if start and end:
        return {
            "metrics": metrics,
            "group_by": [{"name": group_by}] if group_by else [],
            "pivot_group_by": [],
            "sorts": [],
            "filters": {"smart_datetime_range": {"min": start, "max": end}},
            "date_ranges": [{"modality": "custom_range"}],
            "group_by_totals_selected": True,
            "pivot_group_by_totals_selected": False,
        }
    return {
        "metrics": metrics,
        "group_by": [{"name": group_by}] if group_by else [],
        "pivot_group_by": [],
        "sorts": [],
        "filters": {},
        "date_ranges": [{"modality": "last_30_days"}],
        "group_by_totals_selected": True,
        "pivot_group_by_totals_selected": False,
    }


def pull_sync_report(*, key: str | None = None, start: str | None = None,
                     end: str | None = None, group_by: str | None = None,
                     _post=_post) -> list[dict]:
    """POST the Query Analytics Report and return the flattened rows (see
    parse_sync_report). Calls _post directly so tests can stub it."""
    return parse_sync_report(_post(SYNC_REPORT_PATH, build_sync_report_payload(
        start=start, end=end, group_by=group_by), _api_key(key)))


def parse_sync_report(data: dict) -> list[dict]:
    """Flattens the sync_report bucket response into one row per dimension
    bucket: {key, readable_key, num_emails_sent, num_emails_opened,
    num_emails_clicked, num_emails_replied}. The 'Total' bucket is dropped
    (callers can recompute/roll it up)."""
    out: list[dict] = []
    table = (data.get("response") or {}).get("table_response") or {}
    if not isinstance(table, dict):
        return out
    for _dim, nested in table.items():
        if not isinstance(nested, dict):
            continue
        for b in nested.get("buckets", []):
            if not isinstance(b, dict):
                continue
            if (b.get("key") or "").lower() == "total":
                continue
            out.append({
                "key": b.get("key"),
                "readable_key": b.get("readable_key"),
                "num_emails_sent": b.get("num_emails_sent") or 0,
                "num_emails_opened": b.get("num_emails_opened") or 0,
                "num_emails_clicked": b.get("num_emails_clicked") or 0,
                "num_emails_replied": b.get("num_emails_replied") or 0,
            })
    return out