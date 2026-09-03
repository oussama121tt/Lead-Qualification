"""Stage-0 pre-filter — runs on FREE Apollo search fields BEFORE any
enrichment credit or website fetch is spent.

Rationale (validated manually at ~10/10 accuracy on the reject direction):
Apollo employment/title/company fields alone reliably reject the obvious
non-fits — dev shops, agencies, consultancies, fractional CTOs, enterprise,
and competitors — which on a generic keyword pool are ~25% of results. Killing
them before enrichment roughly doubles the monthly lead ceiling for the same
credit budget.

Deterministic-first: a lead is REJECTED, KEPT, or UNCLEAR from rules alone.
Only UNCLEAR leads optionally go to a cheap Groq pass (config
[prefilter].use_llm). The rules are intentionally conservative on the KEEP
side and aggressive only on unambiguous rejects — a false reject is a lost
good lead, so when in doubt we KEEP (or defer to the LLM), never reject.

Input per person: {first_name, last_name, title, company_name,
headcount|estimated_num_employees, founded_year, ...} — whatever the Apollo
search returns (email/website obfuscated at this stage, that's fine).
"""
from __future__ import annotations

import re

# --- Reject: the person is a service provider / competitor, not a product founder ---
AGENCY_COMPANY_MARKERS = re.compile(
    r"\b(agency|studio|consult(?:ing|ancy|ants?)?|labs?|solutions|software\s+house|"
    r"digital\s+agency|dev\s?shop|web\s+design|it\s+services|systems\s+integrat|"
    r"outsourc|technolog(?:y|ies)\s+partner|interactive|creative\s+agency)\b",
    re.I,
)
AGENCY_TITLE_MARKERS = re.compile(
    r"\b(agency\s+owner|freelance|freelancer|consultant|contractor|"
    r"fractional\s+(?:cto|cpo|coo|cmo)|advisor|mentor|coach|"
    r"managing\s+director\s+at\s+.*\bagency\b)\b",
    re.I,
)
# --- Reject: clearly not a founder/decision-maker persona ---
NON_DECISION_TITLE_MARKERS = re.compile(
    r"\b(intern|student|assistant|recruiter|talent|hr\b|human\s+resources|"
    r"sales\s+(?:rep|representative|development)|sdr\b|bdr\b|account\s+executive|"
    r"support|customer\s+success|bookkeeper|accountant|receptionist)\b",
    re.I,
)
# --- Keep: strong founder/decision-maker persona ---
FOUNDER_TITLE_MARKERS = re.compile(
    r"\b(founder|co-?founder|ceo|owner|managing\s+partner|president|"
    r"chief\s+executive)\b",
    re.I,
)


def evaluate_person(person: dict, *, max_headcount: int = 50,
                    min_headcount: int = 0) -> dict:
    """Deterministic Stage-0 verdict for one Apollo search result.

    Returns {"decision": "keep"|"reject"|"unclear", "reason": str}.
    Never raises. When a signal is ambiguous, prefers 'unclear' (which the
    caller may resolve with a cheap LLM) or 'keep' over 'reject'.
    """
    title = (person.get("title") or "").strip()
    company = (person.get("company_name") or person.get("organization_name") or "").strip()
    headcount = _to_int(person.get("headcount")
                        or person.get("estimated_num_employees")
                        or person.get("organization_num_employees"))

    # 1. Enterprise / too big — by headcount (a free, reliable field).
    if max_headcount and headcount and headcount > max_headcount:
        return {"decision": "reject", "reason": f"headcount {headcount} > {max_headcount} (too big)"}
    if min_headcount and headcount and headcount < min_headcount:
        return {"decision": "reject", "reason": f"headcount {headcount} < {min_headcount}"}

    # 2. Agency / consultancy / dev shop — the #1 competitor category.
    if company and AGENCY_COMPANY_MARKERS.search(company):
        return {"decision": "reject", "reason": f"agency/consultancy company name: '{company}'"}
    if title and AGENCY_TITLE_MARKERS.search(title):
        return {"decision": "reject", "reason": f"service-provider/fractional title: '{title}'"}

    # 3. Non-decision-maker persona (intern, sales, recruiter...).
    if title and NON_DECISION_TITLE_MARKERS.search(title) and not FOUNDER_TITLE_MARKERS.search(title):
        return {"decision": "reject", "reason": f"non-decision-maker title: '{title}'"}

    # 4. Strong founder persona → keep.
    if title and FOUNDER_TITLE_MARKERS.search(title):
        return {"decision": "keep", "reason": f"founder/decision-maker title: '{title}'"}

    # 5. Everything else → unclear (let the LLM decide if enabled, else keep).
    return {"decision": "unclear", "reason": "no decisive title/company signal"}


