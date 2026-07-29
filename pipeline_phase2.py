"""
Phase 2 : web search + rescore (pas de scraping).

run_rescore_pipeline() est un générateur qui recharge le contenu déjà scrapé
depuis la DB, lance une recherche web ScrapeGraphAI (optionnelle), puis rescore.
"""

import time
import db as dbmod
import scraper
import scorer


def _now_ts() -> float:
    return time.monotonic()


def _sleep_check(seconds: float, conn=None, session_id=None, cancellation_check=None):
    """Dort le nombre de secondes demandé."""
    time.sleep(seconds)


def run_rescore_pipeline(conn, throttle_seconds: float = 1.0, session_id: int | None = None, skip_web_search: bool = False, lead_status: str = "RESCORE_PENDING", cancellation_check=None):
    """
    Re-score uniquement (pas de scraping). Utilise le contenu déjà scrapé en DB.
    """
    scoring_criteria = dbmod.get_scoring_criteria(conn, session_id) if session_id else []
    scoring_criteria_custom = dbmod.get_scoring_criteria_custom(conn, session_id) if session_id else ""

    leads = dbmod.get_leads_by_status(conn, lead_status, session_id=session_id)
    total = len(leads)
    started_at = _now_ts()

    for i, lead in enumerate(leads, start=1):
        lead_id = lead["id"]
        step_label = "web_search" if not skip_web_search else "scoring"
        progress = {
            "index": i,
            "total": total,
            "lead_id": lead_id,
            "company_name": lead["company_name"],
            "website_url": lead.get("website_url", ""),
            "step": step_label,
            "status": None,
            "error": None,
            "ts": _now_ts(),
            "started_at": started_at,
        }
        yield dict(progress)

        existing_rows = dbmod.get_lead_content(conn, lead_id)
        if not existing_rows:
            fail_status = f"{lead_status.replace('_PENDING', '_FAILED')}"
            dbmod.update_lead_status(conn, lead_id, fail_status, error="no_scraped_content")
            progress.update(step=step_label, status=fail_status, error="no_scraped_content", ts=_now_ts())
            yield dict(progress)
            continue

        deterministic_signals = None
        signals_row = dbmod.get_lead_technical_signals(conn, lead_id)
        if signals_row:
            deterministic_signals = dict(signals_row)

        # --- Web search (optionnel) ---
        search_results = {}
        if not skip_web_search:
            founder = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
            try:
                search_results = scraper.search_additional_evidence(
                    company_name=lead["company_name"],
                    founder_name=founder or None,
                )
                for source, hits in search_results.items():
                    if isinstance(hits, list) and hits:
                        dbmod.save_search_evidence(
                            conn, lead_id, source,
                            scraper.SEARCH_QUERY_TEMPLATES.get(source, ""),
                            hits,
                        )
                progress.update(search_evidence=search_results, ts=_now_ts())
                yield dict(progress)
            except Exception as e:
                progress.update(search_error=str(e), ts=_now_ts())
                yield dict(progress)

        enriched_rows = list(existing_rows)
        if isinstance(search_results, dict):
            for source, hits in search_results.items():
                if isinstance(hits, list):
                    for h in hits:
                        if isinstance(h, dict) and h.get("content"):
                            enriched_rows.append({
                                "source": f"web_search_{source}",
                                "url": h.get("url", ""),
                                "content": h.get("content", ""),
                            })

        # --- Scoring ---
        progress.update(step="scoring", ts=_now_ts())
        yield dict(progress)

        try:
            score_t0 = _now_ts()
            verdict = scorer.score_content(
                enriched_rows,
                deterministic_signals=deterministic_signals,
                scoring_criteria=scoring_criteria,
                scoring_criteria_custom=scoring_criteria_custom,
            )
            score_elapsed = _now_ts() - score_t0

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
            dbmod.update_lead_status(conn, lead_id, new_status)
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
        _sleep_check(throttle_seconds)
