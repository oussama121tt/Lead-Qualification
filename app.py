"""Flask interface for the Lead Qualification & Scoring pipeline.

Flow: Apollo CSV upload → PostgreSQL (Neon) ingestion → RapidFuzz deduplication →
Firecrawl scraping + Claude scoring → results tables → CSV exports.

Run with: python app.py
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from functools import wraps

import pandas as pd
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, session, url_for, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

import capacity as capacitymod
import campaigns as campaignsmod
import db as dbmod
from db import _now as _db_now
import dedup as dedupmod
import dnc as dncmod
import export as exportmod
import pipeline as pipelinemod
import recipes as recipesmod
import sourcing as sourcingmod
import analytics as analyticsmod
import apollo_analytics as apollo_analyticsmod
from constants import CONFIDENCE_THRESHOLD, NOT_YET_SCORED_STATUSES, OUT_OF_TARGET_SEGMENTS, TARGET_SEGMENTS
from scorer import INVALID_VERDICT_CONFIDENCE_CAP

logger = logging.getLogger("app")



# PostgreSQL (Neon) is required — db.get_connection() raises an error without DATABASE_URL.

app = Flask(__name__)
# Session-cookie signing key. Must be stable across gunicorn workers;
# random per-process (previous) invalidates sessions on every worker
# restart/timeout -> login loop on /progress/stream (seen in prod).
_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret and os.getenv("FLASK_ENV", "").lower() == "production":
    raise RuntimeError("FLASK_SECRET_KEY must be set in production (Render).")
if not _secret:
    _secret = "lead-qualification-engine"
    print("[app] FLASK_SECRET_KEY not set — using dev fallback (set FLASK_SECRET_KEY in production).")
app.config["SECRET_KEY"] = _secret

# ---- Jinja filters for display formatting ----

@app.template_filter("map_offer")
def _map_offer(offer: str | None) -> str:
    mapping = {"ai_audit": "AI Audit", "general_audit": "Technical Audit", "pipeline": "Pipeline Support"}
    return mapping.get(offer) if offer in mapping else "—"

@app.template_filter("map_segment")
def _map_segment(segment: str | None) -> str:
    mapping = {
        "ai_solo_founder": "Solo AI founder",
        "technical_founder": "Technical founder",
        "small_agency_scaling": "Small scaling agency",
        "too_big": "Too big",
        "wrong_field": "Wrong field",
        "unclear": "Unclear",
    }
    return mapping.get(segment) if segment and segment in mapping else (segment or "Not evaluated")

@app.template_filter("map_status_label")
def _map_status_label(status: str | None) -> str:
    mapping = {"FETCH_FAILED": "Scraping failures", "SCORE_FAILED": "Scoring failures",
               "LOW_CONFIDENCE": "Low confidence", "NEEDS_REVIEW": "Needs review",
               "SCORED": "Scored", "NEW": "New", "imported": "Pending",
               "running": "Running", "completed": "Completed", "failed": "Failed"}
    return mapping.get(status) if status and status in mapping else (status or "—")

@app.template_filter("badge_class")
def _badge_class(status: str | None) -> str:
    mapping = {"completed": "badge-success", "SCORED": "badge-success",
               "running": "badge-warning", "imported": "badge-warning", "LOW_CONFIDENCE": "badge-warning",
               "failed": "badge-error", "SCORE_FAILED": "badge-error", "FETCH_FAILED": "badge-error",
               "NEW": "badge-neutral"}
    return mapping.get(status) if status and status in mapping else "badge-neutral"

_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

@app.template_filter("format_datetime")
def _format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{dt.day} {_MONTHS_EN[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"

@app.template_filter("confidence_class")
def _confidence_class(confidence: float | None) -> str:
    """Color class for the confidence %, aligned with the thresholds in scorer.py."""
    if confidence is None:
        return "conf-muted"
    if confidence >= CONFIDENCE_THRESHOLD:
        return "conf-success"
    if confidence < INVALID_VERDICT_CONFIDENCE_CAP:
        return "conf-danger"
    return "conf-warning"

_pipeline_progress: dict[int, dict] = {}
_pipeline_lock = threading.Lock()


def _store_progress(session_id: int, progress: dict):
    with _pipeline_lock:
        _pipeline_progress[session_id] = progress


def _get_progress(session_id: int) -> dict | None:
    with _pipeline_lock:
        return _pipeline_progress.get(session_id)


def _clear_progress(session_id: int):
    with _pipeline_lock:
        _pipeline_progress.pop(session_id, None)


def _background_rescore_pipeline(conn, session_id: int, throttle_seconds: float, lead_status: str = "RESCORE_PENDING"):
    """Runs a rescore (no re-scraping or web search) in a background thread."""
    processed = 0
    try:
        for update in pipelinemod.run_rescore_pipeline(conn, throttle_seconds=throttle_seconds, session_id=session_id, lead_status=lead_status):
            processed += 1
            _store_progress(session_id, {"pipeline_status": "running", **update})
        final_status = "cancelled" if dbmod.is_session_cancelled(conn, session_id) else "completed"
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = final_status
            p["pipeline_status"] = final_status
            p["processed"] = processed
            p["completed_ts"] = time.monotonic()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(conn, session_id, final_status, completed_at=_db_now())
    except Exception as e:
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = "failed"
            p["pipeline_status"] = "failed"
            p["error"] = str(e)
            p["processed"] = processed
            p["completed_ts"] = time.monotonic()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(conn, session_id, "failed", completed_at=_db_now())
    finally:
        conn.close()


def _background_pipeline(conn, session_id: int, throttle_seconds: float, concurrency: int = pipelinemod.DEFAULT_CONCURRENCY):
    """Runs the pipeline in a background thread. In parallel when ``concurrency > 1``:
    several leads are processed simultaneously (each with its own DB connection taken from the pool)."""
    processed = 0
    try:
        for update in pipelinemod.run_pipeline(conn, throttle_seconds=throttle_seconds, session_id=session_id, concurrency=concurrency):
            processed += 1
            _store_progress(session_id, {"pipeline_status": "running", **update})
        final_status = "cancelled" if dbmod.is_session_cancelled(conn, session_id) else "completed"
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = final_status
            p["pipeline_status"] = final_status
            p["processed"] = processed
            p["completed_ts"] = time.monotonic()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(conn, session_id, final_status, completed_at=_db_now())
    except Exception as e:
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = "failed"
            p["pipeline_status"] = "failed"
            p["error"] = str(e)
            p["processed"] = processed
            p["completed_ts"] = time.monotonic()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(conn, session_id, "failed", completed_at=_db_now())
    finally:
        conn.close()


_init_lock = threading.Lock()
_init_done = False


def _ensure_schema_init():
    """Runs the idempotent schema migration exactly once per process, lazily,
    on the FIRST real DB use (first open_db / first request) instead of at
    import time. At-import init used to hit Neon on every `import app` (tests,
    CLI, build) and blocked ~60s until the connection timed out. Requests that
    never touch the DB (offline tests) never trigger it."""
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if _init_done:
            return
        try:
            _init_schema_once()
        except Exception as _init_e:
            # Don't crash requests when Neon is unreachable; the next DB use
            # retries. (Offline tests never open_db, so they never land here.)
            print(f"[init] schema init deferred: {_init_e}")
            return
        _init_done = True


@contextmanager
def open_db():
    _ensure_schema_init()
    conn = dbmod.get_connection()
    try:
        # NB: init_db()/schema DDL is NOT here — _ensure_schema_init() ran it
        # exactly once on the first DB use (see above). Running it per-request
        # would do ~35+ network round-trips to Neon (CREATE/ALTER IF NOT EXISTS)
        # = several seconds of latency on every page.
        yield conn
    finally:
        conn.close()


def _init_schema_once():
    """Creates/updates the schema once per process (idempotent)."""
    conn = dbmod.get_connection()
    try:
        dbmod.init_db(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# The role is stored in the Flask session (signed cookie) at login and is NOT
# re-read from the database on every request (avoids one DB round-trip per
# page, a slowness issue already fixed on this project after the SQLite →
# Postgres migration). Consequence: demoting an admin does not invalidate an
# already-open session until the next logout.
#
# The "blocked" (is_active) status, on the other hand, IS re-verified in the
# database on each request (chosen option for blocking): a blocked user loses
# access at their very next request, not only at their next login. The cost is
# one lightweight query per logged-in request, negligible at this scale.

PUBLIC_ENDPOINTS = {"login", "signup", "static"}


@app.before_request
def _ensure_schema():
    global _init_done
    if not _init_done:
        try:
            _init_schema_once()
            _init_done = True
        except Exception as e:
            print(f"[init] retry failed: {e}")


@app.before_request
def _require_login():
    endpoint = request.endpoint
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return None
    user_id = session.get("user_id")
    if user_id is None:
        return redirect(url_for("login", next=request.path))
    try:
        with open_db() as conn:
            user = dbmod.get_user_by_id(conn, user_id)
    except Exception:
        user = None
    if user is None or not user.get("is_active"):
        session.clear()
        flash("Your account is blocked. Contact an administrator.", "danger")
        return redirect(url_for("login"))
    return None


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("history"))
        return view(*args, **kwargs)

    return wrapped


def _assert_session_access(session_row: dict) -> bool:
    """True if the current user can access this session: admin, or owner
    (owner_id matches their user_id). A legacy session (owner_id NULL) is
    only accessible to an admin."""
    if session.get("role") == "admin":
        return True
    return session_row.get("owner_id") == session.get("user_id")


def _require_session(session_id: int):
    """Checks that the session exists AND is accessible to the current user.
    Returns (session_row, None) on success, (None, response) on failure."""
    with open_db() as conn:
        session_row = dbmod.get_analysis_session(conn, session_id)
    if session_row is None:
        flash("Session not found.", "error")
        return None, redirect(url_for("home"))
    if not _assert_session_access(session_row):
        flash("Access not authorized to this analysis.", "danger")
        return None, redirect(url_for("history"))
    return session_row, None


def _resolve_accessible_session(session_id: int | None):
    """Resolves a session_id coming from a query arg: an explicit id is checked
    for ownership; when absent, falls back to the latest session the current
    user can access (global latest for an admin)."""
    if session_id is not None:
        _, denied = _require_session(session_id)
        if denied is not None:
            return None, denied
        return session_id, None
    owner_id = None if session.get("role") == "admin" else session.get("user_id")
    with open_db() as conn:
        latest = dbmod.get_latest_session_id(conn, owner_id=owner_id)
    if latest is None:
        return None, redirect(url_for("home"))
    return latest, None


def missing_api_keys():
    return [k for k in ("FIRECRAWL_API_KEY", "GROQ_API_KEY") if not os.getenv(k)]


def _table_html(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return '<div class="empty-state">No data to display for now.</div>'
    display_df = df.loc[:, columns].copy()
    # XSS fix: every data cell is HTML-escaped — scraped-site text and LLM
    # output are untrusted input. Only the app-generated 'detail' link column
    # (built in _load_dashboard_data, no external data in it beyond the
    # numeric lead id) is left as markup.
    import html as _html
    for col in display_df.columns:
        if col == "detail":
            continue
        display_df[col] = display_df[col].map(
            lambda v: _html.escape(str(v)) if isinstance(v, str) else v
        )
    return display_df.to_html(index=False, classes="table table-hover table-striped align-middle", escape=False)


def _summary_context(conn, session_id=None, owner_id=None):
    if session_id is not None:
        owner_id = None
    leads = dbmod.get_leads(conn, session_id=session_id, owner_id=owner_id)
    scored = dbmod.get_leads_with_scores(conn, session_id=session_id, owner_id=owner_id)
    statuses = Counter((lead.get("status") or "UNKNOWN") for lead in leads)
    return {
        "total_leads": len(leads),
        "ready_to_process": len(dbmod.get_leads_to_process(conn, session_id=session_id, owner_id=owner_id)),
        "duplicates": sum(1 for lead in leads if lead.get("is_duplicate")),
        "scored": sum(1 for row in scored if row.get("segment")),
        "needs_review": sum(1 for row in scored if row.get("needs_human_review") == 1),
        "fetch_failed": statuses.get("FETCH_FAILED", 0),
        "score_failed": statuses.get("SCORE_FAILED", 0),
        "low_confidence": statuses.get("LOW_CONFIDENCE", 0),
        "status_counts": dict(statuses),
    }


def _session_summary(conn, session_id: int | None, owner_id: int | None = None):
    session = None
    if session_id is not None:
        session = dbmod.get_analysis_session(conn, session_id)
        # Not accessible (missing or another user's session): fall back to
        # the latest session the current user can access.
        if session is not None and owner_id is not None and session.get("owner_id") != owner_id:
            session = None
    if session is None:
        session_id = dbmod.get_latest_session_id(conn, owner_id=owner_id)
        session = dbmod.get_analysis_session(conn, session_id) if session_id is not None else None
    return session_id, session


def _load_dashboard_data(conn, session_id=None, selected_lead_id=None, segment_filter=None, needs_review=False, hide_duplicates=True):
    leads = pd.DataFrame(dbmod.get_leads(conn, session_id=session_id))
    if not leads.empty:
        leads = leads.fillna("")
        leads.insert(0, "detail", leads["id"].apply(
            lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">View</a>',
        ))

    scores = pd.DataFrame(dbmod.get_leads_with_scores(conn, session_id=session_id))
    if not scores.empty:
        scores = scores.fillna("")
        if segment_filter:
            scores = scores[scores["segment"].isin(segment_filter)]
        if needs_review:
            scores = scores[scores["needs_human_review"] == 1]
        if hide_duplicates:
            scores = scores[scores["is_duplicate"] == 0]
        scores = scores.copy()
        scores.insert(0, "detail", scores["id"].apply(
            lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">View</a>',
        ))

    lead_detail = None
    if not scores.empty:
        available_ids = set(scores["id"].tolist())
        if selected_lead_id not in available_ids:
            selected_lead_id = int(scores.iloc[0]["id"])
        lead_detail = scores[scores["id"] == selected_lead_id].iloc[0].to_dict()
        # Parse JSON fields from DB text columns
        for field in ("evidence_quotes", "personalization_hooks", "built_with_ai_signals", "technical_signals", "pain_signals", "sensitive_data_categories", "budget_evidence", "budget_blockers"):
            val = lead_detail.get(field)
            if isinstance(val, str):
                try:
                    lead_detail[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass

        # SGAI web search results
        lead_detail["web_search_evidence"] = dbmod.get_lead_search_evidence(conn, selected_lead_id)

    segments = []
    if not scores.empty and "segment" in scores.columns:
        segments = sorted([seg for seg in scores["segment"].dropna().unique().tolist() if seg])

    return {
        "leads": leads,
        "scores": scores,
        "lead_detail": lead_detail,
        "segments": segments,
        "selected_lead_id": selected_lead_id,
    }


def _csv_response(filename: str, csv_text: str):
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_ingest(conn, uploaded_file, session_id=None):
    if uploaded_file is None or uploaded_file.filename == "":
        raise ValueError("No CSV file provided.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        tmp.write(uploaded_file.read())
        tmp_path = tmp.name
    try:
        batch_id = f"batch_{uuid.uuid4().hex[:8]}"
        summary = dbmod.insert_leads_from_csv(conn, tmp_path, batch_id, session_id=session_id)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return batch_id, summary


@app.route("/", methods=["GET"])
def home():
    owner_id = None if session.get("role") == "admin" else session.get("user_id")
    with open_db() as conn:
        sessions = dbmod.list_analysis_sessions(conn, limit=50, owner_id=owner_id)
        summary = _summary_context(conn, owner_id=owner_id)

    return render_template(
        "home.html",
        api_key_missing=missing_api_keys(),
        summary=summary,
        sessions=sessions,
    )


@app.route("/history", methods=["GET"])
def history():
    owner_id = None if session.get("role") == "admin" else session.get("user_id")
    with open_db() as conn:
        sessions = dbmod.list_analysis_sessions(conn, limit=50, owner_id=owner_id)
    # Non-admin: each user's own history is numbered per user (1, 2, 3...),
    # regardless of the other users' sessions in between. The admin's
    # combined history keeps the global ids.
    return render_template("history.html", sessions=sessions, show_rank=owner_id is not None)


@app.route("/dashboard", methods=["GET"])
def dashboard():
    selected_lead_id = request.args.get("lead_id", type=int)
    selected_session_id = request.args.get("session_id", type=int)
    segment_raw = request.args.getlist("segment")
    if not segment_raw:
        segment_value = request.args.get("segment", default="")
        segment_filter = [segment_value] if segment_value else []
    else:
        segment_filter = [segment for segment in segment_raw if segment]

    needs_review = request.args.get("needs_review") == "1"
    hide_duplicates = request.args.get("hide_duplicates", "1") != "0"

    owner_id = None if session.get("role") == "admin" else session.get("user_id")
    if selected_session_id is not None:
        with open_db() as conn:
            session_row = dbmod.get_analysis_session(conn, selected_session_id)
        if session_row is None:
            flash("Session not found.", "error")
            return redirect(url_for("home"))
        if not _assert_session_access(session_row):
            flash("Access not authorized to this analysis.", "danger")
            return redirect(url_for("history"))

    with open_db() as conn:
        selected_session_id, selected_session = _session_summary(conn, selected_session_id, owner_id=owner_id)
        sessions = dbmod.list_analysis_sessions(conn, limit=50, owner_id=owner_id)
        summary = _summary_context(conn, session_id=selected_session_id, owner_id=owner_id)
        if selected_session_id is not None:
            summary = {
                **summary,
                "selected_session_label": selected_session.get("label") if selected_session else None,
                "selected_session_status": selected_session.get("status") if selected_session else None,
            }
        data = _load_dashboard_data(
            conn, session_id=selected_session_id, selected_lead_id=selected_lead_id,
            segment_filter=segment_filter, needs_review=needs_review, hide_duplicates=hide_duplicates,
        )

        leads_table = _table_html(
            data["leads"],
            ["detail", "id", "company_name", "website_url", "email", "status", "is_duplicate", "batch_id"],
        )
        scores_table = _table_html(
            data["scores"],
            [
                "detail", "id", "company_name", "website_url", "segment", "confidence",
                "company_stage", "recommended_offer", "needs_human_review", "status", "disqualify_reason",
            ],
        )

    return render_template(
        "dashboard.html",
        api_key_missing=missing_api_keys(),
        summary=summary,
        sessions=sessions,
        selected_session_id=selected_session_id,
        selected_session=selected_session,
        session_name=selected_session.get("label") if selected_session else "History",
        leads_table=leads_table,
        scores_table=scores_table,
        segments=data["segments"],
        selected_segments=segment_filter,
        needs_review=needs_review,
        hide_duplicates=hide_duplicates,
        lead_detail=data["lead_detail"],
        selected_lead_id=data["selected_lead_id"],
        timestamp=datetime.utcnow().strftime("%Y%m%d_%H%M"),
    )


@app.route("/upload", methods=["POST"])
def upload_and_review():
    """Step 1: import CSV → ingest + dedup → redirect to the review page."""
    uploaded_file = request.files.get("csv_file")
    fuzzy_threshold = request.form.get("fuzzy_threshold", type=int, default=90)

    if uploaded_file is None or uploaded_file.filename == "":
        flash("Add a CSV file before continuing.", "error")
        return redirect(url_for("home"))

    if missing_api_keys():
        flash(
            "Missing environment keys: FIRECRAWL_API_KEY and/or GROQ_API_KEY. "
            "Import is possible, but the scraping/scoring pipeline may fail.",
            "warning",
        )

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Analysis {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
            owner_id=session.get("user_id"),
        )
        batch_id, ingest_summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)
        dnc_flagged = dncmod.flag_batch_on_import(conn, session_id)
        if dnc_flagged:
            flash(f"{dnc_flagged} lead(s) on the do-not-contact registry were excluded.", "info")

    flash(
        f"{ingest_summary['inserted']} row(s) imported, {ingest_summary['skipped_no_website']} ignored. "
        f"Duplicates: {dedup_summary['exact_email']} email, {dedup_summary['domain']} domain, "
        f"{dedup_summary['fuzzy_company']} fuzzy.",
        "success",
    )
    return redirect(url_for("import_review", session_id=session_id))


@app.route("/import/<int:session_id>", methods=["GET"])
def import_review(session_id: int):
    """Step 2: review page for imported leads + choice of scoring criteria."""
    session, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        leads = dbmod.get_leads(conn, session_id=session_id)
        keepers = [l for l in leads if not l.get("is_duplicate")]
        duplicates = [l for l in leads if l.get("is_duplicate")]
        custom_criteria = dbmod.get_scoring_criteria_custom(conn, session_id)

    criteria_options = [
        {"key": "ai_solo_founder", "label": "TARGET: Non-tech vibe coder", "desc": "Non-technical founder building with AI (Cursor, Bolt, Lovable, Replit, vibe coding)."},
        {"key": "technical_founder", "label": "TARGET: Tech person using AI", "desc": "Technical team using AI as a development tool."},
        {"key": "solo_or_small", "label": "Solo / Micro-team", "desc": "Single founder or a team of 1-5 people."},
        {"key": "agency_or_studio", "label": "Agency / Studio", "desc": "Service provider, web agency, creation studio."},
        {"key": "no_ai", "label": "Established without AI signals", "desc": "Established company with no indication of building via AI."},
        {"key": "wrong_field", "label": "Not our target", "desc": "Unrelated sector, agency, or organization without AI dev usage."},
    ]

    return render_template(
        "import_review.html",
        session_id=session_id,
        session=session,
        keepers=keepers,
        duplicates=duplicates,
        criteria_options=criteria_options,
        custom_criteria=custom_criteria,
    )


@app.route("/import/<int:session_id>/start", methods=["POST"])
def start_pipeline_from_review(session_id: int):
    """Step 3: save the criteria, select the leads, launch the pipeline."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    criteria = request.form.getlist("criteria")
    custom_criteria = request.form.get("custom_criteria", "").strip()
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    concurrency = request.form.get("concurrency", type=int, default=pipelinemod.DEFAULT_CONCURRENCY)
    selected_ids = request.form.getlist("lead_ids")
    dup_ids = request.form.getlist("dup_ids")

    with open_db() as conn:
        dbmod.save_scoring_criteria(conn, session_id, criteria)
        dbmod.save_scoring_criteria_custom(conn, session_id, custom_criteria)

        # Include the checked duplicates in the analysis
        dup_int_ids = [int(s) for s in dup_ids if s.isdigit()]
        if dup_int_ids:
            conn.executemany(
                "UPDATE leads SET is_duplicate = 0, duplicate_of_id = NULL, duplicate_reason = NULL, status = 'NEW' WHERE id = ?",
                [(did,) for did in dup_int_ids],
            )
            conn.commit()
            selected_ids.extend(str(did) for did in dup_int_ids)

        if selected_ids:
            # Mark the unselected leads as SKIPPED
            selected_set = set(int(x) for x in selected_ids if x.isdigit())
            all_leads = dbmod.get_leads(conn, session_id=session_id)
            skip_ids = [
                lead["id"]
                for lead in all_leads
                if not lead.get("is_duplicate")
                and lead.get("status") == "NEW"
                and lead["id"] not in selected_set
            ]
            dbmod.update_leads_status(conn, skip_ids, "SKIPPED")

        to_process = dbmod.get_leads_to_process(conn, session_id=session_id)
        if not to_process:
            flash("No lead selected.", "warning")
            return redirect(url_for("import_review", session_id=session_id))
        dbmod.resume_analysis_session(conn, session_id)

    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(), session_id, throttle_seconds),
        kwargs={"concurrency": concurrency},
        daemon=True,
    ).start()

    flash(f"Pipeline launched with {len(to_process)} lead(s).", "info")
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/analyze-pending/<int:session_id>", methods=["POST"])
def analyze_pending(session_id: int):
    """Launches the analysis of pending leads (SKIPPED / NEW not yet processed)."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    selected_ids = request.form.getlist("lead_ids")
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    concurrency = request.form.get("concurrency", type=int, default=pipelinemod.DEFAULT_CONCURRENCY)
    with open_db() as conn:
        if selected_ids:
            ids = [int(x) for x in selected_ids if x.isdigit()]
            dbmod.update_leads_status(conn, ids, "NEW")
            dbmod.set_last_batch_ids(conn, session_id, ids)
        else:
            conn.execute("UPDATE leads SET status = 'NEW' WHERE session_id = ? AND status IN ('SKIPPED', 'NEW') AND is_duplicate = 0", (session_id,))
            conn.commit()
            all_now_new = [r["id"] for r in conn.execute("SELECT id FROM leads WHERE session_id = ? AND status = 'NEW' AND is_duplicate = 0", (session_id,)).fetchall()]
            dbmod.set_last_batch_ids(conn, session_id, all_now_new)

        to_process = dbmod.get_leads_to_process(conn, session_id=session_id)
        if selected_ids:
            to_process = [l for l in to_process if l["id"] in ids]
        if not to_process:
            flash("No pending leads.", "warning")
            return redirect(url_for("results_view", session_id=session_id))
        dbmod.resume_analysis_session(conn, session_id)

    _clear_progress(session_id)
    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(), session_id, throttle_seconds),
        kwargs={"concurrency": concurrency},
        daemon=True,
    ).start()

    flash(f"Analysis launched for {len(to_process)} pending lead(s).", "info")
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id: int):
    """Deletes a session and all of its data."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied
    try:
        with open_db() as conn:
            dbmod.delete_analysis_session(conn, session_id)
        flash(f"Session #{session_id} deleted.", "success")
    except Exception as e:
        flash(f"Error while deleting: {e}", "error")
    next_url = request.args.get("next")
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("history"))


