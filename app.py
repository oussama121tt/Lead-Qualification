"""Interface Flask complète pour le pipeline Lead Qualification & Scoring.

Flux couvert par cette UI :
upload CSV Apollo -> ingestion SQLite -> déduplication RapidFuzz ->
scraping Firecrawl + scoring Groq -> tableaux de résultats -> exports CSV.

Lancer avec : python app.py
"""

import json
import os
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
from flask import Flask, Response, flash, redirect, render_template, request, url_for, stream_with_context

import db as dbmod
from db import _now as _db_now
import dedup as dedupmod
import export as exportmod
import pipeline as pipelinemod


if os.getenv("VERCEL"):
    DB_PATH = "/tmp/leads.db"
else:
    DB_PATH = os.getenv("DB_PATH", dbmod.DB_PATH_DEFAULT)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "lead-qualification-engine")

# --- Stockage thread-safe de la progression du pipeline ---
_pipeline_progress: dict[int, dict] = {}
_pipeline_lock = threading.Lock()


def _store_progress(session_id: int, progress: dict):
    """Met à jour la progression pour une session (thread-safe)."""
    with _pipeline_lock:
        _pipeline_progress[session_id] = progress


def _get_progress(session_id: int) -> dict | None:
    """Lit la dernière progression connue pour une session (thread-safe)."""
    with _pipeline_lock:
        return _pipeline_progress.get(session_id)


def _clear_progress(session_id: int):
    """Nettoie la progression après rechargement (thread-safe)."""
    with _pipeline_lock:
        _pipeline_progress.pop(session_id, None)


def _background_pipeline(conn, session_id: int, throttle_seconds: float):
    """
    Executé dans un thread d'arrière-plan. Consomme le générateur
    run_pipeline() et stocke chaque mise à jour dans _pipeline_progress.
    À la fin, stocke un état 'completed' ou 'failed'.
    """
    processed = 0
    final_progress = None
    try:
        for update in pipelinemod.run_pipeline(conn, throttle_seconds=throttle_seconds, session_id=session_id):
            processed += 1
            _store_progress(session_id, {"status": "running", **update})
        # Succès
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = "completed"
            p["processed"] = processed
            p["completed_ts"] = time.time()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(conn, session_id, "completed", completed_at=_db_now())
    except Exception as e:
        with _pipeline_lock:
            p = _pipeline_progress.get(session_id, {})
            p["status"] = "failed"
            p["error"] = str(e)
            p["processed"] = processed
            p["completed_ts"] = time.time()
            _pipeline_progress[session_id] = p
        dbmod.update_analysis_session_status(
            conn, session_id, "failed",
            completed_at=_db_now(),
        )
    finally:
        conn.close()


@contextmanager
def open_db():
    conn = dbmod.get_connection(DB_PATH)
    try:
        dbmod.init_db(conn)
        yield conn
    finally:
        conn.close()


def missing_api_keys():
    return [k for k in ("FIRECRAWL_API_KEY", "GROQ_API_KEY") if not os.getenv(k)]


