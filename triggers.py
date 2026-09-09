"""Trigger monitoring — the "solve WHEN" layer.

Scored leads are re-checked on a schedule (weekly, via
`python tools/run_triggers.py`). When a readiness signal CHANGES, the lead
jumps to the top of the review queue with a short human-readable hook
("Saw you're hiring your first engineer" ...).

Check tiers (cost discipline):
- TIER 1 — cheap, EVERY lead: hiring_engineer, pricing_introduced,
  compliance_page, custom_domain_move, enterprise_logo, app_store_launch.
  Deterministic text/domain extraction over the site's public pages fetched
  with the free site_fetcher.
- TIER 2 — credit-governed, EVERY lead: team_growth (free Apollo search) and
  funding_announced / product_hunt (one PAID ScrapeGraphAI web search per lead
  run). TWO INDEPENDENT vendor budgets: team_growth gates on the Apollo cap
  (apollo_client.check_credit_budget, [apollo].monthly_credit_cap) while the
  web search lane gates on its OWN SGAI cap (sgai_client.check_credit_budget,
  [triggers].sgai_monthly_credit_cap). When one vendor's budget is exhausted
  its lane is skipped — the prior snapshot is left untouched and
  apollo_budget_reached / sgai_budget_reached flips in the trigger_state —
  and the OTHER vendor keeps running.
- TIER 3 — EXPENSIVE (LinkedIn lane), HIGH-SCORING leads only:
  founder_posts_pain. Never called for mid/low leads.

Every check is baseline-first: the first observation stores the snapshot in
`leads.trigger_state` WITHOUT firing; a change on a later run fires exactly
one event and updates the snapshot. Cheap checks never call the LinkedIn
lane (linkedin_lane.harvest_founder_profile is only reachable through the
founder_posts_pain check).

The seams (fetch / apollo_search / web_search / linkedin_harvest) default to
the real implementations and can be swapped in tests.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import apollo_client
import db as dbmod
import scraper
import sgai_client
import site_fetcher
from constants import TARGET_SEGMENTS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRIGGER_PRIORITIES = {
    "hiring_engineer": 4,
    "funding_announced": 4,
    "founder_posts_pain": 4,
    "pricing_introduced": 3,
    "custom_domain_move": 3,
    "app_store_launch": 3,
    "product_hunt": 3,
    "team_growth": 3,
    "compliance_page": 2,
    "enterprise_logo": 2,
}

# Careers/pricing/compliance page discovery (keywords first, then well-known
# standard paths — mirrors the scraper's own KEYWORDS/COMMON_PATH_CANDIDATES).
CAREERS_KEYWORDS = ("careers", "jobs", "join")
CAREERS_PATHS = ("/careers", "/jobs")
PRICING_KEYWORDS = ("pricing", "plans", "price")
PRICING_PATHS = ("/pricing", "/plans")
COMPLIANCE_KEYWORDS = ("privacy", "dpa", "soc2", "soc 2", "gdpr", "security", "terms")
COMPLIANCE_PATHS = ("/privacy", "/privacy-policy", "/dpa", "/terms", "/security")

# HTTP statuses that definitively mean "the page is not there" (as opposed to a
# network-layer failure, which proves nothing). A confirmed absence updates the
# snapshot to False so a later appearance re-fires the trigger.
ABSENT_STATUS_CODES = (404, 410)

STORE_HOST_MARKERS = (
    "github", "linkedin", "twitter", "x.com", "facebook", "instagram",
    "youtube", "tiktok", "medium", "substack", "producthunt", "apple",
    "appstore", "play.google", "testflight", "microsoft", "steam",
    "google", "discord", "slack", "telegram", "newsletter",
)

CUSTOMER_SECTION_MARKERS = (
    "trusted by", "customers include", "clients include", "loved by",
    "used by", "backed by", "featured in", "as seen in", "our customers are",
)
CUSTOMER_GENERIC_TOKENS = {
    "company", "companies", "customer", "customers", "client", "clients",
    "team", "their", "our", "and", "the", "with", "we", "have", "over",
    "more", "than", "trusted", "brands", "logos", "as", "featuring",
    "featured", "including", "people", "worldwide", "since", "from",
}
_CUSTOMER_NAME_RE = re.compile(
    r"([A-Z][A-Za-z0-9&'/-]*(?:\s+[A-Z][A-Za-z0-9&'/-]*){0,3})"
)

APP_STORE_HOSTS = (
    "apps.apple.com", "play.google.com", "testflight.apple.com",
    "apps.microsoft.com", "store.steampowered.com",
)

FUNDING_PATTERNS = (
    re.compile(r"\braised\s+(?:us\$|usd|[$€£])?\s?\d", re.I),
    re.compile(r"\b(?:series|round|pre-seed|seed)\s+[a-z0-9$\s]{0,12}\b", re.I),
    re.compile(r"\bsecured\s+(?:us\$|usd|[$€£])", re.I),
    re.compile(r"\ban[nn]ounc(?:ed|es)?\s+.{0,60}funding", re.I),
    re.compile(r"\b\d{1,3}\s?(?:m|million)\s+(?:in\s+)?funding\b", re.I),
)
PRICE_WORDS = ("price", "pricing", "plans", "subscription", "per month", "/mo", "/month")

PAIN_MARKERS = (
    "we're hiring", "we are hiring", "hiring our first", "feedback", "churn",
    "founders", "audit", "help us", "need help", "needs help", "struggling",
    "hard to", "pain point", "problem", "little traction", "who can",
    "recommend", "migration", "performance", "too slow", "confusing",
    "drop off", "reviews", "looks great, but", "v1", "mvp", "iteration",
)

# ---------------------------------------------------------------------------
# Seams (monkeypatchable in tests)
# ---------------------------------------------------------------------------


def _default_fetch(url, timeout: float, per_domain_delay: float) -> dict:
    return site_fetcher.fetch_page(url, timeout=timeout, per_domain_delay=per_domain_delay)


def _default_apollo_search(filters: dict) -> list[dict]:
    # Free search; only a few samples are needed to read the org headcount.
    return apollo_client.search_people_all(filters, max_people=100, per_page=100)


def _default_web_search(company_name: str, founder_name: str | None = None,
                        **kwargs) -> dict:
    return scraper.search_additional_evidence(
        company_name,
        founder_name=founder_name,
        limit_per_query=2,
        skip_person_linkedin=True,
        **kwargs,
    )


def _default_linkedin_harvest(linkedin_url: str, cfg_linkedin, conn) -> dict:
    import linkedin_lane
    return linkedin_lane.harvest_founder_profile(linkedin_url, cfg_linkedin, conn)


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


log = logging.getLogger(__name__)


class _Ctx:
    """Per-run shared state + injectable seams."""

    def __init__(self, conn, cfg, *, fetch=None, apollo_search=None,
                 web_search=None, linkedin_harvest=None, now=None):
        self.conn = conn
        self.cfg = cfg
        self.fetch = fetch or _default_fetch
        self.apollo_search = apollo_search or _default_apollo_search
        self.web_search = web_search or _default_web_search
        self.linkedin_harvest = linkedin_harvest or _default_linkedin_harvest
        self.now = now
        self._homepages: dict = {}
        self._websearch_results: dict | None = None
        # observation memoization: every network-touching probe is cached so
        # the locked phase of a run can diff observations against the fresh
        # snapshot WITHOUT doing any HTTP work.
        self._probes: dict = {}
        self._apollo: dict = {}
        self._linkedin: dict = {}
        # Per-vendor budget state for THIS run: a lane that hit its monthly cap
        # sets its flag, which is persisted into the snapshot as
        # apollo_budget_reached / sgai_budget_reached so debug output says
        # WHICH vendor tripped. Defaults False; recovered lanes clear it.
        self.budget_flags = {"apollo": False, "sgai": False}
        # The SGAI lane tolls 1 nominal credit per successful governed search;
        # Web searches are memoized per run, so toll exactly once per run.
        self._sgai_tolled = False

    # -- timing ----------------------------------------------------------
    def now_iso(self) -> str:
        return _now_iso(self.now)

    def next_check_at(self) -> str:
        dt = datetime.fromisoformat(self.now_iso())
        return (dt + timedelta(days=self.cfg.triggers.interval_days)).isoformat()

    # -- cached fetches ---------------------------------------------------
    def homepage(self, lead) -> dict | None:
        url = (lead.get("website_url") or "").strip()
        if not url:
            return None
        if url not in self._homepages:
            self._homepages[url] = self._fetch_ok(
                url, self.cfg.website.page_timeout, self.cfg.website.per_domain_delay
            )
        return self._homepages.get(url)

    def websearch(self, lead, founder_name: str | None = None) -> dict:
        if self._websearch_results is None:
            company = (lead.get("company_name") or "").strip()
            try:
                self._websearch_results = self.web_search(
                    company, founder_name=founder_name
                )
            except Exception:
                self._websearch_results = {}
        return self._websearch_results or {}

    def governed_websearch(self, lead, founder_name: str | None = None):
        """The SGAI search lane, gated by its OWN vendor budget.

        funding_announced and product_hunt run for EVERY due lead on EVERY run
        behind a PAID ScrapeGraphAI search (sources: LinkedIn, Product Hunt,
        Twitter/X, GitHub, interviews). That spend bills to SGAI, NOT Apollo,
        so it gates on sgai_client against [triggers].sgai_monthly_credit_cap —
        fully independent of team_growth's Apollo cap. Each successful run
        tolls 1 nominal credit (sgai_usage), so the cap is a real, counted
        budget (re-point the toll when real SGAI pricing is known).

        When the SGAI cap is exhausted: set sgai_budget_reached, return None,
        and the caller SKIPS the search entirely — no new state, no fire, the
        prior snapshot is preserved until budget returns.

        Returns the search results dict, or None when paused by the cap.
        """
        try:
            # needed=1: this search bills one nominal credit, so at exactly
            # the cap the lane must pause instead of overshooting by one.
            sgai_client.check_credit_budget(
                self.conn, 0 if self._sgai_tolled else 1,
                self.cfg.triggers.sgai_monthly_credit_cap,
            )
        except sgai_client.SgaiCreditCapReached:
            self.budget_flags["sgai"] = True
            return None
        results = self.websearch(lead, founder_name=founder_name)
        if not self._sgai_tolled:
            self._sgai_tolled = True
            try:
                sgai_client.record_credits(self.conn, 1)
            except Exception:
                # A failed toll must not silently kill the lane mid-run.
                pass
        return results

    def _fetch_ok(self, url, timeout, delay) -> dict | None:
        try:
            page = self.fetch(url, timeout, delay)
        except Exception:
            page = None
        if page is None or not page.get("ok"):
            return None
        if scraper._looks_broken(page.get("text") or ""):
            return None
        return page

    def _fetch_probe(self, url, timeout, delay) -> tuple:
        """One probed fetch, memoized per URL. Returns (page | None, status | None).

        `status None` means the probe itself failed at the network layer
        (exception/timeout), so some categories of page cannot claim to be
        CONFIRMED ABSENT from that result — the snapshot must be left alone.
        A 404/410 response is an actual HTTP answer: the page is definitively
        not there, and a later reappearance is a real transition.

        Memoized (like `homepage`): the run's first probe is the only network
        call; every later call — including the re-diff inside the locked
        transaction — replays the cached observation.
        """
        if url in self._probes:
            return self._probes[url]
        try:
            page = self.fetch(url, timeout, delay)
        except Exception:
            page = None
        if page is None:
            self._probes[url] = (None, None)
        else:
            status = page.get("status")
            if page.get("ok") and not scraper._looks_broken(page.get("text") or ""):
                self._probes[url] = (page, status)
            else:
                self._probes[url] = (None, status)
        return self._probes[url]

    def apollo(self, filters: dict) -> list:
        """One Apollo search per filter-set per run (memoized)."""
        key = json.dumps(filters, sort_keys=True, default=str)
        if key not in self._apollo:
            try:
                self._apollo[key] = ("ok", self.apollo_search(filters))
            except Exception as exc:
                self._apollo[key] = ("exc", exc)
        kind, payload = self._apollo[key]
        if kind == "exc":
            raise payload
        return payload

    def linkedin(self, url: str, cfg_linkedin) -> dict:
        """One LinkedIn harvest per URL per run (memoized)."""
        if url not in self._linkedin:
            try:
                self._linkedin[url] = (
                    "ok", self.linkedin_harvest(url, cfg_linkedin, self.conn)
                )
            except Exception as exc:
                self._linkedin[url] = ("exc", exc)
        kind, payload = self._linkedin[url]
        if kind == "exc":
            raise payload
        return payload

    def resolve_page(self, lead, keywords: tuple, well_known_paths: tuple) -> tuple | None:
        """Finds one page for a category: same-domain homepage links matching
        the keywords first, then the well-known standard paths.

        Returns one of:
          (page, False)      — a usable page was found;
          (None, True)       — at least one probed path returned a definitive
                               absent status (404/410) and no usable page
                               exists: the page is CONFIRMED ABSENT;
          (None, False)      — probes failed at the network layer only: NOT
                               absent, the snapshot must be left untouched.
        Returns None when the lead has no resolvable base URL.
        """
        home = self.homepage(lead)
        base = _base_url(lead.get("website_url"))
        if base is None:
            return None
        candidates = []
        if home:
            for link in home.get("links") or []:
                low = link.lower()
                if not _same_domain(link, base):
                    continue
                if any(k in low for k in keywords):
                    candidates.append(link)
                    break
        candidates.extend(base + path for path in well_known_paths)
        confirmed_absent = False
        for url in candidates:
            page, status = self._fetch_probe(
                url, self.cfg.website.page_timeout, self.cfg.website.per_domain_delay
            )
            if page is not None:
                return page, False
            if status in ABSENT_STATUS_CODES:
                confirmed_absent = True
        return None, confirmed_absent


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso(now=None) -> str:
    if now:
        return now
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or "").lower()


def _base_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "https://" + url)
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "https"
    return f"{scheme}://{parsed.netloc}".rstrip("/")


def _same_domain(link: str, base: str) -> bool:
    return _host(link).endswith(_host(base)) or _host(base).endswith(_host(link))


def _load_snapshot(lead) -> dict:
    raw = lead.get("trigger_state")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _is_builder_subdomain_host(host: str) -> str | None:
    """Returns the builder name if the host sits on a builder subdomain
    (e.g. acme.lovable.app -> 'lovable'), else None."""
    low = host.lower()
    for builder, suffix in scraper.BUILDER_SUBDOMAIN_SUFFIXES.items():
        suffix = suffix.lstrip(".")
        if low.endswith(suffix) and len(low) > len(suffix):
            return builder
    return None


def _is_high_value_lead(lead, cfg) -> bool:
    if lead.get("needs_human_review"):
        return False
    if lead.get("segment") not in TARGET_SEGMENTS:
        return False
    return float(lead.get("confidence") or 0) >= cfg.triggers.high_value_confidence


def _max_employee_count(people: list[dict]) -> int | None:
    best = None
    for p in people or []:
        org = p.get("organization") or {}
        for key in (
            "estimated_num_employees",
            "headcount",
        ):
            raw = org.get(key)
            if raw is None and key == "headcount":
                raw = p.get("estimated_num_employees")
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            best = value if best is None else max(best, value)
            break
    return best


def _apollo_org_filters(lead) -> dict:
    host = _host(lead.get("website_url"))
    domain = None
    if host:
        labels = host.split(".")
        if len(labels) > 2:
            domain = ".".join(labels[-2:])
        else:
            domain = host
    if domain:
        return {"organization_domains": [domain]}
    company = (lead.get("company_name") or "").strip()
    return {"q_keywords": company} if company else {}


# ---------------------------------------------------------------------------
# Customer/logo extraction (deterministic textual)
# ---------------------------------------------------------------------------


def _extract_named_customers(text: str) -> list[str]:
    """Named-customer snapshot: Title-case phrases sitting right after a
    customer-showcase marker, with generic words filtered out. Pure text rule
    (no network, no LLM); enough to detect "a new named customer appeared"."""
    if not text:
        return []
    found: list[str] = []
    lowered = text.lower()
    for marker in CUSTOMER_SECTION_MARKERS:
        start = 0
        while True:
            idx = lowered.find(marker, start)
            if idx < 0:
                break
            chunk = text[idx + len(marker): idx + len(marker) + 300]
            for match in _CUSTOMER_NAME_RE.finditer(chunk):
                name = " ".join(match.group(1).split())
                words = name.split()
                if len(words) < 1 or len(words) > 4:
                    continue
                if name.lower().split()[0] in CUSTOMER_GENERIC_TOKENS:
                    continue
                if all(w.lower() in CUSTOMER_GENERIC_TOKENS for w in words):
                    continue
                if name not in found:
                    found.append(name)
            start = idx + len(marker)
        if len(found) >= 8:
            break
    return found[:8]


def _detect_funding(results_by_source: dict) -> list[str]:
    """Scans web-search hits for funding-announcement snippets."""
    found: list[str] = []
    for source, hits in (results_by_source or {}).items():
        for hit in hits or []:
            content = hit.get("content") or hit.get("title") or ""
            if not content:
                continue
            for pattern in FUNDING_PATTERNS:
                m = pattern.search(content)
                if m:
                    snippet = " ".join(content[max(0, m.start() - 40): m.start() + 90].split())
                    if snippet and snippet not in found:
                        found.append(snippet[:160])
                    break
        if len(found) >= 5:
            break
    return found[:5]


def _pain_detected(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in PAIN_MARKERS)


# ---------------------------------------------------------------------------
# Per-check functions
# ---------------------------------------------------------------------------


def _check_hiring_engineer(ctx, lead, snapshot):
    """Careers page gains an engineering role (diff of the careers signal)."""
    res = ctx.resolve_page(lead, CAREERS_KEYWORDS, CAREERS_PATHS)
    if res is None:
        return None
    page, absent = res
    if absent:
        # Careers CONFIRMED absent (404/410): flip the stored state to False so
        # a later careers page/engineering hire is a real True->False->True
        # transition and re-fires.
        return ({"has_careers_page": False, "hiring_technical": False,
                 "engineering_keywords_found": []}, [])
    if page is None:
        return None
    sig = scraper.extract_careers_signal((page.get("text") or ""))
    has_careers = bool(sig.get("has_careers_page_content"))
    engineering = has_careers and bool(sig.get("hiring_technical"))
    new_state = {
        "has_careers_page": has_careers,
        "hiring_technical": bool(sig.get("hiring_technical")),
        "engineering_keywords_found": sorted(sig.get("engineering_keywords_found") or []),
    }
    prior = snapshot.get("hiring_engineer")
    fired = []
    if prior and not (prior.get("has_careers_page") and prior.get("hiring_technical")) and engineering:
        roles = ", ".join(new_state["engineering_keywords_found"][:3]) or "their first engineer"
        fired = [_event(lead, ctx, "hiring_engineer",
                        f"careers page now hiring: {roles}",
                        hook="Saw you're hiring an engineer")]
    return new_state, fired


def _check_pricing_introduced(ctx, lead, snapshot):
    """A pricing page appears, or free -> paid (a visible price appears)."""
    res = ctx.resolve_page(lead, PRICING_KEYWORDS, PRICING_PATHS)
    if res is None:
        return None
    page, absent = res
    if absent:
        # Pricing CONFIRMED absent (404/410): flip the stored state to False so
        # a later pricing page re-fires (True -> False -> True).
        return ({"has_pricing_page": False, "has_visible_price": False,
                 "pricing_motion": None}, [])
    if page is None:
        return None
    text = (page.get("text") or "")[:8000]
    sig = None
    try:
        sig = scraper.extract_pricing_signal(text)
    except Exception:
        sig = None
    has_pricing = bool(sig and sig.get("has_pricing_page_content"))
    has_visible_price = bool(sig and sig.get("has_visible_price"))
    # Uri-plan fallback: even without a dedicated pricing page the word may
    # appear on the homepage or well-known /pricing page that failed parsing.
    has_visible_price = has_visible_price or bool(
        any(w in text.lower() for w in PRICE_WORDS)
    )
    new_state = {
        "has_pricing_page": has_pricing,
        "has_visible_price": has_visible_price,
        "pricing_motion": (sig or {}).get("pricing_motion"),
    }
    prior = snapshot.get("pricing_introduced")
    fired = []
    if prior:
        if not prior.get("has_pricing_page") and has_pricing:
            fired = [_event(lead, ctx, "pricing_introduced",
                            "pricing page appeared",
                            hook="Saw you added a pricing page")]
        elif not prior.get("has_visible_price") and has_visible_price:
            fired = [_event(lead, ctx, "pricing_introduced",
                            "a visible price now appears",
                            hook="Saw you added a pricing page")]
    return new_state, fired


def _check_compliance_page(ctx, lead, snapshot):
    """A privacy / DPA / SOC2 page appears."""
    res = ctx.resolve_page(lead, COMPLIANCE_KEYWORDS, COMPLIANCE_PATHS)
    if res is None:
        return None
    page, absent = res
    if absent:
        # Compliance CONFIRMED absent (404/410): flip the stored state to False
        # so a later compliance page re-fires (True -> False -> True).
        return ({"compliance_page": False, "markers": []}, [])
    if page is None:
        return None
    text = (page.get("text") or "").lower()
    markers = [m for m in COMPLIANCE_KEYWORDS if m in text]
    new_state = {"compliance_page": bool(markers), "markers": markers}
    prior = snapshot.get("compliance_page")
    fired = []
    if prior and not prior.get("compliance_page") and new_state["compliance_page"]:
        fired = [_event(lead, ctx, "compliance_page",
                        "compliance page appeared: " + ", ".join(markers),
                        hook="Saw you added a compliance page")]
    return new_state, fired


def _check_custom_domain_move(ctx, lead, snapshot):
    """Site left a builder subdomain (e.g. acme.lovable.app -> acme.com)."""
    host = _host(lead.get("website_url"))
    builder = _is_builder_subdomain_host(host)
    new_state = {
        "host": host,
        "on_builder_subdomain": builder is not None,
        "builder": builder,
        "found_custom_domain": None,
    }
    if builder is None:
        # Nothing to move FROM: this lead was already on its own domain.
        return new_state, []
    home = ctx.homepage(lead)
    custom = None
    if home:
        custom = _discover_custom_domain(home, host, builder)
    new_state["found_custom_domain"] = custom
    prior = snapshot.get("custom_domain_move")
    fired = []
    if (
        prior
        and prior.get("on_builder_subdomain")
        and not prior.get("found_custom_domain")
        and custom
    ):
        fired = [_event(lead, ctx, "custom_domain_move",
                        f"now served on {custom} (was on {builder} subdomain)",
                        hook=f"Saw you moved off the {builder} subdomain")]
    return new_state, fired


def _discover_custom_domain(home: dict, old_host: str, builder: str) -> str | None:
    """Finds a plausible custom domain the builder-subdomain site now points
    at: a same-brand apex from the homepage's own links/og metadata."""
    label = old_host.split(".")[0].lower()
    candidates = list(home.get("links") or [])
    html = home.get("html") or ""
    for attr in ("og:url", "og:image", "twitter:url"):
        m = re.search(rf'{attr}[^>]*content=["\']([^"\']+)["\']', html, re.I)
        if m:
            candidates.append(m.group(1))
    for candidate in candidates:
        try:
            c_host = _host(candidate)
        except Exception:
            continue
        if not c_host or c_host == old_host:
            continue
        if c_host.endswith(f".{builder}") or any(c_host.endswith(f".{m}") for m in STORE_HOST_MARKERS):
            continue
        c_label = c_host.split(".")[0].lower()
        if c_label == label or c_label.startswith(label) or c_label.endswith(label):
            return c_host
    return None