@app.route("/session/<int:session_id>/cancel", methods=["POST"])
def cancel_session(session_id: int):
    """Marks a running analysis session as cancelled (cooperative stop).

    The pipeline (pipeline.py) checks the flag between leads and between
    futures; leads already running finish cleanly, queued ones never
    start. The background thread then writes 'cancelled' to both the
    in-memory progress and the DB so the SSE stream and the history page
    reflect the real outcome.
    """
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied
    with open_db() as conn:
        dbmod.cancel_analysis_session(conn, session_id)
    flash("Cancellation requested: running leads will finish, queued ones will not start.", "info")
    return redirect(url_for("progress_view", session_id=session_id))


def _homepage_content(conn, lead_id: int) -> str:
    """Returns the first non-empty scraped page content for the lead (the homepage)."""
    for row in dbmod.get_lead_content(conn, lead_id):
        if (row.get("content") or "").strip():
            return row["content"]
    return ""


# ---------------------------------------------------------------------------
# Email jobs — generation and sending run in BACKGROUND threads.
# Doing N LLM calls (or N sends with a 10s throttle) inside one HTTP request
# gets killed by any production WSGI worker timeout (gunicorn default: 30s).
# The request now only starts the job; the review page polls its status.
# ---------------------------------------------------------------------------

