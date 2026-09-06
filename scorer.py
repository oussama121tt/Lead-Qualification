"""
AI lead scoring via the llm_provider abstraction (Groq by default; switch to
Claude with SCORING_LLM_PROVIDER=anthropic in .env — no code change).

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
   separately from the site content with EQUAL weight (never silently
   merged into the site text as before, which lost title/url). Persisted in
   the DB (lead_search_evidence) and reloaded on a rescore, so this evidence
   is not lost between scoring runs.

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

load_dotenv()

from constants import CONFIDENCE_THRESHOLD, VALID_SEGMENTS
from llm_provider import get_llm_provider

MODEL = os.getenv("GROQ_SCORING_MODEL", "openai/gpt-oss-120b")
MAX_CONTENT_CHARS = 16000  # legacy — still used by the retry paths (site content only)
MAX_SITE_CONTENT_CHARS = 12000  # dedicated budget for the site (first-party) content
MAX_WEB_EVIDENCE_CHARS = 12000  # same dedicated budget as the site — web search carries
                                # equal weight, not a discounted subset
MAX_OUTPUT_TOKENS = 2048
RETRY_MAX_CONTENT_CHARS = 6000
RETRY_MAX_OUTPUT_TOKENS = 1024

# Hard upper bound for a single Groq call. Without it, the OpenAI SDK
# default (~600s) would leave a Stop clicked during scoring blocked for
# minutes — the pipeline cannot abort a blocking network call from outside.
GROQ_TIMEOUT_SECONDS = 90

# Confidence capped when the LLM returns an out-of-schema segment/offer —
# a verdict we just force-corrected cannot stay "confident".
INVALID_VERDICT_CONFIDENCE_CAP = 0.3


SYSTEM_PROMPT = """You are a senior B2B lead analyst for RuyaTech. Use only supplied Apollo metadata,
official site content, web evidence, and verified deterministic signals.
OFFERS: ai_audit is for an AI-built product owned by a non-technical founder; general_audit is
for a technical team needing architecture or security review; pipeline is for a scaling agency.
Choose exactly one segment: ai_solo_founder, technical_founder, small_agency_scaling, too_big,
wrong_field, or unclear. Map those segments to ai_audit, general_audit, pipeline, none, none,
and normally none. Unclear means insufficient evidence, not wrong_field or too_big.
STRONG signals are app_builder_fingerprint, explicit AI authorship, or a verified single-commit
GitHub repository combined with an app builder. site_builder_fingerprint (Framer/Webflow/Wix/
Squarespace/Carrd) is metadata only and never changes the segment. on_builder_subdomain=true is
near-proof of AI-build and early stage. MEDIUM signals are explicit vibe-language or high AI-style
density. Treat isolated evidence cautiously. Site and web evidence have equal weight; person_*
evidence describes the founder. Cursor alone never proves ai_solo_founder.
Describe the analyzed company, never clients or testimonials. Content marked [ATTRIBUTED QUOTE ...]
or [THIRD-PARTY CONTENT SECTION ...] is third-party unless its attribution names the analyzed
founder. Cite non-deterministic signals with exact evidence_quotes. Hooks are situational, never
biographical, and each must be {"hook":"...","based_on":"exact quote"}. Use only supplied content;
demos, product AI features, and client capabilities do not prove AI construction. Never invert a
capability into pain. Confidence below 0.7 requires needs_human_review=true. Decide in order:
enough evidence, AI-built non-technical team, technical team, scaling agency, too_big, wrong_field,
otherwise unclear.
Identify sensitive categories only when stated or clearly implied: minors, health_phi, biometric,
payments, identity_documents, financial, legal, location, employee_data, none. Set
sensitive_data_categories to a list of those exact keys and data_sensitivity_score from 0 to 100
for breach impact; use [] and 0 when none.
Set budget_signal to strong, moderate, weak, or none. Record paid pricing, hiring, funding, exits,
or enterprise logos in budget_evidence. Record nonprofit funding, student founder, side project,
default builder subdomain, or shrinking headcount in budget_blockers. A strong blocker caps the
budget signal at weak.

