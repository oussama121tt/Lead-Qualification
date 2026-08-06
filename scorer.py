"""
AI lead scoring via the Groq API (OpenAI SDK compatible).

Evaluates each lead from Apollo metadata, the scraped site content,
and the deterministic signals computed by scraper.py. Produces a structured
JSON verdict (segment, confidence, signals, hooks, disqualification).

Four input flows:
1. lead_metadata — name, title, company, email (from the Apollo CSV). The
   contact's title (e.g. "CTO" vs "Founder") is a direct signal for
   distinguishing technical_founder / ai_solo_founder, and must never be
   absent from the prompt (cf. original spec FR-3: "Input: lead metadata +
   parsed site text").
2. Scraped text (site content, first-party) — analyzed by the LLM for
   tone, specificity, explicit mentions, and signs of AI construction.
3. deterministic_signals (optional) — DOM/CSS/meta/git signals already
   computed by scraper.py, provided as-is with their explicit reliability
   level. The LLM must not re-derive these signals, only interpret them
   according to their indicated weight.
4. web_search_evidence (optional) — escalation web search results
   (LinkedIn, Product Hunt, GitHub, interviews), formatted and budgeted
   separately from the site content: THIRD-PARTY evidence, never silently
   merged into the site text as before (which lost title/url and the
   reliability distinction). Persisted in the DB (lead_search_evidence) and
   reloaded on a rescore, so this evidence is not lost between scoring runs.

Segments (aligned with the original spec, FR-3):
  ai_solo_founder | technical_founder | small_agency_scaling | too_big |
  wrong_field | unclear
Offers:
  ai_audit | general_audit | pipeline | none
"""

import json
import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

from constants import CONFIDENCE_THRESHOLD, VALID_SEGMENTS

MODEL = "llama-3.3-70b-versatile"
MAX_CONTENT_CHARS = 16000  # legacy — still used by the retry paths (site content only)
MAX_SITE_CONTENT_CHARS = 12000  # dedicated budget for the site (first-party) content
MAX_WEB_EVIDENCE_CHARS = 4000   # dedicated separate budget for web search (third-party) —
                                 # never overridden by a verbose site that would consume the whole quota
MAX_OUTPUT_TOKENS = 2048
RETRY_MAX_CONTENT_CHARS = 6000
RETRY_MAX_OUTPUT_TOKENS = 1024

# Confidence capped when the LLM returns an out-of-schema segment/offer —
# a verdict we just force-corrected cannot stay "confident".
INVALID_VERDICT_CONFIDENCE_CAP = 0.3

