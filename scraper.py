"""
Fetch and parse the website via Firecrawl.

Steps:
1. Scrape the site (Firecrawl API): homepage + up to 4 key pages
   (about, pricing, careers, product).
2. Extract deterministic technical signals (DOM/CSS/meta/git):
   AI builder fingerprint, trending fonts, visual patterns,
   "vibe-coding" language, GitHub commit analysis.

All signals computable by rule are computed here, without any LLM call.
The LLM (scorer.py) only receives the text + these signals as JSON.
"""

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from dotenv import load_dotenv
from firecrawl import Firecrawl

load_dotenv()
KEYWORDS = {
    "about": ["about", "team"],
    "pricing": ["pricing", "plans", "price"],
    "careers": ["careers", "jobs"],
    "product": ["product", "services", "solutions", "features"],
}

# Fallback when link-based discovery (homepage -> result.links) finds
# nothing for a category: the most common standard paths, tried in order.
# Useful for sites where the nav is not exposed in the static HTML (menu
# generated in JS, Firecrawl misses it), or that simply have no direct
# link to those pages from the home page.
COMMON_PATH_CANDIDATES = {
    "about": ["/about", "/about-us", "/team", "/company"],
    "pricing": ["/pricing", "/plans"],
    "careers": ["/careers", "/jobs"],
    "product": ["/product", "/products", "/services", "/solutions", "/features"],
}

MAX_CONTENT_CHARS_PER_PAGE = 32000  # ~8000 tokens, free tier guardrail

# Pages that return HTTP 200 on the Firecrawl side but whose render crashed
# on the client side (React/Next.js SPA that crashes before displaying the
# real content), or real error/404 pages. Without this filter, this text is
# sent as-is to the scoring as if it were the site's real content.
#
# Literal markers: fast, but miss formatting variants (e.g. "# 404\n\nPage
# Not Found" never contains the exact substring "404 not found"). Hence the
# addition of BROKEN_PAGE_PATTERNS as a complement.
BROKEN_PAGE_MARKERS = [
    "client-side exception has occurred",
    "application error",
    "hydration failed",
    "unhandled runtime error",
    "this page could not be found",
    "404 not found",
    "404: this page could not be found",
    "500 internal server error",
]

# Regex patterns: cover 404s/errors where the number and the message are
# separated (Markdown heading "# 404" followed by a "Page Not Found"
# paragraph), regardless of the exact word order or punctuation.
BROKEN_PAGE_PATTERNS = [
    r"#\s*404\b",                          # Markdown heading "# 404"
    r"\b404\b.{0,40}page\s+not\s+found",   # "404 ... page not found" (max 40 chars gap)
    r"page\s+not\s+found.{0,40}\b404\b",   # reverse order
    r"\boops!?\b.{0,60}vanished",          # phrases like "Oops! ... vanished into thin air"
    r"\bpage\s+(?:you('|')?re\s+)?looking\s+for\b.{0,60}(?:doesn'?t\s+exist|not\s+found|vanished)",
]

MIN_VALID_CONTENT_CHARS = 50  # below this, content too trivial to be a real page

# ---------------------------------------------------------------------------
# Deterministic signatures — checklist from the monitoring (Design Slop Cop,
# isthatvibecoded.com, detectvibecode.com...). Each entry = a simple regex
# on the raw HTML. None of these values is judged by an LLM.
# ---------------------------------------------------------------------------

GENERATOR_FINGERPRINTS = {
    # builder name → patterns searched in the raw HTML / meta / links
    "lovable": [r"lovable\.dev", r"lovable-tagger", r"gpteng\.co"],
    "v0": [r"v0\.dev", r"vusercontent\.net"],
    "bolt": [r"bolt\.new", r"stackblitz"],
    "replit": [r"replit\.com", r"replit\.dev"],
    "cursor": [r"built with cursor", r"cursor\.sh"],
}

TREND_FONTS = [
    "Space Grotesk", "Instrument Serif", "Geist", "Syne", "Fraunces",
]

VISUAL_PATTERNS = {
    "purple_accent": [r"(?:bg|text|border)-(?:indigo|violet|purple)-[4-7]00"],
    "gradient": [r"bg-gradient-to-\w+", r"from-\w+-\d{3}\s+to-\w+-\d{3}"],
    "glassmorphism": [r"backdrop-blur", r"backdrop-filter"],
    "colored_glow": [r"shadow-(?:indigo|violet|purple|blue)-\d{3}"],
    "numbered_steps": [r"step[\s\-_]?[1-3]", r"how[\s\-]it[\s\-]works"],
    "stat_banner": [r"\d[\d,\.]*\s?(?:\+|k\+)\s*(?:users|customers|clients)"],
    "headline_badge": [r"rounded-full[^\"']*(?:badge|pill|eyebrow)"],
    "faq_accordion": [r"frequently asked questions", r"faq[\s\-_]accordion"],
    "shadcn_ui": [r"data-radix-", r"class=\"[^\"]*\bring-offset-background\b"],
}