def _check_enterprise_logo(ctx, lead, snapshot):
    """A named customer appears on the homepage (logo wall)."""
    home = ctx.homepage(lead)
    if home is None:
        return None
    customers = _extract_named_customers(home.get("text") or "")
    new_state = {"named_customers": customers}
    prior = snapshot.get("enterprise_logo")
    fired = []
    if prior and not prior.get("named_customers") and customers:
        fired = [_event(lead, ctx, "enterprise_logo",
                        "named customer(s) on homepage: " + ", ".join(customers[:3]),
                        hook="Saw a new named customer")]
    return new_state, fired


def _check_app_store_launch(ctx, lead, snapshot):
    """An App Store / Google Play / TestFlight badge appears."""
    home = ctx.homepage(lead)
    if home is None:
        return None
    store_links = sorted({
        u for u in (home.get("links") or [])
        if any(_host(u).endswith(st) for st in APP_STORE_HOSTS)
    })
    new_state = {"store_links": store_links[:5]}
    prior = snapshot.get("app_store_launch")
    fired = []
    if prior and not prior.get("store_links") and store_links:
        fired = [_event(lead, ctx, "app_store_launch",
                        "app store listing appeared: " + store_links[0],
                        hook="Saw your app live on the App Store")]
    return new_state, fired


