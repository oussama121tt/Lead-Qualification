"""
CSV export — three output formats for reporting and human review.

1. export_scraping_csv(): one row per scraped page (source, url, content,
   deterministic signals computed by scraper.py). Usable in Excel/Sheets.

2. export_scores_csv(): one row per lead with the latest scoring verdict
   (segment, confidence, signals, hooks). Main export point toward the
   sending tools (Instantly/Smartlead).

3. export_readable_csv(): one row per lead with truncated page previews
   and signals translated into phrases. Designed for quick human review.

Separated from db.py: reporting functions, no lifecycle operations.
"""

import csv
import json
import io
import argparse

import db as dbmod


def _flatten(value):
    """
    Makes a value usable in a CSV cell:
    - None -> empty string
    - list/tuple -> joined with ' | '
    - dict -> compact JSON (readable but copyable)
    - string already serialized as JSON (TEXT columns of db.py) -> parsed
      first for a clean rendering, otherwise returned as-is
    - scalar -> unchanged
    """
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


# ---------------------------------------------------------------------------
# 1) Scraping CSV (pages + deterministic signals)
# ---------------------------------------------------------------------------

SCRAPING_FIELDS = [
    "lead_id", "company_name", "website_url", "status", "error",
    "source", "url", "content_chars", "content",
    "generator_fingerprint", "generator_meta_tag",
    "trend_fonts_found", "visual_patterns_triggered",
    "vibe_language_matches", "github_repo_url", "github_check",
    "ai_style_phrases_found", "ai_style_phrase_density",
    "ai_authorship_disclosures_found",
]


def _iter_scraping_rows(conn, session_id=None):
    """Row generator for the scraping CSV. Factored out for file and memory use."""
    leads = dbmod.get_leads(conn, include_duplicates=True, session_id=session_id)
    lead_ids = [lead["id"] for lead in leads]
    pages_map = dbmod.get_lead_content_map(conn, lead_ids)
    signals_map = dbmod.get_lead_technical_signals_map(conn, lead_ids)

    for lead in leads:
        lead_id = lead["id"]
        pages = pages_map.get(lead_id, [])
        signals = signals_map.get(lead_id) or {}

        base_row = {
            "lead_id": lead_id,
            "company_name": lead.get("company_name", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "error": lead.get("error", ""),
            "generator_fingerprint": signals.get("generator_fingerprint", ""),
            "generator_meta_tag": signals.get("generator_meta_tag", ""),
            "trend_fonts_found": _flatten(signals.get("trend_fonts_found")),
            "visual_patterns_triggered": _flatten(signals.get("visual_patterns_triggered")),
            "vibe_language_matches": _flatten(signals.get("vibe_language_matches")),
            "github_repo_url": signals.get("github_repo_url", ""),
            "github_check": _flatten(signals.get("github_check")),
            "ai_style_phrases_found": _flatten(signals.get("ai_style_phrases_found")),
            "ai_style_phrase_density": signals.get("ai_style_phrase_density", ""),
            "ai_authorship_disclosures_found": _flatten(signals.get("ai_authorship_disclosures_found")),
        }

        if not pages:
            yield {**base_row, "source": "", "url": "", "content_chars": 0, "content": ""}
            continue

        for page in pages:
            content = page.get("content") or ""
            yield {
                **base_row,
                "source": page.get("source", ""),
                "url": page.get("url", ""),
                "content_chars": len(content),
                "content": content,
            }


def export_scraping_csv(conn, output_path: str, session_id=None) -> int:
    """
    One row per (lead, scraped page). Leads without content (not yet scraped,
    or FETCH_FAILED) are still written with an empty content row, so no lead
    of the batch is lost in the output file.

    Returns the number of rows written.
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SCRAPING_FIELDS)
        writer.writeheader()
        for row in _iter_scraping_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def scraping_csv_string(conn, session_id=None) -> str:
    """
    Same content as export_scraping_csv, but returned as an in-memory string
    (no disk write) — for the download button.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SCRAPING_FIELDS)
    writer.writeheader()
    for row in _iter_scraping_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 2) Scoring CSV (LLM verdicts)
# ---------------------------------------------------------------------------