VIBE_LANGUAGE_MARKERS = [
    "built with cursor", "built with v0", "made with lovable", "built with bolt",
    "vibe coded", "vibe-coded", "no-code",
]

# Typical writing style of AI-generated/assisted content — independent of
# VIBE_LANGUAGE_MARKERS (which detects explicit mentions of build tools).
# Here we detect clichéd marketing turns of phrase, over-represented in
# generated or heavily LLM-assisted copywriting. A single match is not
# significant; it is the density (several distinct occurrences on the same
# page) that is the useful signal.
AI_STYLE_PHRASES = [
    "seamlessly integrate", "seamless integration", "unlock the power of",
    "unlock the full potential", "elevate your", "revolutionize the way",
    "revolutionize how", "game-changer", "game changing", "cutting-edge",
    "state-of-the-art", "harness the power of", "empower you to",
    "empower your team", "in today's fast-paced world", "in today's digital age",
    "in today's rapidly evolving", "at the intersection of", "whether you're a",
    "whether you are a", "dive into", "navigate the complexities of",
    "take your business to the next level", "robust and scalable",
    "effortlessly", "streamline your workflow", "supercharge your",
    "unleash the power", "transform the way you", "tailored to your needs",
    "one-stop solution", "end-to-end solution", "peace of mind",
    "step into the future", "reimagine how", "redefine how",
]

# Explicit mentions of AI-generated content (distinct from the two lists
# above: here the company says it itself, not a stylistic inference).
AI_AUTHORSHIP_DISCLOSURES = [
    "written with ai", "generated with ai", "powered by gpt", "powered by chatgpt",
    "ai-generated content", "content generated by ai", "drafted by ai",
]


# ---------------------------------------------------------------------------
# Extraction of targeted signals — careers & pricing (point #3 of the fix
# plan). Instead of sending the raw text of these two pages to the LLM
# (often 3-8K characters of HR boilerplate or pricing grid), we compute
# here a compact deterministic signal. Double win: volume divided by
# ~20-50x, and a more reliable signal than a text interpretation by LLM.
# ---------------------------------------------------------------------------

ENGINEERING_ROLE_KEYWORDS = [
    "engineer", "developer", "backend", "back-end", "frontend", "front-end",
    "full-stack", "fullstack", "devops", "sre", "site reliability",
    "data scientist", "machine learning", "ml engineer", "software architect",
    "qa engineer", "sdet", "platform engineer",
]

OTHER_ROLE_KEYWORDS = [
    "sales", "account executive", "marketing", "customer success", "support",
    "designer", "product manager", "operations", "recruiter", "finance",
    "content writer", "community manager", "growth",
]

SELF_SERVE_CTA_MARKERS = [
    "sign up", "start free trial", "start your free trial", "get started free",
    "buy now", "start for free", "try for free", "subscribe now", "upgrade now",
]

SALES_LED_CTA_MARKERS = [
    "contact us", "book a demo", "talk to sales", "request a quote",
    "schedule a call", "contact sales", "get in touch", "request a demo",
]

VISIBLE_PRICE_PATTERN = re.compile(r"[$€£]\s?\d|\b\d+\s?(?:/mo|/month|per month)\b", re.IGNORECASE)


def extract_careers_signal(content: str) -> dict:
    """
    Deterministic signal on a careers/jobs page: ratio of technical vs
    non-technical roles mentioned. Does NOT count the exact number of open
    positions (Firecrawl returns text, not a reliable structured list) —
    just the presence of technical vs non-technical vocabulary, which is
    enough as a signal of technical growth pressure (or its absence).
    """
    text = (content or "").lower()
    if not text.strip():
        return {"has_careers_page_content": False}

    eng_matches = sorted({kw for kw in ENGINEERING_ROLE_KEYWORDS if kw in text})
    other_matches = sorted({kw for kw in OTHER_ROLE_KEYWORDS if kw in text})
    total = len(eng_matches) + len(other_matches)

    return {
        "has_careers_page_content": True,
        "engineering_keywords_found": eng_matches,
        "other_keywords_found": other_matches,
        "hiring_technical": len(eng_matches) > 0,
        "engineering_ratio": round(len(eng_matches) / total, 2) if total else None,
    }


