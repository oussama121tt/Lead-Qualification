"""Apollo REST API client — people SEARCH (free) and people ENRICH (1 credit
each), with a persistent monthly credit governor.

Env: APOLLO_API_KEY (required to make live calls).

Design rules baked in:
- SEARCH is free and returns name/title/company/headcount/founded with the
  email/website obfuscated. ENRICH (people bulk_match) costs 1 credit per
  person and reveals email, LinkedIn, website, employment history.
- NEVER enrich before the Stage-0 pre-filter has run (the caller enforces the
  order; enrich_people() only ever enriches the list it is handed).
- The credit governor (backed by the apollo_usage table) blocks any enrichment
  that would push the running MONTHLY total past config [apollo].monthly_credit_cap.
  It reserves before the call and records actual usage after.

The HTTP layer is isolated in _post() so tests can monkeypatch it without a key.
"""
from __future__ import annotations

import os
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

APOLLO_BASE = "https://api.apollo.io/api/v1"
# NB: /mixed_people/search is UI-session-only and returns 403 API_INACCESSIBLE
# for API keys (even master keys). API-key people search lives at
# /mixed_people/api_search — verified live 2026-08.
SEARCH_PATH = "/mixed_people/api_search"
BULK_MATCH_PATH = "/people/bulk_match"


class ApolloError(RuntimeError):
    pass


class ApolloCreditCapReached(RuntimeError):
    def __init__(self, used: int, cap: int, needed: int):
        super().__init__(f"Apollo monthly credit cap: {used}/{cap} used, {needed} more needed")
        self.used, self.cap, self.needed = used, cap, needed


def _api_key() -> str:
    key = os.getenv("APOLLO_API_KEY", "").strip()
    if not key:
        raise ApolloError("APOLLO_API_KEY not set in .env")
    return key


