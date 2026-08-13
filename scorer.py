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
from openai import OpenAI

load_dotenv()

from constants import CONFIDENCE_THRESHOLD, VALID_SEGMENTS

MODEL = "llama-3.3-70b-versatile"
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
- Both evidence blocks carry EQUAL WEIGHT: "Information collected on this lead" (the
  official site) and "Web search results" (LinkedIn, Product Hunt, GitHub, interviews,
  directories) — the web can be the ONLY source that reveals vibe-coding, a technical
  team, or an agency, so NEVER discount it because it is not the official site.
  An explicit mention found in either block (e.g. a post where the founder
  himself admits to vibe-coding) is a STRONG signal regardless of which block it
  comes from. Conversely, a vague, out-of-context search snippet, or one that seems to
  be about another company with the same name, carries equal doubt whether it appears
  in the site content or in the web results.
- Evidence labeled "person_*" (person_linkedin, person_github) describes the founder
  HIMSELF (his own LinkedIn profile, his own GitHub) — treat it as a PRIORITY signal
  to distinguish technical_founder from ai_solo_founder, more reliable than a signal
  inferred from the company's site: a founder's own profile showing engineering work,
  commits, or a technical history points toward technical_founder; a profile with no
  technical trace while the product is AI-built points toward ai_solo_founder.