_email_jobs: dict[int, dict] = {}
_email_jobs_lock = threading.Lock()


def _set_email_job(session_id: int, **fields):
    with _email_jobs_lock:
        job = _email_jobs.get(session_id, {})
        job.update(fields)
        _email_jobs[session_id] = job


def _get_email_job(session_id: int) -> dict | None:
    with _email_jobs_lock:
        job = _email_jobs.get(session_id)
        return dict(job) if job else None


def _background_generate_emails(session_id: int, lead_ids: list[int], include_unapproved: bool):
    import emailer

    conn = dbmod.get_connection()
    generated = failed = skipped = unapproved = 0
    try:
        scores = {lead["id"]: lead for lead in dbmod.get_leads_with_scores(conn, session_id=session_id)}
        approved_ids = {l["id"] for l in _categorize_leads(list(scores.values()))["approved"]}
        for i, lead_id in enumerate(lead_ids):
            _set_email_job(session_id, kind="generate", status="running",
                           done=i, total=len(lead_ids))
            lead = scores.get(lead_id)
            if lead is None:
                failed += 1
                continue
            # Approval gate (spec hard rule): only leads in the qualified
            # bucket get an email drafted, unless explicitly overridden.
            # Skipped leads are counted and reported — never silent.
            if not include_unapproved and lead_id not in approved_ids:
                unapproved += 1
                continue
            if lead.get("email_status") in ("sent", "draft"):
                skipped += 1
                continue
            try:
                email = emailer.generate_email_for_lead(
                    lead, _homepage_content(conn, lead_id),
                    cost_cb=pipelinemod._make_cost_cb(conn, session_id, lead_id, "email"),
                )
                dbmod.update_lead_email_status(
                    conn,
                    lead_id,
                    subject=email["subject"],
                    body=email["body"],
                    status="draft",
                    provider=os.environ.get("EMAIL_LLM_PROVIDER", "groq"),
                )
                generated += 1
            except Exception as e:
                failed += 1
                dbmod.update_lead_email_status(conn, lead_id, status="failed", error=str(e))
        message = (f"{generated} draft(s) generated, {failed} failed, {skipped} already "
                   f"drafted/sent, {unapproved} not in 'Ready to approve' (skipped).")
        _set_email_job(session_id, kind="generate", status="done",
                       done=len(lead_ids), total=len(lead_ids), message=message)
    except Exception as e:
        _set_email_job(session_id, kind="generate", status="failed", error=str(e))
    finally:
        conn.close()


@app.route("/session/<int:session_id>/prepare_emails", methods=["POST"])
def prepare_emails(session_id: int):
    """Starts a BACKGROUND job that generates a DRAFT email per selected lead.

    Only leads in the 'Ready to approve' category are drafted (hard rule of
    the original spec) unless include_unapproved=1 is posted explicitly.
    Nothing is ever sent here — drafts land on the review page, where the
    human reads, edits, and confirms.
    """
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    lead_ids = [int(x) for x in request.form.getlist("lead_ids") if x.isdigit()]
    if not lead_ids:
        flash("Select at least one lead to email.", "warning")
        return redirect(url_for("results_view", session_id=session_id))

    job = _get_email_job(session_id)
    if job and job.get("status") == "running":
        flash("An email job is already running for this session — wait for it to finish.", "warning")
        return redirect(url_for("email_review_view", session_id=session_id))

    include_unapproved = request.form.get("include_unapproved") == "1"
    _set_email_job(session_id, kind="generate", status="running", done=0, total=len(lead_ids))
    threading.Thread(
        target=_background_generate_emails,
        args=(session_id, lead_ids, include_unapproved),
        daemon=True,
    ).start()

    flash(f"Generating {len(lead_ids)} email draft(s) in the background — "
          "this page refreshes automatically when they are ready.", "info")
    return redirect(url_for("email_review_view", session_id=session_id))


@app.route("/session/<int:session_id>/email_review")
def email_review_view(session_id: int):
    """Review page: shows every drafted email with editable subject/body.

    The user reads each generated email, edits it if necessary, then
    confirms the send — the review IS the human validation step.
    """
    session_row, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)
    drafts = [l for l in scores_data if l.get("email_status") == "draft"]

    return render_template("email_review.html", session=session_row, drafts=drafts)