def _table_html(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return '<div class="empty-state">Aucune donnée à afficher pour le moment.</div>'
    display_df = df.loc[:, columns].copy()
    return display_df.to_html(index=False, classes="table table-hover table-striped align-middle", escape=False)


def _summary_context(conn, session_id=None):
    leads = dbmod.get_leads(conn, session_id=session_id)
    scored = dbmod.get_leads_with_scores(conn, session_id=session_id)
    statuses = Counter((lead.get("status") or "UNKNOWN") for lead in leads)

    return {
        "total_leads": len(leads),
        "ready_to_process": len(dbmod.get_leads_to_process(conn, session_id=session_id)),
        "duplicates": sum(1 for lead in leads if lead.get("is_duplicate")),
        "scored": sum(1 for row in scored if row.get("segment")),
        "needs_review": sum(1 for row in scored if row.get("needs_human_review") == 1),
        "fetch_failed": statuses.get("FETCH_FAILED", 0),
        "score_failed": statuses.get("SCORE_FAILED", 0),
        "low_confidence": statuses.get("LOW_CONFIDENCE", 0),
        "status_counts": dict(statuses),
    }


def _session_summary(conn, session_id: int | None):
    session = None
    if session_id is not None:
        session = dbmod.get_analysis_session(conn, session_id)
    if session is None:
        session_id = dbmod.get_latest_session_id(conn)
        session = dbmod.get_analysis_session(conn, session_id) if session_id is not None else None
    return session_id, session


def _load_dashboard_data(conn, session_id=None, selected_lead_id=None, segment_filter=None, needs_review=False, hide_duplicates=True):
    leads = pd.DataFrame(dbmod.get_leads(conn, session_id=session_id))
    if not leads.empty:
        leads = leads.fillna("")
        leads.insert(
            0,
            "detail",
            leads["id"].apply(lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">Voir</a>'),
        )

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
        scores.insert(
            0,
            "detail",
            scores["id"].apply(lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">Voir</a>'),
        )

    lead_detail = None
    if not scores.empty:
        available_ids = set(scores["id"].tolist())
        if selected_lead_id not in available_ids:
            selected_lead_id = int(scores.iloc[0]["id"])
        lead_detail = scores[scores["id"] == selected_lead_id].iloc[0].to_dict()

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
        raise ValueError("Aucun fichier CSV fourni.")

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

    with open_db() as conn:
        selected_session_id, selected_session = _session_summary(conn, selected_session_id)
        sessions = dbmod.list_analysis_sessions(conn, limit=50)
        summary = _summary_context(conn, session_id=selected_session_id)
        if selected_session_id is not None:
            summary = {
                **summary,
                "selected_session_label": selected_session.get("label") if selected_session else None,
                "selected_session_status": selected_session.get("status") if selected_session else None,
            }
        data = _load_dashboard_data(
            conn,
            session_id=selected_session_id,
            selected_lead_id=selected_lead_id,
            segment_filter=segment_filter,
            needs_review=needs_review,
            hide_duplicates=hide_duplicates,
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
        session_name=selected_session.get("label") if selected_session else "Historique",
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


@app.route("/start-analysis", methods=["POST"])
def start_analysis():
    uploaded_file = request.files.get("csv_file")
    fuzzy_threshold = request.form.get("fuzzy_threshold", type=int, default=90)
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=2.5)

    if uploaded_file is None or uploaded_file.filename == "":
        flash("Ajoute un fichier CSV avant de lancer l'analyse complète.", "error")
        return redirect(url_for("dashboard"))

    if missing_api_keys():
        flash(
            "Clés d'environnement manquantes : FIRECRAWL_API_KEY et/ou GROQ_API_KEY. "
            "Le CSV peut être importé, mais le pipeline de scraping/scoring risque d'échouer.",
            "warning",
        )

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(conn, label=f"Analysis {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}", source_filename=uploaded_file.filename)
        batch_id, ingest_summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)

        # Lance le pipeline en arrière-plan
        threading.Thread(
            target=_background_pipeline,
            args=(dbmod.get_connection(DB_PATH), session_id, throttle_seconds),
            daemon=True,
        ).start()

    flash(
        f"Batch {batch_id} importé : {ingest_summary['inserted']} lignes ajoutées, "
        f"{ingest_summary['skipped_no_website']} ignorées. Dédup: email {dedup_summary['exact_email']}, "
        f"domaine {dedup_summary['domain']}, fuzzy {dedup_summary['fuzzy_company']}.",
        "success",
    )
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/ingest", methods=["POST"])
def ingest_only():
    uploaded_file = request.files.get("csv_file")
    if uploaded_file is None or uploaded_file.filename == "":
        flash("Ajoute un CSV avant l'ingestion.", "error")
        return redirect(url_for("dashboard"))

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(conn, label=f"Import {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}", source_filename=uploaded_file.filename)
        batch_id, summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dbmod.update_analysis_session_status(conn, session_id, "completed", completed_at=_db_now())

    flash(
        f"Batch {batch_id} ingéré : {summary['inserted']} lignes ajoutées, "
        f"{summary['skipped_no_website']} sans site web.",
        "success",
    )
    return redirect(url_for("dashboard", session_id=session_id))


@app.route("/dedup", methods=["POST"])
def dedup_only():
    threshold = request.form.get("fuzzy_threshold", type=int, default=90)
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        summary = dedupmod.run_dedup(conn, fuzzy_threshold=threshold, session_id=selected_session_id)

    flash(
        f"Dédup terminée : email {summary['exact_email']}, domaine {summary['domain']}, "
        f"fuzzy {summary['fuzzy_company']}, conservés {summary['kept_original']}.",
        "success",
    )
    return redirect(url_for("dashboard", session_id=selected_session_id))


@app.route("/pipeline", methods=["POST"])
def pipeline_only():
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=2.5)
    selected_session_id = request.args.get("session_id", type=int)

    with open_db() as conn:
        if selected_session_id is None:
            selected_session_id = dbmod.get_latest_session_id(conn)
        to_process = dbmod.get_leads_to_process(conn, session_id=selected_session_id)
        if not to_process:
            flash("Aucun lead prêt à être traité.", "warning")
            return redirect(url_for("dashboard", session_id=selected_session_id))

    # Lance le pipeline en arrière-plan
    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(DB_PATH), selected_session_id, throttle_seconds),
        daemon=True,
    ).start()

    flash("Pipeline lancé en arrière-plan. Tu peux suivre la progression ci-dessous.", "info")
    return redirect(url_for("progress_view", session_id=selected_session_id))


@app.route("/download/scraping.csv", methods=["GET"])
def download_scraping_csv():
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        csv_text = exportmod.scraping_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scraping_results_{timestamp}.csv", csv_text)


@app.route("/download/scores.csv", methods=["GET"])
def download_scores_csv():
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        csv_text = exportmod.scores_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scores_results_{timestamp}.csv", csv_text)


@app.route("/sessions/<int:session_id>", methods=["GET"])
def session_redirect(session_id: int):
    return redirect(url_for("dashboard", session_id=session_id))


# ---------------------------------------------------------------------------
# Progression temps réel (SSE)
# ---------------------------------------------------------------------------

@app.route("/progress/<int:session_id>")
def progress_view(session_id: int):
    """Page de progression qui écoute le flux SSE et se redirige vers le dashboard à la fin."""
    return render_template(
        "progress.html",
        session_id=session_id,
        api_key_missing=missing_api_keys(),
    )


@app.route("/progress/<int:session_id>/stream")
def progress_stream(session_id: int):
    """Endpoint SSE : stream les mises à jour de progression au fur et à mesure."""

    def generate():
        last_sent = None
        while True:
            prog = _get_progress(session_id)
            if prog is None:
                # Pipeline pas encore lancé – on attend
                yield f"data: {json.dumps({'status': 'waiting'})}\n\n"
                time.sleep(1)
                continue

            status = prog.get("status")

            # Toujours envoyer la progression running
            if status == "running":
                yield f"data: {json.dumps(prog, default=str)}\n\n"
                if prog.get("step") != last_sent:
                    last_sent = prog.get("step")
                time.sleep(0.5)
                continue

            # Terminé ou échec – on envoie l'état final puis on ferme le flux
            if status in ("completed", "failed"):
                yield f"data: {json.dumps(prog, default=str)}\n\n"
                break

            # Fallback
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True)