def extract_pricing_signal(content: str) -> dict:
    """
    Deterministic signal on a pricing page: self-serve motion (CTA "Sign
    up"/"Start free trial") vs sales-led (CTA "Contact us"/"Book a demo").
    A self-serve site with visible pricing is a signal of "early/scaling"
    stage, unlike a 100% sales-led site with no displayed price.
    """
    text = (content or "").lower()
    if not text.strip():
        return {"has_pricing_page_content": False}

    self_serve_hits = sorted({m for m in SELF_SERVE_CTA_MARKERS if m in text})
    sales_hits = sorted({m for m in SALES_LED_CTA_MARKERS if m in text})
    has_visible_price = bool(VISIBLE_PRICE_PATTERN.search(text))

    if self_serve_hits and not sales_hits:
        motion = "self_serve"
    elif sales_hits and not self_serve_hits:
        motion = "sales_led"
    elif self_serve_hits and sales_hits:
        motion = "mixed"
    else:
        motion = "unclear"

    return {
        "has_pricing_page_content": True,
        "self_serve_markers_found": self_serve_hits,
        "sales_led_markers_found": sales_hits,
        "has_visible_price": has_visible_price,
        "pricing_motion": motion,
    }


def _format_signal_as_text(label: str, signal: dict) -> str:
    """Compact and readable representation of a deterministic signal, for
    storage in `lead_content.content` instead of the raw text."""
    lines = [f"[{label} — deterministic extraction, no raw text]"]
    for key, value in signal.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


_client_pool: list[Firecrawl] | None = None
_client_pool_lock = threading.Lock()
_client_pool_dead: set[int] = set()


def _get_client_pool() -> list[Firecrawl]:
    """Creates (only once) one Firecrawl client per API key configured in .env.

    The pool is shared by all requests: a key that exhausts its quota is
    marked 'dead' for the rest of the execution and ignored — the other
    keys keep working in its place.
    """
    global _client_pool
    with _client_pool_lock:
        if _client_pool is None:
            keys = []
            for k in ("FIRECRAWL_API_KEY", "FIRECRAWL_API_KEY_2", "FIRECRAWL_API_KEY_3", "FIRECRAWL_API_KEY_4", "FIRECRAWL_API_KEY_5"):
                val = os.getenv(k)
                if val:
                    keys.append(val)
            if not keys:
                raise RuntimeError("No Firecrawl API key configured in .env")
            _client_pool = [Firecrawl(api_key=key, timeout=120) for key in keys]
        return _client_pool


def _is_quota_error(error_msg: str) -> bool:
    """True if the error means 'quota/credits exhausted' (key to ignore)."""
    msg = error_msg.lower()
    return any(kw in msg for kw in (
        "insufficient credits", "no credits", "out of credits", "quota exceeded",
        "credit limit", "not enough credits", "insufficient_credits",
        "402", "429 quota", "billing", "upgrade required", "payment required",
    ))


def _parse_retry_after(error_msg: str) -> float | None:
    """Extracts the `retry after Ns` delay from the Firecrawl error message.

    E.g. "... please retry after 14s, resets at ..." -> 14.0
    Returns None if not found.
    """
    m = re.search(r"retry after\s+([\d.]+)\s*s", error_msg, re.IGNORECASE)
    if m:
        return float(m.group(1)) + 1.0  # small safety margin
    return None


def _firecrawl_scrape(url: str, *args, **kwargs):
    """Wrapper around `app.scrape()` with a pool of Firecrawl keys.

    - Round-robin over all configured keys.
    - A key in QUOTA error is marked 'dead' for the whole run and ignored:
      the other keys keep working in its place.
    - A rate-limited key is bypassed immediately (move to the next one).
    - If ALL keys are dead/rate-limited, wait the shortest delay then retry
      one last pass.
    """
    clients = _get_client_pool()
    max_rounds = 2  # two full passes over all live keys
    last_exc = None

    for attempt_round in range(max_rounds):
        for idx, client in enumerate(clients):
            if idx in _client_pool_dead:
                continue
            try:
                return client.scrape(url, *args, **kwargs)
            except Exception as e:
                last_exc = e
                msg = str(e)
                if _is_quota_error(msg):
                    with _client_pool_lock:
                        _client_pool_dead.add(idx)
                    print(f"[scraper] Key {idx+1} out of credits, ignored for the rest ({url})")
                    continue
                if "rate limit" in msg.lower() or "rate_limit" in msg.lower():
                    delay = _parse_retry_after(msg)
                    if delay:
                        print(f"[scraper] Key {idx+1} rate-limited for {url}, "
                              f"switching to next key (retry after {delay:.0f}s)")
                    continue
                # Non-rate-limit, non-quota error: raise immediately
                raise

        # All live keys got rate-limited: wait before retrying
        delay = _parse_retry_after(str(last_exc)) or 15.0
        print(f"[scraper] All keys rate-limited for {url}, "
              f"waiting {delay:.0f}s before retry {attempt_round+2}/{max_rounds}")
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, 0.5)
            time.sleep(chunk)
            remaining -= chunk

    raise last_exc


