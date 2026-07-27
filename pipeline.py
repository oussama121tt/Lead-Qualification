"""
Orchestrateur (étapes 3 + 5 enchaînées, lead par lead).

`run_pipeline` est un générateur : il yield un dict de progression après chaque
lead, pour pouvoir alimenter une barre de progression Streamlit sans bloquer
l'UI jusqu'à la fin du batch. Une exception sur un lead ne casse jamais le
batch entier (garde-fou "qualité, pas quantité" du projet : on isole les échecs).
"""

import time
import db as dbmod
import scraper
import scorer

DEFAULT_THROTTLE_SECONDS = 2.5  # tier gratuit Groq (~30 req/min) + Firecrawl 1 req/sec/domaine


def _now_ts() -> float:
    """Monotonic timestamp for progress tracking (elapsed time)."""
    return time.monotonic()


def run_pipeline(conn, throttle_seconds: float = DEFAULT_THROTTLE_SECONDS, session_id: int | None = None):
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
        try:
            scrape_result = scraper.scrape_website(website, throttle_seconds=1.0)
        except Exception as e:
            dbmod.update_lead_status(conn, lead_id, "FETCH_FAILED")
            progress.update(step="scraping", status="FETCH_FAILED", error=str(e))
            yield dict(progress)
            continue

        dbmod.update_lead_status(conn, lead_id, scrape_result["status"])
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

        progress.update(step="scraping_done", status=scrape_result["status"], error=scrape_result["error"])
        yield dict(progress)

        if scrape_result["status"] == "FETCH_FAILED":
            # On score quand même en LOW_CONFIDENCE via scorer (pas de contenu ->
            # unclear/needs_human_review), pour ne pas bloquer le lead silencieusement.
            pass

        # --- Scoring ---
        progress["step"] = "scoring"
        yield dict(progress)

        # Regroupe les signaux déterministes du scraper (technical_signals +
        # github_check) en un seul bloc pour le prompt — voir scorer.py pour
        # la distinction avec le champ `technical_signals` du verdict LLM.
        deterministic_signals = None
        if scrape_result.get("technical_signals"):
            deterministic_signals = dict(scrape_result["technical_signals"])
            deterministic_signals["github_check"] = scrape_result.get("github_check")

        try:
            verdict = scorer.score_content(scrape_result["rows"], deterministic_signals=deterministic_signals)

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
            new_status = "LOW_CONFIDENCE" if verdict.get("needs_human_review") else "SCORED"
            dbmod.update_lead_status(conn, lead_id, new_status)
        except Exception as e:
            dbmod.update_lead_status(conn, lead_id, "SCORE_FAILED")
            progress.update(step="scoring", status="SCORE_FAILED", error=str(e))
            yield dict(progress)
            time.sleep(throttle_seconds)
            continue

        progress.update(step="done", status=new_status, error=None, verdict=verdict)
        yield dict(progress)

        time.sleep(throttle_seconds)  # respecter les quotas des tiers gratuits