def _background_send_emails(session_id: int, payload: list[dict]):
    """Sends the confirmed emails one by one with the anti-spam throttle,
    in a background thread (a 10s-throttled loop inside an HTTP request
    would be killed by the WSGI worker timeout after ~3 emails)."""
    from gmail_sender import THROTTLE_SECONDS, send_email

    conn = dbmod.get_connection()
    sent = failed = skipped = dnc_skipped = 0
    try:
        # DNC registry is checked again at send time, not just at import: a
        # lead that entered the registry after import (a sent email, an export,
        # a manual add) is never mailed. Skips it with a visible reason.
        dnc_emails, dnc_domains = dncmod.load_sets(conn)
        scores = {lead["id"]: lead for lead in dbmod.get_leads_with_scores(conn, session_id=session_id)}
        for i, item in enumerate(payload):
            _set_email_job(session_id, kind="send", status="running",
                           done=i, total=len(payload))
            lead = scores.get(item["lead_id"])
            if lead is None:
                failed += 1
                continue
            if lead.get("email_status") == "sent":
                skipped += 1
                continue
            # Blocked by the do-not-contact registry: email and/or domain.
            reason = dncmod.check_lead(
                lead.get("email"), lead.get("domain_normalized"),
                dnc_emails, dnc_domains,
            )
            if reason:
                dnc_skipped += 1
                dbmod.update_lead_email_status(
                    conn, item["lead_id"], status="do_not_contact", error=reason,
                )
                continue
            subject = item["subject"] or lead.get("email_subject") or ""
            body = item["body"] or lead.get("email_body") or ""
            if not subject or not body:
                failed += 1
                dbmod.update_lead_email_status(conn, item["lead_id"], status="failed", error="empty email content")
                continue
            try:
                send_email(lead.get("email") or "", subject, body)
                dbmod.update_lead_email_status(
                    conn,
                    item["lead_id"],
                    subject=subject,
                    body=body,
                    status="sent",
                    provider=os.environ.get("EMAIL_LLM_PROVIDER", "groq"),
                    sent_at=_db_now(),
                )
                # A sent email is permanent do-not-contact — the next import
                # cannot re-contact this person (the 22-person-re-email fix).
                dncmod.add(conn, email=lead.get("email"),
                           domain=lead.get("domain_normalized"), reason="sent")
                sent += 1
            except Exception as e:
                failed += 1
                dbmod.update_lead_email_status(conn, item["lead_id"], status="failed", error=str(e))
            if i < len(payload) - 1:
                time.sleep(THROTTLE_SECONDS)
        message = (f"{sent} email(s) sent, {failed} failed, {skipped} already sent, "
                   f"{dnc_skipped} blocked by do-not-contact.")
        _set_email_job(session_id, kind="send", status="done",
                       done=len(payload), total=len(payload), message=message)
    except Exception as e:
        _set_email_job(session_id, kind="send", status="failed", error=str(e))
    finally:
        conn.close()


@app.route("/session/<int:session_id>/send_emails", methods=["POST"])
def send_emails(session_id: int):
    """Starts a BACKGROUND job that sends the (possibly edited) drafts.

    The review page posts the edited subject/body per lead — what is sent
    is exactly what the user saw and approved. Nothing is ever sent
    without this explicit confirmation. The edits are captured from the
    form NOW; the sending itself runs in a background thread.
    """
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    lead_ids = [int(x) for x in request.form.getlist("lead_ids") if x.isdigit()]
    if not lead_ids:
        flash("Select at least one email to send.", "warning")
        return redirect(url_for("email_review_view", session_id=session_id))

    from gmail_sender import GmailNotConfigured, get_credentials

    try:
        get_credentials()
    except GmailNotConfigured as e:
        flash(f"{e} See setup instructions in setup_gmail.py.", "error")
        return redirect(url_for("email_review_view", session_id=session_id))

    job = _get_email_job(session_id)
    if job and job.get("status") == "running":
        flash("An email job is already running for this session — wait for it to finish.", "warning")
        return redirect(url_for("email_review_view", session_id=session_id))

    payload = [
        {
            "lead_id": lid,
            "subject": request.form.get(f"subject_{lid}") or "",
            "body": request.form.get(f"body_{lid}") or "",
        }
        for lid in lead_ids
    ]
    _set_email_job(session_id, kind="send", status="running", done=0, total=len(payload))
    threading.Thread(
        target=_background_send_emails,
        args=(session_id, payload),
        daemon=True,
    ).start()

    flash(f"Sending {len(payload)} email(s) in the background "
          f"(~10s between each to stay under the spam radar).", "info")
    return redirect(url_for("email_review_view", session_id=session_id))