def _scrape_pages_in_parallel(urls_by_category: dict[str, str], throttle_seconds: float = 1.0) -> dict:
    """Scrapes several pages in parallel, one thread per Firecrawl key.

    Distributes the URLs across threads (max 1 thread per key to avoid
    blowing a key's rate limit). A key with exhausted quota is ignored by
    _firecrawl_scrape, the others keep going.

    Returns {category: (url, FirecrawlResponse | Exception)}.
    """
    clients = _get_client_pool()
    n_workers = max(1, min(len(clients), len(urls_by_category)))
    if n_workers == 1:
        # Single key: sequential, but keep the original throttle
        results = {}
        for category, url in urls_by_category.items():
            time.sleep(throttle_seconds)
            try:
                r = _firecrawl_scrape(url, formats=["markdown"], only_main_content=True, timeout=10000)
                results[category] = (url, r)
            except Exception as e:
                results[category] = (url, e)
        return results

    # Parallel: worker i uses key i (round-robin of URLs across workers)
    items = list(urls_by_category.items())

    def _worker(url: str):
        return _firecrawl_scrape(url, formats=["markdown"], only_main_content=True, timeout=10000)

    results = {}
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {}
        for i, (category, url) in enumerate(items):
            future = pool.submit(_worker, url)
            futures[future] = category
        for future in as_completed(futures):
            category = futures[future]
            url = dict(urls_by_category)[category]
            try:
                r = future.result()
            except Exception as e:
                r = e
            results[category] = (url, r)
    return results


def _normalize_domain(url: str) -> str:
    """
    Returns the normalized netloc (no www., lowercase) of a URL, so two
    "same domain" URLs can be compared without being fooled by www vs non-www.
    """
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _is_same_domain(link: str, homepage_url: str) -> bool:
    """
    True only if `link` points to the same domain as the homepage.
    Prevents mistakenly matching an external link (e.g. g2.com, a third-party
    blog post, a LinkedIn page) just because the URL contains a keyword
    like "product" or "about".
    """
    link_domain = _normalize_domain(link)
    home_domain = _normalize_domain(homepage_url)
    return bool(link_domain) and link_domain == home_domain


def _is_real_subpage(link: str, homepage_url: str) -> bool:
    """
    Rejects anchors (#services) and any link that actually points to the
    same page as the homepage (common on single-page sites, very frequent
    among solo/vibe-coded founders — exactly our target).
    Without this filter, we would pay a Firecrawl credit to re-scrape
    identical content twice.

    NB: this filter only catches duplicates by URL/fragment. SPAs that
    serve identical content on *distinct* URLs (e.g. /about and /
    returning the same client-side shell) are NOT filtered here — they are
    filtered a posteriori in scrape_website() by content hash, once the
    text has actually been fetched.
    """
    link_no_fragment = link.split("#", 1)[0].rstrip("/")
    homepage_no_fragment = homepage_url.split("#", 1)[0].rstrip("/")
    if "#" in link and link_no_fragment == homepage_no_fragment:
        return False
    if link_no_fragment == homepage_no_fragment:
        return False
    return True


def _url_exists(url: str, timeout: float = 5.0) -> bool:
    """
    Checks that a candidate URL really responds (status < 400) BEFORE
    spending a Firecrawl credit on it. Uses `requests` (free) instead of
    attempting the scrape directly.

    HEAD first (lightest); if the host does not support it (405/501,
    frequent on Vercel/Netlify for dynamically generated routes), fall back
    to GET. Any exception (timeout, DNS, SSL...) = URL considered
    non-existent, never blocking for the rest of the pipeline.
    """
    import requests

    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (405, 501):
            resp = requests.get(url, timeout=timeout, allow_redirects=True, stream=True)
        return resp.status_code < 400
    except Exception:
        return False


