"""
Orchestrator of the scraping + scoring pipeline, lead by lead.

run_pipeline() is a generator: it yields a progress dict after each lead
to feed the real-time interface. An exception on a lead never breaks the
whole batch.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import costlog
import db as dbmod
import linkedin_lane
import scraper
import scorer
from db import _now as _db_now
from runconfig import load_config

from constants import CONFIDENCE_THRESHOLD

DEFAULT_THROTTLE_SECONDS = 15  # Firecrawl free tier ~10 req/min
DEFAULT_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "3") or "3")


def _make_cost_cb(conn, session_id: int | None, lead_id: int | None, purpose: str):
    """Callback handed to scorer/emailer so EVERY LLM call is logged to
    llm_calls with tokens, latency, and estimated cost (FR-7)."""
    def cb(meta: dict, latency_ms: int):
        costlog.log_call(
            conn,
            session_id=session_id,
            lead_id=lead_id,
            purpose=purpose,
            provider=meta.get("provider", "?"),
            model=meta.get("model", "?"),
            tokens_in=meta.get("tokens_in", 0),
            tokens_out=meta.get("tokens_out", 0),
            latency_ms=latency_ms,
            created_at=_db_now(),
        )
    return cb


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

def _cancelled(conn, session_id: int | None, cancellation_check=None) -> bool:
    """Returns True when the analysis session has been cancelled."""
    if cancellation_check is not None:
        return bool(cancellation_check())
    return conn is not None and session_id is not None and dbmod.is_session_cancelled(conn, session_id)


def _sleep_check(seconds: float, conn=None, session_id=None, cancellation_check=None) -> bool:
    """Sleeps for the requested number of seconds, checking for cancellation.

    Returns True when the sleep was cut short by a cancellation request —
    the caller should stop scheduling new leads as soon as possible.
    """
    remaining = seconds
    while remaining > 0:
        if _cancelled(conn, session_id, cancellation_check):
            return True
        chunk = min(remaining, 0.5)
        time.sleep(chunk)
        remaining -= chunk
    return False


def _build_lead_metadata(lead: dict) -> dict:
    """Extracts Apollo metadata from a lead for the scoring prompt."""
    import json as _json
    meta = {
        "first_name": lead.get("first_name"),
        "last_name": lead.get("last_name"),
        "title": lead.get("title"),
        "company_name": lead.get("company_name"),
        "email": lead.get("email"),
        "website_url": lead.get("website_url"),
        "apollo_email_status": lead.get("apollo_email_status"),
    }
    # Apollo enrichment (when the lead came from the API): the founder's
    # career and the org facts are direct evidence for technical_founder vs
    # ai_solo_founder and for budget_signal.
    for key in ("apollo_person", "apollo_org"):
        raw = lead.get(key)
        if raw:
            try:
                meta[key] = _json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                pass
    return meta


def _fetch_web_search_evidence(conn, lead_id: int, lead: dict, technical_signals: dict | None = None,
                               notes: list | None = None) -> dict:
    """Runs the web search escalation (LinkedIn, Product Hunt, GitHub,
    founder person_* profiles, etc.), persists each source in
    the DB (lead_search_evidence), and returns the results as a dict
    {source: [hits]} — SEPARATE from the scraped site content.

    Only called by _process_lead when pass 1 was ambiguous (see the caller);
    a clear-cut lead never pays the search cost.

    Before: hits were merged directly into `rows` (loss of title/url, and no
    distinction of site vs web reliability in the prompt).
    Now: `scorer.score_content()` receives this dict via its own
    `web_search_evidence` parameter, formatted and budgeted separately (see
    scorer._format_web_search_evidence).

    technical_signals: the scraper's deterministic signals for this lead
    (scrape_result["technical_signals"]). When available, the site's own
    LinkedIn links and founder-name mentions take priority over the CRM
    fields below — a CRM field can be a placeholder/test value (confirmed
    case: "Wael Test" as the Apollo contact name sent the person search to
    a completely unrelated LinkedIn profile), while a link or name the
    company put on its own site cannot be confused with a homonym.
    """
    notes = notes if notes is not None else []
    company_name = lead.get("company_name", "")
    if not company_name:
        notes.append("web search skipped: lead has no company name")
        return {}
    technical_signals = technical_signals or {}

    site_founder_candidates = technical_signals.get("founder_name_candidates") or []
    csv_founder_name = " ".join(filter(None, [lead.get("first_name"), lead.get("last_name")])).strip() or None
    founder_name = site_founder_candidates[0] if site_founder_candidates else csv_founder_name

    known_linkedin_company_url = technical_signals.get("linkedin_company_url")
    person_urls = technical_signals.get("linkedin_person_urls") or []
    # Only trust it when there is EXACTLY ONE candidate — with several
    # /in/ links (a team page listing multiple people), picking one would
    # be a guess, same risk as the name-search bug this is meant to avoid.
    known_linkedin_person_url = person_urls[0] if len(person_urls) == 1 else None

    # The founder's LinkedIn URL from the Apollo CSV (optional FR-1 column)
    # outranks the site-link candidate: it names THE contact we are emailing,
    # not just "a person the site links to".
    csv_person_url = (lead.get("linkedin_url") or "").strip() or None
    person_profile_url = csv_person_url or known_linkedin_person_url

    # --- Founder LinkedIn deep harvest (merged from lead_tool) ---
    # Full profile + attributed posts, sequential/paced/capped. When it
    # succeeds it REPLACES the snippet-based person_linkedin evidence with
    # far richer, code-attributed content. When it can't run (capped, no
    # key, failure), the classic search path below still covers the lead —
    # and the coverage note says exactly what happened. Nothing silent.
    harvest_hits = None
    if person_profile_url:
        cfg = load_config()
        harvest = linkedin_lane.harvest_founder_profile(person_profile_url, cfg.linkedin, conn)
        notes.extend(harvest.get("notes") or [])
        if harvest["status"] == "ok":
            harvest_hits = harvest["hits"]
            notes.append(
                f"linkedin founder profile harvested in full "
                f"({sum(1 for h in harvest_hits if h['title'].startswith('AUTHORED'))} authored post(s))")
        elif harvest["status"] == "capped":
            notes.append("linkedin harvest capped; fell back to snippet search evidence")
        elif harvest["status"] in ("no_key", "keys_exhausted"):
            notes.append(f"linkedin harvest unavailable ({harvest['status']}); fell back to snippet search")
        else:
            notes.append(f"linkedin harvest failed ({harvest.get('reason')}); fell back to snippet search")
    else:
        notes.append("no founder linkedin url known (csv or site); person evidence limited to name search")

    try:
        search_results = scraper.search_additional_evidence(
            company_name=company_name,
            founder_name=founder_name,
            limit_per_query=2,
            known_linkedin_company_url=known_linkedin_company_url,
            known_linkedin_person_url=person_profile_url,
            # When the deep harvest succeeded we already have the person
            # evidence — skip the redundant person scrape/search entirely.
            skip_person_linkedin=harvest_hits is not None,
        )
        if "_error" in search_results:
            notes.append(f"web search skipped: {search_results['_error']}")
            search_results = {}
    except Exception as e:
        notes.append(f"web search failed: {e}")
        search_results = {}

    if harvest_hits is not None:
        search_results["person_linkedin"] = harvest_hits

    collected = {}
    for source, hits in search_results.items():
        if isinstance(hits, list) and hits:
            dbmod.save_search_evidence(conn, lead_id, source, "", hits)
            collected[source] = hits
    if not collected:
        notes.append("web escalation returned no usable evidence")
    return collected


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
    """Processes a SINGLE lead end to end (Firecrawl scrape + scoring) in its
    own DB connection (taken from the shared pool).

    Scoring runs in two passes, separated by a CONDITIONAL web search
    escalation (FR-3): pass 1 scores the scraped site content alone (no SGAI
    credits spent); pass 2 re-scores with the web evidence (company + founder
    person_* search) ONLY when pass 1 was ambiguous (confidence < 0.7 /
    needs_human_review).

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
    coverage: list = []   # evidence coverage notes — flushed to the DB in finally

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
        # --- Session budget gate (FR-7) ---
        # Checked BEFORE any spend on this lead. When the cap is hit the
        # whole session is cancelled cooperatively: leads in flight finish,
        # queued ones never start, and each skipped lead says why.
        cap_usd = load_config().budget.session_cap_usd
        if session_id is not None and cap_usd > 0:
            try:
                costlog.check_budget(conn, session_id, cap_usd)
            except costlog.BudgetExceeded as be:
                dbmod.update_lead_progress(conn, lead_id, status="SCORE_FAILED",
                                           error=f"budget_exceeded: {be}")
                coverage.append(f"skipped: session LLM budget cap reached (${be.cap:.2f})")
                dbmod.cancel_analysis_session(conn, session_id)
                events.append(_base({"step": "done", "status": "SCORE_FAILED",
                                     "error": f"budget_exceeded: {be}"}))
                return events

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
        coverage.extend(scrape_result.get("fetch_notes") or [])
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

        # --- Public surface scan (config-gated; OFF until legal sign-off) ---
        # Deterministic GET/HEAD-only checks on the lead's public surface,
        # stored as verified findings for internal review. The scan is a
        # several-single-requests-per-domain affair; it never writes to the
        # target and every finding row required verified=1 + evidence excerpt.
        if load_config().surface_scan.enabled:
            try:
                import surface_scan
                cfg = load_config().surface_scan
                findings = surface_scan.scan_site(
                    website,
                    timeout=cfg.timeout,
                    per_domain_delay=cfg.per_domain_delay,
                    max_findings=cfg.max_findings,
                )
                written = dbmod.save_lead_public_findings(conn, lead_id, findings)
                if written:
                    coverage.append(f"surface scan: {written} verified finding(s) (internal review only)")
                else:
                    coverage.append("surface scan: no verified findings")
            except Exception as _scan_e:
                coverage.append(f"surface scan failed: {_scan_e}")

        # --- Scoring, pass 1: site content only, NO web search yet ---
        # Groups the scraper's deterministic signals (technical_signals +
        # github_check) into a single block for the prompt.
        deterministic_signals = None
        if scrape_result.get("technical_signals"):
            deterministic_signals = dict(scrape_result["technical_signals"])
            deterministic_signals["github_check"] = scrape_result.get("github_check")

        lead_metadata = _build_lead_metadata(lead)

        # site_content_missing: the official site produced no usable content.
        # Based on the ACTUAL rows content, not on the scraper's overall
        # status: scraper.scrape_website() returns "FETCH_FAILED" also when the
        # homepage was scraped but every sub-page was unusable (scraper.py:
        # len(rows)==1 and unusable >= len(other_pages)) — real site content
        # EXISTS then, and the flag must stay False. Previously the flag was
        # True in that case, so the verdict wrongly said "site_content_missing"
        # while lead_content held the scraped text.
        site_content_missing = not any(
            (content or "").strip() for _, _, content in scrape_result["rows"]
        )

        cost_cb = _make_cost_cb(conn, session_id, lead_id, "score")

        def _score(web_evidence):
            return scorer.score_content(
                scrape_result["rows"],
                deterministic_signals=deterministic_signals,
                lead_metadata=lead_metadata,
                web_search_evidence=web_evidence,
                scoring_criteria=scoring_criteria,
                scoring_criteria_custom=scoring_criteria_custom,
                site_content_missing=site_content_missing,
                cost_cb=cost_cb,
            )

        score_t0 = _now_ts()
        try:
            verdict = _score(None)
        except Exception as e:
            score_elapsed = _now_ts() - score_t0
            dbmod.update_lead_progress(conn, lead_id, status="SCORE_FAILED", error=str(e), score_seconds=score_elapsed)
            events.append(_base({"step": "scoring", "status": "SCORE_FAILED", "error": str(e), "score_seconds": score_elapsed}))
            return events

        # --- Web search escalation (conditional, FR-3) ---
        # The web search (company sources + founder person_*) runs when
        # pass 1 was ambiguous (confidence < 0.7 => needs_human_review),
        # OR for confident small_agency_scaling leads (segment by the LLM,
        # hiring_technical by the deterministic careers signal) — the
        # high-value confident case that was previously never verified.
        # Clear-cut leads (too_big, wrong_field, confident non-agency
        # verdicts) never pay the SGAI credit cost — "quality, not
        # quantity": credits are saved on leads that would be rejected
        # anyway.
        web_evidence = {}
        should_escalate_web = (
            verdict.get("needs_human_review")
            or verdict.get("confidence", 0.0) < CONFIDENCE_THRESHOLD
            or (
                verdict.get("segment") == "small_agency_scaling"
                and bool(deterministic_signals and deterministic_signals.get("hiring_technical"))
                and verdict.get("confidence", 0.0) >= CONFIDENCE_THRESHOLD
            )
        )
        if should_escalate_web:
            events.append(_base({"step": "web_search", "status": None, "error": None}))
            web_evidence = _fetch_web_search_evidence(conn, lead_id, lead, technical_signals=scrape_result.get("technical_signals"), notes=coverage)
            if web_evidence:
                try:
                    verdict = _score(web_evidence)
                except Exception as e:
                    # The escalation was bonus evidence — keep the pass-1
                    # verdict (already flagged for human review) rather than
                    # failing a lead that was already scored.
                    note = f"web_escalation_second_pass_failed: {e}"
                    existing = verdict.get("disqualify_reason")
                    verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note
        else:
            coverage.append("web escalation skipped: pass-1 verdict was clear-cut")
        score_elapsed = _now_ts() - score_t0

        try:
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
        try:
            dbmod.append_coverage_notes(conn, lead_id, coverage)
        except Exception:
            pass  # coverage notes must never fail a lead
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
            if _cancelled(conn, session_id, cancellation_check):
                return
            for ev in _process_lead(lead, session_id, scoring_criteria, scoring_criteria_custom, throttle_seconds):
                yield from _emit(ev, i)
            if _sleep_check(throttle_seconds, conn, session_id, cancellation_check):
                return
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
            if _cancelled(conn, session_id, cancellation_check):
                # Cancellation between two leads: futures that have NOT
                # started yet are cancelled (they will never run); leads
                # already in flight keep finishing cleanly, their results
                # are simply no longer reported.
                pool.shutdown(wait=False, cancel_futures=True)
                return


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
        if _cancelled(conn, session_id, cancellation_check):
            return
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

        # Same structural safeguard as the main pass: a rescore without any
        # usable site content (dict rows — db.get_lead_content) keeps
        # needs_human_review forced by scorer._apply_site_missing_guard.
        site_content_missing = not any(
            (row.get("content") or "").strip() for row in existing_rows
        )

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
                site_content_missing=site_content_missing,
                cost_cb=_make_cost_cb(conn, session_id, lead_id, "rescore"),
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
            if _sleep_check(throttle_seconds, conn, session_id, cancellation_check):
                return
            continue

        progress.update(step="done", status=new_status, error=None, verdict=verdict, ts=_now_ts())
        yield dict(progress)
        if _sleep_check(throttle_seconds, conn, session_id, cancellation_check):
            return