"""
Orchestrateur du pipeline scraping + scoring, lead par lead.

run_pipeline() est un générateur : il yield un dict de progression après
chaque lead pour alimenter l'interface temps réel. Une exception sur un
lead ne casse jamais le batch entier.
"""

import time
import db as dbmod
import scraper
import scorer

DEFAULT_THROTTLE_SECONDS = 15  # Firecrawl free tier ~10 req/min


def _now_ts() -> float:
    """Monotonic timestamp for progress tracking (elapsed time)."""
    return time.monotonic()


# def _sleep_check(seconds: float, conn, session_id: int | None, cancellation_check=None):
#     remaining = seconds
#     while remaining > 0:
#         if cancellation_check and cancellation_check():
#             return
#         if not cancellation_check and session_id and dbmod.is_session_cancelled(conn, session_id):
#             return
#         chunk = min(remaining, 0.5)
#         time.sleep(chunk)
#         remaining -= chunk

def _sleep_check(seconds: float, conn=None, session_id=None, cancellation_check=None):
    """Dort le nombre de secondes demandé."""
    time.sleep(seconds)


def run_pipeline(conn, throttle_seconds: float = DEFAULT_THROTTLE_SECONDS, session_id: int | None = None, cancellation_check=None):
    # Charger les critères de scoring sélectionnés par l'utilisateur
    scoring_criteria = dbmod.get_scoring_criteria(conn, session_id) if session_id else []
    scoring_criteria_custom = dbmod.get_scoring_criteria_custom(conn, session_id) if session_id else ""

    leads = dbmod.get_leads_to_process(conn, session_id=session_id)
    total = len(leads)
    started_at = _now_ts()

    for i, lead in enumerate(leads, start=1):
        lead_id = lead["id"]
        website = lead["website_url"]
        progress = {
            "index": i,
            "total": total,
            "lead_id": lead_id,
            "company_name": lead["company_name"],
            "website_url": website,
            "step": "scraping",
            "status": None,
            "error": None,
            "ts": _now_ts(),
            "started_at": started_at,
        }
        yield dict(progress)

        # --- Scraping ---
        scrape_t0 = _now_ts()
        try:
            scrape_result = scraper.scrape_website(website, throttle_seconds=1.0)
        except Exception as e:
            err_str = str(e)
            scrape_elapsed = _now_ts() - scrape_t0
            dbmod.update_lead_status(conn, lead_id, "FETCH_FAILED", error=err_str)
            dbmod.record_lead_timing(conn, lead_id, scrape_seconds=scrape_elapsed)
            progress.update(step="scraping", status="FETCH_FAILED", error=err_str, scrape_seconds=scrape_elapsed, ts=_now_ts())
            yield dict(progress)
            continue

        scrape_elapsed = _now_ts() - scrape_t0

        dbmod.record_lead_timing(conn, lead_id, scrape_seconds=scrape_elapsed)
        dbmod.update_lead_status(conn, lead_id, scrape_result["status"], error=scrape_result.get("error"))
        if scrape_result["rows"]:
            dbmod.save_lead_content(conn, lead_id, scrape_result["rows"])

        # Signaux déterministes (fonts, patterns visuels, fingerprint de
        # builder, pattern git) : sauvegardés tels quels, jamais interprétés
        # ici — le jugement reste dans le scoring ci-dessous.
        if scrape_result.get("technical_signals"):
            dbmod.save_lead_technical_signals(
                conn,
                lead_id,
                scrape_result["technical_signals"],
                scrape_result.get("github_check"),
            )

        progress.update(step="scraping_done", status=scrape_result["status"], error=scrape_result["error"], ts=_now_ts())
        yield dict(progress)

        # Même si le scraping a échoué (FETCH_FAILED), on continue vers le
        # scoring : scorer.py gère le cas rows=[] et renvoie un verdict
        # unclear/needs_human_review, pour ne pas bloquer le lead silencieusement.

        # --- Scoring ---
        progress.update(step="scoring", ts=_now_ts())
        yield dict(progress)

        # Regroupe les signaux déterministes du scraper (technical_signals +
        # github_check) en un seul bloc pour le prompt — voir scorer.py pour
        # la distinction avec le champ `technical_signals` du verdict LLM.
        deterministic_signals = None
        if scrape_result.get("technical_signals"):
            deterministic_signals = dict(scrape_result["technical_signals"])
            deterministic_signals["github_check"] = scrape_result.get("github_check")

        try:
            score_t0 = _now_ts()
            verdict = scorer.score_content(scrape_result["rows"], deterministic_signals=deterministic_signals, scoring_criteria=scoring_criteria, scoring_criteria_custom=scoring_criteria_custom)
            score_elapsed = _now_ts() - score_t0

            # Garde-fou domain_mismatch : si l'email et le site scrapé ne
            # partagent pas le même domaine, le verdict porte peut-être sur la
            # MAUVAISE entreprise. On ne fait jamais confiance à la confidence
            # du modèle dans ce cas — human review obligatoire, quoi qu'il dise.
            if lead.get("domain_mismatch"):
                reason = lead.get("domain_mismatch_reason") or "email/website domain mismatch"
                warning = (
                    f"domain_mismatch: {reason} — ce verdict décrit peut-être "
                    "la mauvaise entreprise, à confirmer manuellement avant tout envoi"
                )
                verdict["needs_human_review"] = True
                existing = verdict.get("disqualify_reason")
                verdict["disqualify_reason"] = f"{existing} | {warning}" if existing else warning

            dbmod.save_lead_score(conn, lead_id, verdict)
            dbmod.record_lead_timing(conn, lead_id, score_seconds=score_elapsed)
            new_status = "LOW_CONFIDENCE" if verdict.get("needs_human_review") else "SCORED"
            scrape_err = scrape_result.get("error") or progress.get("error")
            dbmod.update_lead_status(conn, lead_id, new_status, error=scrape_err)
        except Exception as e:
            score_elapsed = _now_ts() - score_t0
            dbmod.update_lead_status(conn, lead_id, "SCORE_FAILED", error=str(e))
            dbmod.record_lead_timing(conn, lead_id, score_seconds=score_elapsed)
            progress.update(step="scoring", status="SCORE_FAILED", error=str(e), score_seconds=score_elapsed, ts=_now_ts())
            yield dict(progress)
            _sleep_check(throttle_seconds)
            continue

        progress.update(step="done", status=new_status, error=None, verdict=verdict, ts=_now_ts())
        yield dict(progress)

        _sleep_check(throttle_seconds)  # respecter les quotas