SCORE_FIELDS = [
    "lead_id", "first_name", "last_name", "title", "company_name", "email", "website_url",
    "status", "error", "is_duplicate", "duplicate_reason",
    "segment", "confidence", "needs_human_review", "company_stage",
    "recommended_offer", "disqualify_reason",
    "built_with_ai_signals", "technical_signals", "pain_signals",
    "evidence_quotes", "personalization_hooks", "scored_at",
]


def _iter_score_rows(conn, session_id=None):
    """
    Row generator for the scoring CSV — factored out to be consumed both by
    export_scores_csv (file) and scores_csv_string (in-memory, for the
    download button).
    """
    leads = dbmod.get_leads_with_scores(conn, session_id=session_id)
    for lead in leads:
        yield {
            "lead_id": lead["id"],
            "first_name": lead.get("first_name", ""),
            "last_name": lead.get("last_name", ""),
            "title": lead.get("title", ""),
            "company_name": lead.get("company_name", ""),
            "email": lead.get("email", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "is_duplicate": lead.get("is_duplicate", 0),
            "duplicate_reason": lead.get("duplicate_reason", ""),
            "segment": lead.get("segment", ""),
            "confidence": lead.get("confidence", ""),
            "needs_human_review": lead.get("needs_human_review", ""),
            "company_stage": lead.get("company_stage", ""),
            "recommended_offer": lead.get("recommended_offer", ""),
            "disqualify_reason": lead.get("disqualify_reason", ""),
            "built_with_ai_signals": _flatten(lead.get("built_with_ai_signals")),
            "technical_signals": _flatten(lead.get("technical_signals")),
            "pain_signals": _flatten(lead.get("pain_signals")),
            "evidence_quotes": _flatten(lead.get("evidence_quotes")),
            "personalization_hooks": _flatten(lead.get("personalization_hooks")),
            "scored_at": lead.get("scored_at", ""),
        }


def export_scores_csv(conn, output_path: str, session_id=None) -> int:
    """
    One row per lead (latest scoring verdict, via db.get_leads_with_scores).
    Leads that were never scored still appear, with empty verdict columns,
    to keep a complete trace of the batch (useful to spot FETCH_FAILED /
    SCORE_FAILED).

    Returns the number of rows written.
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS)
        writer.writeheader()
        for row in _iter_score_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def scores_csv_string(conn, session_id=None) -> str:
    """
    Same content as export_scores_csv, but returned as an in-memory string
    (no disk write) — for the Flask download button.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SCORE_FIELDS)
    writer.writeheader()
    for row in _iter_score_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 3) Human-readable CSV — one row per lead, designed to be scanned visually
#    (quick human review over a whole batch), not for line-by-line analysis
#    of the raw content (that remains the role of the scraping CSV above).
# ---------------------------------------------------------------------------

DEFAULT_PREVIEW_CHARS = 400

READABLE_FIELDS = [
    "lead_id", "company_name", "website_url", "status",
    "segment", "confidence", "needs_human_review", "recommended_offer", "disqualify_reason",
    "signals_summary", "github_check_summary",
    "homepage_preview", "about_preview", "product_preview", "pricing_preview", "careers_preview",
    "evidence_quotes", "personalization_hooks",
    "search_evidence",
]