def _post(path: str, payload: dict, timeout: float = 45.0) -> dict:
    """Single POST to the Apollo API. Isolated for testability."""
    resp = requests.post(
        f"{APOLLO_BASE}{path}",
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": _api_key(),
        },
        json=payload,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ApolloError(f"Apollo HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as e:
        raise ApolloError(f"Apollo returned non-JSON: {e}")


# ---------------------------------------------------------------------------
# Credit governor (monthly), persisted in apollo_usage(month, credits_used)
# ---------------------------------------------------------------------------

def ensure_usage_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS apollo_usage ("
        "month TEXT PRIMARY KEY, credits_used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()


def _this_month() -> str:
    return datetime.now().strftime("%Y-%m")


def credits_used_this_month(conn) -> int:
    row = conn.execute(
        "SELECT credits_used FROM apollo_usage WHERE month = ?", (_this_month(),)
    ).fetchone()
    return row["credits_used"] if row else 0


def record_credits(conn, n: int) -> None:
    if n <= 0:
        return
    conn.execute(
        "INSERT INTO apollo_usage (month, credits_used) VALUES (?, ?) "
        "ON CONFLICT (month) DO UPDATE SET credits_used = apollo_usage.credits_used + ?",
        (_this_month(), n, n),
    )
    conn.commit()


def check_credit_budget(conn, needed: int, cap: int) -> None:
    """Raise ApolloCreditCapReached if enriching `needed` people would exceed
    the monthly cap. cap <= 0 disables the check."""
    if cap <= 0:
        return
    used = credits_used_this_month(conn)
    if used + needed > cap:
        raise ApolloCreditCapReached(used, cap, needed)


# ---------------------------------------------------------------------------
# Search (free) and enrich (credits)
# ---------------------------------------------------------------------------

def search_people(filters: dict, *, page: int = 1, per_page: int = 100) -> dict:
    """One page of people search (FREE). `filters` is passed through to the
    Apollo search body (e.g. person_titles, q_keywords, organization_num_
    employees_ranges, person_locations). Returns the raw Apollo response
    (people[] + pagination)."""
    payload = {**filters, "page": page, "per_page": min(per_page, 100)}
    return _post(SEARCH_PATH, payload)


def search_people_all(filters: dict, *, max_people: int = 500, per_page: int = 100) -> list[dict]:
    """Paginate search until max_people or the results run out. FREE (no credits).
    Returns a flat list of person dicts (obfuscated — search level)."""
    people: list[dict] = []
    page = 1
    while len(people) < max_people:
        data = search_people(filters, page=page, per_page=per_page)
        batch = data.get("people") or data.get("contacts") or []
        if not batch:
            break
        people.extend(batch)
        # api_search returns total_entries (no pagination object); stop as soon
        # as we have them all instead of paying for an empty extra page.
        total = data.get("total_entries")
        if total is not None and len(people) >= total:
            break
        pagination = data.get("pagination") or {}
        total_pages = pagination.get("total_pages")
        if total_pages and page >= total_pages:
            break
        page += 1
    return people[:max_people]


def _person_match_key(p: dict) -> dict:
    """Build the identity fields Apollo bulk_match needs for one person."""
    key = {}
    if p.get("id"):
        key["id"] = p["id"]
    name = " ".join(filter(None, [p.get("first_name"), p.get("last_name")])).strip()
    if name:
        key["name"] = name
    org = p.get("organization") or {}
    domain = org.get("primary_domain") or p.get("organization_domain")
    if domain:
        key["domain"] = domain
    company = p.get("company_name") or org.get("name") or p.get("organization_name")
    if company:
        key["organization_name"] = company
    return key


def enrich_people(conn, people: list[dict], *, monthly_cap: int,
                  reveal_personal_emails: bool = False) -> dict:
    """Enrich (bulk_match) a list of PRE-FILTERED people — 1 credit each.

    Enforces the monthly credit governor BEFORE the call (reserves), then
    records actual usage after. Returns
    {"enriched": [person dicts with email/linkedin/website/history], "credits": N}.

    IMPORTANT: only call this with the KEEP list from prefilter — never the
    raw search results.
    """
    if not people:
        return {"enriched": [], "credits": 0}

    ensure_usage_table(conn)
    check_credit_budget(conn, len(people), monthly_cap)  # raises if over cap

    enriched: list[dict] = []
    total_credits = 0
    # Apollo bulk_match accepts up to 10 details per call.
    for i in range(0, len(people), 10):
        chunk = people[i:i + 10]
        payload = {
            "details": [_person_match_key(p) for p in chunk],
            "reveal_personal_emails": reveal_personal_emails,
        }
        data = _post(BULK_MATCH_PATH, payload)
        matches = data.get("matches") or []
        for m in matches:
            if m:
                enriched.append(m)
        # Apollo charges a credit per successfully matched person.
        credits = sum(1 for m in matches if m)
        total_credits += credits
        record_credits(conn, credits)

    return {"enriched": enriched, "credits": total_credits}


def _compact_employment(history, limit: int = 8) -> list:
    """Keep the fields that carry signal (title/org/dates/current), drop noise."""
    out = []
    for e in (history or [])[:limit]:
        if not isinstance(e, dict):
            continue
        out.append({
            "title": e.get("title"),
            "organization": e.get("organization_name"),
            "start": (e.get("start_date") or "")[:7] or None,
            "end": (e.get("end_date") or "")[:7] or None,
            "current": bool(e.get("current")),
        })
    return out


def person_to_lead_row(p: dict) -> dict:
    """Map an enriched Apollo person into the row shape the ingester
    understands. Beyond the basic CSV columns it carries the high-signal
    enrichment data the scorer should see:
      - email_status  (verified | guessed | unavailable | ...)
      - apollo_person (seniority, headline, location, employment history:
        the founder's career is the strongest technical-vs-non-technical signal)
      - apollo_org    (headcount, founded year, industry, growth, revenue)
    """
    import json as _json
    org = p.get("organization") or {}
    website = (p.get("website_url") or org.get("website_url")
               or ("https://" + org["primary_domain"] if org.get("primary_domain") else ""))
    apollo_person = {
        "seniority": p.get("seniority"),
        "headline": p.get("headline"),
        "city": p.get("city"),
        "country": p.get("country"),
        "employment_history": _compact_employment(p.get("employment_history")),
    }
    apollo_org = {
        "name": org.get("name"),
        "domain": org.get("primary_domain"),
        "employees": org.get("estimated_num_employees"),
        "founded_year": org.get("founded_year"),
        "industry": org.get("industry"),
        "headcount_growth_6m": org.get("organization_headcount_six_month_growth"),
        "headcount_growth_12m": org.get("organization_headcount_twelve_month_growth"),
        "revenue": org.get("organization_revenue_printed"),
        "linkedin_url": org.get("linkedin_url"),
        "keywords": (org.get("keywords") or [])[:12],
    }
    return {
        "first_name": p.get("first_name") or "",
        "last_name": p.get("last_name") or "",
        "title": p.get("title") or "",
        "company_name": p.get("company_name") or org.get("name") or "",
        "email": p.get("email") or "",
        "website_url": website or "",
        "linkedin_url": p.get("linkedin_url") or "",
        "apollo_email_status": p.get("email_status") or "",
        "apollo_person": _json.dumps(apollo_person, ensure_ascii=False),
        "apollo_org": _json.dumps(apollo_org, ensure_ascii=False),
    }
