"""Interface Flask pour le pipeline Lead Qualification & Scoring.

Flux : upload CSV Apollo → ingestion SQLite → déduplication RapidFuzz →
scraping Firecrawl + scoring Claude → tableaux de résultats → exports CSV.

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
from flask import Flask, Response, flash, jsonify, redirect, render_template, request, url_for, stream_with_context

import db as dbmod
from db import _now as _db_now
import dedup as dedupmod
import export as exportmod
import pipeline as pipelinemod
from scorer import CONFIDENCE_THRESHOLD, INVALID_VERDICT_CONFIDENCE_CAP



DB_PATH = os.getenv("DB_PATH", dbmod.DB_PATH_DEFAULT)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "lead-qualification-engine")

# ---- Filtres Jinja pour le formatage d'affichage ----

@app.template_filter("map_offer")
def _map_offer(offer: str | None) -> str:
    mapping = {"ai_audit": "Audit IA", "general_audit": "Audit technique", "pipeline": "Accompagnement pipeline"}
    return mapping.get(offer) if offer in mapping else "—"

@app.template_filter("map_segment")
def _map_segment(segment: str | None) -> str:
    mapping = {
        "ai_solo_founder": "Fondateur solo IA",
        "technical_founder": "Fondateur technique",
        "small_agency_scaling": "Petite agence en scaling",
        "too_big": "Trop grand",
        "wrong_field": "Mauvais secteur",
        "unclear": "Incertain",
        "vibe_coder": "Vibe Coder",
        "technical_ai_user": "Utilisateur IA technique",
        "not_target": "Hors cible",
    }
    return mapping.get(segment) if segment and segment in mapping else (segment or "Non évalué")

@app.template_filter("map_status_label")
def _map_status_label(status: str | None) -> str:
    mapping = {"FETCH_FAILED": "Échecs scraping", "SCORE_FAILED": "Échecs scoring",
               "LOW_CONFIDENCE": "Confiance faible", "NEEDS_REVIEW": "À vérifier",
               "SCORED": "Scoré", "NEW": "Nouveau", "imported": "En attente",
               "running": "En cours", "completed": "Terminé", "failed": "Échec"}
    return mapping.get(status) if status and status in mapping else (status or "—")

@app.template_filter("badge_class")
def _badge_class(status: str | None) -> str:
    mapping = {"completed": "bg-success", "failed": "bg-danger", "running": "bg-warning text-dark",
               "imported": "bg-info text-dark", "SCORED": "bg-success", "LOW_CONFIDENCE": "bg-warning text-dark",
               "SCORE_FAILED": "bg-danger", "FETCH_FAILED": "bg-danger", "NEW": "bg-secondary"}
    return mapping.get(status) if status and status in mapping else "bg-secondary"

_MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

@app.template_filter("format_datetime")
def _format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    return f"{dt.day} {_MONTHS_FR[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"

@app.template_filter("confidence_class")
def _confidence_class(confidence: float | None) -> str:
    """Classe de couleur du % de confiance, alignée sur les seuils de scorer.py."""
    if confidence is None:
        return "conf-muted"
    if confidence >= CONFIDENCE_THRESHOLD:
        return "conf-success"
    if confidence < INVALID_VERDICT_CONFIDENCE_CAP:
        return "conf-danger"
    return "conf-warning"

_pipeline_progress: dict[int, dict] = {}
# _pipeline_cancelled: dict[int, threading.Event] = {}
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


# def _register_cancellation(session_id: int) -> threading.Event:
#     event = threading.Event()
#     with _pipeline_lock:
#         _pipeline_cancelled[session_id] = event
#     return event
#
#
# def _set_cancelled(session_id: int):
#     with _pipeline_lock:
#         event = _pipeline_cancelled.get(session_id)
#         if event:
#             event.set()
#
#
# def _unregister_cancellation(session_id: int):
#     with _pipeline_lock:
#         _pipeline_cancelled.pop(session_id, None)
#
#
# def _is_cancelled_event(session_id: int) -> bool:
#     with _pipeline_lock:
#         event = _pipeline_cancelled.get(session_id)
#         return event is not None and event.is_set()
#
#
# def _make_cancellation_check(session_id: int, conn):
#     def check():
#         if _is_cancelled_event(session_id):
#             return True
#         return session_id is not None and dbmod.is_session_cancelled(conn, session_id)
#     return check


def _background_rescore_pipeline(conn, session_id: int, throttle_seconds: float, lead_status: str = "RESCORE_PENDING"):
    """Exécute un rescore (pas de re-scraping ni recherche web) dans un thread d'arrière-plan."""
    # cancel_event = _register_cancellation(session_id)
    processed = 0
    try:
        # cancellation_check = _make_cancellation_check(session_id, conn)
        for update in pipelinemod.run_rescore_pipeline(conn, throttle_seconds=throttle_seconds, session_id=session_id, lead_status=lead_status):
            # if update.get("step") != "cancelled":
            processed += 1
            _store_progress(session_id, {"pipeline_status": "running", **update})
        # is_cancelled = cancel_event.is_set() or dbmod.is_session_cancelled(conn, session_id)
        # final_status = "cancelled" if is_cancelled else "completed"
        final_status = "completed"
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
        # _unregister_cancellation(session_id)
        conn.close()