def _looks_broken(markdown: str) -> bool:
    """
    Detects a page that technically responded but is not usable: client-side
    render crash, error/404 page, or nearly empty content.
    Does not judge anything about the site's substance — filters only the
    "technical noise" before it reaches the LLM scoring.

    Checks both literal markers (fast, standard cases) and regex patterns
    (covers "shattered" 404s across multiple lines/wordings,
    e.g. "# 404\n\nPage Not Found\n\nOops! ... vanished into thin air").
    """
    text = (markdown or "").strip()
    if len(text) < MIN_VALID_CONTENT_CHARS:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in BROKEN_PAGE_MARKERS):
        return True
    if any(re.search(p, lowered, flags=re.IGNORECASE) for p in BROKEN_PAGE_PATTERNS):
        return True
    return False


def _content_fingerprint(markdown: str) -> str:
    """
    Normalized content hash (collapsed spaces, case-insensitive) to detect
    pages that return identical text despite different URLs — typical of
    SPAs where all routes serve the same client-side shell before the real
    (JS) routing takes over, not captured by Firecrawl. Normalizing spaces
    avoids missing a duplicate because of a trivial spacing/line-break
    difference.
    """
    text = markdown or ""
    # Remove the noise that differs between two pages of the same SPA:
    # image URLs, links, base64, without losing the real text.
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'<Base64-Image-Removed>', '', text)
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _find_key_pages(homepage_url: str):
    # rawHtml in addition to markdown/links: needed for deterministic
    # signal extraction, markdown alone is not enough.
    result = _firecrawl_scrape(homepage_url, formats=["markdown", "rawHtml", "links"], timeout=10000)
    all_links = [
        link for link in (result.links or [])
        if _is_real_subpage(link, homepage_url)
    ]

    # Fix bug #2: only keep same-domain links for keyword matching.
    # An external link (g2.com, a review site, LinkedIn...) whose URL
    # contains "product" or "about" must never be chosen as the lead's
    # "product"/"about" page.
    same_domain_links = [
        link for link in all_links if _is_same_domain(link, homepage_url)
    ]

    found_pages = {"homepage": homepage_url}
    for category, keywords in KEYWORDS.items():
        for link in same_domain_links:
            link_lower = link.lower()
            if any(kw in link_lower for kw in keywords):
                found_pages[category] = link
                break

    # Automatic fallback: for any category not found through the homepage
    # links (JS nav not exposed to Firecrawl, site with no direct link,
    # etc.), try the most common standard paths. They are only scraped if
    # they actually exist (_url_exists, free) — never spend a Firecrawl
    # credit blindly on a 404.
    # (These candidates are built on the homepage domain, so they are
    # always "same domain" by construction — no need to re-filter here.)
    base = homepage_url.rstrip("/")
    for category, candidate_paths in COMMON_PATH_CANDIDATES.items():
        if category in found_pages:
            continue
        for path in candidate_paths:
            candidate_url = base + path
            if _url_exists(candidate_url):
                found_pages[category] = candidate_url
                break

    # Catch-all for "product" only: if the category is still empty after
    # keywords + standard paths (e.g. Linear uses /intake, /plan,
    # /build instead of /product), take the first unassigned link.
    # Only done for product (not about/careers/pricing) because it is the
    # only category vague enough to have a thousand different names.
    if "product" not in found_pages:
        assigned = set(found_pages.values())
        for link in same_domain_links:
            if link not in assigned and link != homepage_url:
                found_pages["product"] = link
                break

    # all_links (from any origin) is returned as-is for
    # extract_technical_signals, which needs to find an external GitHub
    # link — only the key-page matching must be restricted to the lead's
    # domain.
    return found_pages, result, all_links