def _check_product_hunt(ctx, lead, snapshot):
    """A Product Hunt listing/badge appears (site link, or paid SGAI searched
    evidence — credit-governed like team_growth)."""
    new_state = {"product_hunt_link": False, "ph_seen": False}
    home = ctx.homepage(lead)
    if home:
        link = next(
            (u for u in home.get("links") or []
             if "producthunt.com" in _host(u)),
            None,
        )
        if link:
            new_state["product_hunt_link"] = True
    if not new_state["product_hunt_link"]:
        results = ctx.governed_websearch(lead)
        if results is None:
            # Monthly budget exhausted: pause the paid search lane, keep the
            # prior snapshot untouched (no state churn, no false re-fire).
            return None
        ph_hits = results.get("product_hunt") or []
        new_state["ph_seen"] = bool(ph_hits)
    prior = snapshot.get("product_hunt")
    fired = []
    appeared = new_state["product_hunt_link"]
    if not appeared and new_state["ph_seen"]:
        appeared = True
    if prior and not (prior.get("product_hunt_link") or prior.get("ph_seen")) and appeared:
        fired = [_event(lead, ctx, "product_hunt",
                        "Product Hunt listing appeared",
                        hook="Saw you launched on Product Hunt")]
    return new_state, fired


def _check_team_growth(ctx, lead, snapshot):
    """Apollo headcount rises past a threshold since the last snapshot.

    Governor hook: the (free) search is gated behind
    apollo_client.check_credit_budget — Apollo-touching checks pause when the
    account is already over its monthly credit budget, setting the
    apollo_budget_reached snapshot flag (INDEPENDENT of the SGAI budget that
    gates funding_announced/product_hunt). The apollo_usage table
    is guaranteed by db._schema_sql (and ensured up-front by
    run_checks_for_lead before the per-lead transaction opens, because its
    CREATE+commit would otherwise release the row lock mid-transaction).
    """
    prior = snapshot.get("team_growth")
    try:
        apollo_client.check_credit_budget(
            ctx.conn, 0, ctx.cfg.apollo.monthly_credit_cap
        )
        filters = _apollo_org_filters(lead)
        if not filters:
            return None
        people = ctx.apollo(filters)
    except apollo_client.ApolloCreditCapReached:
        ctx.budget_flags["apollo"] = True
        return None
    current = _max_employee_count(people)
    new_state = {"headcount": current, "as_of": ctx.now_iso()}
    fired = []
    if prior and prior.get("headcount") is not None and current is not None:
        if current >= prior["headcount"] + ctx.cfg.triggers.team_growth_delta:
            fired = [_event(lead, ctx, "team_growth",
                            f"team headcount {prior['headcount']} -> {current}",
                            hook="Saw your team grew")]
    return new_state, fired