def _preview(text, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """
    Truncated and cleaned preview of a page's content: newlines collapsed
    into single spaces (prevents a CSV cell from visually exploding across
    multiple lines in Excel/Sheets), cut at `max_chars` with an explicit "…"
    to signal that it is only an excerpt.
    """
    if not text:
        return "(page not found / not scraped)"
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + " …"


def _format_signals_summary(signals: dict) -> str:
    """
    Translates the `technical_signals` dict (raw JSON, designed for the LLM
    prompt) into a single sentence readable by a human in quick review. No
    interpretation added: only the signals already computed deterministically
    by scraper.py, formatted.
    """
    if not signals:
        return "No technical signals computed."

    parts = []

    if signals.get("generator_fingerprint"):
        parts.append(f"AI generator detected: {signals['generator_fingerprint']}")

    if signals.get("generator_meta_tag"):
        parts.append(f"<meta generator> tag: {signals['generator_meta_tag']}")

    fonts = signals.get("trend_fonts_found") or []
    if fonts:
        parts.append(f"Trend fonts: {', '.join(fonts)}")

    patterns = signals.get("visual_patterns_triggered") or []
    if patterns:
        parts.append(f"Visual patterns ({len(patterns)}/9): {', '.join(patterns)}")

    vibe = signals.get("vibe_language_matches") or []
    if vibe:
        parts.append(f"Explicit \"vibe-coding\" language: {', '.join(vibe)}")

    phrases = signals.get("ai_style_phrases_found") or []
    density = signals.get("ai_style_phrase_density")
    if phrases:
        shown = ", ".join(phrases[:5])
        suffix = ", ..." if len(phrases) > 5 else ""
        parts.append(f"Generic marketing phrases (density {density}): {shown}{suffix}")

    disclosures = signals.get("ai_authorship_disclosures_found") or []
    if disclosures:
        parts.append(f"Explicit mention of AI-generated content: {', '.join(disclosures)}")

    if signals.get("github_repo_url"):
        parts.append(f"Public GitHub repo: {signals['github_repo_url']}")

    if not parts:
        return "No deterministic technical signals detected."
    return " | ".join(parts)


def _format_github_check_summary(github_check) -> str:
    """Readable summary of the git check, if it was performed."""
    if not isinstance(github_check, dict) or not github_check.get("checked"):
        return ""
    evidence = github_check.get("evidence", {}) or {}
    parts = [f"{evidence.get('total_commits_seen', '?')} commits seen (API page)"]
    if evidence.get("single_commit_repo"):
        parts.append("⚠️ single-commit repo")
    first_msg = evidence.get("first_commit_message")
    if first_msg:
        parts.append(f'first commit: "{first_msg[:60]}"')
    return " | ".join(parts)


def _iter_readable_rows(conn, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS):
    """
    Row generator for the human-readable CSV — one row per lead. Groups the
    scraped pages by `source` (homepage/about/product/pricing/careers) to give
    each one its own preview column, instead of one row per page as in the raw
    scraping CSV.

    NB: pricing_preview and careers_preview for now remain a truncated preview
    of the raw text (not yet the targeted signal "self-serve vs sales-only"
    / "N engineering jobs" recommended — this point remains to be coded
    separately, in scraper.py, once the dedicated extractors are written).
    """
    leads = dbmod.get_leads_with_scores(conn, session_id=session_id)
    lead_ids = [lead["id"] for lead in leads]
    pages_map = dbmod.get_lead_content_map(conn, lead_ids)
    signals_map = dbmod.get_lead_technical_signals_map(conn, lead_ids)
    evidence_map = dbmod.get_lead_search_evidence_map(conn, lead_ids)

    for lead in leads:
        lead_id = lead["id"]
        pages_by_source = {}
        for page in pages_map.get(lead_id, []):
            pages_by_source[page.get("source", "")] = page.get("content", "")

        signals = signals_map.get(lead_id) or {}
        search_evidence_list = evidence_map.get(lead_id, [])
        search_summary_parts = []
        for ev in search_evidence_list:
            src = ev.get("source", "?")
            hits = ev.get("results") or []
            titles = [h.get("title", "") for h in hits if isinstance(h, dict)]
            search_summary_parts.append(f"{src}: {' | '.join(titles)}")
        search_summary = " ||| ".join(search_summary_parts) if search_summary_parts else ""

        yield {
            "lead_id": lead_id,
            "company_name": lead.get("company_name", ""),
            "website_url": lead.get("website_url", ""),
            "status": lead.get("status", ""),
            "segment": lead.get("segment", ""),
            "confidence": lead.get("confidence", ""),
            "needs_human_review": lead.get("needs_human_review", ""),
            "recommended_offer": lead.get("recommended_offer", ""),
            "disqualify_reason": lead.get("disqualify_reason", ""),
            "signals_summary": _format_signals_summary(signals),
            "github_check_summary": _format_github_check_summary(signals.get("github_check")),
            "homepage_preview": _preview(pages_by_source.get("homepage", ""), preview_chars),
            "about_preview": _preview(pages_by_source.get("about", ""), preview_chars),
            "product_preview": _preview(pages_by_source.get("product", ""), preview_chars),
            "pricing_preview": _preview(pages_by_source.get("pricing", ""), preview_chars),
            "careers_preview": _preview(pages_by_source.get("careers", ""), preview_chars),
            "evidence_quotes": _flatten(lead.get("evidence_quotes")),
            "personalization_hooks": _flatten(lead.get("personalization_hooks")),
            "search_evidence": search_summary,
        }


def export_readable_csv(conn, output_path: str, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> int:
    """
    One row per lead, designed to be opened in Excel/Sheets and scanned at a
    glance: segment/confidence first, technical signals translated into a
    single sentence, a short preview of each page (not the full raw text).

    It complements export_scraping_csv (full audit, one row per page) — not a
    replacement. Use this one for a quick review, the other to re-analyze the
    raw content in detail if needed.

    Returns the number of rows written (= number of leads).
    """
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=READABLE_FIELDS)
        writer.writeheader()
        for row in _iter_readable_rows(conn, session_id=session_id, preview_chars=preview_chars):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def readable_csv_string(conn, session_id=None, preview_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    """
    Same content as export_readable_csv, returned in memory (no disk write) —
    for the Flask download button.
    """
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=READABLE_FIELDS)
    writer.writeheader()
    for row in _iter_readable_rows(conn, session_id=session_id, preview_chars=preview_chars):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 4) SGAI web search CSV
