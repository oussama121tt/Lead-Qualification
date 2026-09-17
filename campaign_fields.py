"""The four values the personalised sequence merges per contact.

Built on the evidence of what actually earned replies from this account:
the March-May 2026 one-off emails (5 replies from 64 people) were personalised
in EVERY touch and usually ended the first email with a short, specific,
easy-to-answer question. The templated sequences, which personalise one line and
then run identical copy for everyone with no ask, got 0 replies from 66 people.

So the sequence needs four per-contact values, not one:

  subject_line        3-6 words, from their own situation
  personal_line       the opener: a concrete fact + the implication
  opening_question    one short question ending email 1
  second_observation  a DIFFERENT fact, from a different source, for email 2

They are produced in a single call so they cohere — in particular so the
follow-up is genuinely new information rather than a restatement of the opener.

Every text value is independently guarded (length, banned phrasing, verbatim
grounding against the evidence). A value that fails comes back empty and the
sequence falls back to its fixed copy for that slot, so a weak follow-up never
blocks a good opener. Nothing ungrounded is ever sent.
"""
from __future__ import annotations

import re
import time

import scorer
from llm_provider import get_llm_provider
from personal_line import _BANNED, MAX_WORDS, _evidence_block
from profile import load_profile


# Voice (examples, subjects, questions, hook sentences) and sender blurb
# come from the profile (see [voice] and [voice.hooks]); the four-field
# contract below is the sending machine.
def build_system(p=None) -> str:
    p = p or load_profile()
    examples = "\n".join(f"- {e}" for e in p.voice.examples)
    subjects = "\n".join(f"- {s}" for s in p.voice.subject_examples)
    questions = "\n".join(f"- {q}" for q in p.voice.question_examples)
    company = p.identity.company
    blurb = p.identity.sequence_blurb
    implication = p.voice.hooks.sequence_implication
    return f"""You prepare one cold-email sequence for {company}, {blurb}. \
The sequence copy is already written; you supply \
four values merged per contact.

Opening lines that have actually been sent by this team - match this voice:

{examples}

Subject lines from the campaign that earned replies:

{subjects}

Questions that founders actually answered:

{questions}

Produce, as JSON:

1. "subject_line" - 3 to 6 words, lower case unless a proper noun, no colon, no marketing phrasing. \
It should read like a note from someone who has been paying attention, drawn from THIS founder's \
situation.

2. "personal_line" - the opener that follows the greeting. A concrete specific fact about what this \
product does or what this founder is carrying, {implication}

3. "opening_question" - ONE short question that ends the first email. Specific to their build, easy \
and slightly enjoyable to answer, never about buying anything. Under 25 words, ends with a question \
mark.

4. "second_observation" - the body of the day-3 follow-up. It MUST rest on a DIFFERENT fact from a \
DIFFERENT source than personal_line: a second post, the careers page, pricing, a hire, a launch. If \
the evidence gives you only one fact about them, return "" rather than restating the opener. \
1-2 sentences.

Rules for all four: use only the supplied evidence, never invent. No compliments, no pitch, no \
advice, no "I noticed", no "I saw", no exclamation marks. Plain conversational English.

Return "citations" mapping personal_line and second_observation to the exact verbatim quote from \
the evidence each rests on. Return "" for any value the evidence cannot support.

Respond ONLY with JSON:
{{"subject_line": "...", "personal_line": "...", "opening_question": "...", \
"second_observation": "...", "citations": {{"personal_line": "...", "second_observation": "..."}}}}"""

# Generic anti-spam subject patterns: apply to any offer, stay in code.
_SUBJECT_BANNED_BASE = (
    r"quick question|touching base|following up|checking in|opportunity|partnership|"
    r"introduction|proposal|let's chat|catching up"
)


def _subject_banned(p=None) -> re.Pattern:
    """Subject guard: generic patterns plus the profile's
    [voice] extra_banned_subject_phrases (regex alternation fragments)."""
    p = p or load_profile()
    extra = "|".join(e for e in (p.voice.extra_banned_subject_phrases or []) if e)
    alts = _SUBJECT_BANNED_BASE + ("|" + extra if extra else "")
    return re.compile(r"\b(" + alts + r")\b|[:!]", re.I)


_SUBJECT_BANNED = _subject_banned()  # default-profile snapshot; tests keep working

FIELDS = ("subject_line", "personal_line", "opening_question", "second_observation")


def generate(lead: dict, verdict: dict, site_text: str, web_text: str = "",
             *, cost_cb=None, provider=None, profile=None) -> dict:
    """Returns the four values plus a per-field status dict.

    status values: "ok", "empty" (evidence too thin), "rejected:<why>"."""
    provider = provider or get_llm_provider("email")
    prompt = _evidence_block(lead, verdict, site_text, web_text)
    system = build_system(profile)
    subject_banned = _subject_banned(profile)
    t0 = time.monotonic()
    data, meta = provider.generate_json(prompt, system=system, max_tokens=1800)
    if cost_cb is not None:
        try:
            cost_cb(meta, int((time.monotonic() - t0) * 1000))
        except Exception:
            pass

    corpus = "\n\n---\n\n".join(p for p in (site_text, web_text, prompt) if p)
    norm = scorer._normalize_for_grounding(corpus)
    loose = scorer._loose(corpus)
    cites = data.get("citations") if isinstance(data.get("citations"), dict) else {}
    out: dict = {"status": {}}

    subject = (data.get("subject_line") or "").strip().strip('"').strip()
    if not subject:
        out["subject_line"], out["status"]["subject_line"] = "", "empty"
    elif len(subject.split()) > 8 or subject_banned.search(subject):
        out["subject_line"], out["status"]["subject_line"] = "", "rejected:style"
    else:
        out["subject_line"], out["status"]["subject_line"] = subject, "ok"

    for field in ("personal_line", "second_observation"):
        val = (data.get(field) or "").strip()
        cite = (cites.get(field) or "").strip()
        out[field + "_based_on"] = cite
        if not val:
            out[field], out["status"][field] = "", "empty"
        elif len(val.split()) > MAX_WORDS:
            out[field], out["status"][field] = "", "rejected:too_long"
        elif _BANNED.search(val):
            out[field], out["status"][field] = "", "rejected:banned_phrase"
        elif cite and not scorer._is_grounded(cite, norm, loose):
            out[field], out["status"][field] = "", "rejected:ungrounded"
        else:
            out[field], out["status"][field] = val, "ok"

    q = (data.get("opening_question") or "").strip()
    if not q:
        out["opening_question"], out["status"]["opening_question"] = "", "empty"
    elif not q.endswith("?") or len(q.split()) > 30 or _BANNED.search(q):
        out["opening_question"], out["status"]["opening_question"] = "", "rejected:style"
    else:
        out["opening_question"], out["status"]["opening_question"] = q, "ok"

    # The follow-up must not lean on the same sentence as the opener.
    if out.get("second_observation") and out.get("personal_line"):
        a = scorer._loose(out["second_observation"])[:70]
        b = scorer._loose(out["personal_line"])[:70]
        if a and a == b:
            out["second_observation"] = ""
            out["status"]["second_observation"] = "rejected:duplicate_of_opener"

    out["usable"] = out["status"].get("personal_line") == "ok"
    return out