- CURSOR - SPECIAL RULE (weaker than the Lovable/Bolt/v0 fingerprints): Cursor is a
  general-purpose IDE that leaves no detectable HTML fingerprint on the site (unlike
  "lovable-tagger" or "v0.dev" client scripts), and it is used by BOTH non-technical
  founders and very experienced engineers. A bare mention of Cursor ("built with
  cursor" in the site text or in the web search results) is therefore NEVER
  sufficient BY ITSELF to classify the lead as ai_solo_founder. It MUST be
  corroborated by at least one of: github_check.single_commit_repo = true, OR a
  founder's own profile (person_linkedin / person_github) showing an absence of
  technical background. Without such corroboration, a Cursor mention alone orients
  toward "unclear" (insufficient evidence) or "technical_founder" depending on the
  other signals - never ai_solo_founder on its own.

FIRST-PARTY VS. CLIENT CONTENT — CRITICAL DISTINCTION:
Scraped site content often mixes two different voices: the company describing ITSELF,
and the company describing ITS OWN CLIENTS (testimonials, case studies, portfolio
items, "what we built for X"). This is especially common for agencies/studios
(small_agency_scaling), whose entire site is often built around client success
stories.
- built_with_ai_signals, technical_signals, and pain_signals must describe the
  ANALYZED COMPANY ITSELF — never a client mentioned in a testimonial, case study,
  or portfolio entry.
- A phrase like "we rescue broken products" or "our client's MVP was falling apart"
  describes a SERVICE OFFERED TO OTHERS, not a problem the analyzed company itself
  has. Do not extract this as a pain_signal for the analyzed company.
- A testimonial quote from a named client ("I built my MVP with vibe coding...") is
  evidence about THAT CLIENT, not about the site's owner — never attribute it to the
  company being scored.
- Before extracting any signal, ask: "is this text describing the company I am
  scoring, or a business it works with / has worked with?" If it's the latter,
  discard it for built_with_ai_signals/technical_signals/pain_signals — it can still
  inform company_stage or segment (e.g. many detailed case studies suggest an
  established agency), but must not be cited as if it were first-party evidence
  about the analyzed company.

STRUCTURAL DETECTION — do not rely on specific wording, rely on these PATTERNS
(they recur across virtually every agency/services site, regardless of the exact
vocabulary used):
1. Markdown blockquotes (lines starting with ">") are almost always testimonials —
   treat their content as evidence about the QUOTED PERSON/COMPANY, never about the
   site owner, regardless of what the quote says or who appears to be "speaking".
2. Any block immediately followed or preceded by a name + title + company line
   (e.g. "Jane D. — Founder, Acme") is an attributed quote. The site owner is
   never the subject of an attributed quote's content, even if grammatically
   first-person ("I built...", "We were struggling...").
3. Sections under headers containing words like "Testimonial", "Case Stud*",
   "Portfolio", "What We've Built", "Client*", "What Founders/Clients Say",
   "Success Stor*", "Our Work" describe THIRD PARTIES (past or prospective
   clients), never the site owner.
4. A narrative in past tense introducing an unnamed or named third party ("A
   founder came to us with...", "A client was losing...", "One of our clients...")
   is a case study about that third party — the problem described belongs to
   them, not to the site owner, even when the same sentence later describes what
   the site owner did about it.
5. Present-tense capability statements ("We rescue X", "We fix X", "We help
   companies that struggle with X") describe a SERVICE OFFERED, not a problem the
   site owner has — this holds regardless of which specific problem X is named.
Apply these five structural patterns to ANY site, not just ones matching a
specific vocabulary — the test is the STRUCTURE (quotation, attribution, section
header, narrative tense, capability framing), never a fixed list of phrases.

RULES:
1. Every signal cited in built_with_ai_signals/technical_signals/pain_signals MUST have an
   exact citation in evidence_quotes (except signals already verified in
   deterministic_signals, which you can cite by their field name), AND must pass the
   first-party check above — never a quote describing a client or case study subject.
2. Personalization hooks MUST be SITUATIONAL (e.g. "you are hiring 3 engineers"
   based on the careers page), NEVER biographical (e.g. never where someone studied, their
   age, their personal background). Content wrapped in [ATTRIBUTED QUOTE ...] or
   [THIRD-PARTY CONTENT SECTION ...] markers (added upstream by the scraper) has been
   structurally flagged as a likely testimonial/case-study/portfolio block: check the
   attributed name/company against lead_metadata — if it matches the analyzed company's
   own founder/name, treat the content as first-party as usual; if it names someone else
   (a different founder, a different company), it is third-party and must be excluded from
   built_with_ai_signals/technical_signals/pain_signals exactly like any other client
   testimonial. Never surface the literal marker text itself in evidence_quotes or hooks.
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
10. Every personalization_hook MUST be an object {"hook": "...", "based_on": "..."} where
    "based_on" is an EXACT, word-for-word quote copied from the content provided (not a
    paraphrase) that the hook is built from. A hook without a verbatim "based_on" citation
    will be programmatically discarded, regardless of how well-written it is — this is
    enforced in code, not just a style preference. Test before writing a hook: could you
    point to the exact sentence it comes from? If not, do not generate it.
11. NEVER invert a capability statement into an assumed pain. If the site says "we do
    X for our clients" (a service offered), that does NOT mean the analyzed company
    itself needs X or has the problem X solves — that would be projecting the
    company's own marketing pitch back onto itself. A personalization_hook may only
    claim the analyzed company "has" a problem, a need, or an experience if "based_on"
    is a quote that directly states this about the company itself (e.g. its own careers
    page, its own tech stack, its own product state) — never derived by flipping a
    description of what it sells or does for others. Note: even a well-formed "based_on"
    citation gets discarded downstream if it falls inside a testimonial/case-study block
    describing someone other than the analyzed company (see the [ATTRIBUTED QUOTE]/
    [THIRD-PARTY CONTENT SECTION] markers in the content) — so citing such a block does
    not satisfy rule 10 either.

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
  "personalization_hooks": [{"hook": "...", "based_on": "exact verbatim quote from the content"}],
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

    Why separate rather than merged into `rows` as before: `title`/`url`
    are lost if simply concatenated into `rows`, so the model could not
    point back to the source. Kept as its own block, it is presented with
    the same weight as the site content (both are grounding sources for
    `evidence_quotes`).

    Args:
        web_search_evidence: dict {source: [{"url":..,"title":..,"content":..}, ...]}
            — format returned by scraper.search_additional_evidence(), or
            rebuilt from db.get_lead_search_evidence() for a rescore.
        max_chars: dedicated budget, independent of the site content budget.
    """
    if not web_search_evidence:
        return ""

    chunks = []
    # person_* sources describe the founder himself (his own LinkedIn/GitHub)
    # — ordered FIRST, they are the priority signal for distinguishing
    # technical_founder vs ai_solo_founder.
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
        timeout=GROQ_TIMEOUT_SECONDS,
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


def _retry_after_failure(rows, deterministic_signals, build_user_content, error_str, grounding_source, site_content_missing=False, lead_metadata=None) -> dict:
    """Retries the scoring with reduced content after a JSON parsing failure."""
    try:
        shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
        verdict = _call_llm(build_user_content(shorter_text), max_output_tokens=RETRY_MAX_OUTPUT_TOKENS)
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
        verdict = _call_llm(build_user_content(text))
        verdict = _apply_confidence_guard(verdict)
        verdict = _validate_verdict(verdict)
        verdict = _verify_evidence_grounding(verdict, grounding_source, lead_metadata)
        verdict = _verify_hooks_grounding(verdict, grounding_source, lead_metadata)
        return _apply_site_missing_guard(verdict, site_content_missing)
    except json.JSONDecodeError as e:
        return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e), grounding_source, site_content_missing, lead_metadata)
    except Exception as e:
        if _is_json_parse_error(e):
            return _retry_after_failure(rows, deterministic_signals, build_user_content, str(e), grounding_source, site_content_missing, lead_metadata)
        if _is_rate_limit_error(e):
            try:
                shorter_text = rows_to_text(rows, max_chars=RETRY_MAX_CONTENT_CHARS)
                verdict = _call_llm(build_user_content(shorter_text))
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