def _check_funding_announced(ctx, lead, snapshot):
    """A new funding announcement surfaces in web search — a PAID SGAI search
    (credit-governed like team_growth)."""
    results = ctx.governed_websearch(lead)
    if results is None:
        # Monthly budget exhausted: pause the paid search lane, keep the
        # prior snapshot untouched (no state churn, no false re-fire).
        return None
    mentions = _detect_funding(results)
    new_state = {"funding_mentions": mentions, "as_of": ctx.now_iso()}
    prior = snapshot.get("funding_announced")
    fired = []
    if prior and mentions:
        already = set(prior.get("funding_mentions") or [])
        new_mentions = [m for m in mentions if m not in already]
        if new_mentions:
            fired = [_event(lead, ctx, "funding_announced",
                            "funding news: " + new_mentions[0][:140],
                            hook="Saw your funding news")]
    return new_state, fired


def _check_founder_posts_pain(ctx, lead, snapshot):
    """EXPENSIVE: founder LinkedIn posts describing a pain point. Only called
    for high-scoring leads (gated in run_checks_for_lead)."""
    prior = snapshot.get("founder_posts_pain") or {}
    was_tracked = "founder_posts_pain" in snapshot
    seen = set(prior.get("seen_posts") or [])
    url = (lead.get("linkedin_url") or "").strip()
    if not url:
        # Preserve prior seen posts; nothing new to observe.
        return {"status": "no_linkedin_url", "seen_posts": sorted(seen)}, []
    try:
        result = ctx.linkedin(url, ctx.cfg.linkedin)
    except Exception as exc:
        return {"status": f"failed: {exc.__class__.__name__}", "seen_posts": sorted(seen)}, []
    new_seen = set(seen)
    fired_list = []
    for hit in result.get("hits") or []:
        hit_url = hit.get("url")
        if not hit_url or hit_url in new_seen:
            continue
        if not _pain_detected((hit.get("title") or "") + "\n" + (hit.get("content") or "")):
            continue
        # Baseline-first: the first observation never fires.
        if was_tracked:
            snippet = (hit.get("title") or hit.get("content") or "")[:120].strip()
            fired_list.append(_event(lead, ctx, "founder_posts_pain",
                                     f"founder post: {snippet}",
                                     hook="Saw the founder posting about a pain point"))
        new_seen.add(hit_url)
    # On a failed/capped harvest, keep the previously-seen posts so a later
    # retry can still detect posts that are NEW since the last GOOD harvest.
    if result.get("status") != "ok" and prior:
        new_seen = set(prior.get("seen_posts") or [])
        fired_list = []
    new_state = {"status": result.get("status"), "seen_posts": sorted(new_seen)}
    return new_state, fired_list


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# trigger name <-> snapshot key used by each check (they match by convention,
# kept explicit so a rename can't silently desync the stored state).
_CHECK_KEYS = {
    "hiring_engineer": "hiring_engineer",
    "pricing_introduced": "pricing_introduced",
    "compliance_page": "compliance_page",
    "custom_domain_move": "custom_domain_move",
    "enterprise_logo": "enterprise_logo",
    "app_store_launch": "app_store_launch",
    "product_hunt": "product_hunt",
    "team_growth": "team_growth",
    "funding_announced": "funding_announced",
    "founder_posts_pain": "founder_posts_pain",
}