@app.route("/session/<int:session_id>/email_job", methods=["GET"])
def email_job_status(session_id: int):
    """Polled by the review page to display background email-job progress."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return jsonify({"status": "denied"})
    return jsonify(_get_email_job(session_id) or {"status": "idle"})


def _categorize_leads(scores_data: list, reorder_queue: bool = True) -> dict:
    """Distributes scored leads into the 5 categories.

    Simplified logic:
    - Pending       : not yet scored successfully (SCORE_FAILED, FETCH_FAILED, ...)
    - To review     : needs_human_review=True (covers unclear, confidence < 0.7,
      domain_mismatch, ungrounded quotes...)
    - Approved      : target segment AND no human review required
    - Out of target : too_big/wrong_field segment AND no human review required
    - Not selected  : SKIPPED at import
    - Already exported: flagged by the inter-batch dedup
      (already_exported_previous_batch) — analyzed but excluded from new CSV exports.

    `reorder_queue` (config [triggers].reorder_queue): when true, triggered
    leads sort to the top of their bucket (priority desc, then id asc). When
    false the bucket order is untouched — triggers still fire, log and set
    trigger_hook, only the ORDER BY change is skipped.
    """
    approved, not_selected, out_of_target, to_review, pending = [], [], [], [], []

    for lead in scores_data:
        if lead.get("is_duplicate"):
            is_already_exported = (
                lead.get("duplicate_of_id") is None
                and lead.get("duplicate_reason") == "already_exported_previous_batch"
            )
            if not is_already_exported:
                continue
            # Analyzed but excluded from new CSV exports (same domains were
            # exported in a previous batch). It still belongs in its real
            # category below — only a badge marks it as already exported.
            lead["already_exported"] = True

        segment = lead.get("segment")
        status = lead.get("status", "NEW")

        if status == "SKIPPED":
            not_selected.append(lead)
        elif status in NOT_YET_SCORED_STATUSES:
            pending.append(lead)
        elif lead.get("needs_human_review"):
            to_review.append(lead)
        elif segment in TARGET_SEGMENTS:
            approved.append(lead)
        elif segment in OUT_OF_TARGET_SEGMENTS:
            out_of_target.append(lead)
        else:
            to_review.append(lead)

        if lead in approved and lead.get("budget_signal") == "none" and lead.get("budget_blockers"):
            approved.remove(lead)
            reason = lead.get("disqualify_reason") or ""
            lead["disqualify_reason"] = f"{reason} | budget blocker" if reason else "budget blocker"
            to_review.append(lead)

    # Task 9 — triggered leads surface at the top of the queue (priority
    # desc, then id asc), with the hook text already on the lead row. Gated by
    # [triggers].reorder_queue: when false, only the ORDER BY is skipped —
    # firing/logging/hook-setting are unaffected.
    if reorder_queue:
        approved.sort(key=lambda l: (-(l.get("trigger_priority") or 0), l.get("id") or 0))
        to_review.sort(key=lambda l: (-(l.get("trigger_priority") or 0), l.get("id") or 0))

    _log_approved_below_trigger_gate(approved)

    return {
        "approved": approved,
        "not_selected": not_selected,
        "out_of_target": out_of_target,
        "to_review": to_review,
        "pending": pending,
    }


def _log_approved_below_trigger_gate(approved: list) -> None:
    """Fix 7 decision visibility: the trigger check gate
    (triggers._is_high_value_lead — needs_human_review falsy + target segment
    + confidence >= [triggers].high_value_confidence) stays STRICTER than this
    page's approved bucket, so leads can be approved here without ever being
    expensively re-checked for triggers. Log that gap (debug level) so it is
    observable over time rather than silent. Never raises.
    """
    try:
        from runconfig import load_config
        from triggers import _is_high_value_lead
        cfg = load_config()
    except Exception:
        return
    for lead in approved:
        try:
            if _is_high_value_lead(lead, cfg):
                continue
        except Exception:
            continue
        logger.debug(
            "lead %s (%s) approved for review but below the trigger gate: "
            "segment=%s confidence=%s needs_human_review=%s (not trigger-checked)",
            lead.get("id"),
            lead.get("company_name") or lead.get("website_url") or "?",
            lead.get("segment"),
            lead.get("confidence"),
            lead.get("needs_human_review"),
        )


@app.route("/results/<int:session_id>", methods=["GET"])
def results_view(session_id: int):
    """Step 4: results page with 5 categories."""
    session, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)

        from runconfig import load_config
        categories = _categorize_leads(
            scores_data, reorder_queue=load_config().triggers.reorder_queue
        )
        approved = categories["approved"]
        not_selected = categories["not_selected"]
        out_of_target = categories["out_of_target"]
        to_review = categories["to_review"]
        pending = categories["pending"]

        summary = {
            "total": len(scores_data),
            "scored": len([l for l in scores_data if l.get("segment")]),
            "approved": len(approved),
            "approved_without_draft": len(
                [l for l in approved if not exportmod._first_line_from(l).strip()]
            ),
            "to_review": len(to_review),
            "out_of_target": len(out_of_target),
            "not_selected": len(not_selected),
            "pending": len(pending),
            "triggered": len([l for l in scores_data if l.get("trigger_priority")]),
        }

        # Running LLM spend for this session (FR-7): every scoring/email
        # call is logged in llm_calls; the total is surfaced here.
        try:
            import costlog as costlogmod
            summary["llm_spend"] = costlogmod.session_spend(conn, session_id)
        except Exception:
            summary["llm_spend"] = None

        # Load web search evidence for inline display (same structure as web_search_view)
        evidence_by_lead = dbmod.get_search_evidence_for_session(conn, session_id)
        has_search_evidence = bool(evidence_by_lead)
        search_leads = []
        if has_search_evidence:
            leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
            for lead in leads:
                evidence = evidence_by_lead.get(lead["id"], [])
                if evidence:
                    search_leads.append({"lead": lead, "evidence": evidence})

    return render_template(
        "results.html",
        session=session,
        summary=summary,
        categories={
            "approved": approved,
            "not_selected": not_selected,
            "out_of_target": out_of_target,
            "to_review": to_review,
            "pending": pending,
        },
        has_search_evidence=has_search_evidence,
        search_leads=search_leads,
    )


@app.route("/rescore/<int:session_id>", methods=["POST"])
def rescore_leads(session_id: int):
    """Re-runs the scoring on the selected leads (without re-scraping or web search)."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    selected_ids = request.form.getlist("lead_ids")

    with open_db() as conn:
        if selected_ids:
            to_rescore = [int(x) for x in selected_ids if x.isdigit()]
        else:
            # Fallback: all leads to review
            scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)
            to_rescore = []
            for lead in scores_data:
                if lead.get("is_duplicate"):
                    continue
                segment = lead.get("segment")
                status = lead.get("status", "NEW")
                disqualify = lead.get("disqualify_reason") or ""
                is_scoring_error = "api_error" in disqualify.lower() or "no_content_scraped" in disqualify.lower()
                if not is_scoring_error and (status == "LOW_CONFIDENCE" or lead.get("needs_human_review")):
                    if lead.get("id"):
                        to_rescore.append(lead["id"])

        if not to_rescore:
            flash("No lead selected for re-scoring.", "warning")
            return redirect(url_for("results_view", session_id=session_id))

        # Reset the statuses and delete the old scores (no re-scraping)
        if to_rescore:
            dbmod.update_leads_status(conn, to_rescore, "RESCORE_PENDING")
            conn.execute("DELETE FROM lead_scores WHERE lead_id = ANY(%s)", (to_rescore,))
            conn.commit()
        dbmod.resume_analysis_session(conn, session_id)

    new_conn = dbmod.get_connection()
    threading.Thread(
        target=_background_rescore_pipeline,
        args=(new_conn, session_id, 1.0),
        kwargs={"lead_status": "RESCORE_PENDING"},
        daemon=True,
    ).start()

    flash(f"{len(to_rescore)} lead(s) marked for re-scoring.", "info")
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/lead/<int:lead_id>/review", methods=["GET"])
def lead_review_view(lead_id: int):
    """Structured per-lead review page: verdict, decision rationale and all evidence."""
    with open_db() as conn:
        lead = dbmod.get_lead_with_score(conn, lead_id)
        if lead is None:
            flash("Lead not found.", "error")
            return redirect(url_for("home"))
        session_row, denied = _require_session(lead["session_id"])
        if denied is not None:
            return denied
        for field in (
            "evidence_quotes", "personalization_hooks", "built_with_ai_signals",
            "technical_signals", "pain_signals", "sensitive_data_categories", "budget_evidence", "budget_blockers",
        ):
            val = lead.get(field)
            if isinstance(val, str):
                try:
                    lead[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        content_rows = dbmod.get_lead_content(conn, lead_id)
        technical = dbmod.get_lead_technical_signals(conn, lead_id)
        search_evidence = dbmod.get_lead_search_evidence(conn, lead_id)
        coverage_notes = dbmod.get_coverage_notes(conn, lead_id)
        public_findings = dbmod.get_lead_public_findings(conn, lead_id)
        trigger_events = dbmod.get_lead_trigger_events(conn, lead_id)
        try:
            lead["trigger_state_parsed"] = json.loads(lead.get("trigger_state")) if lead.get("trigger_state") else None
        except (json.JSONDecodeError, TypeError):
            lead["trigger_state_parsed"] = None
    return render_template(
        "lead_review.html",
        session=session_row,
        lead=lead,
        content_rows=content_rows,
        technical=technical,
        search_evidence=search_evidence,
        coverage_notes=coverage_notes,
        public_findings=public_findings,
        trigger_events=trigger_events,
    )


@app.route("/session/<int:session_id>/bulk_approve", methods=["POST"])
def bulk_approve(session_id: int):
    """Bulk-approve the selected leads (clears the review flag) — the volume
    review path. Posts lead_ids[]; with confidence_min set, approves all
    to-review leads at or above that confidence in one click."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied
    selected = [int(x) for x in request.form.getlist("lead_ids") if x.isdigit()]
    conf_min = request.form.get("confidence_min", type=float)
    with open_db() as conn:
        if not selected and conf_min is not None:
            rows = dbmod.get_leads_with_scores(conn, session_id=session_id)
            selected = [
                l["id"] for l in rows
                if not l.get("is_duplicate")
                and l.get("needs_human_review")
                and (l.get("confidence") or 0) >= conf_min
            ]
        n = 0
        for lid in selected:
            dbmod.mark_lead_approved(conn, lid)
            n += 1
        conn.commit()
    flash(f"{n} lead(s) approved.", "success")
    return redirect(url_for("results_view", session_id=session_id))


@app.route("/download/instantly.csv", methods=["GET"])
def download_instantly_csv():
    """Approval-gated Instantly/Smartlead export with {{first_line}}. Records
    every exported lead both in the do-not-contact registry AND in
    export_history (same table as scores.csv) so the next batch's dedup
    treats same-domain re-exports as already sent."""
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied
    approved_only = request.args.get("all") != "1"
    with open_db() as conn:
        rows = exportmod.instantly_rows(conn, session_id=selected_session_id, approved_only=approved_only)
        csv_text = exportmod.instantly_csv_string(conn, session_id=selected_session_id, approved_only=approved_only)
        # DNC + export history: never contact these again.
        dnc_rows = [{"email": r["email"],
                     "domain_normalized": dbmod._normalize_domain(r.get("website_url", ""))}
                    for r in rows]
        dncmod.add_many_from_leads(conn, dnc_rows, reason="instantly_export")
        dbmod.record_export(conn, [r["id"] for r in rows if r.get("id")], session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    if not rows:
        flash("No approved leads to export. Approve leads first (or use ?all=1).", "warning")
        return redirect(url_for("results_view", session_id=selected_session_id))
    return _csv_response(f"instantly_{timestamp}.csv", csv_text)


# ---------------------------------------------------------------------------
# Apollo sourcing — search + Stage-0 pre-filter + credit-gated enrich
# ---------------------------------------------------------------------------

_sourcing_jobs: dict[int, dict] = {}
_sourcing_lock = threading.Lock()


def _set_sourcing_job(job_id, **f):
    with _sourcing_lock:
        j = _sourcing_jobs.get(job_id, {})
        j.update(f)
        _sourcing_jobs[job_id] = j


@app.route("/sourcing", methods=["GET"])
def sourcing_view():
    with open_db() as conn:
        recipe_list = recipesmod.list_all(conn)
        import apollo_client
        apollo_client.ensure_usage_table(conn)
        used = apollo_client.credits_used_this_month(conn)
        campaigns_list = []
        for c in campaignsmod.list_all(conn):
            d = dict(c)
            d["lead_count"] = (
                conn.execute("SELECT COUNT(*) FROM leads WHERE session_id = ?",
                             (d.get("session_id"),)).fetchone()[0]
                if d.get("session_id") else 0
            )
            campaigns_list.append(d)
    from runconfig import load_config
    cfg = load_config()
    return render_template("sourcing.html", recipes=recipe_list,
                           campaigns=campaigns_list,
                           credits_used=used, credit_cap=cfg.apollo.monthly_credit_cap,
                           apollo_ready=bool(os.getenv("APOLLO_API_KEY")))


@app.route("/sourcing/recipe", methods=["POST"])
def sourcing_create_recipe():
    name = (request.form.get("name") or "").strip()
    filters_raw = (request.form.get("filters") or "").strip()
    if not name or not filters_raw:
        flash("Recipe needs a name and a filters JSON.", "error")
        return redirect(url_for("sourcing_view"))
    try:
        filters = json.loads(filters_raw)
    except json.JSONDecodeError as e:
        flash(f"Filters must be valid JSON: {e}", "error")
        return redirect(url_for("sourcing_view"))
    with open_db() as conn:
        recipesmod.create(conn, name, filters)
    flash(f"Recipe '{name}' saved.", "success")
    return redirect(url_for("sourcing_view"))


@app.route("/sourcing/recipe/<int:recipe_id>/outcomes", methods=["POST"])
def sourcing_record_outcomes(recipe_id: int):
    """Manually record send/reply outcomes for a recipe.

    Option B outcome-tracking: there is no Apollo analytics write-back wired up
    yet, so sent/replies are entered by hand (or via tools/run_recipe_outcomes.py
    for a batch). This feeds recipes.record_outcomes so the recipe UI's
    Sent/Replies/Reply-rate are backed by real, intentional data rather than
    silently blank — and the UI labels them as manually-entered.
    """
    try:
        sent = int(request.form.get("sent") or 0)
        replies = int(request.form.get("replies") or 0)
    except (TypeError, ValueError):
        flash("Sent/Replies must be whole numbers.", "error")
        return redirect(url_for("sourcing_view"))
    if sent < 0 or replies < 0 or replies > sent:
        flash("Replies can't exceed sent, and neither can be negative.", "error")
        return redirect(url_for("sourcing_view"))
    with open_db() as conn:
        if recipesmod.get(conn, recipe_id) is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("sourcing_view"))
        recipesmod.record_outcomes(conn, recipe_id, sent=sent, replies=replies)
    flash(f"Recorded {sent} sent / {replies} replies for recipe #{recipe_id} (manual entry).", "success")
    return redirect(url_for("sourcing_view"))


def _background_sourcing(job_id, recipe_id, owner_id, dry_run, campaign_id=None):
    conn = dbmod.get_connection()
    try:
        _set_sourcing_job(job_id, status="running")
        summary = sourcingmod.run_recipe(conn, recipe_id=recipe_id, owner_id=owner_id,
                                         dry_run=dry_run, campaign_id=campaign_id)
        _set_sourcing_job(job_id, status="done", summary=summary)
    except Exception as e:
        _set_sourcing_job(job_id, status="failed", error=str(e))
    finally:
        conn.close()


@app.route("/sourcing/run/<int:recipe_id>", methods=["POST"])
def sourcing_run(recipe_id: int):
    dry_run = request.form.get("dry_run") == "1"
    owner_id = session.get("user_id")
    job_id = recipe_id
    _set_sourcing_job(job_id, status="running")
    threading.Thread(target=_background_sourcing,
                     args=(job_id, recipe_id, owner_id, dry_run), daemon=True).start()
    flash("Sourcing run started" + (" (dry run — no credits spent)" if dry_run else "") +
          ". Watch its status below.", "info")
    return redirect(url_for("sourcing_view"))


@app.route("/sourcing/job/<int:recipe_id>", methods=["GET"])
def sourcing_job_status(recipe_id: int):
    with _sourcing_lock:
        return jsonify(_sourcing_jobs.get(recipe_id) or {"status": "idle"})


# ---------------------------------------------------------------------------
# Phase 2 — Campaigns: recipe run -> keyboard review queue -> Ship
# Ship produces capacity-scheduled Instantly-format CSVs + DNC/export-history
# recording. It is an EXPORT, never a send and never an Apollo enroll.
# ---------------------------------------------------------------------------

def _fleet_mailboxes(cfg):
    from capacity import Mailbox, SequenceSchedule  # noqa: F401 (SequenceSchedule re-exported for callers)
    return [
        Mailbox(name=m.name, daily_cap=m.daily_cap,
                ramp_start=(date.fromisoformat(m.ramp_start) if m.ramp_start else None),
                ramp_days=m.ramp_days, ramp_start_cap=m.ramp_start_cap)
        for m in cfg.sending.mailboxes
    ]


def _sent_contacts(conn):
    """Global already-committed snapshot for capacity math, two sources:

    1. Actually-sent leads: `email_sent_at` set → touch 1 completed, T2/T3
       still scheduled (same interpretation as the /capacity route).
    2. Shipped-but-not-yet-sent leads: every SCHIPPED campaign's stored batch
       plan reserves its leads' T1/T2/T3 on their batch dates (touches_sent=0),
       so a second campaign scheduled the same day SEES the first campaign's
       committed slots — the whole reason this planner exists. Ship is an
       export (it never sets email_sent_at), so without this a lead shipped in
       campaign A would be invisible to campaign B and the two together could
       bust a future date's cap even though each individually fit.

    A lead that appears in both is counted once: `email_sent_at` is the
    stronger signal (it is genuinely underway), so the plan entry is skipped.
    """
    sent_ids = set()
    contacts = []
    rows = conn.execute("SELECT id, email_sent_at FROM leads WHERE email_sent_at IS NOT NULL").fetchall()
    for r in rows:
        sent_ids.add(r["id"])
        try:
            sent_on = r["email_sent_at"][:10]
            contacts.append({"added_on": date.fromisoformat(sent_on), "touches_sent": 1})
        except (ValueError, TypeError):
            continue
    campaignsmod.ensure_table(conn)
    plan_rows = conn.execute(
        "SELECT ship_plan FROM campaigns WHERE status = 'shipped' AND ship_plan IS NOT NULL"
    ).fetchall()
    for r in plan_rows:
        try:
            batches = json.loads(r["ship_plan"]).get("batches", [])
        except (TypeError, ValueError):
            continue
        for day_s, ids in batches:
            try:
                d = date.fromisoformat(day_s)
            except (ValueError, TypeError):
                continue
            for lid in ids:
                if lid in sent_ids:
                    continue  # already booked from source 1
                contacts.append({"added_on": d, "touches_sent": 0})
    return contacts


@app.route("/campaigns/new", methods=["POST"])
def campaign_new():
    """Three-click flow start: pick a recipe, a `reviewing` campaign is created,
    and its sourcing run starts in the background — results land in a fresh
    session linked as the campaign's review queue."""
    recipe_id = request.form.get("recipe_id", type=int)
    if not recipe_id:
        flash("Campaign needs a recipe.", "error")
        return redirect(url_for("sourcing_view"))
    name = (request.form.get("name") or "").strip()
    with open_db() as conn:
        if recipesmod.get(conn, recipe_id) is None:
            flash("Recipe not found.", "error")
            return redirect(url_for("sourcing_view"))
        campaignsmod.ensure_table(conn)
        campaign_id = campaignsmod.create(conn, recipe_id, name or None)
    owner_id = session.get("user_id")
    _set_sourcing_job(campaign_id, status="running")
    threading.Thread(target=_background_sourcing,
                     args=(campaign_id, recipe_id, owner_id, False, campaign_id),
                     daemon=True).start()
    flash("Campaign created — sourcing run started; review its queue when it lands.", "info")
    return redirect(url_for("campaign_view", campaign_id=campaign_id))


@app.route("/campaign/<int:campaign_id>", methods=["GET"])
def campaign_view(campaign_id: int):
    from runconfig import load_config
    cfg = load_config()
    with open_db() as conn:
        camp = campaignsmod.get(conn, campaign_id)
        if camp is None:
            flash("Campaign not found.", "error")
            return redirect(url_for("sourcing_view"))
        queued = []
        approved_count = 0
        rejected_count = 0
        if camp.get("session_id"):
            leads = dbmod.get_leads_with_scores(conn, session_id=camp["session_id"])
            queued = [
                l for l in leads
                if not l.get("is_duplicate")
                and l.get("review_status") not in dbmod.VALID_REVIEW_STATUSES
            ]
            approved_count = sum(1 for l in leads if l.get("review_status") == "APPROVED")
            rejected_count = sum(1 for l in leads if l.get("review_status") == "REJECTED")
        job = dict(_sourcing_jobs.get(campaign_id) or {})
    plan_batches = []
    if camp.get("ship_plan"):
        try:
            plan_batches = json.loads(camp["ship_plan"]).get("batches", [])
        except (TypeError, ValueError):
            plan_batches = []
    seq_offsets = cfg.sending.sequence_offsets or []
    return render_template("campaign_view.html", camp=camp, job=job, queued=queued,
                           approved_count=approved_count, rejected_count=rejected_count,
                           plan_batches=plan_batches, seq_offsets=seq_offsets)


@app.route("/campaign/<int:campaign_id>/review", methods=["GET", "POST"])
def campaign_review(campaign_id: int):
    """Keyboard review queue over the campaign's un-decided leads.

    GET renders the whole queue (review_status not yet set) server-side.
    POST records ONE decision (A=APPROVED / X=REJECTED) plus an optional hook
    override (the E key's text input), returning remaining-count JSON so the
    JS advances instantly — twenty-plus A/X per lead is a burst of 20 requests,
    not 20 reloads.
    """
    with open_db() as conn:
        camp = campaignsmod.get(conn, campaign_id)
        if camp is None:
            if request.method == "POST":
                return jsonify({"ok": False, "error": "campaign not found"}), 404
            flash("Campaign not found.", "error")
            return redirect(url_for("sourcing_view"))
        if camp.get("session_id") is None:
            if request.method == "POST":
                return jsonify({"ok": False, "error": "no sourcing session linked yet"}), 409
            flash("The sourcing run hasn't inserted leads yet — check back shortly.", "info")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))

        if request.method == "POST":
            lead_id = request.form.get("lead_id", type=int)
            decision = request.form.get("decision") or ""
            hook = (request.form.get("hook") or "").strip()
            if lead_id is None:
                return jsonify({"ok": False, "error": "lead_id required"}), 400
            all_leads = dbmod.get_leads_with_scores(conn, session_id=camp["session_id"])
            queued_leads = [l for l in all_leads if not l.get("is_duplicate")]
            ids = {l["id"] for l in queued_leads}
            if lead_id not in ids:
                return jsonify({"ok": False, "error": "lead not in campaign queue"}), 404
            # Hook override persists on the lead row regardless of decision.
            if hook:
                dbmod.update_lead_hook_override(conn, lead_id, hook)
            if decision in dbmod.VALID_REVIEW_STATUSES:
                if decision == "APPROVED":
                    dbmod.mark_lead_approved(conn, lead_id)
                    conn.commit()
                else:
                    dbmod.set_lead_review(conn, lead_id, decision)
            # Re-query AFTER the write so remaining is the true queue size.
            remaining = sum(
                1 for l in dbmod.get_leads_with_scores(conn, session_id=camp["session_id"])
                if l.get("review_status") not in dbmod.VALID_REVIEW_STATUSES
            )
            return jsonify({"ok": True, "remaining": remaining, "decision": decision or None,
                            "hook": hook or None})

        leads = dbmod.get_leads_with_scores(conn, session_id=camp["session_id"])
    queued = [
        l for l in leads
        if not l.get("is_duplicate")
        and l.get("review_status") not in dbmod.VALID_REVIEW_STATUSES
    ]
    cards = [_queue_card(l) for l in queued]
    return render_template("campaign_queue.html", camp=camp, cards=cards,
                           total=len(queued))