SYSTEM_PROMPT = """You are a senior analyst who evaluates B2B leads for a technical
development agency (RuyaTech). You receive the contact's Apollo metadata, the scraped
content of their website, and deterministic signals that have already been computed (do not
re-derive them).

THE TWO OFFERS WE SELL:
- Technical audit — for founders who have a fragile product behind a nice facade
  (ai_audit if built with AI by a non-technical person, general_audit if technical team
  but with tech debt/gaps).
- AI lead-gen pipeline — sold to agencies/studios that are scaling their own client
  acquisition (offer "pipeline").

OUR PRIMARY TARGET: non-technical founders who use AI to build
their product (vibe coding, Cursor, Bolt, Lovable, Replit, etc.). They need a technical
audit because their code lacks robustness.

SEGMENTS (choose EXACTLY ONE, never an invented value outside this list):
- ai_solo_founder — non-technical founder, product built with AI (vibe coding).
  → recommended_offer: ai_audit
- technical_founder — technical founder/team, uses AI as a dev tool (not as a
  crutch). → recommended_offer: general_audit
- small_agency_scaling — agency or services studio in a scaling phase (hiring, several
  visible clients, looking to industrialize). → recommended_offer: pipeline
- too_big — established company, size/maturity far above the targeted persona (large
  team, mature product for years, no sign of technical fragility).
  → recommended_offer: none
- wrong_field — sector unrelated to our offers (no software product, no technical
  site to audit). → recommended_offer: none
- unclear — INSUFFICIENT EVIDENCE to decide between the categories above. This is a normal
  and honest state, not a failure: use it whenever the content is too thin, too
  ambiguous, or contradictory to choose a segment with confidence. → needs_human_review
  necessarily true, recommended_offer usually none unless there is a partial exploitable signal.

Never confuse "unclear" (not enough evidence) with "wrong_field" (clear evidence this is
not our target) or "too_big" (clear evidence this is too big) — these three segments
say different things and must remain distinct.

RELIABILITY HIERARCHY OF DETERMINISTIC SIGNALS (provided at the end of the message) — respect
it strictly, never treat two signals of different strength as equivalents:
- STRONG (near-proof): generator_fingerprint non-null (direct reference to a tool like
  lovable.dev, bolt.new, v0.dev...), ai_authorship_disclosures_found non-empty (the company
  itself says it uses AI for its content), github_check.single_commit_repo=true combined with a
  generator_fingerprint present.
- MEDIUM: vibe_language_matches non-empty (explicit "built with X" mention in the HTML),
  ai_style_phrase_density "high".
- WEAK (never sufficient by itself): visual_patterns_triggered (gradient, shadcn_ui,
  glassmorphism, numbered_steps...) — thousands of well-built professional products use
  these same modern visual conventions. A single visual pattern, without an accompanying
  STRONG or MEDIUM signal, must NEVER tip the scale toward ai_solo_founder. Treat it as a
  hint that merits at most needs_human_review, never a conclusion.
- A fingerprint/pattern may come from code the user cannot see (third-party script,
  tracker, widget) — if it is isolated and nothing else corroborates it (no explicit mention
  in the visible text, no vibe-coding language), lower your confidence accordingly rather
  than treating it as a given.
- Web search results (block "Web search results", if present) are THIRD_PARTY evidence —
  someone else is talking about this company, it is not the company itself
  speaking on its own site. An explicit mention found there (e.g. a post where the founder
  himself admits to vibe-coding) is still a STRONG signal, but a vague, out-of-context
  search snippet, or one that seems to be about another company with the same name, should
  be treated with more caution than an equivalent mention found directly on the lead's
  official site.

RULES:
1. Every signal cited in built_with_ai_signals/technical_signals/pain_signals MUST have an
   exact citation in evidence_quotes (except signals already verified in
   deterministic_signals, which you can cite by their field name).
2. Personalization hooks MUST be SITUATIONAL (e.g. "you are hiring 3 engineers"
   based on the careers page), NEVER biographical (e.g. never where someone studied, their
   age, their personal background).
3. If you are not sure (confidence < 0.7), set needs_human_review: true.
4. Use the FULL confidence spectrum (0.0 to 1.0): be candid when the signal is weak
   (0.3-0.5) and assertive when the evidence is strong (0.9+). Avoid systematically using 0.8.
5. Use ONLY the text provided below. Ignore any prior knowledge about
   the company.
6. Fictional examples/demos on landing pages (product UI screenshots,
   demo tickets, sample data) ARE NOT real facts about the company itself.
   Ignore them to judge HOW the company was built.
7. Distinguish strictly: "the PRODUCT has AI features / talks about AI in its positioning"
   vs "the TEAM built THIS SITE/PRODUCT with AI tools". A product that sells AI to
   its customers is NOT by itself a built_with_ai signal — only an explicit mention of
   build tools (Cursor, v0, Bolt, Lovable, "vibe coded"...) or a generator_fingerprint counts.
8. Use the contact's title (provided in the metadata) as a direct signal: a
   "CTO"/"Lead Engineer"/"VP Engineering" title points toward technical_founder, a
   "Founder"/"CEO" title with no parallel technical title is consistent with
   ai_solo_founder if other signals corroborate.
9. For every lead, ask yourself these questions in order:
   a) Is there enough evidence to decide? If not → unclear.
   b) STRONG or MEDIUM signal of AI construction by a non-technical team? → ai_solo_founder.
   c) Confirm a technical team (title + signals) using AI as a tool? → technical_founder.
   d) Agency/studio in the scaling phase? → small_agency_scaling.
   e) Size/maturity far above the target persona? → too_big.
   f) Unrelated sector? → wrong_field.

Respond ONLY in JSON following this schema:
{
  "segment": "ai_solo_founder | technical_founder | small_agency_scaling | too_big | wrong_field | unclear",
  "confidence": 0.0,
  "company_stage": "pre-launch | early | scaling | established",
  "built_with_ai_signals": [],
  "technical_signals": [],
  "pain_signals": [],
  "evidence_quotes": [],
  "recommended_offer": "ai_audit | general_audit | pipeline | none",
  "personalization_hooks": [],
  "disqualify_reason": null,
  "needs_human_review": false
}"""

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")
    return _client