def _background_pipeline(conn, session_id: int, throttle_seconds: float):
    """Exécute le pipeline dans un thread d'arrière-plan."""
    # cancel_event = _register_cancellation(session_id)
    processed = 0
    # is_cancelled = False
    try:
        # cancellation_check = _make_cancellation_check(session_id, conn)
        for update in pipelinemod.run_pipeline(conn, throttle_seconds=throttle_seconds, session_id=session_id):
            # if update.get("step") != "cancelled":
            processed += 1
            _store_progress(session_id, {"pipeline_status": "running", **update})
        # is_cancelled = cancel_event.is_set() or dbmod.is_session_cancelled(conn, session_id)
        # final_status = "cancelled" if is_cancelled else "completed"
        final_status = "completed"
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
        # _unregister_cancellation(session_id)
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
        leads.insert(0, "detail", leads["id"].apply(
            lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">Voir</a>',
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
            lambda lead_id: f'<a class="btn btn-sm btn-outline-primary" href="/?lead_id={lead_id}">Voir</a>',
        ))

    lead_detail = None
    if not scores.empty:
        available_ids = set(scores["id"].tolist())
        if selected_lead_id not in available_ids:
            selected_lead_id = int(scores.iloc[0]["id"])
        lead_detail = scores[scores["id"] == selected_lead_id].iloc[0].to_dict()
        # Parse JSON fields from DB text columns
        for field in ("evidence_quotes", "personalization_hooks", "built_with_ai_signals", "technical_signals", "pain_signals"):
            val = lead_detail.get(field)
            if isinstance(val, str):
                try:
                    lead_detail[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass

        # Résultats de recherche web SGAI
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
def home():
    with open_db() as conn:
        sessions = dbmod.list_analysis_sessions(conn, limit=50)
        summary = _summary_context(conn)

    return render_template(
        "home.html",
        api_key_missing=missing_api_keys(),
        summary=summary,
        sessions=sessions,
    )


@app.route("/history", methods=["GET"])
def history():
    with open_db() as conn:
        sessions = dbmod.list_analysis_sessions(conn, limit=50)
    return render_template("history.html", sessions=sessions)


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


@app.route("/upload", methods=["POST"])
def upload_and_review():
    """Etape 1: import CSV → ingest + dedup → redirect vers la page de revue."""
    uploaded_file = request.files.get("csv_file")
    fuzzy_threshold = request.form.get("fuzzy_threshold", type=int, default=90)

    if uploaded_file is None or uploaded_file.filename == "":
        flash("Ajoute un fichier CSV avant de continuer.", "error")
        return redirect(url_for("home"))

    if missing_api_keys():
        flash(
            "Cles d'environnement manquantes : FIRECRAWL_API_KEY et/ou GROQ_API_KEY. "
            "L'import est possible, mais le pipeline de scraping/scoring risque d'echouer.",
            "warning",
        )

    with open_db() as conn:
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Analysis {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
        )
        batch_id, ingest_summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)

    flash(
        f"{ingest_summary['inserted']} ligne(s) importee(s), {ingest_summary['skipped_no_website']} ignoree(s). "
        f"Doublons : {dedup_summary['exact_email']} email, {dedup_summary['domain']} domaine, "
        f"{dedup_summary['fuzzy_company']} fuzzy.",
        "success",
    )
    return redirect(url_for("import_review", session_id=session_id))