def _queue_card(lead: dict) -> dict:
    """Display shape for one queue card — keeps Jinja free of JSON parsing."""
    hooks = lead.get("personalization_hooks") or ""
    hook_list = []
    if isinstance(hooks, str) and hooks.strip()[:1] in "[{":
        try:
            hooks = json.loads(hooks)
        except (json.JSONDecodeError, TypeError):
            hooks = []
    if isinstance(hooks, list):
        for h in hooks:
            if isinstance(h, dict):
                hook_list.append(str(h.get("hook") or ""))
            else:
                hook_list.append(str(h))
    return {
        "id": lead["id"],
        "company": lead.get("company_name") or "",
        "name": " ".join(filter(None, [lead.get("first_name"), lead.get("last_name")])),
        "email": lead.get("email") or "",
        "title": lead.get("title") or lead.get("job_title") or "",
        "segment": lead.get("segment") or "",
        "offer": lead.get("recommended_offer") or "",
        "hooks": hook_list,
        "trigger_hook": lead.get("trigger_hook") or "",
        "hook_override": lead.get("hook_override") or "",
        "website": lead.get("website_url") or "",
    }


@app.route("/campaign/<int:campaign_id>/ship", methods=["POST"])
def campaign_ship(campaign_id: int):
    """Freezes the approved queue into capacity-scheduled Instantly batches.

    NOT a send. For each send date the approved leads are split so the batch
    fits what the fleet can absorb that day (capacity.schedule_by_capacity),
    the plan is stored on the campaign, every shipped lead is recorded in the
    DNC registry + export_history (identical guarantee to /download/instantly.csv),
    and per-batch CSVs are regenerated on demand.
    """
    from runconfig import load_config
    from capacity import SequenceSchedule

    cfg = load_config()
    with open_db() as conn:
        camp = campaignsmod.get(conn, campaign_id)
        if camp is None:
            flash("Campaign not found.", "error")
            return redirect(url_for("sourcing_view"))
        if camp["status"] == "shipped":
            flash("Campaign already shipped (a campaign ships once).", "warning")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))
        sid = camp.get("session_id")
        if sid is None:
            flash("No sourcing session linked yet — wait for the run to finish.", "error")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))
        leads = dbmod.get_leads_with_scores(conn, session_id=sid)
        approved = [l for l in leads
                    if not l.get("is_duplicate") and l.get("review_status") == "APPROVED"]
        if not approved:
            flash("Nothing to ship — approve leads in the review queue first.", "warning")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))
        batch_ids = [l["id"] for l in approved]

        mailboxes = _fleet_mailboxes(cfg)
        seq = SequenceSchedule(offsets=cfg.sending.sequence_offsets)
        try:
            batches = capacitymod.schedule_by_capacity(
                mailboxes, seq, _sent_contacts(conn), date.today(), batch_ids)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))

        plan = {"batches": [[d.isoformat(), ids] for d, ids in batches]}
        # DNC + export history: exactly what the instant-instant export records.
        dnc_rows = [{"email": l["email"],
                     "domain_normalized": dbmod._normalize_domain(l.get("website_url", ""))}
                    for l in approved]
        dncmod.add_many_from_leads(conn, dnc_rows, reason="instantly_export")
        dbmod.record_export(conn, batch_ids, session_id=sid)
        campaignsmod.mark_shipped(conn, campaign_id, ship_plan=json.dumps(plan))
        n_batches = len(batches)
        n_leads = len(batch_ids)
    flash(f"Shipped {n_leads} approved lead(s) into {n_batches} capacity-scheduled "
          f"batch(es) — CSVs are listed below.", "success")
    return redirect(url_for("campaign_view", campaign_id=campaign_id))


@app.route("/campaign/<int:campaign_id>/batch/<int:batch_idx>/download", methods=["GET"])
def campaign_batch_download(campaign_id: int, batch_idx: int):
    """Re-generates one shipped batch's Instantly CSV from the stored plan."""
    with open_db() as conn:
        camp = campaignsmod.get(conn, campaign_id)
        if camp is None or not camp.get("ship_plan"):
            flash("Campaign not found or not shipped yet.", "error")
            return redirect(url_for("sourcing_view"))
        try:
            batches = json.loads(camp["ship_plan"]).get("batches", [])
            day_s, ids = batches[batch_idx]
        except (TypeError, ValueError, IndexError):
            flash("Batch not found.", "error")
            return redirect(url_for("campaign_view", campaign_id=campaign_id))
        leads = dbmod.get_leads_with_scores(conn, session_id=camp["session_id"])
        by_id = {l["id"]: l for l in leads}
        rows = [by_id[i] for i in ids if i in by_id]
    csv_text = exportmod.instantly_csv_for_leads(rows)
    return _csv_response(f"campaign_{campaign_id}_batch_{day_s}.csv", csv_text)