def _event(lead, ctx, trigger: str, detail: str, hook: str) -> dict:
    return {
        "lead_id": lead.get("id"),
        "company_name": lead.get("company_name"),
        "trigger": trigger,
        "detail": detail,
        "hook": hook,
        "priority": TRIGGER_PRIORITIES.get(trigger, 1),
        "detected_at": ctx.now_iso(),
    }


def run_checks_for_lead(conn, lead, cfg, *, fetch=None, apollo_search=None,
                        web_search=None, linkedin_harvest=None, now=None) -> list[dict]:
    """Runs the tier-appropriate checks for one scored lead.

    Baseline-first: the first run stores each check's observation in
    `lead.trigger_state` without firing; a LATER change fires exactly one
    event. Events are persisted to lead_trigger_events, the lead's
    trigger_priority/trigger_hook are bumped, and the fired hook is injected
    into the latest lead_scores.personalization_hooks (idempotent, exactly one
    active trigger hook for the outreach paths). Returns the fired events.

    Two phases, so the row lock is held ONLY around the final
    re-read-compare-write — never around HTTP I/O:

    - Phase A (NO lock): every check runs against the snapshot the lead came
      in with and produces its observation `{check_key: new_state}`. All
      network happens here; every observation is memoized on the context.
    - Phase B (SHORT locked transaction): the leads row is re-read under a
      row-level lock (SELECT ... FOR UPDATE on PostgreSQL), each phase-A
      observation is diffed against that FRESH snapshot, and the writes
      (events, trigger fields, hook injection, snapshot, next_check_at) are
      committed in one go. Because every observation was cached in phase A,
      phase B performs zero network calls. On any failure the transaction
      rolls back — no partial writes.

    Concurrency: two overlapping scheduler runs serialize on the row lock, so
    they cannot both fire the same transition or clobber each other's snapshot
    write. The diff uses the snapshot re-read under the lock, not the one the
    lead arrived with.
    """
    ctx = _Ctx(conn, cfg,
               fetch=fetch, apollo_search=apollo_search,
               web_search=web_search, linkedin_harvest=linkedin_harvest, now=now)
    # The usage tables must exist before the per-lead transaction opens:
    # creating them commits, which would release the row lock mid-transaction.
    try:
        apollo_client.ensure_usage_table(conn)
    except Exception:
        pass
    try:
        sgai_client.ensure_usage_table(conn)
    except Exception:
        pass

    # Phase A — produce every observation. No lock, no writes; the only place
    # network happens (the slow part of a trigger run).
    checks = _checks_for(lead, cfg)
    start = _load_snapshot(lead)
    candidates: dict = {}
    for check in checks:
        try:
            new_state, _events = check(ctx, lead, start)
        except Exception:
            # Per-check isolation: one broken check never kills the run.
            continue
        if new_state is not None:
            candidates[_key_for(check)] = check

    # Phase B — short locked transaction: re-read fresh state under the lock,
    # diff the cached observations against it, write, commit.
    fired: list[dict] = []
    try:
        fresh = dbmod.lock_lead_trigger_row(conn, lead["id"]) or {}
        snapshot = fresh.get("trigger_state") or {}
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (json.JSONDecodeError, TypeError):
                snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        for check in candidates.values():
            try:
                new_state, events = check(ctx, lead, snapshot)
            except Exception:
                # Per-check isolation: one broken check never kills the run.
                continue
            if events:
                for ev in events:
                    fired.append(ev)
                    dbmod.save_lead_trigger_event(
                        conn, ev["lead_id"], ev["trigger"], ev["detected_at"],
                        ev["detail"], commit=False,
                    )
            if new_state is not None:
                snapshot[_key_for(check)] = new_state

        # Vendor-specific budget flags: WHICH vendor was over its cap this run
        # (or recovered). Persisted so logs/debug output never need to
        # cross-reference provider dashboards.
        snapshot["apollo_budget_reached"] = ctx.budget_flags["apollo"]
        snapshot["sgai_budget_reached"] = ctx.budget_flags["sgai"]

        if fired:
            best = max(fired, key=lambda ev: ev["priority"])
            prior_priority = fresh.get("trigger_priority") or lead.get("trigger_priority") or 0
            dbmod.update_lead_trigger_fields(
                conn,
                lead["id"],
                trigger_priority=max(prior_priority, best["priority"]),
                trigger_hook=best["hook"],
                commit=False,
            )
            # The fired hook reaches outreach through the verdict field the
            # email prompt and the Instantly export already read.
            dbmod.inject_trigger_hook(
                conn,
                lead["id"],
                {"hook": best["hook"], "based_on": (best["detail"] or "")[:300],
                 "source": "trigger"},
                commit=False,
            )

        dbmod.update_lead_trigger_fields(
            conn,
            lead["id"],
            trigger_state=json.dumps(snapshot, ensure_ascii=False),
            next_check_at=ctx.next_check_at(),
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fired


def _checks_for(lead, cfg) -> list:
    """The tier-appropriate checks for this lead (shared by both phases)."""
    checks = [
        _check_hiring_engineer,
        _check_pricing_introduced,
        _check_compliance_page,
        _check_custom_domain_move,
        _check_enterprise_logo,
        _check_app_store_launch,
        _check_product_hunt,
        _check_team_growth,
        _check_funding_announced,
    ]
    if _is_high_value_lead(lead, cfg):
        # Expensive LinkedIn lane — gated to high-scoring leads only.
        checks.append(_check_founder_posts_pain)
    return checks


def _key_for(check) -> str:
    return _CHECK_KEYS[check.__name__.split("_", 2)[2]]


# ---------------------------------------------------------------------------
# Scheduler entry (used by tools/run_triggers.py)
# ---------------------------------------------------------------------------


def run_due_leads(conn, leads, cfg, *, now=None, **seams) -> dict:
    """Runs the tier-appropriate checks on all due leads; returns a summary."""
    summary = {
        "checked": 0, "fired": 0, "failed": 0, "events": [], "errors": [],
        "budget_paused": {"apollo": 0, "sgai": 0},
    }
    for lead in leads:
        try:
            events = run_checks_for_lead(conn, lead, cfg, now=now, **seams)
        except Exception as exc:
            # Per-lead isolation, but never silent: the failure is counted and
            # named in the summary so the scheduler log shows WHICH lead broke.
            summary["failed"] += 1
            summary["errors"].append({
                "lead_id": lead.get("id"),
                "company": lead.get("company_name") or lead.get("website_url") or "",
                "error": f"{exc.__class__.__name__}: {exc}"[:300],
            })
            log.warning("trigger run failed for lead %s: %s", lead.get("id"), exc)
            continue
        summary["checked"] += 1
        if events:
            summary["fired"] += len(events)
            summary["events"].extend(events)
        # Which vendors were over budget for this lead at the last run? Read
        # back the snapshot the run just wrote (vendor flags live there).
        row = conn.execute(
            "SELECT trigger_state FROM leads WHERE id = ?", (lead["id"],)
        ).fetchone()
        state = _load_snapshot({"trigger_state": row["trigger_state"] if row else None})
        if state.get("apollo_budget_reached"):
            summary["budget_paused"]["apollo"] += 1
        if state.get("sgai_budget_reached"):
            summary["budget_paused"]["sgai"] += 1
    return summary