def _match_any(patterns: list, text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def extract_technical_signals(raw_html: str, all_links: list, homepage_text: str = "") -> dict:
    """
    Step 3bis. Computes only deterministic signals (no LLM).
    Returns a dict ready to be injected as-is into the scoring prompt
    (`technical_signals` field of the LLM verdict), with the raw evidence
    attached to each triggered signal — never an already-interpreted verdict.

    NB: we do not go through Firecrawl's `result.metadata` (it is a
    Pydantic object without `.get()`, and nothing guarantees it captures an
    arbitrary <meta name="generator"> tag). We extract that tag directly
    from raw_html by regex — more reliable and independent of Firecrawl's
    parsing.
    """
    raw_html = raw_html or ""
    homepage_text = homepage_text or ""
    signals = {
        "generator_fingerprint": None,
        "vibe_language_matches": [],
        "trend_fonts_found": [],
        "visual_patterns_triggered": [],
        "generator_meta_tag": None,
        "github_repo_url": None,
        "ai_style_phrases_found": [],
        "ai_style_phrase_density": "none",
        "ai_authorship_disclosures_found": [],
    }

    generator_match = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        raw_html,
        flags=re.IGNORECASE,
    )
    if generator_match:
        signals["generator_meta_tag"] = generator_match.group(1)

    # Builder fingerprint (the strongest signal, cf. isthatvibecoded.com)
    for builder, patterns in GENERATOR_FINGERPRINTS.items():
        if _match_any(patterns, raw_html):
            signals["generator_fingerprint"] = builder
            break

    # Explicit language ("built with X") — searched as-is, not inferred
    lowered = raw_html.lower()
    signals["vibe_language_matches"] = [
        m for m in VIBE_LANGUAGE_MARKERS if m in lowered
    ]

    # Trending fonts
    signals["trend_fonts_found"] = [f for f in TREND_FONTS if f.lower() in lowered]

    # Visual patterns (14 categories in the Design Slop Cop style)
    signals["visual_patterns_triggered"] = [
        name for name, patterns in VISUAL_PATTERNS.items() if _match_any(patterns, raw_html)
    ]

    # Writing style: scanned on visible text, not raw HTML.
    lowered_text = homepage_text.lower()
    found_phrases = [phrase for phrase in AI_STYLE_PHRASES if phrase in lowered_text]
    signals["ai_style_phrases_found"] = found_phrases
    if len(found_phrases) >= 4:
        signals["ai_style_phrase_density"] = "high"
    elif len(found_phrases) >= 2:
        signals["ai_style_phrase_density"] = "medium"
    elif len(found_phrases) == 1:
        signals["ai_style_phrase_density"] = "low"

    signals["ai_authorship_disclosures_found"] = [
        disclosure for disclosure in AI_AUTHORSHIP_DISCLOSURES if disclosure in lowered_text
    ]

    # Public GitHub link, for the git check. Deliberately NOT
    # restricted to the same domain: a lead may legitimately link to an
    # external GitHub repo (GitHub org different from the site's domain).
    for link in all_links or []:
        if "github.com" in link.lower() and "/issues" not in link and "/pull" not in link:
            signals["github_repo_url"] = link
            break

    return signals


def check_github_repo_pattern(repo_url: str) -> dict:
    """
    Step 3ter (optional, only if a public repo was found).
    Checks the "single massive commit / generic message" pattern via the
    public GitHub API (unauthenticated, 60 req/h — throttle if used on
    many leads). Judges nothing: returns raw facts, the judgment
    ("vibe-coded or not") stays in the LLM scoring.
    """
    import requests

    result = {"repo_url": repo_url, "checked": False, "evidence": {}, "error": None}
    try:
        m = re.search(r"github\.com/([^/]+)/([^/?#]+)", repo_url)
        if not m:
            result["error"] = "Unrecognized GitHub URL"
            return result
        owner, repo = m.group(1), m.group(2)

        commits_resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            params={"per_page": 100},
            timeout=10,
        )
        if commits_resp.status_code != 200:
            result["error"] = f"GitHub API status {commits_resp.status_code}"
            return result

        commits = commits_resp.json()
        result["checked"] = True
        result["evidence"]["total_commits_seen"] = len(commits)
        if commits:
            first_commit = commits[-1]  # oldest on the fetched page
            result["evidence"]["first_commit_message"] = first_commit.get("commit", {}).get("message", "")
            result["evidence"]["single_commit_repo"] = len(commits) <= 1
    except Exception as e:
        result["error"] = str(e)

    return result