def _strip_images(text: str) -> str:
    """Removes image and media markers from the text before sending it to the LLM."""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'!\[.*?\]\s*\[.*?\]', '', text)
    text = re.sub(r'<(img|figure|picture|video|source|svg|canvas)[^>]*>', '', text)
    text = re.sub(r'</(img|figure|picture|video|source|svg|canvas)>', '', text)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'https?://\S+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)(?:\?\S*)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]\(\s*[^)]+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b\w+\.(?:png|jpe?g|gif|svg|webp|bmp|ico)(?:\?\S*)?', '', text, flags=re.IGNORECASE)
    return text


def _format_lead_metadata(lead_metadata: dict | None) -> str:
    """
    Formats the lead's Apollo metadata (name, title, company, email) into a
    text block for the prompt. Absent from the original FR-3 schema if we do
    not add it — notably the contact's title is a direct signal for
    distinguishing technical_founder from ai_solo_founder.
    """
    if not lead_metadata:
        return ""
    fields = [
        ("Name", " ".join(filter(None, [lead_metadata.get("first_name"), lead_metadata.get("last_name")])).strip()),
        ("Title", lead_metadata.get("title")),
        ("Company", lead_metadata.get("company_name")),
        ("Email", lead_metadata.get("email")),
        ("Website", lead_metadata.get("website_url")),
    ]
    lines = [f"- {label}: {value}" for label, value in fields if value]
    if not lines:
        return ""
    return "Contact metadata (Apollo source):\n" + "\n".join(lines)


def _format_web_search_evidence(web_search_evidence: dict | None, max_chars: int = MAX_WEB_EVIDENCE_CHARS) -> str:
    """
    Formats the web search results (escalation, LinkedIn/Product Hunt/
    GitHub/interviews) into a block DISTINCT from the site content.

    Why separate rather than merged into `rows` as before: the site is
    first-party evidence (the company talks about itself), the web search is
    often third-party (someone else talks about them, or a truncated
    out-of-context search snippet) — the LLM must be able to distinguish the
    two reliability levels, not treat them as equivalent. `title`/`url` are
    also preserved here (lost if simply concatenated into `rows` as before).

    Args:
        web_search_evidence: dict {source: [{"url":..,"title":..,"content":..}, ...]}
            — format returned by scraper.search_additional_evidence(), or
            rebuilt from db.get_lead_search_evidence() for a rescore.
        max_chars: dedicated budget, independent of the site content budget.
    """
    if not web_search_evidence:
        return ""

    chunks = []
    for source, hits in web_search_evidence.items():
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            content = (hit.get("content") or "").strip()
            if not content:
                continue
            title = hit.get("title", "")
            url = hit.get("url", "")
            chunks.append(f"[{source}] {title} ({url})\n{content}")

    if not chunks:
        return ""

    joined = "\n\n".join(chunks)[:max_chars]
    return (
        "Web search results (THIRD-PARTY evidence — someone else is talking about this "
        "company, this is NOT the lead's official site. To be treated with more caution "
        "than a mention found directly on the site):\n\n" + joined
    )