# ---------------------------------------------------------------------------
# Phase 4 — Analytics: Signal→Outcome attribution (Task 16), Cost per outcome
# (Task 17) and Channel comparison (Task 18). lead_outcomes is populated
# nightly from apollo_analytics.sync_analytics_report (Task 13); the /analytics
# sync button runs it on demand. Channels are set per session.
# ---------------------------------------------------------------------------

@app.route("/analytics", methods=["GET"])
def analytics():
    with open_db() as conn:
        signals, baseline = analyticsmod.attribution(conn)
        last_sync = analyticsmod.last_sync_summary(conn)
    return render_template("analytics.html", signals=signals, baseline=baseline,
                           last_sync=last_sync, min_sent=analyticsmod.MIN_SENT_FOR_LIFT)


@app.route("/analytics/sync", methods=["POST"])
def analytics_sync():
    """Runs the Task 13 Apollo analytics pull now (nightly = tools/run_outcomes_sync.py).
    days=1 to match the nightly job's default lookback (the cron wrapper runs the
    same window), so a manual button click and the cron produce the same sweep."""
    try:
        with open_db() as conn:
            result = apollo_analyticsmod.sync_analytics_report(conn, days=1)
    except apollo_analyticsmod.ApolloAnalyticsError as e:
        flash(f"Analytics sync failed: {e}", "error")
        return redirect(url_for("analytics"))
    flash(f"Analytics sync ({result['month']}): {result['matched']}/{result['messages']} "
          f"messages matched to leads, {result['replied']} replies, "
          f"{result['unmatched']} unmatched.", "info")
    return redirect(url_for("analytics"))


@app.route("/analytics/costs", methods=["GET"])
def analytics_costs():
    from runconfig import load_config
    cfg = load_config()
    with open_db() as conn:
        data = analyticsmod.cost_per_outcome(conn, cfg=cfg.costs)
    return render_template("analytics_costs.html", data=data)


@app.route("/analytics/channels", methods=["GET"])
def analytics_channels():
    owner_id = None if session.get("role") == "admin" else session.get("user_id")
    with open_db() as conn:
        channels = analyticsmod.channel_comparison(conn)
        sessions = analyticsmod.session_channels(conn, owner_id=owner_id)
    return render_template("analytics_channels.html", channels=channels,
                           sessions=sessions, channel_names=analyticsmod.CHANNELS)


@app.route("/analytics/sessions/<int:session_id>/channel", methods=["POST"])
def analytics_set_channel(session_id: int):
    channel = (request.form.get("channel") or "").strip().lower()
    if channel not in analyticsmod.CHANNELS:
        flash(f"Unknown channel: {channel or '(empty)'}", "error")
        return redirect(url_for("analytics_channels"))
    with open_db() as conn:
        row = dbmod.get_analysis_session(conn, session_id)
        if row is None:
            flash("Session not found.", "error")
            return redirect(url_for("analytics_channels"))
        if not _assert_session_access(row):
            flash("Access not authorized to this analysis.", "danger")
            return redirect(url_for("analytics_channels"))
        dbmod.set_session_channel(conn, session_id, channel)
    flash(f"Session #{session_id} channel set to {channel}.", "info")
    return redirect(url_for("analytics_channels"))


@app.route("/capacity", methods=["GET"])
def capacity_view():
    """Send-capacity planner (3.4): forecasts sends/day, shows how many NEW
    contacts can be added on a date, flags follow-up collisions, and shows the
    per-mailbox warmup ramp. Separate concern from the recipe UI.

    The model is pure (capacity.py); this route just feeds it the fleet config
    (config.toml [sending]) and a snapshot of already-added contacts built from
    the leads table. With no per-touch tracking yet, a lead whose email_sent_at
    is set is treated as touch 1 of the sequence completed (T2/T3 still pending).
    """
    from runconfig import load_config
    from capacity import Mailbox, SequenceSchedule

    cfg = load_config()
    mailboxes = _fleet_mailboxes(cfg)
    seq = SequenceSchedule(offsets=cfg.sending.sequence_offsets)

    horizon = 30
    start = date.today()

    # Snapshot of already-added contacts: any lead with a sent email booked at
    # least touch 1; the remaining touches are still scheduled.
    with open_db() as conn:
        contacts = _sent_contacts(conn)

    fx = capacitymod.forecast(mailboxes, seq, contacts, start, horizon_days=horizon)
    collisions = capacitymod.find_collisions(mailboxes, seq, contacts, start, horizon_days=horizon)
    max_new_today = capacitymod.max_new_contacts(mailboxes, seq, contacts, start, horizon_days=horizon)
    ramps = {m.name: capacitymod.ramp_schedule(m, start, horizon_days=horizon)
             for m in mailboxes}

    # Single-date view for the "you can add N on date D" box (default today).
    target_s = request.args.get("target")
    target = date.fromisoformat(target_s) if target_s else start
    max_new_target = capacitymod.max_new_contacts(
        mailboxes, seq, contacts, target, horizon_days=horizon)

    forecast_rows = []
    for d, info in sorted(fx.items()):
        forecast_rows.append({
            "date": d.isoformat(),
            "capacity": info["capacity"],
            "scheduled": info["scheduled"],
            "forecast": info["forecast"],
            "collision": info["collision"],
        })
    collision_rows = [{"date": d.isoformat(), "scheduled": sched, "capacity": cap}
                      for d, sched, cap in collisions]
    ramp_rows = {
        name: [{"date": d.isoformat(), "cap": cap} for d, cap in sched]
        for name, sched in ramps.items()
    }

    return render_template(
        "capacity.html",
        today=start.isoformat(),
        target=target.isoformat(),
        forecast_rows=forecast_rows,
        collision_rows=collision_rows,
        max_new_today=max_new_today,
        max_new_target=max_new_target,
        ramp_rows=ramp_rows,
        touch_count=seq.touch_count,
        total_capacity_today=capacitymod.total_capacity(mailboxes, start),
    )


@app.route("/lead/<int:lead_id>/approve", methods=["POST"])
def approve_lead(lead_id: int):
    """Manual review outcome: clear the review flag so the lead moves to 'Ready to approve'."""
    with open_db() as conn:
        row = conn.execute(
            "SELECT session_id, company_name FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if row is None:
            flash("Lead not found.", "error")
            return redirect(url_for("home"))
        _, denied = _require_session(row["session_id"])
        if denied is not None:
            return denied
        dbmod.mark_lead_approved(conn, lead_id)
        conn.commit()
        flash(f"{row['company_name'] or 'Lead'} marked as ready to approve.", "success")
    return redirect(url_for("lead_review_view", lead_id=lead_id))


@app.route("/lead/<int:lead_id>/rescore", methods=["POST"])
def rescore_lead(lead_id: int):
    """Manual review outcome: re-run the LLM scoring on this lead (no re-scraping)."""
    with open_db() as conn:
        row = conn.execute(
            "SELECT session_id FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if row is None:
            flash("Lead not found.", "error")
            return redirect(url_for("home"))
        _, denied = _require_session(row["session_id"])
        if denied is not None:
            return denied
        conn.execute("UPDATE leads SET status = 'RESCORE_PENDING' WHERE id = ?", (lead_id,))
        conn.execute("DELETE FROM lead_scores WHERE lead_id = ?", (lead_id,))
        conn.commit()
        session_id = row["session_id"]
        dbmod.resume_analysis_session(conn, session_id)
    new_conn = dbmod.get_connection()
    threading.Thread(
        target=_background_rescore_pipeline,
        args=(new_conn, session_id, 1.0),
        kwargs={"lead_status": "RESCORE_PENDING"},
        daemon=True,
    ).start()
    flash("Lead marked for re-scoring.", "info")
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/start-analysis", methods=["POST"])
def start_analysis():
    uploaded_file = request.files.get("csv_file")
    fuzzy_threshold = request.form.get("fuzzy_threshold", type=int, default=90)
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    concurrency = request.form.get("concurrency", type=int, default=pipelinemod.DEFAULT_CONCURRENCY)

    if uploaded_file is None or uploaded_file.filename == "":
        flash("Add a CSV file before launching the full analysis.", "error")
        return redirect(url_for("dashboard"))

    if missing_api_keys():
        flash(
            "Missing environment keys: FIRECRAWL_API_KEY and/or GROQ_API_KEY. "
            "The CSV can be imported, but the scraping/scoring pipeline may fail.",
            "warning",
        )

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Analysis {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
            owner_id=session.get("user_id"),
        )
        batch_id, ingest_summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)
        dnc_flagged = dncmod.flag_batch_on_import(conn, session_id)
        if dnc_flagged:
            flash(f"{dnc_flagged} lead(s) on the do-not-contact registry were excluded.", "info")

        threading.Thread(
            target=_background_pipeline,
            args=(dbmod.get_connection(), session_id, throttle_seconds),
            kwargs={"concurrency": concurrency},
            daemon=True,
        ).start()

    flash(
        f"Batch {batch_id} imported: {ingest_summary['inserted']} rows added, "
        f"{ingest_summary['skipped_no_website']} ignored. Dedup: email {dedup_summary['exact_email']}, "
        f"domain {dedup_summary['domain']}, fuzzy {dedup_summary['fuzzy_company']}.",
        "success",
    )
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/ingest", methods=["POST"])
def ingest_only():
    uploaded_file = request.files.get("csv_file")
    if uploaded_file is None or uploaded_file.filename == "":
        flash("Add a CSV file before ingesting.", "error")
        return redirect(url_for("dashboard"))

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Import {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
            owner_id=session.get("user_id"),
        )
        batch_id, summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dbmod.update_analysis_session_status(conn, session_id, "completed", completed_at=_db_now())

    flash(
        f"Batch {batch_id} ingested: {summary['inserted']} rows added, "
        f"{summary['skipped_no_website']} without a website.",
        "success",
    )
    return redirect(url_for("dashboard", session_id=session_id))


@app.route("/dedup", methods=["POST"])
def dedup_only():
    threshold = request.form.get("fuzzy_threshold", type=int, default=90)
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied
    with open_db() as conn:
        summary = dedupmod.run_dedup(conn, fuzzy_threshold=threshold, session_id=selected_session_id)

    flash(
        f"Dedup done: email {summary['exact_email']}, domain {summary['domain']}, "
        f"fuzzy {summary['fuzzy_company']}, kept {summary['kept_original']}.",
        "success",
    )
    return redirect(url_for("dashboard", session_id=selected_session_id))


@app.route("/pipeline", methods=["POST"])
def pipeline_only():
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    concurrency = request.form.get("concurrency", type=int, default=pipelinemod.DEFAULT_CONCURRENCY)
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied

    with open_db() as conn:
        to_process = dbmod.get_leads_to_process(conn, session_id=selected_session_id)
        if not to_process:
            flash("No leads ready to be processed.", "warning")
            return redirect(url_for("dashboard", session_id=selected_session_id))
        dbmod.resume_analysis_session(conn, selected_session_id)

    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(), selected_session_id, throttle_seconds),
        kwargs={"concurrency": concurrency},
        daemon=True,
    ).start()

    flash("Pipeline launched in the background. You can follow the progress below.", "info")
    return redirect(url_for("progress_view", session_id=selected_session_id))