def scrape_website(homepage_url: str, throttle_seconds: float = 1.0) -> dict:
    """
    Scrapes the homepage + up to 4 key pages discovered automatically,
    and computes the deterministic technical signals on the homepage.

    Returns:
        {
            "status": "PARSED" | "FETCH_PARTIAL" | "FETCH_FAILED",
            "rows": [(source, url, content), ...],
            "technical_signals": {...} | None,
            "github_check": {...} | None,
            "error": str | None,
        }
    """
    try:
        pages, homepage_result, all_links = _find_key_pages(homepage_url)
    except Exception as e:
        err = str(e)
        print(f"[scraper] _find_key_pages failed for {homepage_url}: {err}")
        return {
            "status": "FETCH_FAILED",
            "rows": [],
            "technical_signals": None,
            "github_check": None,
            "error": err,
        }

    homepage_markdown = homepage_result.markdown or ""

    if _looks_broken(homepage_markdown):
        print(f"[scraper] Homepage broken/empty for {homepage_url} ({len(homepage_markdown)} chars)")
        return {
            "status": "FETCH_FAILED",
            "rows": [],
            "technical_signals": None,
            "github_check": None,
            "error": "homepage_render_error_or_empty_content",
        }

    rows = [("homepage", homepage_url, homepage_markdown[:MAX_CONTENT_CHARS_PER_PAGE])]
    seen_fingerprints = {_content_fingerprint(homepage_markdown)}

    technical_signals = extract_technical_signals(
        raw_html=getattr(homepage_result, "raw_html", None),
        all_links=all_links,
        homepage_text=homepage_markdown,
    )

    github_check = None
    if technical_signals.get("github_repo_url"):
        github_check = check_github_repo_pattern(technical_signals["github_repo_url"])

    failures = 0
    duplicates = 0
    other_pages = {k: v for k, v in pages.items() if k != "homepage"}

    # Scrape the key pages in parallel (1 thread per Firecrawl key).
    # A key with exhausted quota is ignored by the pool, the others continue.
    scraped_pages = _scrape_pages_in_parallel(other_pages, throttle_seconds=throttle_seconds)

    for category, url in other_pages.items():
        result = scraped_pages[category]
        if isinstance(result[1], Exception):
            failures += 1
            print(f"Failed on {category} ({url}): {result[1]}")
            continue
        r = result[1]
        raw_content = (r.markdown or "")[:MAX_CONTENT_CHARS_PER_PAGE]
        if _looks_broken(raw_content):
            failures += 1
            print(f"Page ignored (broken/empty render) on {category} ({url})")
            continue

        fingerprint = _content_fingerprint(raw_content)
        if fingerprint in seen_fingerprints:
            duplicates += 1
            print(
                f"Page ignored (content identical to an already "
                f"kept page, likely SPA shell) on {category} ({url})"
            )
            continue
        seen_fingerprints.add(fingerprint)

        if category == "careers":
            content = _format_signal_as_text("Careers", extract_careers_signal(raw_content))
        elif category == "pricing":
            content = _format_signal_as_text("Pricing", extract_pricing_signal(raw_content))
        else:
            content = raw_content

        rows.append((category, url, content))

    # A SPA duplicate is not a "failure" in the same way a timeout or a
    # 404 is (the page exists, it is just useless) — but it still counts
    # as "no additional useful content" when determining the final
    # status below.
    unusable = failures + duplicates

    if len(rows) == 1 and other_pages:
        status = "FETCH_PARTIAL" if unusable < len(other_pages) else "FETCH_FAILED"
    elif unusable > 0:
        status = "FETCH_PARTIAL"
    else:
        status = "PARSED"

    return {
        "status": status,
        "rows": rows,
        "technical_signals": technical_signals,
        "github_check": github_check,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Step 4 — Targeted web search (escalation).
# Uses the ScrapeGraphAI Search API (POST /api/search).
# Results are raw (url, title, content) — the judgment stays in the
# LLM scoring. Only triggered after scoring pass 1 for leads with low
# confidence or needs_human_review.
# ---------------------------------------------------------------------------

_SGAI_BASE_URL = "https://v2-api.scrapegraphai.com/api"

_sgai_keys: list[str] | None = None
_sgai_keys_lock = threading.Lock()
_sgai_keys_dead: set[int] = set()

SEARCH_QUERY_TEMPLATES: dict[str, str] = {
    "linkedin":     '"{company}" site:linkedin.com/in OR site:linkedin.com/company',
    "product_hunt": '"{company}" site:producthunt.com',
    "twitter":      '"{company}" (site:twitter.com OR site:x.com) (vibe coded OR built with AI OR built in a weekend)',
    "github":       '"{company}" site:github.com',
    "interviews":   '"{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)',
}


def _get_sgai_keys() -> list[str]:
    """Returns the configured SGAI keys (SGAI_API_KEY, _2, _3, ...)."""
    global _sgai_keys
    with _sgai_keys_lock:
        if _sgai_keys is None:
            keys = []
            for k in ("SGAI_API_KEY", "SGAI_API_KEY_2", "SGAI_API_KEY_3", "SGAI_API_KEY_4", "SGAI_API_KEY_5"):
                val = os.getenv(k)
                if val:
                    keys.append(val)
            _sgai_keys = keys
        return _sgai_keys


def _sgai_request(method: str, path: str, payload: dict, timeout: int):
    """POST to the SGAI API with a pool of keys.

    - Round-robin over all configured keys.
    - A key in quota/credits error is marked 'dead' for the whole
      execution and ignored: the other keys keep working in its place.
    - If ALL keys are exhausted, raise the last error.
    """
    import requests

    keys = _get_sgai_keys()
    if not keys:
        raise RuntimeError("No ScrapeGraphAI API key configured in .env")

    last_exc = None
    for idx, key in enumerate(keys):
        if idx in _sgai_keys_dead:
            continue
        try:
            resp = requests.post(
                f"{_SGAI_BASE_URL}/{path}",
                headers={
                    "SGAI-APIKEY": key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp
            msg = f"SGAI HTTP {resp.status_code}: {resp.text[:200]}"
            if _is_quota_error(msg):
                with _sgai_keys_lock:
                    _sgai_keys_dead.add(idx)
                print(f"[scraper] SGAI key {idx+1} out of credits, ignored for the rest")
                continue
            last_exc = Exception(msg)
            continue
        except Exception as e:
            last_exc = e
            msg = str(e)
            if _is_quota_error(msg):
                with _sgai_keys_lock:
                    _sgai_keys_dead.add(idx)
                print(f"[scraper] SGAI key {idx+1} out of credits, ignored for the rest")
                continue
    raise last_exc if last_exc else Exception("All SGAI keys are exhausted")


def _sgai_search_one(source: str, query: str, limit_per_query: int) -> dict:
    """One SGAI search query for a source (used by a thread)."""
    results: list = []
    try:
        resp = _sgai_request("search", "search", {"query": query, "numResults": limit_per_query}, timeout=35)
        data = resp.json()
        raw_results = data.get("results") or []
        results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
            }
            for r in raw_results
            if r.get("url")
        ]
    except Exception as e:
        return {"error": str(e)}
    return results


def _sgai_linkedin_full_scrape(results: list) -> list:
    """Full scrape of the best LinkedIn page found (markdown + structured JSON)."""
    if not results:
        return results
    best = None
    for hit in results:
        u = hit.get("url", "").lower()
        if "/company/" in u or "/company/" in hit.get("content", "").lower():
            best = hit
            break
    if not best:
        best = results[0]
    lk_url = best["url"]
    try:
        resp = _sgai_request(
            "scrape",
            "scrape",
            payload={
                "url": lk_url,
                "formats": [{"type": "markdown"}, {"type": "json", "prompt": "Extract company name, description, headquarters, industry, company size, number of employees, specialties, website, and founders"}],
            },
            timeout=45,
        )
        sd = resp.json()
        results_data = sd.get("results") or {}
        md_parts = results_data.get("markdown", {}).get("data") or []
        json_part = results_data.get("json", {}).get("data")
        full = "\n\n".join(md_parts) if md_parts else ""
        if json_part:
            import json as _json
            full += "\n\n--- Structured ---\n" + _json.dumps(json_part, ensure_ascii=False)
        if full:
            best["content"] = full
            best["title"] = f"{best['title']} (full scrape)"
    except Exception as scrape_err:
        print(f"[scraper] LinkedIn full scrape failed {lk_url}: {scrape_err}")
        # Keep the search content, it is already decent
    return results


def search_additional_evidence(
    company_name: str,
    founder_name: str | None = None,
    limit_per_query: int = 3,
    throttle_seconds: float = 1.0,
) -> dict:
    """
    Queries ScrapeGraphAI Search for each targeted source (LinkedIn,
    Product Hunt, Twitter/X, GitHub, interviews). Returns raw results
    with inline content: the scoring LLM can then quote passages
    verbatim in evidence_quotes.

    Each source = one distinct query, executed IN PARALLEL (1 thread
    per SGAI key). A key with exhausted quota is ignored by the pool,
    the others keep going.

    Returns:
        {
            "linkedin": [{ "url": "...", "title": "...", "content": "..." }],
            "product_hunt": ... | {"error": "..."},
            ...
        }
    """
    if not _get_sgai_keys():
        return {"_error": "SGAI_API_KEY not configured in .env"}

    founder_name = founder_name or ""
    queries = {}
    for source, template in SEARCH_QUERY_TEMPLATES.items():
        if "{founder}" in template and not founder_name:
            continue
        queries[source] = template.format(company=company_name, founder=founder_name)

    if not queries:
        return {}

    n_workers = max(1, min(len(_get_sgai_keys()), len(queries)))
    results_by_source: dict = {}

    if n_workers == 1:
        # Single key: sequential, with the original throttle between sources
        for source, query in queries.items():
            time.sleep(throttle_seconds)
            results_by_source[source] = _sgai_search_one(source, query, limit_per_query)
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_sgai_search_one, source, query, limit_per_query): source
                for source, query in queries.items()
            }
            for future in as_completed(futures):
                source = futures[future]
                results_by_source[source] = future.result()

    # LinkedIn: full scrape of the best URL found
    if "linkedin" in results_by_source and isinstance(results_by_source["linkedin"], list):
        results_by_source["linkedin"] = _sgai_linkedin_full_scrape(results_by_source["linkedin"])

    return results_by_source