def rows_to_text(rows: list, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Concatenates the scraped pages into a single text block for the LLM prompt."""
    chunks = [f"## Source: {source}\n{content}" for source, _url, content in rows if content]
    full_text = "\n\n---\n\n".join(chunks)
    return _strip_images(full_text[:max_chars])


VALID_OFFERS = {"ai_audit", "general_audit", "pipeline", "none"}
VALID_STAGES = {"pre-launch", "early", "scaling", "established"}


def _validate_verdict(verdict: dict) -> dict:
    """Validates and fixes the enum fields of the LLM verdict.

    Important: when segment or recommended_offer is out of schema, we also
    cap `confidence` — a verdict that was just force-corrected cannot remain
    shown as "confident" (bug fixed: before, an invalid segment forced to
    "unclear" could keep its original confidence at 0.9, which is
    contradictory).
    """
    forced_correction = False

    if verdict.get("segment") not in VALID_SEGMENTS:
        verdict["segment"] = "unclear"
        verdict["needs_human_review"] = True
        note = "invalid_segment_fixed_to_unclear"
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note
        forced_correction = True

    if verdict.get("recommended_offer") not in VALID_OFFERS:
        verdict["recommended_offer"] = "none"
        forced_correction = True

    if verdict.get("company_stage") not in VALID_STAGES:
        verdict["company_stage"] = None

    if forced_correction:
        try:
            current_confidence = float(verdict.get("confidence", 0.0))
        except (TypeError, ValueError):
            current_confidence = 0.0
        verdict["confidence"] = min(current_confidence, INVALID_VERDICT_CONFIDENCE_CAP)
        verdict["needs_human_review"] = True

    return verdict


def _empty_verdict(disqualify_reason: str) -> dict:
    """Empty verdict for failure cases (no content, API error, etc.)."""
    return {
        "segment": "unclear",
        "confidence": 0.0,
        "company_stage": None,
        "built_with_ai_signals": [],
        "technical_signals": [],
        "pain_signals": [],
        "evidence_quotes": [],
        "recommended_offer": "none",
        "personalization_hooks": [],
        "disqualify_reason": disqualify_reason,
        "needs_human_review": True,
    }


def _is_rate_limit_error(e: Exception) -> bool:
    """Detects an API quota exceeded (429) to retry with reduced content."""
    status = getattr(e, "status_code", None)
    body = str(e)
    return status in (413, 429) or "rate_limit_exceeded" in body or "rate limit" in body


def _is_json_parse_error(e: Exception) -> bool:
    """Detects a non-JSON API response (truncation, malformation)."""
    status = getattr(e, "status_code", None)
    if status == 400:
        return True
    if isinstance(e, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
        return True
    return False


def _call_llm(user_content: str, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    """Calls Groq and parses the JSON response."""
    client = _get_client()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def _apply_confidence_guard(verdict: dict) -> dict:
    """Forces needs_human_review=True when the confidence is below the threshold."""
    if verdict.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        verdict["needs_human_review"] = True
    return verdict


def _normalize_for_grounding(s: str) -> str:
    """Light normalization to compare quotes against the source text."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _verify_evidence_grounding(verdict: dict, source_text: str) -> dict:
    """Checks that each evidence_quote appears word for word in the source text."""
    quotes = verdict.get("evidence_quotes") or []
    if not quotes:
        return verdict

    normalized_source = _normalize_for_grounding(source_text)
    grounded = []
    ungrounded = []
    for q in quotes:
        if isinstance(q, str) and _normalize_for_grounding(q) in normalized_source:
            grounded.append(q)
        else:
            ungrounded.append(q)

    if ungrounded:
        verdict["evidence_quotes"] = grounded
        verdict["needs_human_review"] = True
        note = f"ungrounded_evidence_quotes_removed: {len(ungrounded)} citation(s) not found in source text"
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note

    return verdict


def _retry_after_failure(rows, deterministic_signals, build_user_content, error_str) -> dict:
    """Retries the scoring with reduced content after a JSON parsing failure."""
    try:
        shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
        verdict = _call_llm(build_user_content(shorter_text), max_output_tokens=RETRY_MAX_OUTPUT_TOKENS)
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
        return _verify_evidence_grounding(verdict, shorter_text)
    except Exception as e2:
        return _empty_verdict(f"json_parse_failed: {error_str} | retry_error: {e2}")


def score_content(
    rows: list,
    deterministic_signals: dict | None = None,
    lead_metadata: dict | None = None,
    web_search_evidence: dict | None = None,
    scoring_criteria: list[str] | None = None,
    scoring_criteria_custom: str = "",
) -> dict:
    """Evaluates a lead from the Apollo metadata, the scraped content, and the
    deterministic signals.

    Args:
        rows: List of (source, url, content) tuples from the scraper — site
            content ONLY (first-party). Web search results must no longer be
            merged here, see web_search_evidence.
        deterministic_signals: Dict of DOM/CSS/meta/git signals computed by scraper.py.
        lead_metadata: Dict of the lead's Apollo fields (first_name, last_name, title,
            company_name, email, website_url) — cf. FR-3 of the spec,
            "Input: lead metadata + parsed site text". Missing until now, added here.
        web_search_evidence: Dict {source: [{"url","title","content"}, ...]} of
            web search results (escalation). Formatted and budgeted separately
            from the site content (third-party evidence, never silently merged
            as before — see _format_web_search_evidence).
        scoring_criteria: List of criteria selected by the user to guide the scoring.
        scoring_criteria_custom: Free text entered by the user for a custom criterion.

    Returns:
        Dict matching the JSON schema of the verdict (segment, confidence, etc.).
    """
    text = rows_to_text(rows, max_chars=MAX_SITE_CONTENT_CHARS)
    web_evidence_block = _format_web_search_evidence(web_search_evidence)

    if not text.strip() and not web_evidence_block:
        return _empty_verdict("no_content_scraped")

    def build_user_content(t: str) -> str:
        parts = []

        metadata_block = _format_lead_metadata(lead_metadata)
        if metadata_block:
            parts.append(metadata_block)

        if t.strip():
            parts.append(f"Information collected on this lead (official site):\n\n{t}")

        if web_evidence_block:
            parts.append(web_evidence_block)

        has_criteria = bool(scoring_criteria) or bool(scoring_criteria_custom)
        if has_criteria:
            criteria_block = "Scoring criteria selected by the user (give more weight to these criteria):\n"
            if scoring_criteria:
                criteria_desc = {
                    "ai_solo_founder": "PRIMARY TARGET: identify non-technical founders who build with AI (vibe coding, Cursor, Bolt, Lovable, Replit) — corresponds to the ai_solo_founder segment.",
                    "technical_founder": "SECONDARY TARGET: identify technical teams that use AI as a development tool — corresponds to the technical_founder segment.",
                    "solo_or_small": "Identify solo founders or micro-teams (1-5 people).",
                    "agency_or_studio": "Identify agencies / services studios that are scaling — corresponds to the small_agency_scaling segment.",
                    "no_ai": "Identify established companies with no AI-construction signal.",
                    "wrong_field": "Identify leads that are clearly not our target (too_big, wrong_field).",
                }
                for c in scoring_criteria:
                    desc = criteria_desc.get(c, c)
                    criteria_block += f"\n- {c}: {desc}"
            if scoring_criteria_custom:
                criteria_block += f"\n- Custom criterion: {scoring_criteria_custom}"
            parts.append(criteria_block)

        if deterministic_signals:
            signals_json = json.dumps(deterministic_signals, ensure_ascii=False, indent=2)
            parts.append(
                "Deterministic signals already verified (do not re-derive, do not invent "
                "beyond what follows — apply the reliability hierarchy STRONG/MEDIUM/WEAK "
                f"described in your instructions):\n{signals_json}"
            )

        return "\n\n---\n\n".join(parts)

    try:
        verdict = _call_llm(build_user_content(text))
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
        return _verify_evidence_grounding(verdict, text)
    except json.JSONDecodeError as e:
        return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e))
    except Exception as e:
        if _is_json_parse_error(e):
            return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e))
        if _is_rate_limit_error(e):
            try:
                shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
                verdict = _call_llm(build_user_content(shorter_text))
                verdict = _apply_confidence_guard(verdict)
                verdict = _validate_verdict(verdict)
                return _verify_evidence_grounding(verdict, shorter_text)
            except Exception as e2:
                return _empty_verdict(f"api_error_after_retry: {e2}")
        raise