@app.route("/import/<int:session_id>", methods=["GET"])
def import_review(session_id: int):
    """Etape 2: page de revue des leads importes + choix des criteres de scoring."""
    with open_db() as conn:
        session = dbmod.get_analysis_session(conn, session_id)
        if session is None:
            flash("Session introuvable.", "error")
            return redirect(url_for("home"))

        leads = dbmod.get_leads(conn, session_id=session_id)
        keepers = [l for l in leads if not l.get("is_duplicate")]
        duplicates = [l for l in leads if l.get("is_duplicate")]
        custom_criteria = dbmod.get_scoring_criteria_custom(conn, session_id)

    criteria_options = [
        {"key": "vibe_coder", "label": "CIBLE : Vibe codeur non-tech", "desc": "Fondateur non-technique, construit avec IA (Cursor, Bolt, Lovable, Replit, vibe coding)."},
        {"key": "technical_ai_user", "label": "CIBLE : Tech qui utilise l'IA", "desc": "Equipe technique qui utilise l'IA comme outil de developpement."},
        {"key": "solo_or_small", "label": "Solo / Micro-equipe", "desc": "Fondateur unique ou equipe de 1-5 personnes."},
        {"key": "agency_or_studio", "label": "Agence / Studio", "desc": "Prestataire de services, agence web, studio de creation."},
        {"key": "no_ai", "label": "Etabli sans signal IA", "desc": "Entreprise etablie sans indication de construction via IA."},
        {"key": "not_target", "label": "Pas notre cible", "desc": "Secteur sans rapport, agence, ou organisation sans usage IA dev."},
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
    """Etape 3: sauvegarde les criteres, selectionne les leads, lance le pipeline."""
    criteria = request.form.getlist("criteria")
    custom_criteria = request.form.get("custom_criteria", "").strip()
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    selected_ids = request.form.getlist("lead_ids")
    dup_ids = request.form.getlist("dup_ids")

    with open_db() as conn:
        dbmod.save_scoring_criteria(conn, session_id, criteria)
        dbmod.save_scoring_criteria_custom(conn, session_id, custom_criteria)

        # Inclure les doublons coches dans l'analyse
        if dup_ids:
            for dup_id_str in dup_ids:
                if dup_id_str.isdigit():
                    did = int(dup_id_str)
                    conn.execute("UPDATE leads SET is_duplicate = 0, duplicate_of_id = NULL, duplicate_reason = NULL, status = 'NEW' WHERE id = ?", (did,))
                    selected_ids.append(dup_id_str)
            conn.commit()

        if selected_ids:
            # Marquer les non-selectionnes comme SKIPPED
            selected_set = set(int(x) for x in selected_ids if x.isdigit())
            all_leads = dbmod.get_leads(conn, session_id=session_id)
            for lead in all_leads:
                if not lead.get("is_duplicate") and lead.get("status") == "NEW":
                    if lead["id"] not in selected_set:
                        dbmod.update_lead_status(conn, lead["id"], "SKIPPED")

        to_process = dbmod.get_leads_to_process(conn, session_id=session_id)
        if not to_process:
            flash("Aucun lead selectionne.", "warning")
            return redirect(url_for("import_review", session_id=session_id))
        dbmod.update_analysis_session_status(conn, session_id, "running")

    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(DB_PATH), session_id, throttle_seconds),
        daemon=True,
    ).start()

    flash(f"Pipeline lance avec {len(to_process)} lead(s).", "info")
    return redirect(url_for("progress_view", session_id=session_id))