# ---------------------------------------------------------------------------

SEARCH_FIELDS = [
    "lead_id", "company_name", "website_url", "source", "query",
    "result_url", "result_title", "result_snippet",
]


def _iter_search_rows(conn, session_id=None):
    """Row generator for the web search CSV — one row per result."""
    leads = dbmod.get_leads(conn, session_id=session_id, include_duplicates=False)
    evidence_map = dbmod.get_lead_search_evidence_map(conn, [lead["id"] for lead in leads])
    for lead in leads:
        evidence = evidence_map.get(lead["id"], [])
        for ev in evidence:
            source = ev.get("source", "")
            query = ev.get("query", "")
            results = ev.get("results") or []
            if isinstance(results, list):
                for hit in results:
                    yield {
                        "lead_id": lead["id"],
                        "company_name": lead.get("company_name", ""),
                        "website_url": lead.get("website_url", ""),
                        "source": source,
                        "query": query,
                        "result_url": hit.get("url", ""),
                        "result_title": hit.get("title", ""),
                        "result_snippet": (hit.get("content") or "")[:500],
                    }
            elif isinstance(results, dict) and "error" in results:
                yield {
                    "lead_id": lead["id"],
                    "company_name": lead.get("company_name", ""),
                    "website_url": lead.get("website_url", ""),
                    "source": source,
                    "query": query,
                    "result_url": "",
                    "result_title": "ERROR",
                    "result_snippet": results["error"],
                }

    # Also includes leads without search evidence (empty row with just the header)
    # to signal that they were scanned but did not trigger any search.


def export_search_csv(conn, output_path: str, session_id=None) -> int:
    """Exports the SGAI web search to CSV — one row per result."""
    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SEARCH_FIELDS)
        writer.writeheader()
        for row in _iter_search_rows(conn, session_id=session_id):
            writer.writerow(row)
            rows_written += 1
    return rows_written


def search_csv_string(conn, session_id=None) -> str:
    """Same content as export_search_csv, but in memory."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SEARCH_FIELDS)
    writer.writeheader()
    for row in _iter_search_rows(conn, session_id=session_id):
        writer.writerow(row)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI — to run the exports without going through Flask
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Exports the scraping and scoring results to CSV from the PostgreSQL database."
    )
    parser.add_argument("--scraping-out", default="scraping_results.csv", help="Output path for the scraping CSV")
    parser.add_argument("--scores-out", default="scores_results.csv", help="Output path for the scoring CSV")
    parser.add_argument("--search-out", default="search_results.csv", help="Output path for the web search CSV")
    args = parser.parse_args()

    conn = dbmod.get_connection()
    dbmod.init_db(conn)  # no-op if the tables already exist (CREATE TABLE IF NOT EXISTS)

    n_scraping = export_scraping_csv(conn, args.scraping_out)
    n_scores = export_scores_csv(conn, args.scores_out)
    n_search = export_search_csv(conn, args.search_out)

    print(f"[export] {n_scraping} rows written -> {args.scraping_out}")
    print(f"[export] {n_scores} rows written -> {args.scores_out}")
    print(f"[export] {n_search} rows written -> {args.search_out}")


if __name__ == "__main__":
    main()