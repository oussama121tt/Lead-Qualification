"""
Orchestrator of the scraping + scoring pipeline, lead by lead.

run_pipeline() is a generator: it yields a progress dict after each lead
to feed the real-time interface. An exception on a lead never breaks the
whole batch.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import db as dbmod
import scraper
import scorer

DEFAULT_THROTTLE_SECONDS = 15  # Firecrawl free tier ~10 req/min
DEFAULT_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "3") or "3")


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
    """Sleeps for the requested number of seconds."""
    time.sleep(seconds)


def _build_lead_metadata(lead: dict) -> dict:
    """Extracts Apollo metadata from a lead for the scoring prompt."""
    return {
        "first_name": lead.get("first_name"),
        "last_name": lead.get("last_name"),
        "title": lead.get("title"),
        "company_name": lead.get("company_name"),
        "email": lead.get("email"),
        "website_url": lead.get("website_url"),
    }


def _fetch_web_search_evidence(conn, lead_id: int, lead: dict) -> dict:
    """Runs a web search (LinkedIn, Product Hunt, etc.), persists each source in
    the DB (lead_search_evidence), and returns the results as a dict
    {source: [hits]} — SEPARATE from the scraped site content.

    Before: hits were merged directly into `rows` (loss of title/url, and no
    distinction of site vs web reliability in the prompt).
    Now: `scorer.score_content()` receives this dict via its own
    `web_search_evidence` parameter, formatted and budgeted separately (see
    scorer._format_web_search_evidence).
    """
    company_name = lead.get("company_name", "")
    if not company_name:
        return {}
    founder_name = " ".join(filter(None, [lead.get("first_name"), lead.get("last_name")])).strip() or None
    try:
        search_results = scraper.search_additional_evidence(
            company_name=company_name,
            founder_name=founder_name,
            limit_per_query=2,
        )
        if "_error" in search_results:
            return {}
        for source, hits in search_results.items():
            if isinstance(hits, list) and hits:
                dbmod.save_search_evidence(conn, lead_id, source, "", hits)
        return {
            source: hits for source, hits in search_results.items()
            if isinstance(hits, list) and hits
        }
    except Exception:
        return {}


def _load_persisted_web_evidence(conn, lead_id: int) -> dict:
    """Reloads the web evidence already collected and persisted (lead_search_evidence)
    for a lead — used by run_rescore_pipeline, which does not run a new web
    search but must NOT lose the one from the first pass either (fixed bug:
    previously, a rescore only reloaded lead_content, never
    lead_search_evidence — web evidence paid for in SGAI credits silently
    disappeared)."""
    evidence_rows = dbmod.get_lead_search_evidence(conn, lead_id)
    web_evidence: dict = {}
    for row in evidence_rows:
        source = row.get("source")
        hits = row.get("results")
        if not source or not isinstance(hits, list):
            continue
        web_evidence.setdefault(source, []).extend(hits)
    return web_evidence


def _process_lead(lead, session_id, scoring_criteria, scoring_criteria_custom, throttle_seconds):
    """Processes a SINGLE lead end to end (Firecrawl scrape + SG web search
    + scoring) in its own DB connection (taken from the shared pool).

    Returns the list of progress dicts for the lead — same format as the
    yields of the old sequential run_pipeline. Used as-is in sequential
    mode (concurrency<=1) and as a parallel work unit (one thread per
    batch of leads, each with its own connection).

    NB: no inter-lead throttle here — concurrency is what limits the
    throughput. Any error on a lead is caught and turned into a *FAILED
    event: a lead that crashes never makes the others fail.
    """
    conn = dbmod.get_connection()
    events = []
    lead_id = lead["id"]
    website = lead["website_url"]

    def _base(extra=None):
        d = {
            "lead_id": lead_id,
            "company_name": lead.get("company_name"),
            "website_url": website,
            "ts": _now_ts(),
        }
        if extra:
            d.update(extra)
        return d

    try:
        events.append(_base({"step": "scraping", "status": None, "error": None}))

        # --- Scraping ---
        scrape_t0 = _now_ts()
        try:
            scrape_result = scraper.scrape_website(website, throttle_seconds=1.0)
        except Exception as e:
            err_str = str(e)
            scrape_elapsed = _now_ts() - scrape_t0
            dbmod.update_lead_progress(conn, lead_id, status="FETCH_FAILED", error=err_str, scrape_seconds=scrape_elapsed)
            events.append(_base({"step": "scraping", "status": "FETCH_FAILED", "error": err_str, "scrape_seconds": scrape_elapsed}))
            return events

        scrape_elapsed = _now_ts() - scrape_t0
        dbmod.update_lead_progress(conn, lead_id, status=scrape_result["status"], error=scrape_result.get("error"), scrape_seconds=scrape_elapsed)
        if scrape_result["rows"]:
            dbmod.save_lead_content(conn, lead_id, scrape_result["rows"])

        # Deterministic signals (fonts, visual patterns, builder fingerprint,
        # git pattern): saved as-is, never interpreted here — judgment stays
        # in the scoring below.
        if scrape_result.get("technical_signals"):
            dbmod.save_lead_technical_signals(
                conn,
                lead_id,
                scrape_result["technical_signals"],
                scrape_result.get("github_check"),
            )

        events.append(_base({"step": "scraping_done", "status": scrape_result["status"], "error": scrape_result.get("error"), "scrape_seconds": scrape_elapsed}))

        # --- Web Search (integrated into Phase 1) ---
        # Fetches web evidence (LinkedIn, Product Hunt, GitHub, etc.)
        # SEPARATELY from the scraped site content.
        web_evidence = _fetch_web_search_evidence(conn, lead_id, lead)

        # --- Scoring ---
        # Groups the scraper's deterministic signals (technical_signals +
        # github_check) into a single block for the prompt.
        deterministic_signals = None
        if scrape_result.get("technical_signals"):
            deterministic_signals = dict(scrape_result["technical_signals"])
            deterministic_signals["github_check"] = scrape_result.get("github_check")

        try:
            score_t0 = _now_ts()
            lead_metadata = _build_lead_metadata(lead)
            verdict = scorer.score_content(
                scrape_result["rows"],
                deterministic_signals=deterministic_signals,
                lead_metadata=lead_metadata,
                web_search_evidence=web_evidence,
                scoring_criteria=scoring_criteria,
                scoring_criteria_custom=scoring_criteria_custom,
            )
            score_elapsed = _now_ts() - score_t0

            # domain_mismatch safeguard: if the email and the scraped site do
            # not share the same domain, the verdict may concern the WRONG
            # company. In that case we never trust the model's confidence —
            # human review is mandatory, no matter what it says.
            if lead.get("domain_mismatch"):
                reason = lead.get("domain_mismatch_reason") or "email/website domain mismatch"
                warning = (
                    f"domain_mismatch: {reason} — this verdict may describe the "
                    "wrong company, please confirm manually before any send"
                )
                verdict["needs_human_review"] = True
                existing = verdict.get("disqualify_reason")
                verdict["disqualify_reason"] = f"{existing} | {warning}" if existing else warning

            dbmod.save_lead_score(conn, lead_id, verdict)
            new_status = "LOW_CONFIDENCE" if verdict.get("needs_human_review") else "SCORED"
            scrape_err = scrape_result.get("error")
            dbmod.update_lead_progress(conn, lead_id, status=new_status, error=scrape_err, score_seconds=score_elapsed)
        except Exception as e:
            score_elapsed = _now_ts() - score_t0
            dbmod.update_lead_progress(conn, lead_id, status="SCORE_FAILED", error=str(e), score_seconds=score_elapsed)
            events.append(_base({"step": "scoring", "status": "SCORE_FAILED", "error": str(e), "score_seconds": score_elapsed}))
            return events

        events.append(_base({"step": "done", "status": new_status, "error": None, "verdict": verdict}))
        return events
    except Exception as fatal:
        events.append(_base({"step": "done", "status": "SCORE_FAILED", "error": str(fatal)}))
        return events
    finally:
        conn.close()


def run_pipeline(conn, throttle_seconds: float = DEFAULT_THROTTLE_SECONDS, session_id: int | None = None, cancellation_check=None, concurrency: int = DEFAULT_CONCURRENCY):
    """Orchestrator of the scraping + scoring pipeline, lead by lead.

    With ``concurrency <= 1`` (or a single lead to process): historical
    sequential behavior, waiting ``throttle_seconds`` between each lead.

    With ``concurrency > 1``: leads are processed IN PARALLEL (at most
    ``concurrency`` workers, each with its own DB connection taken from the
    pool). A batch's duration drops from about ``N * T`` to ``N/w * T`` —
    essential when each lead waits a long time on Firecrawl/SGAI.

    Yields the progress events (same format as before) in the order of
    completion of the workers.
    """
    scoring_criteria = dbmod.get_scoring_criteria(conn, session_id) if session_id else []
    scoring_criteria_custom = dbmod.get_scoring_criteria_custom(conn, session_id) if session_id else ""

    leads = dbmod.get_leads_to_process(conn, session_id=session_id)
    total = len(leads)
    started_at = _now_ts()
    if total == 0:
        return

    def _emit(ev: dict, index: int):
        ev["index"] = index
        ev["total"] = total
        ev["started_at"] = started_at
        yield dict(ev)

    if concurrency <= 1 or total == 1:
        for i, lead in enumerate(leads, start=1):
            for ev in _process_lead(lead, session_id, scoring_criteria, scoring_criteria_custom, throttle_seconds):
                yield from _emit(ev, i)
            _sleep_check(throttle_seconds)
        return

    with ThreadPoolExecutor(max_workers=min(concurrency, total)) as pool:
        futures = {
            pool.submit(_process_lead, lead, session_id, scoring_criteria, scoring_criteria_custom, throttle_seconds): i
            for i, lead in enumerate(leads, start=1)
        }
        for future in as_completed(futures):
            index = futures[future]
            for ev in future.result():
                yield from _emit(ev, index)


def run_rescore_pipeline(conn, throttle_seconds: float = 1.0, session_id: int | None = None, lead_status: str = "RESCORE_PENDING", cancellation_check=None):
    """
    Rescore only (no re-scraping and no web search).
    Reloads the already-scraped content from the DB and reruns the LLM.
    Used by the "Re-score" button from the results page.
    """
    scoring_criteria = dbmod.get_scoring_criteria(conn, session_id) if session_id else []
    scoring_criteria_custom = dbmod.get_scoring_criteria_custom(conn, session_id) if session_id else ""

    leads = dbmod.get_leads_by_status(conn, lead_status, session_id=session_id)
    total = len(leads)
    started_at = _now_ts()

    for i, lead in enumerate(leads, start=1):
        lead_id = lead["id"]
        progress = {
            "index": i,
            "total": total,
            "lead_id": lead_id,
            "company_name": lead["company_name"],
            "website_url": lead.get("website_url", ""),
            "step": "scoring",
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
            progress.update(step="scoring", status=fail_status, error="no_scraped_content", ts=_now_ts())
            yield dict(progress)
            continue

        deterministic_signals = None
        signals_row = dbmod.get_lead_technical_signals(conn, lead_id)
        if signals_row:
            deterministic_signals = dict(signals_row)

        # Reloads the web evidence already collected on the first pass —
        # without this, a rescore silently lost everything the web search had
        # found (fixed bug: lead_search_evidence exists in the DB but was
        # never read back here).
        web_evidence = _load_persisted_web_evidence(conn, lead_id)

        try:
            score_t0 = _now_ts()
            lead_metadata = _build_lead_metadata(lead)
            verdict = scorer.score_content(
                existing_rows,
                deterministic_signals=deterministic_signals,
                lead_metadata=lead_metadata,
                web_search_evidence=web_evidence,
                scoring_criteria=scoring_criteria,
                scoring_criteria_custom=scoring_criteria_custom,
            )
            score_elapsed = _now_ts() - score_t0

            if lead.get("domain_mismatch"):
                reason = lead.get("domain_mismatch_reason") or "email/website domain mismatch"
                warning = (
                    f"domain_mismatch: {reason} — this verdict may describe the "
                    "wrong company, please confirm manually before any send"
                )
                verdict["needs_human_review"] = True
                existing = verdict.get("disqualify_reason")
                verdict["disqualify_reason"] = f"{existing} | {warning}" if existing else warning

            dbmod.save_lead_score(conn, lead_id, verdict)
            new_status = "LOW_CONFIDENCE" if verdict.get("needs_human_review") else "SCORED"
            dbmod.update_lead_progress(conn, lead_id, status=new_status, score_seconds=score_elapsed)
        except Exception as e:
            score_elapsed = _now_ts() - score_t0
            dbmod.update_lead_progress(conn, lead_id, status="SCORE_FAILED", error=str(e), score_seconds=score_elapsed)
            progress.update(step="scoring", status="SCORE_FAILED", error=str(e), score_seconds=score_elapsed, ts=_now_ts())
            yield dict(progress)
            _sleep_check(throttle_seconds)
            continue

        progress.update(step="done", status=new_status, error=None, verdict=verdict, ts=_now_ts())
        yield dict(progress)
        _sleep_check(throttle_seconds)