@app.route("/analyser-attente/<int:session_id>", methods=["POST"])
def analyser_attente(session_id: int):
    """Lance l'analyse des leads en attente (SKIPPED / NEW non traites)."""
    selected_ids = request.form.getlist("lead_ids")
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    with open_db() as conn:
        if selected_ids:
            ids = [int(x) for x in selected_ids if x.isdigit()]
            for lid in ids:
                dbmod.update_lead_status(conn, lid, "NEW")
            dbmod.set_last_batch_ids(conn, session_id, ids)
        else:
            conn.execute("UPDATE leads SET status = 'NEW' WHERE session_id = ? AND status IN ('SKIPPED', 'NEW') AND is_duplicate = 0", (session_id,))
            conn.commit()
            all_now_new = [r["id"] for r in conn.execute("SELECT id FROM leads WHERE session_id = ? AND status = 'NEW' AND is_duplicate = 0", (session_id,)).fetchall()]
            dbmod.set_last_batch_ids(conn, session_id, all_now_new)

        to_process = dbmod.get_leads_to_process(conn, session_id=session_id)
        if not to_process:
            flash("Aucun lead en attente.", "warning")
            return redirect(url_for("results_view", session_id=session_id))
        dbmod.update_analysis_session_status(conn, session_id, "running")

    _clear_progress(session_id)
    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(DB_PATH), session_id, throttle_seconds),
        daemon=True,
    ).start()

    flash(f"Analyse lancee pour {len(to_process)} lead(s) en attente.", "info")
    return redirect(url_for("progress_view", session_id=session_id))


# @app.route("/resume/<int:session_id>", methods=["POST"])
# def resume_analysis(session_id: int):
#     """Réactive une session annulée et relance le pipeline."""
#     throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
#     try:
#         with open_db() as conn:
#             dbmod.resume_analysis_session(conn, session_id)
#             to_process = dbmod.get_leads_to_process(conn, session_id=session_id)
#             if not to_process:
#                 flash("Tous les leads ont deja ete traites.", "warning")
#                 return redirect(url_for("results_view", session_id=session_id))
#         _clear_progress(session_id)
#         threading.Thread(
#             target=_background_pipeline,
#             args=(dbmod.get_connection(DB_PATH), session_id, throttle_seconds),
#             daemon=True,
#         ).start()
#         flash("Analyse relancee.", "info")
#         return redirect(url_for("progress_view", session_id=session_id))
#     except Exception as e:
#         flash(f"Erreur lors de la reprise : {e}", "error")
#         return redirect(url_for("home"))


