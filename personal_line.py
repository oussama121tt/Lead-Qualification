"""Personal Line generator — the one personalised sentence that opens the
existing Apollo sequences.

The sequence copy (subject, body, follow-ups) is already written and already
sent: only the second line of email 1 changes per contact, supplied through the
Apollo contact custom field "Personal Line". This module produces that line in
the SAME voice as the lines Wael/Oussama already sent, using the 14 real
examples below as the style reference.

The house pattern, read off those examples:
  - a concrete observation about what the product actually does, or about the
    founder's own situation, taken from evidence;
  - followed by the implication that makes an audit matter (whose data it
    holds, how heavy the permission is, how thin the team is);
  - no compliment, no pitch, no advice, no question, no "I noticed";
  - 1-2 sentences, plain conversational English.

Grounding: the generated line is checked against the same corpus the scorer
uses. A line that cannot be traced to the evidence is rejected and the caller
falls back to no personalisation (the generic sequence), never to a guess.
"""
from __future__ import annotations

import json
import re

import scorer
from llm_provider import get_llm_provider

# Real lines from the AIAUDIT campaigns (Apollo, Aug-Sep 2026). These are the
# style contract; do not replace them with invented examples.
EXAMPLES = [
    "Swipe-native shopping means checkout has to be instant and correct every time, and you're building that on your own.",
    "Maggie helps mums find activities near them, which means the app knows where children will be and when. That's a heavier data load than most free apps carry.",
    "A year on the App Store means a year of medication records and dependants' details accumulating, shared across people who aren't in the same household.",
    "HoopDee calculates when milk expires. Most apps fail by annoying someone; yours fails by feeding a baby something it shouldn't.",
    "Weave coordinates care for people already in hardship, which makes it about the last place anyone would want a data problem.",
    "Aviva reads families' school emails to build their calendars. Inbox access is about the heaviest permission an app can hold, and you're holding it for parents.",
    "Ove is built for girls going through puberty, so the data belongs to minors and UK rules treat that more strictly than almost anything else.",
    "Doozi's whole promise is that every pin is verified and nothing is paid placement. Trust claims like that live or die on what the software actually enforces.",
    "You've just joined Honeymoon as an Earned Media Director while running Tblscape's subscriptions and payments. That's a lot of plates.",
    "SYNC pulls wearable data alongside cycle tracking, so it's holding a continuous stream of something quite personal rather than an occasional log.",
    "You're running a full real-estate practice and building Paced at the same time, which usually means the app gets your evenings and the plumbing underneath gets whatever's left.",
]

SYSTEM_PROMPT = """You write ONE personalised opening line for a cold email from RuyaTech, a technical \
agency that audits and rescues software products. The rest of the email is already written; you \
supply only the line that follows the greeting.

House style, learned from lines that have actually been sent:

{examples}

Rules, all mandatory:
- State a concrete, specific fact about what THIS product does, or about THIS founder's own \
situation, taken only from the evidence supplied. Name the product or the thing it handles.
- Then give the implication that makes a code audit matter: whose data it holds, how sensitive \
that data is, how heavy a permission it needs, how much the founder is carrying alone, or what a \
failure would actually cost. The implication must follow from the fact, not be asserted.
- 1 or 2 sentences. Under 45 words. Plain conversational English.
- NEVER: compliment them, pitch the service, give advice, ask a question, use "I noticed", \
"I saw", "Love what you're doing", "impressive", "exciting", or any exclamation mark.
- NEVER invent a fact. If the evidence does not support a specific observation, say so by \
returning an empty line rather than writing something generic.
- Do not greet, do not sign off, do not mention RuyaTech or an audit.

Respond ONLY with JSON: {{"line": "...", "based_on": "exact verbatim quote from the evidence that \
the observation rests on"}}. Return {{"line": "", "based_on": ""}} when the evidence is too thin."""

MAX_WORDS = 55
_BANNED = re.compile(
    r"\b(i noticed|i saw|i came across|i love|love what|impressive|exciting|congrat|"
    r"you should|you might want|consider |have you |reach out|happy to|we can help|"
    r"our team|we offer|let me know)\b|!",
    re.I,
)


def _evidence_block(lead: dict, verdict: dict, site_text: str, web_text: str = "") -> str:
    parts = []
    meta = scorer._format_lead_metadata(lead.get("_metadata") or {})
    if meta:
        parts.append(meta)
    sens = verdict.get("sensitive_data_categories") or []
    if isinstance(sens, str):
        sens = scorer.json.loads(sens) if sens.strip().startswith("[") else [sens]
    facts = [f"segment: {verdict.get('segment')}", f"offer: {verdict.get('recommended_offer')}"]
    if [s for s in sens if s and s != "none"]:
        facts.append("sensitive data handled: " + ", ".join(s for s in sens if s and s != "none"))
    for key in ("built_with_ai_signals", "technical_signals", "pain_signals", "evidence_quotes"):
        vals = verdict.get(key) or []
        if isinstance(vals, str):
            try:
                vals = json.loads(vals)
            except (json.JSONDecodeError, TypeError):
                vals = [vals]
        if vals:
            facts.append(f"{key}: " + "; ".join(str(v)[:200] for v in vals[:5]))
    hooks = verdict.get("personalization_hooks") or []
    if isinstance(hooks, str):
        try:
            hooks = json.loads(hooks)
        except (json.JSONDecodeError, TypeError):
            hooks = []
    for h in hooks:
        if isinstance(h, dict) and h.get("based_on"):
            facts.append(f"observation: {h.get('hook')} (source: \"{h['based_on'][:200]}\")")
    parts.append("Verdict and signals:\n" + "\n".join(f"- {f}" for f in facts))
    if site_text:
        parts.append("Their website:\n" + site_text[:6000])
    if web_text:
        parts.append("Web and LinkedIn evidence:\n" + web_text[:4000])
    return "\n\n---\n\n".join(parts)


def generate(lead: dict, verdict: dict, site_text: str, web_text: str = "",
             *, cost_cb=None, provider=None) -> dict:
    """Returns {"line", "based_on", "status"}.

    status: "ok" (grounded, usable), "empty" (model declined: evidence too
    thin), "rejected:<why>" (failed a guard). Only "ok" should ever be written
    to Apollo; everything else means send the generic sequence instead."""
    import time as _time
    provider = provider or get_llm_provider("email")
    prompt = _evidence_block(lead, verdict, site_text, web_text)
    system = SYSTEM_PROMPT.format(examples="\n".join(f"- {e}" for e in EXAMPLES))
    t0 = _time.monotonic()
    data, meta = provider.generate_json(prompt, system=system, max_tokens=900)
    if cost_cb is not None:
        try:
            cost_cb(meta, int((_time.monotonic() - t0) * 1000))
        except Exception:
            pass

    line = (data.get("line") or "").strip()
    based_on = (data.get("based_on") or "").strip()
    if not line:
        return {"line": "", "based_on": "", "status": "empty"}
    if len(line.split()) > MAX_WORDS:
        return {"line": "", "based_on": based_on, "status": "rejected:too_long"}
    if _BANNED.search(line):
        return {"line": "", "based_on": based_on, "status": "rejected:banned_phrase"}
    corpus = "\n\n---\n\n".join(p for p in (site_text, web_text, prompt) if p)
    norm = scorer._normalize_for_grounding(corpus)
    loose = scorer._loose(corpus)
    if based_on and not scorer._is_grounded(based_on, norm, loose):
        return {"line": "", "based_on": based_on, "status": "rejected:ungrounded"}
    return {"line": line, "based_on": based_on, "status": "ok"}