Respond ONLY with JSON using EXACTLY these keys (no others, no renaming):
{
  "segment": "ai_solo_founder | technical_founder | small_agency_scaling | too_big | wrong_field | unclear",
  "confidence": 0.0,
  "company_stage": "pre-launch | early | scaling | established",
  "built_with_ai_signals": [],
  "technical_signals": [],
  "pain_signals": [],
  "evidence_quotes": [],
  "recommended_offer": "ai_audit | general_audit | pipeline | none",
  "personalization_hooks": [{"hook": "...", "based_on": "exact verbatim quote from the content"}],
  "sensitive_data_categories": [],
  "data_sensitivity_score": 0,
  "budget_signal": "strong | moderate | weak | none",
  "budget_evidence": [],
  "budget_blockers": [],
  "disqualify_reason": null,
  "needs_human_review": false
}"""

# Every key the parser reads. A test asserts each one is named in the prompt,
# so a future "prompt diet" can never silently drop the schema again (this
# exact regression happened: the model started returning "hooks"/"offer" and
# every verdict parsed as empty with confidence 0).
SCHEMA_KEYS = (
    "segment", "confidence", "company_stage", "built_with_ai_signals",
    "technical_signals", "pain_signals", "evidence_quotes", "recommended_offer",
    "personalization_hooks", "sensitive_data_categories", "data_sensitivity_score",
    "budget_signal", "budget_evidence", "budget_blockers", "disqualify_reason",
    "needs_human_review",
)

# Defensive key normalization: common aliases a model may emit for our
# schema keys. Applied BEFORE validation, and the verdict is flagged when
# any alias was needed — drift is corrected AND visible, never silent.
_KEY_ALIASES = {
    "hooks": "personalization_hooks",
    "personalisation_hooks": "personalization_hooks",
    "offer": "recommended_offer",
    "recommended_offering": "recommended_offer",
    "quotes": "evidence_quotes",
    "evidence": "evidence_quotes",
    "stage": "company_stage",
    "ai_signals": "built_with_ai_signals",
    "built_with_ai": "built_with_ai_signals",
    "tech_signals": "technical_signals",
    "pains": "pain_signals",
    "pain": "pain_signals",
    "sensitive_data": "sensitive_data_categories",
    "sensitivity_score": "data_sensitivity_score",
    "budget": "budget_signal",
    "human_review": "needs_human_review",
    "needs_review": "needs_human_review",
    "disqualify": "disqualify_reason",
    "reason": "disqualify_reason",
}


def _normalize_verdict_keys(verdict: dict) -> dict:
    """Map known aliases onto the canonical schema keys. Records which
    aliases were used in disqualify_reason so the drift is auditable."""
    if not isinstance(verdict, dict):
        return verdict
    renamed = []
    for alias, canon in _KEY_ALIASES.items():
        if alias in verdict and canon not in verdict:
            verdict[canon] = verdict.pop(alias)
            renamed.append(f"{alias}->{canon}")
    if renamed:
        note = "schema_key_aliases_normalized: " + ", ".join(renamed)
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note
    return verdict


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
    if lead_metadata.get("apollo_email_status"):
        lines.append(f"- Email status (Apollo): {lead_metadata['apollo_email_status']}")

    person = lead_metadata.get("apollo_person") or {}
    if isinstance(person, dict) and person:
        if person.get("seniority"):
            lines.append(f"- Seniority: {person['seniority']}")
        if person.get("headline"):
            lines.append(f"- Headline: {person['headline']}")
        loc = ", ".join(filter(None, [person.get("city"), person.get("country")]))
        if loc:
            lines.append(f"- Location: {loc}")
        history = person.get("employment_history") or []
        if history:
            lines.append("- Career history (most recent first; this is the founder's OWN background: "
                         "engineering/CTO roles point to technical_founder, non-technical roles "
                         "with an AI-built product point to ai_solo_founder):")
            for e in history[:8]:
                end = e.get("end") or ("now" if e.get("current") else "?")
                lines.append(f"    * {e.get('title') or '?'} @ {e.get('organization') or '?'} ({e.get('start') or '?'} to {end})")

    org = lead_metadata.get("apollo_org") or {}
    if isinstance(org, dict) and org:
        facts = []
        if org.get("employees") is not None:
            facts.append(f"employees={org['employees']}")
        if org.get("founded_year"):
            facts.append(f"founded={org['founded_year']}")
        if org.get("industry"):
            facts.append(f"industry={org['industry']}")
        if org.get("headcount_growth_6m") is not None:
            facts.append(f"headcount_growth_6m={org['headcount_growth_6m']}")
        if org.get("revenue"):
            facts.append(f"revenue={org['revenue']}")
        if facts:
            lines.append("- Company facts (Apollo): " + ", ".join(facts))
        if org.get("keywords"):
            lines.append("- Company keywords: " + ", ".join(str(k) for k in org["keywords"][:12]))

    if not lines:
        return ""
    return "Contact metadata (Apollo source):\n" + "\n".join(lines)


def _format_web_search_evidence(web_search_evidence: dict | None, max_chars: int = MAX_WEB_EVIDENCE_CHARS) -> str:
    """
    Formats the web search results (escalation, LinkedIn/Product Hunt/
    GitHub/interviews) into a block DISTINCT from the site content.

    Why separate rather than merged into `rows` as before: `title`/`url`
    are lost if simply concatenated into `rows`, so the model could not
    point back to the source. Kept as its own block, it is presented with
    the same weight as the site content (both are grounding sources for
    `evidence_quotes`).

    """
    if not web_search_evidence:
        return ""

    chunks = []
    ordered_sources = sorted(
        web_search_evidence.items(),
        key=lambda kv: (0, kv[0]) if kv[0].startswith("person_") else (1, kv[0]),
    )
    for source, hits in ordered_sources:
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
        "Web search results (LinkedIn — company page and the founder's own profile "
        "(person_*), Product Hunt, GitHub, interviews, directories — same weight as "
        "the site content above):\n\n" + joined
    )


def rows_to_text(rows: list, max_chars: int = MAX_CONTENT_CHARS) -> str:
    """Concatenates the scraped pages into a single text block for the LLM prompt.

    Accepts both (source, url, content) tuples (scraper output) and dict rows
    ({"source": ..., "url": ..., "content": ...} — db.get_lead_content on a
    rescore). Fixed bug: dict rows were unpacked as their KEYS when iterated,
    so a rescore silently fed placeholder text ("## Source: source") to the LLM.
    """
    chunks = []
    for row in rows:
        if isinstance(row, dict):
            source = row.get("source")
            content = row.get("content")
        else:
            source, _url, content = row
        if content:
            chunks.append(f"## Source: {source}\n{content}")
    full_text = "\n\n---\n\n".join(chunks)
    return _strip_images(full_text[:max_chars])


VALID_OFFERS = {"ai_audit", "general_audit", "pipeline", "none"}
VALID_STAGES = {"pre-launch", "early", "scaling", "established"}
VALID_SENSITIVE_DATA_CATEGORIES = {
    "minors", "health_phi", "biometric", "payments", "identity_documents",
    "financial", "legal", "location", "employee_data", "none",
}
VALID_BUDGET_SIGNALS = {"strong", "moderate", "weak", "none"}


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

    categories = verdict.get("sensitive_data_categories", [])
    if isinstance(categories, str):
        try:
            categories = json.loads(categories)
        except (json.JSONDecodeError, TypeError):
            categories = [categories]
    if not isinstance(categories, list):
        categories = []
    verdict["sensitive_data_categories"] = [
        category for category in categories
        if isinstance(category, str) and category in VALID_SENSITIVE_DATA_CATEGORIES
    ]
    if "none" in verdict["sensitive_data_categories"] and len(verdict["sensitive_data_categories"]) > 1:
        verdict["sensitive_data_categories"].remove("none")
    try:
        sensitivity_score = int(float(verdict.get("data_sensitivity_score", 0)))
    except (TypeError, ValueError):
        sensitivity_score = 0
    verdict["data_sensitivity_score"] = max(0, min(100, sensitivity_score))

    budget_signal = verdict.get("budget_signal", "none")
    verdict["budget_signal"] = budget_signal if budget_signal in VALID_BUDGET_SIGNALS else "none"
    for field in ("budget_evidence", "budget_blockers"):
        value = verdict.get(field, [])
        verdict[field] = value if isinstance(value, list) else []

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
        "sensitive_data_categories": [],
        "data_sensitivity_score": 0,
        "budget_signal": "none",
        "budget_evidence": [],
        "budget_blockers": [],
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


def _call_llm(user_content: str, max_output_tokens: int = MAX_OUTPUT_TOKENS,
              cost_cb=None) -> dict:
    """Calls the configured scoring LLM (llm_provider) and parses the JSON
    response. cost_cb, when provided, receives (meta, latency_ms) after every
    call — including retries — so no LLM spend is ever unlogged (FR-7)."""
    import time as _time
    provider = get_llm_provider("scoring")
    t0 = _time.monotonic()
    data, meta = provider.generate_json(
        user_content,
        system=SYSTEM_PROMPT,
        temperature=0.2,
        max_tokens=max_output_tokens,
    )
    if cost_cb is not None:
        try:
            cost_cb(meta, int((_time.monotonic() - t0) * 1000))
        except Exception:
            pass  # cost logging must never fail a scoring call
    return _normalize_verdict_keys(data)


def _apply_confidence_guard(verdict: dict) -> dict:
    """Forces needs_human_review=True when the confidence is below the threshold."""
    if verdict.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        verdict["needs_human_review"] = True
    return verdict


_QUOTE_CHARS = "\"'“”‘’«»`‹›"
_QUOTE_TRANS = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "«": '"', "»": '"', " ": " ", "…": "...",
})


def _normalize_for_grounding(s: str) -> str:
    """Normalization used on BOTH the model's citation and the source text
    before the verbatim check: lowercase, whitespace-collapsed, curly quotes
    and non-breaking spaces folded to ASCII, and any WRAPPING quotation marks
    stripped. Models routinely return citations as "\"exact text\"" — without
    this, a real citation failed the check on its quote characters alone and
    good hooks were silently discarded (observed live on gpt-oss-120b)."""
    t = (s or "").translate(_QUOTE_TRANS).strip().strip(_QUOTE_CHARS).strip()
    return re.sub(r"\s+", " ", t.lower())


# Same tag names as scraper.py's _tag_attributed_content — kept in sync
# manually since scorer.py has no import dependency on scraper.py by design
# (scoring must be testable/runnable without the scraping stack).
_THIRD_PARTY_BLOCK_RE = re.compile(
    r"\[(?:ATTRIBUTED QUOTE[^\]]*|THIRD-PARTY CONTENT SECTION[^\]]*)\](.*?)"
    r"\[/(?:ATTRIBUTED QUOTE|THIRD-PARTY CONTENT SECTION)\]",
    re.DOTALL,
)


def _third_party_spans(source_text: str, lead_metadata: dict | None) -> list[tuple[int, int]]:
    """Character spans (in the ORIGINAL, non-normalized source_text) that are
    structurally tagged as testimonial/case-study/portfolio content (by
    scraper.py's _tag_attributed_content) AND are not attributed to the lead
    itself.

    Two block shapes are tagged, with different attribution evidence:

    - [ATTRIBUTED QUOTE ...] blockquotes: the attribution is the trailing
      name/title/company line(s) that scraper.py pulls into the tag after the
      quoted lines (e.g. "— Oussama, Founder, RuyaTech"). The lead's own
      name/company must appear in that ATTRIBUTION, not merely inside the
      quoted text: client testimonials frequently mention the founder's
      first name in the quote body ("Oussama launched it in two weeks")
      while being attributed to someone else — those stay excluded.

    - [THIRD-PARTY CONTENT SECTION ...] heading sections: attribution
      evidence is limited to the HEADING and the first content line. A
      section opened by "## Testimonials"/"## Success stories"/"## Our work"
      is third-party by structure; a founder name or company mention
      LATER in the section (quote bodies, closing boilerplate like
      "founded by Oussama") must NOT rescue it. Only a section that
      names/discloses the lead up front (heading + first line,
      e.g. "## What we've built by RuyaTech") is treated as the lead's own.

    This is a code-level filter: it does not depend on the LLM having
    correctly judged the block, so it still catches a hallucinated
    first-party attribution even if the model's own reasoning missed it.
    """
    spans: list[tuple[int, int]] = []
    own_names = set()
    if lead_metadata:
        for key in ("first_name", "last_name", "company_name"):
            v = (lead_metadata.get(key) or "").strip().lower()
            if v and len(v) > 1:
                own_names.add(v)
    for m in _THIRD_PARTY_BLOCK_RE.finditer(source_text):
        block = m.group(1)
        if m.group(0).startswith("[ATTRIBUTED QUOTE"):
            # Attribution = the trailing non-blockquote lines after the
            # quoted text (name/title/company). No attribution -> treat as
            # testimonial (consistent with the structural rule "blockquotes
            # are almost always testimonials"), never as the lead's own quote.
            trailing = []
            for ln in reversed(block.splitlines()):
                if not ln.strip():
                    continue
                if ln.lstrip().startswith(">"):
                    break
                trailing.append(ln.strip())
            attribution = " ".join(reversed(trailing)).lower()
        else:
            # Section: attribution evidence = heading + first content line,
            # before any quoted block (quote bodies are third-party content,
            # not self-attribution).
            opening = []
            for ln in block.splitlines():
                stripped = ln.strip()
                if not stripped:
                    continue
                if stripped.lstrip().startswith(">") or len(opening) >= 2:
                    break
                opening.append(stripped)
            attribution = " ".join(opening).lower()
        if own_names and attribution and any(name in attribution for name in own_names):
            continue
        spans.append((m.start(1), m.end(1)))
    return spans


def _quote_is_third_party(quote: str, source_text: str, spans: list[tuple[int, int]]) -> bool:
    """True if `quote` (as found in source_text, case-insensitive) falls
    inside one of the excluded third-party spans."""
    if not spans or not quote:
        return False
    idx = source_text.lower().find(quote.lower())
    if idx == -1:
        return False
    quote_end = idx + len(quote)
    return any(start <= idx < end or start < quote_end <= end for start, end in spans)


def _verify_evidence_grounding(verdict: dict, source_text: str, lead_metadata: dict | None = None) -> dict:
    """Checks that each evidence_quote appears word for word in the source
    text, AND (code-level, not prompt-dependent) that it does not fall
    inside a testimonial/case-study block describing a third party.
    """
    quotes = verdict.get("evidence_quotes") or []
    if not quotes:
        return verdict

    normalized_source = _normalize_for_grounding(source_text)
    spans = _third_party_spans(source_text, lead_metadata)
    grounded = []
    ungrounded = []
    third_party = []
    for q in quotes:
        if not isinstance(q, str):
            ungrounded.append(q)
            continue
        if _normalize_for_grounding(q) not in normalized_source:
            ungrounded.append(q)
        elif _quote_is_third_party(q, source_text, spans):
            third_party.append(q)
        else:
            grounded.append(q)

    if ungrounded or third_party:
        verdict["evidence_quotes"] = grounded
        verdict["needs_human_review"] = True
        notes = []
        if ungrounded:
            notes.append(f"ungrounded_evidence_quotes_removed: {len(ungrounded)} citation(s) not found in source text")
        if third_party:
            notes.append(f"third_party_evidence_quotes_removed: {len(third_party)} citation(s) described a testimonial/case-study subject, not the analyzed company")
        note = " | ".join(notes)
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note

    return verdict


def _verify_hooks_grounding(verdict: dict, source_text: str, lead_metadata: dict | None = None) -> dict:
    """Checks that each personalization_hook carries a "based_on" quote that
    (a) exists word for word in the source text, and (b) does not fall
    inside a third-party testimonial/case-study block. A hook failing either
    check is dropped — this is the code-level backstop for rules 10/11,
    which does not rely on the model having followed them correctly.

    Accepts both the new schema (list of {"hook", "based_on"}) and, for
    backward compatibility with any verdict that slipped through as plain
    strings (e.g. an older cached result, or a model that ignored the
    schema), treats a bare string hook as ungrounded by default rather than
    crashing — it is dropped, not silently trusted.
    """
    hooks = verdict.get("personalization_hooks") or []
    if not hooks:
        return verdict

    normalized_source = _normalize_for_grounding(source_text)
    spans = _third_party_spans(source_text, lead_metadata)
    kept = []
    dropped_count = 0
    for h in hooks:
        if not isinstance(h, dict):
            dropped_count += 1
            continue
        based_on = h.get("based_on")
        hook_text = h.get("hook")
        if not hook_text or not isinstance(based_on, str):
            dropped_count += 1
            continue
        if _normalize_for_grounding(based_on) not in normalized_source:
            dropped_count += 1
            continue
        if _quote_is_third_party(based_on, source_text, spans):
            dropped_count += 1
            continue
        kept.append(h)

    if dropped_count:
        verdict["personalization_hooks"] = kept
        verdict["needs_human_review"] = True
        note = f"ungrounded_or_third_party_hooks_removed: {dropped_count} hook(s) discarded (no valid first-party citation)"
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note

    return verdict

    return verdict


def _retry_after_failure(rows, deterministic_signals, build_user_content, error_str, grounding_source, site_content_missing=False, lead_metadata=None, cost_cb=None) -> dict:
    """Retries the scoring with reduced content after a JSON parsing failure."""
    try:
        shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
        verdict = _call_llm(build_user_content(shorter_text), max_output_tokens=RETRY_MAX_OUTPUT_TOKENS, cost_cb=cost_cb)
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
        verdict = _verify_evidence_grounding(verdict, grounding_source, lead_metadata)
        verdict = _verify_hooks_grounding(verdict, grounding_source, lead_metadata)
        return _apply_site_missing_guard(verdict, site_content_missing)
    except Exception as e2:
        return _apply_site_missing_guard(
            _empty_verdict(f"json_parse_failed: {error_str} | retry_error: {e2}"),
            site_content_missing,
        )


def _apply_site_missing_guard(verdict: dict, site_content_missing: bool) -> dict:
    """Structural backstop for leads scored without official site content.

    A verdict computed on metadata + web evidence alone can never auto-pass:
    human review is mandatory REGARDLESS of what the LLM says — and the
    limitation is traced in disqualify_reason so a reviewer knows WHY the
    review is required (previously this only happened by luck, e.g. when an
    ungrounded evidence quote happened to trip the general confidence guard).

    The note is ALWAYS appended when the flag is set — even when
    needs_human_review is already True (the typical case: the LLM or the
    confidence guard already flagged it). The traceability must not depend
    on the flag being the FIRST mechanism to trip.
    """
    if not site_content_missing:
        return verdict
    note = (
        "site_content_missing: no official site content available (empty or fetch failed) "
        "— verdict relies on metadata and web evidence only"
    )
    verdict["needs_human_review"] = True
    if note not in (verdict.get("disqualify_reason") or ""):
        existing = verdict.get("disqualify_reason")
        verdict["disqualify_reason"] = f"{existing} | {note}" if existing else note
    return verdict


SITE_MISSING_INSTRUCTION = (
    "IMPORTANT — the official website could not be scraped properly (empty content or fetch "
    "failure), so NO reliable site content is available for this lead.\n"
    "- Base your verdict ONLY on the contact metadata and the web search results provided. "
    "Do not invent or assume site content.\n"
    "- Do NOT treat the absence of site content as a signal in either direction: a site can "
    "be temporarily down or blocked without saying anything about the product's quality.\n"
    "- If your verdict relies solely on web evidence with no official site content, set "
    "needs_human_review to true REGARDLESS of confidence, and mention this limitation "
    "explicitly in disqualify_reason."
)


def score_content(
    rows: list,
    deterministic_signals: dict | None = None,
    lead_metadata: dict | None = None,
    web_search_evidence: dict | None = None,
    scoring_criteria: list[str] | None = None,
    scoring_criteria_custom: str = "",
    site_content_missing: bool = False,
    cost_cb=None,
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
            from the site content, with equal weight (both blocks are used for
            evidence grounding), see _format_web_search_evidence.
        scoring_criteria: List of criteria selected by the user to guide the scoring.
        scoring_criteria_custom: Free text entered by the user for a custom criterion.
        site_content_missing: True when the official site produced no usable
            content (empty rows / FETCH_FAILED). The prompt is then told
            explicitly — the LLM must not silently treat the absence as a
            neutral omission — and the verdict is FORCED to
            needs_human_review=True regardless of confidence, with the
            limitation traced in disqualify_reason (see
            _apply_site_missing_guard and SITE_MISSING_INSTRUCTION).
        cost_cb: optional callable (meta, latency_ms) invoked after EVERY LLM
            call this evaluation makes (retries included) — the pipeline uses
            it to log tokens/cost per lead and enforce the session budget cap
            (FR-7). Never raises into the scoring path.

    Returns:
        Dict matching the JSON schema of the verdict (segment, confidence, etc.).
    """
    text = rows_to_text(rows, max_chars=MAX_SITE_CONTENT_CHARS)
    web_evidence_block = _format_web_search_evidence(web_search_evidence)
    grounding_source = "\n\n---\n\n".join(p for p in (text, web_evidence_block) if p.strip())

    if not text.strip() and not web_evidence_block:
        return _apply_site_missing_guard(_empty_verdict("no_content_scraped"), site_content_missing)

    def build_user_content(t: str) -> str:
        parts = []

        metadata_block = _format_lead_metadata(lead_metadata)
        if metadata_block:
            parts.append(metadata_block)

        if t.strip():
            parts.append(f"Information collected on this lead (official site):\n\n{t}")

        if web_evidence_block:
            parts.append(web_evidence_block)

        if site_content_missing:
            parts.append(SITE_MISSING_INSTRUCTION)

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
        verdict = _call_llm(build_user_content(text), cost_cb=cost_cb)
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
        verdict = _verify_evidence_grounding(verdict, grounding_source, lead_metadata)
        verdict = _verify_hooks_grounding(verdict, grounding_source, lead_metadata)
        return _apply_site_missing_guard(verdict, site_content_missing)
    except json.JSONDecodeError as e:
        return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e), grounding_source, site_content_missing, lead_metadata, cost_cb)
    except Exception as e:
        if _is_json_parse_error(e):
            return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e), grounding_source, site_content_missing, lead_metadata, cost_cb)
        if _is_rate_limit_error(e):
            try:
                shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
                verdict = _call_llm(build_user_content(shorter_text), cost_cb=cost_cb)
                verdict = _apply_confidence_guard(verdict)
                verdict = _validate_verdict(verdict)
                verdict = _verify_evidence_grounding(verdict, grounding_source, lead_metadata)
                verdict = _verify_hooks_grounding(verdict, grounding_source, lead_metadata)
                return _apply_site_missing_guard(verdict, site_content_missing)
            except Exception as e2:
                return _apply_site_missing_guard(
                    _empty_verdict(f"api_error_after_retry: {e2}"),
                    site_content_missing,
                )
        raise