@app.route("/session/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id: int):
    """Supprime une session et toutes ses données."""
    try:
        with open_db() as conn:
            dbmod.delete_analysis_session(conn, session_id)
        flash(f"Session #{session_id} supprimee.", "success")
    except Exception as e:
        flash(f"Erreur lors de la suppression : {e}", "error")
    return redirect(url_for("history"))


# @app.route("/stop/<int:session_id>", methods=["POST"])
# def stop_analysis(session_id: int):
#     try:
#         with open_db() as conn:
#             dbmod.cancel_analysis_session(conn, session_id)
#         _set_cancelled(session_id)
#         return jsonify({"success": True, "session_id": session_id})
#     except Exception as e:
#         return jsonify({"success": False, "error": str(e)})


TARGET_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling"}
OUT_OF_TARGET_SEGMENTS = {"too_big", "wrong_field"}
NOT_YET_SCORED_STATUSES = {"SCORE_FAILED", "FETCH_FAILED", "NEW", "PARSED", "FETCH_PARTIAL"}


def _categorize_leads(scores_data: list) -> dict:
    """Répartit les leads scorés dans les 5 catégories.

    Logique (simplifiée) :
    - En attente  : pas encore scoré avec succès (SCORE_FAILED, FETCH_FAILED, ...)
    - À ré-évaluer: needs_human_review=True (couvre unclear, confiance < 0.7,
      domain_mismatch, citations non groundées...)
    - Validés     : segment cible ET pas de review humaine requise
    - Hors cible  : segment too_big/wrong_field ET pas de review humaine requise
    - Non sélectionnés : SKIPPED à l'import
    """
    validees, non_validees, tres_loin, proches, en_attente = [], [], [], [], []

    for lead in scores_data:
        if lead.get("is_duplicate"):
            continue
        segment = lead.get("segment")
        status = lead.get("status", "NEW")

        if status == "SKIPPED":
            non_validees.append(lead)
        elif status in NOT_YET_SCORED_STATUSES:
            en_attente.append(lead)
        elif lead.get("needs_human_review"):
            proches.append(lead)
        elif segment in TARGET_SEGMENTS:
            validees.append(lead)
        elif segment in OUT_OF_TARGET_SEGMENTS:
            tres_loin.append(lead)
        else:
            proches.append(lead)

    return {
        "validees": validees,
        "non_validees": non_validees,
        "tres_loin": tres_loin,
        "proches": proches,
        "en_attente": en_attente,
    }


@app.route("/results/<int:session_id>", methods=["GET"])
def results_view(session_id: int):
    """Etape 4: page de resultats avec 4 categories."""
    with open_db() as conn:
        session = dbmod.get_analysis_session(conn, session_id)
        if session is None:
            flash("Session introuvable.", "error")
            return redirect(url_for("home"))

        scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)

        categories = _categorize_leads(scores_data)
        validees = categories["validees"]
        non_validees = categories["non_validees"]
        tres_loin = categories["tres_loin"]
        proches = categories["proches"]
        en_attente = categories["en_attente"]

        summary = {
            "total": len(scores_data),
            "scored": len([l for l in scores_data if l.get("segment")]),
            "validees": len(validees),
            "proches": len(proches),
            "tres_loin": len(tres_loin),
            "non_validees": len(non_validees),
            "en_attente": len(en_attente),
        }

        # Verifier si des resultats de recherche web existent
        row = conn.execute(
            "SELECT COUNT(*) FROM lead_search_evidence WHERE lead_id IN (SELECT id FROM leads WHERE session_id = ?)",
            (session_id,)
        ).fetchone()
        has_search_evidence = row and row[0] > 0

        # Charger les preuves web search pour affichage inline (meme structure que web_search_view)
        search_leads = []
        if has_search_evidence:
            leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
            for lead in leads:
                evidence = dbmod.get_lead_search_evidence(conn, lead["id"])
                if evidence:
                    search_leads.append({"lead": lead, "evidence": evidence})

    return render_template(
        "results.html",
        session=session,
        summary=summary,
        categories={
            "validees": validees,
            "non_validees": non_validees,
            "tres_loin": tres_loin,
            "proches": proches,
            "en_attente": en_attente,
        },
        has_search_evidence=has_search_evidence,
        search_leads=search_leads,
    )


@app.route("/rescore/<int:session_id>", methods=["POST"])
def rescore_leads(session_id: int):
    """Relance le scoring sur les leads selectionnés (sans re-scraping ni recherche web)."""
    selected_ids = request.form.getlist("lead_ids")

    with open_db() as conn:
        if selected_ids:
            to_rescore = [int(x) for x in selected_ids if x.isdigit()]
        else:
            # Fallback: tous les leads proches
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
            flash("Aucun lead selectionne pour re-scoring.", "warning")
            return redirect(url_for("results_view", session_id=session_id))

        # Reset les statuts et supprimer les anciens scores (pas de re-scraping)
        for lead_id in to_rescore:
            dbmod.update_lead_status(conn, lead_id, "RESCORE_PENDING")
            conn.execute("DELETE FROM lead_scores WHERE lead_id = ?", (lead_id,))
        conn.commit()
        dbmod.update_analysis_session_status(conn, session_id, "running")

    new_conn = dbmod.get_connection(DB_PATH)
    threading.Thread(
        target=_background_rescore_pipeline,
        args=(new_conn, session_id, 1.0),
        kwargs={"lead_status": "RESCORE_PENDING"},
        daemon=True,
    ).start()

    flash(f"{len(to_rescore)} lead(s) marques pour re-scoring.", "info")
    return redirect(url_for("progress_view", session_id=session_id))