LLM_PREFILTER_SYSTEM = (
    "You are a fast pre-filter for a B2B lead list. Given only a person's title, "
    "company name, headcount and founded year (no website), decide if they are "
    "plausibly a NON-TECHNICAL or technical FOUNDER of a small software product "
    "company (our target), or clearly NOT (agency, consultancy, dev shop, "
    "fractional CTO, enterprise employee, recruiter, unrelated sector). "
    "Respond ONLY as JSON: {\"decision\": \"keep\"|\"reject\", \"reason\": \"...\"}. "
    "When genuinely unsure, keep — a wrong reject loses a good lead."
)


def evaluate_unclear_with_llm(person: dict, cost_cb=None) -> dict:
    """Optional cheap Groq pass for UNCLEAR leads only. Returns the same shape
    as evaluate_person. Any failure degrades to 'keep' (never rejects on error)."""
    import time as _time
    from llm_provider import get_llm_provider

    prompt = (
        f"Title: {person.get('title')}\n"
        f"Company: {person.get('company_name') or person.get('organization_name')}\n"
        f"Headcount: {person.get('headcount') or person.get('estimated_num_employees')}\n"
        f"Founded: {person.get('founded_year')}"
    )
    try:
        provider = get_llm_provider("scoring")  # cheap model; scoring provider is fine
        t0 = _time.monotonic()
        data, meta = provider.generate_json(prompt, system=LLM_PREFILTER_SYSTEM,
                                            temperature=0.0, max_tokens=200)
        if cost_cb is not None:
            try:
                cost_cb(meta, int((_time.monotonic() - t0) * 1000))
            except Exception:
                pass
        decision = str(data.get("decision", "keep")).lower()
        if decision not in ("keep", "reject"):
            decision = "keep"
        return {"decision": decision, "reason": f"llm: {data.get('reason', '')}"[:200]}
    except Exception as e:
        return {"decision": "keep", "reason": f"llm prefilter failed, kept: {e}"}


def prefilter_people(people: list[dict], *, max_headcount: int = 50,
                     min_headcount: int = 0, use_llm: bool = False,
                     cost_cb=None) -> dict:
    """Run Stage-0 over a list of Apollo search results.

    Returns {"keep": [...people...], "reject": [(person, reason), ...],
             "stats": {total, kept, rejected, unclear_resolved_by_llm}}.
    The kept list is what should be enriched (1 credit each) — everything
    else never costs a credit or a fetch.
    """
    keep, reject = [], []
    unclear_llm = 0
    for p in people:
        v = evaluate_person(p, max_headcount=max_headcount, min_headcount=min_headcount)
        if v["decision"] == "unclear":
            if use_llm:
                v = evaluate_unclear_with_llm(p, cost_cb=cost_cb)
                unclear_llm += 1
            else:
                v = {"decision": "keep", "reason": "unclear, kept (llm disabled)"}
        if v["decision"] == "reject":
            reject.append((p, v["reason"]))
        else:
            keep.append(p)
    return {
        "keep": keep,
        "reject": reject,
        "stats": {
            "total": len(people),
            "kept": len(keep),
            "rejected": len(reject),
            "unclear_resolved_by_llm": unclear_llm,
        },
    }


def _to_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    m = re.sub(r"[^\d]", "", str(v))
    return int(m) if m else None