@app.route("/lead/<int:lead_id>/review", methods=["POST"])
def review_lead(lead_id: int):
    """Records the human review decision (APPROVED/REJECTED) and the segment override."""
    decision = request.form.get("decision")
    segment_override = (request.form.get("segment") or "").strip() or None
    session_id = request.args.get("session_id", type=int)

    if session_id is None:
        with open_db() as conn:
            lead_row = conn.execute("SELECT session_id FROM leads WHERE id = ?", (lead_id,)).fetchone()
            session_id = lead_row[0] if lead_row else None
    if session_id is None:
        flash("Session not found.", "error")
        return redirect(url_for("home"))
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    if decision not in dbmod.VALID_REVIEW_STATUSES:
        flash(f"Invalid review decision: {decision!r}.", "error")
        return redirect(url_for("dashboard", session_id=session_id, lead_id=lead_id))

    with open_db() as conn:
        if decision == "APPROVED":
            # Single consolidated approve path: status + needs_human_review +
            # review_status + segment override all at once (see db.mark_lead_approved).
            # mark_lead_approved does not commit (bulk_approve / approve_lead call
            # mark_lead_approved as part of a batch then commit once); commit here.
            dbmod.mark_lead_approved(conn, lead_id, segment_override=segment_override)
            conn.commit()
        else:
            dbmod.set_lead_review(conn, lead_id, decision, segment_override=segment_override)

    flash(f"Lead #{lead_id} marked {decision}.", "success")
    return redirect(url_for("dashboard", session_id=session_id, lead_id=lead_id))


@app.route("/download/scraping.csv", methods=["GET"])
def download_scraping_csv():
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied
    with open_db() as conn:
        csv_text = exportmod.scraping_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scraping_results_{timestamp}.csv", csv_text)


@app.route("/download/scores.csv", methods=["GET"])
def download_scores_csv():
    """Exports scores with inter-batch dedup and records the export."""
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied
    with open_db() as conn:
        newly_flagged = dedupmod.run_export_dedup(conn, session_id=selected_session_id)
        if newly_flagged:
            flash(f"{newly_flagged} lead(s) already exported in a previous batch, excluded from this CSV.", "info")
        csv_text = exportmod.scores_csv_string(conn, session_id=selected_session_id)
        exported_leads = dbmod.get_leads(conn, session_id=selected_session_id, include_duplicates=False)
        dbmod.record_export(conn, [lead["id"] for lead in exported_leads], session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scores_results_{timestamp}.csv", csv_text)


@app.route("/download/search.csv", methods=["GET"])
def download_search_csv():
    """Exports SGAI web search results — separate from scraping."""
    selected_session_id, denied = _resolve_accessible_session(request.args.get("session_id", type=int))
    if denied is not None:
        return denied
    with open_db() as conn:
        csv_text = exportmod.search_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"search_results_{timestamp}.csv", csv_text)


@app.route("/export/<int:session_id>/<format>", methods=["GET"])
def export_results(session_id: int, format: str):
    """Exports the results as CSV or PDF (print-friendly HTML)."""
    from io import StringIO
    import csv as csv_module

    session, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)

        # Same category logic as results_view
        from runconfig import load_config
        categories = _categorize_leads(
            scores_data, reorder_queue=load_config().triggers.reorder_queue
        )
        approved = categories["approved"]
        not_selected = categories["not_selected"]
        out_of_target = categories["out_of_target"]
        to_review = categories["to_review"]
        pending = categories["pending"]

    if format == "csv":
        # Generate full CSV
        output = StringIO()
        w = csv_module.writer(output)
        w.writerow(["category", "id", "company_name", "website_url", "segment", "confidence",
                     "company_stage", "recommended_offer", "status", "disqualify_reason"])
        for cat_name, cat_leads in [("approved", approved), ("to_review", to_review),
                                      ("out_of_target", out_of_target), ("not_selected", not_selected)]:
            for lead in cat_leads:
                w.writerow([cat_name, lead.get("id"), lead.get("company_name"), lead.get("website_url"),
                            lead.get("segment"), lead.get("confidence"), lead.get("company_stage"),
                            lead.get("recommended_offer"), lead.get("status"), lead.get("disqualify_reason")])
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
        return _csv_response(f"complete_results_{ts}.csv", output.getvalue())

    # PDF — printable version (the user uses the browser's "Save as PDF")
    return render_template(
        "results_print.html",
        session=session,
        categories={
            "approved": approved,
            "not_selected": not_selected,
            "out_of_target": out_of_target,
            "to_review": to_review,
        },
    )


@app.route("/batch-results/<int:session_id>", methods=["GET"])
def batch_results_view(session_id: int):
    """Results of the last batch of analyzed leads (SKIPPED re-launched or Phase 2)."""
    session, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        batch_ids = dbmod.get_last_batch_ids(conn, session_id)
        if not batch_ids:
            flash("No recent batch found.", "warning")
            return redirect(url_for("results_view", session_id=session_id))
        leads = []
        if batch_ids:
            rows = conn.execute("""
                SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
                       s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
                       s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
                       s.pain_signals, s.scored_at
                FROM leads l
                LEFT JOIN lead_scores s ON s.lead_id = l.id
                    AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
                WHERE l.id = ANY(%s)
            """, (batch_ids,)).fetchall()
            by_id = {row["id"]: dict(row) for row in rows}
            leads = [by_id[lid] for lid in batch_ids if lid in by_id]
    return render_template("batch_results.html", session=session, leads=leads, session_id=session_id)


@app.route("/web-search/<int:session_id>", methods=["GET"])
def web_search_view(session_id: int):
    """Page dedicated to the web search results (ScrapeGraphAI)."""
    session, denied = _require_session(session_id)
    if denied is not None:
        return denied

    with open_db() as conn:
        leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
        evidence_by_lead = dbmod.get_search_evidence_for_session(conn, session_id)
        leads_with_evidence = []
        for lead in leads:
            evidence = evidence_by_lead.get(lead["id"], [])
            if evidence:
                leads_with_evidence.append({"lead": lead, "evidence": evidence})
    return render_template("web_search.html", session=session, leads=leads_with_evidence)


@app.route("/sessions/<int:session_id>", methods=["GET"])
def session_redirect(session_id: int):
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied
    return redirect(url_for("results_view", session_id=session_id))


@app.route("/progress/<int:session_id>")
def progress_view(session_id: int):
    """Real-time pipeline progress page."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied
    return render_template("progress.html", session_id=session_id, api_key_missing=missing_api_keys())


@app.route("/progress/<int:session_id>/stream")
def progress_stream(session_id: int):
    """SSE endpoint that streams pipeline progress updates."""
    _, denied = _require_session(session_id)
    if denied is not None:
        return denied

    def generate():
        while True:
            prog = _get_progress(session_id)
            if prog is None:
                yield f"data: {json.dumps({'status': 'waiting'})}\n\n"
                time.sleep(1)
                continue

            status = prog.get("pipeline_status")

            if status == "running":
                yield f"data: {json.dumps(prog, default=str)}\n\n"
                time.sleep(0.5)
                continue

            if status in ("completed", "failed", "cancelled"):
                yield f"data: {json.dumps(prog, default=str)}\n\n"
                break

            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Authentication pages
# ---------------------------------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("home"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or "@" not in email or "." not in email:
            flash("Enter a valid email address.", "error")
            return render_template("signup.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("signup.html")
        with open_db() as conn:
            if dbmod.get_user_by_email(conn, email):
                flash("An account with this email already exists.", "error")
                return render_template("signup.html")
            user_id = dbmod.create_user(conn, email, generate_password_hash(password))
            user = dbmod.get_user_by_id(conn, user_id)
        session["user_id"] = user_id
        session["role"] = user["role"]
        flash("Welcome! Your account has been created.", "success")
        return redirect(url_for("home"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("home"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        with open_db() as conn:
            user = dbmod.get_user_by_email(conn, email)
            if user and check_password_hash(user["password_hash"], password):
                if not user.get("is_active"):
                    flash("This account is blocked. Contact an administrator.", "danger")
                    return render_template("login.html")
                dbmod.update_last_login(conn, user["id"])
            else:
                user = None
        if user is None:
            flash("Invalid email or password.", "error")
            return render_template("login.html")
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        next_url = request.args.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return redirect(next_url)
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Admin — user management
# ---------------------------------------------------------------------------

@app.route("/admin/users", methods=["GET"])
@admin_required
def admin_users():
    with open_db() as conn:
        users = dbmod.list_users(conn)
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_user_role(user_id: int):
    role = request.form.get("role")
    if role not in ("admin", "user"):
        flash("Invalid role.", "error")
        return redirect(url_for("admin_users"))
    with open_db() as conn:
        target = dbmod.get_user_by_id(conn, user_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and role == "user" and dbmod.count_active_admins(conn) <= 1:
            flash("Cannot demote the last active admin.", "danger")
            return redirect(url_for("admin_users"))
        dbmod.set_user_role(conn, user_id, role)
    flash(f"Role updated for {target['email']}.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_user_toggle_active(user_id: int):
    with open_db() as conn:
        target = dbmod.get_user_by_id(conn, user_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("admin_users"))
        if target.get("is_active") and target["role"] == "admin" and dbmod.count_active_admins(conn) <= 1:
            flash("Cannot block the last active admin.", "danger")
            return redirect(url_for("admin_users"))
        dbmod.set_user_active(conn, user_id, not target.get("is_active"))
    flash(
        f"{target['email']} has been {'blocked' if target.get('is_active') else 'unblocked'}.",
        "success",
    )
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id: int):
    with open_db() as conn:
        target = dbmod.get_user_by_id(conn, user_id)
        if target is None:
            flash("Account not found.", "error")
            return redirect(url_for("admin_users"))
        if target["role"] == "admin" and dbmod.count_active_admins(conn) <= 1:
            flash("Cannot delete the last active admin.", "danger")
            return redirect(url_for("admin_users"))
        if target["id"] == session.get("user_id"):
            flash("You cannot delete your own account.", "danger")
            return redirect(url_for("admin_users"))
        dbmod.delete_user(conn, user_id)
    flash(f"Account {target['email']} deleted.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/history", methods=["GET"])
@admin_required
def admin_user_history(user_id: int):
    with open_db() as conn:
        target_user = dbmod.get_user_by_id(conn, user_id)
        if not target_user:
            flash("Account not found.", "error")
            return redirect(url_for("admin_users"))
        sessions = dbmod.list_analysis_sessions(conn, owner_id=user_id)
    return render_template("admin_user_history.html", target_user=target_user, sessions=sessions, show_rank=True)


if __name__ == "__main__":
    _init_schema_once()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