@app.route("/start-analysis", methods=["POST"])
def start_analysis():
    uploaded_file = request.files.get("csv_file")
    fuzzy_threshold = request.form.get("fuzzy_threshold", type=int, default=90)
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)

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
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Analysis {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
        )
        batch_id, ingest_summary = _run_ingest(conn, uploaded_file, session_id=session_id)
        dedup_summary = dedupmod.run_dedup(conn, fuzzy_threshold=fuzzy_threshold, session_id=session_id)

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
        session_id = dbmod.create_analysis_session(
            conn,
            label=f"Import {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            source_filename=uploaded_file.filename,
        )
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
    throttle_seconds = request.form.get("throttle_seconds", type=float, default=12)
    selected_session_id = request.args.get("session_id", type=int)

    with open_db() as conn:
        if selected_session_id is None:
            selected_session_id = dbmod.get_latest_session_id(conn)
        to_process = dbmod.get_leads_to_process(conn, session_id=selected_session_id)
        if not to_process:
            flash("Aucun lead prêt à être traité.", "warning")
            return redirect(url_for("dashboard", session_id=selected_session_id))

    threading.Thread(
        target=_background_pipeline,
        args=(dbmod.get_connection(DB_PATH), selected_session_id, throttle_seconds),
        daemon=True,
    ).start()

    flash("Pipeline lancé en arrière-plan. Tu peux suivre la progression ci-dessous.", "info")
    return redirect(url_for("progress_view", session_id=selected_session_id))


@app.route("/lead/<int:lead_id>/review", methods=["POST"])
def review_lead(lead_id: int):
    """Enregistre la décision de review humaine (APPROVED/REJECTED) et l'override de segment."""
    decision = request.form.get("decision")
    segment_override = (request.form.get("segment") or "").strip() or None
    session_id = request.args.get("session_id", type=int)

    if decision not in dbmod.VALID_REVIEW_STATUSES:
        flash(f"Décision de review invalide : {decision!r}.", "error")
        return redirect(url_for("dashboard", session_id=session_id, lead_id=lead_id))

    with open_db() as conn:
        dbmod.set_lead_review(conn, lead_id, decision, segment_override=segment_override)

    flash(f"Lead #{lead_id} marqué {decision}.", "success")
    return redirect(url_for("dashboard", session_id=session_id, lead_id=lead_id))


@app.route("/download/scraping.csv", methods=["GET"])
def download_scraping_csv():
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        csv_text = exportmod.scraping_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scraping_results_{timestamp}.csv", csv_text)


@app.route("/download/scores.csv", methods=["GET"])
def download_scores_csv():
    """Export des scores avec dédup inter-batch et enregistrement de l'export."""
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        newly_flagged = dedupmod.run_export_dedup(conn, session_id=selected_session_id)
        if newly_flagged:
            flash(f"{newly_flagged} lead(s) déjà exporté(s) sur un batch précédent, exclu(s) de ce CSV.", "info")
        csv_text = exportmod.scores_csv_string(conn, session_id=selected_session_id)
        exported_leads = dbmod.get_leads(conn, session_id=selected_session_id, include_duplicates=False)
        dbmod.record_export(conn, [lead["id"] for lead in exported_leads], session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"scores_results_{timestamp}.csv", csv_text)


@app.route("/download/search.csv", methods=["GET"])
def download_search_csv():
    """Export des résultats de recherche web SGAI — séparé du scraping."""
    with open_db() as conn:
        selected_session_id = request.args.get("session_id", type=int) or dbmod.get_latest_session_id(conn)
        csv_text = exportmod.search_csv_string(conn, session_id=selected_session_id)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    return _csv_response(f"search_results_{timestamp}.csv", csv_text)


