"""Personalized outreach email generation for the results page.

Each email is generated individually by the LLM (through the provider
abstraction in llm_provider.py), personalized to the detected need of the
company and the recommended RuyaTech offer. This is a NEW step downstream
of the scoring — scorer.py is never touched here.
"""
import json

from llm_provider import get_llm_provider

EMAIL_PROMPT_TEMPLATE = """You write a short, personalized outreach email for RuyaTech,
a technical agency that builds, rescues, and scales SaaS products for founders.

Company: {company_name}
Contact first name (leave "Greetings," without a name if empty): {contact_first_name}
Detected segment: {segment}
Recommended offer: {recommended_offer}
Personalization hooks already identified by the scoring: {personalization_hooks}
Evidence/quotes taken from the site: {evidence_quotes}
Excerpt from the homepage content: {homepage_content}

Context of the RuyaTech offers (pick the one matching recommended_offer, stay faithful to the
exact positioning below — do not generalize, do not reinvent what we offer):

- ai_audit → "Product Rescue & Scale-Up" service: for non-technical founders whose product was
  built with AI (vibe-coding — Cursor, Replit, ChatGPT, Lovable, Bolt) and starts breaking under
  real users. Full code audit, stabilization, refactoring, and getting it back on track —
  typically in 4 to 8 weeks. Concrete example to reuse if relevant: we took over an AI-generated
  SaaS that was collapsing, relaunched it in 2 weeks, 600 paying members 6 months later
  (Bake Genie case study).

- general_audit → same "Product Rescue & Scale-Up" service, for a technical team:
  security and architecture audit, concrete recommendations, fix prioritization.

- pipeline → "AI Agents & Automation" service: custom AI agents and automations plugged into
  existing systems (lead triage, document processing, workflows), not "AI gadgets".
  Concrete example to reuse if relevant: lead triage pipeline delivered to an overwhelmed B2B
  firm — 5h/week of business dev instead of several hours a day, 30K$+ in new contracts in
  30 days.

General RuyaTech proof points, to use sparingly (one if needed, never all at once) to add
credibility without making the email sound like a sales brochure: fixed price announced before
coding (no hourly billing), 10+ delivered projects, 100% of the code belongs to the client
from day one, reply within 4 business hours.

Strict instructions:
- Short subject line specific to this company (not generic, no visible template) — never empty,
  mandatory in every response.
- Body structure, in this order, with a line break between each block:
  1. Short, direct greeting (e.g. "Greetings," or "Hi [first name]," if a contact first name is
     available in the context, otherwise "Greetings,").
  2. Personalized opener (1-2 sentences): the concrete situational detail spotted on their site.
  3. Offer presentation (1-2 sentences): the link between that detail and the recommended
     RuyaTech service, with at most one concrete proof point (case study/figure) if it adds real
     credibility.
  4. Call-to-action (1 sentence): one single clear action (e.g. propose a quick call).
  5. Sign-off + signature (e.g. "Best regards," then a line break, then "Oussama Ibrahim — RuyaTech").
- 4 to 6 sentences total for blocks 1 to 4 (excluding the signature), in English, direct and
  professional tone, no empty superlatives.
- Never write the body as one continuous block of text — the 5 parts above must stay visually
  separated by line breaks in the "body" field.
- SITUATIONAL personalization only (what the company does/uses/publishes) — never biographical
  (nothing about the person themselves).
- Reuse the hooks already provided rather than inventing new unverified ones.
- ONE SINGLE LANGUAGE throughout the email (subject + body): English. The hooks, quotes, and
  content provided may be in French (scraped from the site): translate and adapt them into
  English in the email, never paste them verbatim in their original language. The final email
  must not contain any word, phrase, or quote in a language other than English.
- Use the RuyaTech proof points (case studies, figures) only if they add real credibility to the
  message — never as filler, never more than one per email.
- One single clear call-to-action, toward the recommended offer.
- Do not invent any fact that is not in the context provided above.
- Respond only with this JSON, nothing else: {{"subject": "...", "body": "..."}}
  The "subject" field must never be empty. The "body" field must contain the line breaks
  ("\\n\\n" between each block) that structure the email as described above.
"""

MAX_HOOK_CHARS = 800
MAX_HOMEPAGE_CHARS = 1200


def _as_text(value) -> str:
    """Renders a hook/quotes value into a plain text snippet.

    The scoring stores these fields as JSON (list of objects). Passed
    through get_leads_with_scores they arrive as raw TEXT, so both the
    already-parsed and the still-string forms are handled here.
    """
    if not value:
        return "none"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)[:MAX_HOOK_CHARS]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                parsed = json.loads(stripped)
                return json.dumps(parsed, ensure_ascii=False)[:MAX_HOOK_CHARS]
            except Exception:
                pass
        return stripped[:MAX_HOOK_CHARS]
    return str(value)[:MAX_HOOK_CHARS]


def build_prompt(lead: dict, homepage_content: str) -> str:
    return EMAIL_PROMPT_TEMPLATE.format(
        company_name=lead["company_name"] or "this company",
        contact_first_name=lead.get("first_name") or "",
        segment=lead.get("segment") or "unknown",
        recommended_offer=lead.get("recommended_offer") or "none",
        personalization_hooks=_as_text(lead.get("personalization_hooks")),
        evidence_quotes=_as_text(lead.get("evidence_quotes")),
        homepage_content=(homepage_content or "")[:MAX_HOMEPAGE_CHARS],
    )


def generate_email_for_lead(lead: dict, homepage_content: str) -> dict:
    """Returns {"subject": ..., "body": ...} or raises an exception, handled by the caller."""
    provider = get_llm_provider()
    prompt = build_prompt(lead, homepage_content)
    return provider.generate_json(prompt)