@app.route("/export/<int:session_id>/<format>", methods=["GET"])
def export_results(session_id: int, format: str):
    """Export des resultats en CSV ou PDF (print-friendly HTML)."""
    from io import StringIO
    import csv as csv_module

    with open_db() as conn:
        session = dbmod.get_analysis_session(conn, session_id)
        if session is None:
            flash("Session introuvable.", "error")
            return redirect(url_for("home"))
        scores_data = dbmod.get_leads_with_scores(conn, session_id=session_id)

        # Meme logique de categorie que results_view
        categories = _categorize_leads(scores_data)
        validees = categories["validees"]
        non_validees = categories["non_validees"]
        tres_loin = categories["tres_loin"]
        proches = categories["proches"]
        en_attente = categories["en_attente"]

    if format == "csv":
        # Generer CSV complet
        output = StringIO()
        w = csv_module.writer(output)
        w.writerow(["categorie", "id", "company_name", "website_url", "segment", "confidence",
                     "company_stage", "recommended_offer", "status", "disqualify_reason"])
        for cat_name, cat_leads in [("validees", validees), ("proches", proches),
                                      ("tres_loin", tres_loin), ("non_validees", non_validees)]:
            for lead in cat_leads:
                w.writerow([cat_name, lead.get("id"), lead.get("company_name"), lead.get("website_url"),
                            lead.get("segment"), lead.get("confidence"), lead.get("company_stage"),
                            lead.get("recommended_offer"), lead.get("status"), lead.get("disqualify_reason")])
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M")
        return _csv_response(f"resultats_complets_{ts}.csv", output.getvalue())

    # PDF — version imprimable (l'utilisateur utilise "Enregistrer en PDF" du navigateur)
    return render_template(
        "results_print.html",
        session=session,
        categories={
            "validees": validees,
            "non_validees": non_validees,
            "tres_loin": tres_loin,
            "proches": proches,
        },
    )


@app.route("/batch-results/<int:session_id>", methods=["GET"])
def batch_results_view(session_id: int):
    """Resultats du dernier lot de leads analyses (SKIPPED relances ou Phase 2)."""
    with open_db() as conn:
        session = dbmod.get_analysis_session(conn, session_id)
        if session is None:
            flash("Session introuvable.", "error")
            return redirect(url_for("home"))
        batch_ids = dbmod.get_last_batch_ids(conn, session_id)
        if not batch_ids:
            flash("Aucun lot recent trouve.", "warning")
            return redirect(url_for("results_view", session_id=session_id))
        leads = []
        for lid in batch_ids:
            row = conn.execute("""
                SELECT l.*, s.segment, s.confidence, s.company_stage, s.evidence_quotes,
                       s.personalization_hooks, s.disqualify_reason, s.needs_human_review,
                       s.recommended_offer, s.built_with_ai_signals, s.technical_signals,
                       s.pain_signals, s.scored_at
                FROM leads l
                LEFT JOIN lead_scores s ON s.lead_id = l.id
                    AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
                WHERE l.id = ?
            """, (lid,)).fetchone()
            if row:
                leads.append(dict(row))
    return render_template("batch_results.html", session=session, leads=leads, session_id=session_id)


@app.route("/recherche-web/<int:session_id>", methods=["GET"])
def web_search_view(session_id: int):
    """Page dediee aux resultats de recherche web (ScrapeGraphAI)."""
    with open_db() as conn:
        session = dbmod.get_analysis_session(conn, session_id)
        if session is None:
            flash("Session introuvable.", "error")
            return redirect(url_for("home"))
        leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
        leads_with_evidence = []
        for lead in leads:
            evidence = dbmod.get_lead_search_evidence(conn, lead["id"])
            if evidence:
                leads_with_evidence.append({"lead": lead, "evidence": evidence})
    return render_template("web_search.html", session=session, leads=leads_with_evidence)


@app.route("/sessions/<int:session_id>", methods=["GET"])
def session_redirect(session_id: int):
    return redirect(url_for("results_view", session_id=session_id))


@app.route("/progress/<int:session_id>")
def progress_view(session_id: int):
    """Page de progression temps réel du pipeline."""
    return render_template("progress.html", session_id=session_id, api_key_missing=missing_api_keys())


@app.route("/progress/<int:session_id>/stream")
def progress_stream(session_id: int):
    """Endpoint SSE qui stream les mises à jour de progression."""
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)
