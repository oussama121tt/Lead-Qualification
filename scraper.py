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
# Structural tagging of third-party content (testimonials, case studies,
# portfolio items) — deterministic, no LLM involved. This does NOT decide
# whether a block is first-party or third-party (scraper.py has no reliable
# way to know if an attributed name is the lead's own founder or someone
# else's client — it lacks that context here). It only marks the STRUCTURAL
# pattern (blockquote, or a section under a testimonial/case-study/portfolio
# header) so scorer.py's LLM does not have to rediscover it from wording
# alone, which failed in practice on marketing copy phrased at first person
# ("We rescue broken products"). The LLM still makes the final call, now
# helped by an explicit marker instead of having to infer it from style.
# ---------------------------------------------------------------------------

THIRD_PARTY_SECTION_HEADERS = re.compile(
    r"^#{1,4}\s*.*\b("
    r"testimonials?|case\s+stud(?:y|ies)|portfolio|success\s+stor(?:y|ies)|"
    r"what\s+(?:we'?ve\s+built|clients?\s+say|founders?\s+say|our\s+clients?\s+say)|"
    r"our\s+work|client\s+(?:stories|reviews)"
    r")\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

# Any other markdown heading (#, ##, ###...), used to find where a
# third-party section ends (next heading of any level).
ANY_HEADING = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)

QUOTE_TAG_OPEN = "[ATTRIBUTED QUOTE — check the name/company below against lead_metadata before treating as first-party]"
QUOTE_TAG_CLOSE = "[/ATTRIBUTED QUOTE]"
SECTION_TAG_OPEN = "[THIRD-PARTY CONTENT SECTION — likely describes clients/case studies, check attribution before treating as first-party]"
SECTION_TAG_CLOSE = "[/THIRD-PARTY CONTENT SECTION]"


def _tag_attributed_content(markdown: str) -> str:
    """Wraps structurally-detected testimonial/case-study content with
    explicit markers, so the scoring LLM gets a structural hint instead of
    having to infer "is this about the site owner or a client?" purely from
    wording — which proved unreliable on agency sites phrasing their
    services as first-person capability statements ("We rescue broken
    products"). Best-effort: regex on markdown conventions (blockquotes,
    heading text), not a full parse — a testimonial rendered without a
    leading ">" or an unusual heading phrasing can still slip through
    untagged, scorer.py's own structural-pattern rules remain the
    second line of defense, not a replacement for this.
    """
    if not markdown:
        return markdown

    # 1. Tag contiguous blockquote blocks (lines starting with ">"), plus up
    #    to 3 following non-empty lines (the attribution: name / title / company
    #    usually appears right after the quote in scraped markdown).
    lines = markdown.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith(">"):
            block = [line]
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                block.append(lines[i])
                i += 1
            # Pull in the attribution lines that typically follow (name,
            # title/company) — short lines, not headings, not another quote.
            trailing = []
            trail_count = 0
            while i < len(lines) and trail_count < 4:
                nxt = lines[i]
                if not nxt.strip():
                    trailing.append(nxt)
                    i += 1
                    continue
                if nxt.lstrip().startswith(">") or nxt.lstrip().startswith("#"):
                    break
                if len(nxt) > 120:  # long line -> new paragraph, not an attribution
                    break
                trailing.append(nxt)
                i += 1
                trail_count += 1
            out.append(QUOTE_TAG_OPEN)
            out.extend(block)
            out.extend(trailing)
            out.append(QUOTE_TAG_CLOSE)
            continue
        out.append(line)
        i += 1
    markdown = "\n".join(out)

    # 2. Tag sections opened by a testimonial/case-study/portfolio heading,
    #    up to the next heading of any level (or end of document).
    matches = list(THIRD_PARTY_SECTION_HEADERS.finditer(markdown))
    if not matches:
        return markdown
    result = []
    cursor = 0
    for m in matches:
        result.append(markdown[cursor:m.start()])
        section_start = m.start()
        next_heading = ANY_HEADING.search(markdown, m.end())
        section_end = next_heading.start() if next_heading else len(markdown)
        result.append(SECTION_TAG_OPEN + "\n")
        result.append(markdown[section_start:section_end])
        result.append("\n" + SECTION_TAG_CLOSE)
        cursor = section_end
    result.append(markdown[cursor:])
    return "".join(result)

# ---------------------------------------------------------------------------
# Deterministic signatures — checklist from the monitoring (Design Slop Cop,
# isthatvibecoded.com, detectvibecode.com...). Each entry = a simple regex
# on the raw HTML. None of these values is judged by an LLM.
# ---------------------------------------------------------------------------

APP_BUILDER_FINGERPRINTS = {
    "lovable": [r"lovable\.dev", r"lovable-tagger", r"gpteng\.co"],
    "bolt": [r"bolt\.new", r"stackblitz"],
    "v0": [r"v0\.dev", r"vusercontent\.net"],
    "replit": [r"replit\.com", r"replit\.dev"],
    "bubble": [r"bubble\.io", r"bubbleapps\.io"],
    "flutterflow": [r"flutterflow\.io", r"flutterflow\.app"],
    "glide": [r"glideapps\.com", r"glide\.page"],
    "adalo": [r"adalo\.com"],
    "softr": [r"softr\.io"],
    "base44": [r"base44\.app"],
    "cursor": [r"built with cursor", r"cursor\.sh"],
}

SITE_BUILDER_FINGERPRINTS = {
    "framer": [r"framer\.com", r"framerusercontent"],
    "webflow": [r"webflow\.io", r"assets\.website-files\.com"],
    "squarespace": [r"squarespace\.com", r"static1\.squarespace"],
    "wix": [r"wix\.com", r"wixstatic"],
    "carrd": [r"carrd\.co"],
}

BUILDER_SUBDOMAIN_SUFFIXES = {
    "lovable": ".lovable.app",
    "bolt": ".bolt.host",
    "replit": ".replit.app",
    "bubble": ".bubbleapps.io",
    "vercel": ".vercel.app",
}

TREND_FONTS = [
    "Space Grotesk", "Instrument Serif", "Geist", "Syne", "Fraunces",
]

TRACTION_PATTERNS = {
    "stat_banner": [r"\d[\d,\.]*\s?(?:\+|k\+)\s*(?:users|customers|clients)"],
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
                r = _firecrawl_scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=10000)
                results[category] = (url, r)
            except Exception as e:
                results[category] = (url, e)
        return results

    # Parallel: worker i uses key i (round-robin of URLs across workers)
    items = list(urls_by_category.items())

    def _worker(url: str):
        return _firecrawl_scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=10000)

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


def _choose_key_pages(same_domain_links: list, homepage_url: str) -> dict:
    """Selects the key pages (about/pricing/careers/product) from a list of
    same-domain links: keyword matching, then common standard paths (verified
    with a free HEAD/GET before any paid scrape), then a catch-all for
    "product" only. Shared by the Firecrawl path and the free-first path so
    both discover pages identically."""
    found_pages = {"homepage": homepage_url}
    for category, keywords in KEYWORDS.items():
        for link in same_domain_links:
            link_lower = link.lower()
            if any(kw in link_lower for kw in keywords):
                found_pages[category] = link
                break

    # Automatic fallback: for any category not found through the homepage
    # links (JS nav not exposed, site with no direct link, etc.), try the
    # most common standard paths. Only kept if they actually exist
    # (_url_exists, free) — never spend a paid credit blindly on a 404.
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
    # keywords + standard paths (e.g. Linear uses /intake, /plan, /build
    # instead of /product), take the first unassigned link. Only done for
    # product because it is the only category vague enough to have a
    # thousand different names.
    if "product" not in found_pages:
        assigned = set(found_pages.values())
        for link in same_domain_links:
            if link not in assigned and link != homepage_url:
                found_pages["product"] = link
                break

    return found_pages


def _find_key_pages(homepage_url: str):
    # rawHtml in addition to markdown/links: needed for deterministic
    # signal extraction, markdown alone is not enough.
    result = _firecrawl_scrape(homepage_url, formats=["markdown", "rawHtml", "links"], timeout=10000)
    all_links = [
        link for link in (result.links or [])
        if _is_real_subpage(link, homepage_url)
    ]

    # Only keep same-domain links for keyword matching. An external link
    # (g2.com, a review site, LinkedIn...) whose URL contains "product" or
    # "about" must never be chosen as the lead's "product"/"about" page.
    same_domain_links = [
        link for link in all_links if _is_same_domain(link, homepage_url)
    ]

    found_pages = _choose_key_pages(same_domain_links, homepage_url)

    # all_links (from any origin) is returned as-is for
    # extract_technical_signals, which needs to find an external GitHub
    # link — only the key-page matching must be restricted to the lead's
    # domain.
    return found_pages, result, all_links


def _match_any(patterns: list, text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def extract_text_style_signals(text: str) -> dict:
    """
    Text-only style signals (AI-sounding marketing phrases + explicit
    authorship disclosures), computed identically for ANY page of the site —
    the homepage during scraping, and each individual page in the CSV export.

    Shared by extract_technical_signals and export.py so that a phrase found
    on one page is never reported on another page of the same site.
    """
    lowered = (text or "").lower()
    found_phrases = [phrase for phrase in AI_STYLE_PHRASES if phrase in lowered]
    if len(found_phrases) >= 4:
        density = "high"
    elif len(found_phrases) >= 2:
        density = "medium"
    elif len(found_phrases) == 1:
        density = "low"
    else:
        density = "none"
    return {
        "ai_style_phrases_found": found_phrases,
        "ai_style_phrase_density": density,
        "ai_authorship_disclosures_found": [
            disclosure for disclosure in AI_AUTHORSHIP_DISCLOSURES if disclosure in lowered
        ],
    }


def extract_technical_signals(
    raw_html: str,
    all_links: list,
    homepage_text: str = "",
    homepage_url: str = "",
) -> dict:
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
        "app_builder_fingerprint": None,
        "site_builder_fingerprint": None,
        "on_builder_subdomain": False,
        "on_builder_subdomain_builder": None,
        "generator_fingerprint": None,
        "vibe_language_matches": [],
        "trend_fonts_found": [],
        "traction_signals": [],
        "generator_meta_tag": None,
        "github_repo_url": None,
        "linkedin_company_url": None,
        "linkedin_person_urls": [],
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

    # Product builders are strong signals; site builders are metadata only.
    for builder, patterns in APP_BUILDER_FINGERPRINTS.items():
        if _match_any(patterns, raw_html):
            signals["app_builder_fingerprint"] = builder
            signals["generator_fingerprint"] = builder
            break
    for builder, patterns in SITE_BUILDER_FINGERPRINTS.items():
        if _match_any(patterns, raw_html):
            signals["site_builder_fingerprint"] = builder
            break

    host = _normalize_domain(homepage_url)
    for builder, suffix in BUILDER_SUBDOMAIN_SUFFIXES.items():
        if host.endswith(suffix) and host != suffix[1:]:
            signals["on_builder_subdomain"] = True
            signals["on_builder_subdomain_builder"] = builder
            break

    # Explicit language ("built with X") — searched as-is, not inferred
    lowered = raw_html.lower()
    signals["vibe_language_matches"] = [
        m for m in VIBE_LANGUAGE_MARKERS if m in lowered
    ]

    # Trending fonts
    signals["trend_fonts_found"] = [f for f in TREND_FONTS if f.lower() in lowered]

    signals["traction_signals"] = [
        name for name, patterns in TRACTION_PATTERNS.items() if _match_any(patterns, raw_html)
    ]

    # Writing style: scanned on visible text, not raw HTML. Shared with
    # export.py (per-page recomputation), via extract_text_style_signals.
    signals.update(extract_text_style_signals(homepage_text))

    # Public GitHub link, for the git check. Deliberately NOT
    # restricted to the same domain: a lead may legitimately link to an
    # external GitHub repo (GitHub org different from the site's domain).
    #
    # LinkedIn links declared BY THE COMPANY ITSELF (footer, about/team
    # page) are the most reliable disambiguation source we have: a
    # name-based search for "RuyaTech" can return an unrelated homonymous
    # company (e.g. a different "Ruyatech AI" in another country) — a link
    # the company put on its own site cannot be confused with a homonym.
    # linkedin_company_url keeps only the FIRST match (one company page is
    # enough); linkedin_person_urls keeps ALL /in/ links found (a team/about
    # page can legitimately link several team members).
    person_urls: list[str] = []
    for link in all_links or []:
        link_lower = link.lower()
        if "github.com" in link_lower and "/issues" not in link_lower and "/pull" not in link_lower:
            if signals["github_repo_url"] is None:
                signals["github_repo_url"] = link
        if "linkedin.com/company/" in link_lower:
            if signals["linkedin_company_url"] is None:
                signals["linkedin_company_url"] = link
        elif "linkedin.com/in/" in link_lower:
            person_urls.append(link)
    signals["linkedin_person_urls"] = sorted(set(person_urls))

    return signals


FOUNDER_NAME_PATTERNS = [
    # "founded by John Smith" / "co-founded by John Smith"
    r"(?i:co[\-\s]?founded\s+by)\s+([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2})",
    r"(?i:founded\s+by)\s+([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2})",
    # "John Smith, founder" / "John Smith is the founder/CEO"
    r"([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2}),?\s+(?i:(?:is\s+|was\s+)?(?:the\s+)?(?:co[\-\s]?)?founder)",
    r"([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2})\s+(?i:(?:is|was)\s+(?:the\s+)?(?:co[\-\s]?)?(?:founder|ceo))",
    # "Meet the founder, John Smith" / "Founder & CEO: John Smith"
    r"(?i:meet\s+(?:the\s+)?founder,?)\s+([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2})",
    r"(?i:(?:co[\-\s]?)?founder\s*(?:&|and)?\s*(?:ceo)?\s*[:\-])\s*([A-Z][a-zA-Z'\-]+(?:\s[A-Z][a-zA-Z'\-]+){0,2})",
]

# Words a name-shaped regex match can accidentally pick up (generic role
# nouns, common sentence starters right before "Founder") — filtered out so
# they never get treated as a person's name.
FOUNDER_NAME_STOPWORDS = {
    "the", "our", "we", "this", "meet", "hi", "hello", "team", "company",
    "about", "welcome", "introducing",
}


def extract_founder_name_candidates(text: str) -> list[str]:
    """
    Best-effort, deterministic extraction of a founder's name mentioned in
    the site's own copy (bio, testimonial, "meet the founder" section).

    Why this exists: the CRM-provided contact name (Apollo CSV) is not
    always trustworthy — it can be a placeholder/test value, stale, or
    simply wrong. A name the company mentions about itself, on its own
    site, is independent corroborating evidence and is often MORE reliable
    for finding the right LinkedIn profile than blindly trusting the CSV.

    Honest limitation: this is regex-based pattern matching, not NLP. It
    only catches a handful of common explicit phrasings ("founded by X",
    "X, founder", "meet the founder, X"...). A sentence like "Oussama
    launched it in two weeks" (a founder's name used in a testimonial
    without the word "founder" nearby) will NOT be caught — no attempt is
    made to guess names from context alone, to avoid false positives on
    ordinary capitalized words (product names, place names, etc.).

    Returns a short, deduplicated list of candidate names (order of first
    appearance) — a list, not a single "best" pick, because disambiguating
    between several real candidates (e.g. co-founders) is not something a
    regex can safely decide; that choice belongs to the caller/pipeline.
    """
    if not text:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in FOUNDER_NAME_PATTERNS:
        for m in re.finditer(pattern, text):
            name = m.group(1).strip()
            first_word = name.split()[0].lower() if name.split() else ""
            if first_word in FOUNDER_NAME_STOPWORDS:
                continue
            if len(name) < 3 or len(name) > 60:
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                candidates.append(name)
    return candidates[:5]


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

    FREE-FIRST (merge addition): pages are fetched with plain requests +
    BeautifulSoup first (zero credits — see site_fetcher.py); Firecrawl is
    only used as a fallback for JS-heavy/app-shell pages the free fetch
    cannot read. Disable with [website].free_first=false in config.toml to
    restore the all-Firecrawl behavior.

    Returns:
        {
            "status": "PARSED" | "FETCH_PARTIAL" | "FETCH_FAILED",
            "rows": [(source, url, content), ...],
            "technical_signals": {...} | None,
            "github_check": {...} | None,
            "error": str | None,
            "fetch_notes": [str, ...],   # coverage notes: how each page was fetched
        }
    """
    notes: list[str] = []
    try:
        from runconfig import load_config
        free_first = load_config().website.free_first
    except Exception:
        free_first = True

    if free_first:
        result = _scrape_website_free(homepage_url, notes)
        if result is not None:
            result["fetch_notes"] = notes
            return result
        # Free path unusable for this site — escalate the whole lead to
        # Firecrawl (the note explaining why is already in `notes`).

    result = _scrape_website_firecrawl(homepage_url, throttle_seconds=throttle_seconds)
    result["fetch_notes"] = notes + ["fetched via Firecrawl (paid)"]
    return result


def _scrape_website_free(homepage_url: str, notes: list) -> dict | None:
    """Free-first scrape of a whole site. Returns the same contract as
    scrape_website, or None when the free path cannot handle this site
    (JS-heavy homepage, network failure) and the caller must escalate to
    Firecrawl. Individual sub-pages that fail the free fetch are escalated
    to Firecrawl one by one — never the whole site."""
    import site_fetcher

    try:
        from runconfig import load_config
        cfg_web = load_config().website
        page_timeout = cfg_web.page_timeout
        per_domain_delay = cfg_web.per_domain_delay
    except Exception:
        page_timeout, per_domain_delay = 15.0, 1.0

    home = site_fetcher.fetch_page(homepage_url, timeout=page_timeout,
                                   per_domain_delay=per_domain_delay)
    if not home["ok"]:
        notes.append(f"free homepage fetch failed ({home['error']}); escalated to Firecrawl")
        return None
    if home["js_heavy"]:
        notes.append("homepage is a JS-heavy app shell; escalated to Firecrawl")
        return None
    homepage_text = home["text"]
    if _looks_broken(homepage_text):
        notes.append("free homepage content looked broken/empty; escalated to Firecrawl")
        return None
    notes.append("homepage fetched free (requests)")

    all_links = [l for l in home["links"] if _is_real_subpage(l, homepage_url)]
    same_domain_links = [l for l in all_links if _is_same_domain(l, homepage_url)]
    pages = _choose_key_pages(same_domain_links, homepage_url)

    rows = [("homepage", homepage_url,
             _tag_attributed_content(homepage_text[:MAX_CONTENT_CHARS_PER_PAGE]))]
    seen_fingerprints = {_content_fingerprint(homepage_text)}

    failures = 0
    duplicates = 0
    founder_bio_text_parts = [homepage_text]
    careers_signal = None
    subpage_links: set[str] = set()
    other_pages = {k: v for k, v in pages.items() if k != "homepage"}

    for category, url in other_pages.items():
        raw_content = None
        page = site_fetcher.fetch_page(url, timeout=page_timeout,
                                       per_domain_delay=per_domain_delay)
        if page["ok"] and not page["js_heavy"]:
            raw_content = page["text"][:MAX_CONTENT_CHARS_PER_PAGE]
            for l in page["links"]:
                if _is_real_subpage(l, homepage_url):
                    subpage_links.add(l)
        else:
            # Per-page paid fallback: only THIS page costs a credit.
            why = "JS-heavy" if page["ok"] else f"failed: {page['error']}"
            try:
                r = _firecrawl_scrape(url, formats=["markdown", "links"],
                                      only_main_content=True, timeout=10000)
                raw_content = (r.markdown or "")[:MAX_CONTENT_CHARS_PER_PAGE]
                notes.append(f"{category} page escalated to Firecrawl (free fetch {why})")
                for l in (r.links or []):
                    if _is_real_subpage(l, homepage_url):
                        subpage_links.add(l)
            except Exception as e:
                failures += 1
                notes.append(f"{category} page unusable (free fetch {why}; Firecrawl: {e})")
                continue

        if _looks_broken(raw_content):
            failures += 1
            notes.append(f"{category} page ignored (broken/empty content)")
            continue

        fingerprint = _content_fingerprint(raw_content)
        if fingerprint in seen_fingerprints:
            duplicates += 1
            notes.append(f"{category} page ignored (identical to an already kept page)")
            continue
        seen_fingerprints.add(fingerprint)

        if category in ("about", "product"):
            founder_bio_text_parts.append(raw_content)

        if category == "careers":
            careers_signal = extract_careers_signal(raw_content)
            content = _format_signal_as_text("Careers", careers_signal)
        elif category == "pricing":
            content = _format_signal_as_text("Pricing", extract_pricing_signal(raw_content))
        else:
            content = _tag_attributed_content(raw_content)

        rows.append((category, url, content))

    all_links = list(set(all_links) | subpage_links)

    technical_signals = extract_technical_signals(
        raw_html=home["html"],
        all_links=all_links,
        homepage_text=homepage_text,
    )
    technical_signals["founder_name_candidates"] = extract_founder_name_candidates(
        "\n\n".join(founder_bio_text_parts)
    )
    if isinstance(careers_signal, dict) and "hiring_technical" in careers_signal:
        technical_signals["hiring_technical"] = bool(careers_signal.get("hiring_technical"))

    github_check = None
    if technical_signals.get("github_repo_url"):
        github_check = check_github_repo_pattern(technical_signals["github_repo_url"])

    unusable = failures + duplicates
    status = "FETCH_PARTIAL" if unusable > 0 else "PARSED"

    return {
        "status": status,
        "rows": rows,
        "technical_signals": technical_signals,
        "github_check": github_check,
        "error": None,
    }


def _scrape_website_firecrawl(homepage_url: str, throttle_seconds: float = 1.0) -> dict:
    """All-Firecrawl scrape path (the original behavior) — used when the
    free-first path cannot handle a site, or when free_first is disabled."""
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

    rows = [("homepage", homepage_url, _tag_attributed_content(homepage_markdown[:MAX_CONTENT_CHARS_PER_PAGE]))]
    seen_fingerprints = {_content_fingerprint(homepage_markdown)}

    failures = 0
    duplicates = 0
    other_pages = {k: v for k, v in pages.items() if k != "homepage"}

    # Scrape the key pages in parallel (1 thread per Firecrawl key).
    # A key with exhausted quota is ignored by the pool, the others continue.
    scraped_pages = _scrape_pages_in_parallel(other_pages, throttle_seconds=throttle_seconds)

    # Correction 1 - GitHub link detection was homepage-only: the links of the
    # /about /pricing /careers /product pages were never collected, so a GitHub
    # link present only in a sub-page footer (a frequent case) was invisible.
    # We now ask the "links" format for those already-scraped pages (no extra
    # Firecrawl scrape) and merge them into all_links before
    # extract_technical_signals(). Sub-page links go through the same "real
    # sub-page" filter as the homepage links (_is_real_subpage); key-page
    # discovery in _find_key_pages() keeps the homepage's same-domain filter
    # (untouched). The external GitHub link itself is NOT domain-filtered (a
    # lead may legitimately link to a GitHub org different from its own domain).
    subpage_links: set[str] = set()
    founder_bio_text_parts = [homepage_markdown]
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

        # Founder bios most commonly live on "about" or "product" pages
        # (a "meet the team"/"meet the founder" section) — collected here,
        # on the RAW content, before careers/pricing get replaced below by
        # their compact formatted signal (which would strip any bio text).
        if category in ("about", "product"):
            founder_bio_text_parts.append(raw_content)

        if category == "careers":
            careers_signal = extract_careers_signal(raw_content)
            content = _format_signal_as_text("Careers", careers_signal)
        elif category == "pricing":
            content = _format_signal_as_text("Pricing", extract_pricing_signal(raw_content))
        else:
            content = _tag_attributed_content(raw_content)

        rows.append((category, url, content))

        # Correction 1: this sub-page's own links, same filter as the homepage
        # links (_is_real_subpage) - merged (deduplicated) into all_links below.
        for link in (r.links or []):
            if _is_real_subpage(link, homepage_url):
                subpage_links.add(link)

    # De-duplication of the merged set: the same GitHub repo found in the
    # footer of several pages must never be treated as a multiplied signal.
    all_links = list(set(all_links) | subpage_links)

    technical_signals = extract_technical_signals(
        raw_html=getattr(homepage_result, "raw_html", None),
        all_links=all_links,
        homepage_text=homepage_markdown,
        homepage_url=homepage_url,
    )
    # Founder name(s) mentioned in the site's own copy (homepage + about/
    # product pages) — see extract_founder_name_candidates() docstring for
    # why this exists (a more trustworthy source than a CRM field that can
    # be a placeholder) and its honest limitations (regex, not NLP).
    technical_signals["founder_name_candidates"] = extract_founder_name_candidates(
        "\n\n".join(founder_bio_text_parts)
    )
    # Expose the deterministic careers hiring signal as a first-class
    # technical signal (same bool extract_careers_signal already computes
    # internally) — pipeline.py gates the web escalation on it without
    # re-parsing the formatted text.
    if "careers_signal" in locals() and isinstance(careers_signal, dict) and "hiring_technical" in careers_signal:
        technical_signals["hiring_technical"] = bool(careers_signal.get("hiring_technical"))

    github_check = None
    if technical_signals.get("github_repo_url"):
        github_check = check_github_repo_pattern(technical_signals["github_repo_url"])
    # A SPA duplicate is not a "failure" in the same way a timeout or a
    # 404 is (the page exists, it is just useless) — but it still counts
    # as "no additional useful content" when determining the final
    # status below.
    unusable = failures + duplicates

    # Status semantics: "FETCH_FAILED" is reserved for the truly dead site
    # (homepage itself unreachable or broken -> rows == [], handled above).
    # As soon as the homepage content EXISTS, a sub-page problem only makes
    # the scrape PARTIAL, never FAILED — even when every sub-page was
    # unusable (rows == [homepage] only). Without this split, a human looking
    # at the dashboard could not tell "site totally down" apart from "site up,
    # sub-pages poor", two situations with very different treatments.
    if unusable > 0:
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
    "linkedin":         '"{company}" site:linkedin.com/in OR site:linkedin.com/company',
    "product_hunt":     '"{company}" site:producthunt.com',
    "twitter":          '"{company}" (site:twitter.com OR site:x.com) (vibe coded OR built with AI OR built in a weekend)',
    "github":           '"{company}" site:github.com',
    "interviews":       '"{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)',
    # Founder's OWN profiles: only skip when no founder name is known;
    # used to tell technical_founder vs ai_solo_founder directly.
    "person_linkedin":  '"{founder}" site:linkedin.com/in',
    "person_github":    '"{founder}" site:github.com',
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


def _sgai_scrape_linkedin_url(lk_url: str, title: str, prefer_profile: bool = False) -> dict:
    """Full scrape of ONE LinkedIn URL (markdown + structured JSON).

    Shared by both callers below: the "best guess from a name search"
    path and the "known URL taken directly from the scraped site" path.
    Best-effort by design: on failure, returns a minimal hit with just
    url/title so the caller can still keep something rather than crash.
    """
    hit = {"url": lk_url, "title": title, "content": ""}
    try:
        resp = _sgai_request(
            "scrape",
            "scrape",
            payload={
                "url": lk_url,
                "formats": [{"type": "markdown"}, {"type": "json", "prompt": (
                    "Extract company name, description, headquarters, industry, company size, "
                    "number of employees, specialties, website, and founders"
                    if not prefer_profile else
                    "Extract the person's name, current roles and company, work experience, "
                    "education, skills, and whether they are a founder, CTO, or engineer"
                )}],
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
            hit["content"] = full
            hit["title"] = f"{title} (full scrape)"
    except Exception as scrape_err:
        print(f"[scraper] LinkedIn full scrape failed {lk_url}: {scrape_err}")
        # Best-effort: return the minimal hit, the caller decides what to do with it
    return hit


def _sgai_linkedin_full_scrape(results: list, prefer_profile: bool = False) -> list:
    """Full scrape of the best LinkedIn page found among SEARCH results
    (name-based query — can be wrong on a homonym company/person, see
    scrape_website_linkedin() for the more reliable known-URL path).

    Best-effort by design: if the scrape fails, the original search snippets
    are kept — the full scrape never fails a lead on its own.

    prefer_profile=False (company sources): prefer a /company/ page.
    prefer_profile=True (person_* sources): prefer an /in/ profile — the
    founder's OWN page, the most direct evidence about the person.
    """
    if not results:
        return results
    best = None
    for hit in results:
        u = hit.get("url", "").lower()
        needle = "/in/" if prefer_profile else "/company/"
        if needle in u or needle in hit.get("content", "").lower():
            best = hit
            break
    if not best:
        best = results[0]
    scraped = _sgai_scrape_linkedin_url(best["url"], best.get("title", ""), prefer_profile=prefer_profile)
    if scraped.get("content"):
        best["content"] = scraped["content"]
        best["title"] = scraped["title"]
    return results


def search_additional_evidence(
    company_name: str,
    founder_name: str | None = None,
    limit_per_query: int = 3,
    throttle_seconds: float = 1.0,
    known_linkedin_company_url: str | None = None,
    known_linkedin_person_url: str | None = None,
    skip_person_linkedin: bool = False,
) -> dict:
    """
    Queries ScrapeGraphAI Search for each targeted source (LinkedIn —
    company page AND the founder's own profile (person_*) —, Product Hunt,
    Twitter/X, GitHub, interviews). Returns raw results
    with inline content: the scoring LLM can then quote passages
    verbatim in evidence_quotes.

    known_linkedin_company_url: optional, from
    scraper.extract_technical_signals(...)["linkedin_company_url"] — a
    LinkedIn company URL the site itself links to (footer, about page).
    When present, the "linkedin" company source is scraped DIRECTLY from
    this URL instead of a name-based search: a search for "RuyaTech" can
    return a same-named but unrelated company (confirmed case: a UAE
    defense-AI "Ruyatech AI" outranked the real, smaller "RuyaTech" in
    search results). A link the company put on its own site cannot be
    confused with a homonym, so it is strictly more reliable and skips
    that query entirely (saves one SGAI call too).

    known_linkedin_person_url: optional, typically
    technical_signals["linkedin_person_urls"][0] passed by the CALLER only
    when that list has exactly one entry — deciding "which team member is
    THE founder" among several is a judgment call this function does not
    make; pass None when there is more than one candidate and let the
    name-based person_linkedin search run instead.

    founder_name: prefer a name found on the SITE ITSELF (see
    scraper.extract_founder_name_candidates()) over a CRM-provided one when
    available — the CRM field can be a placeholder/test value (confirmed
    case: "Wael Test" led this search to a completely unrelated LinkedIn
    profile). Passing the right value here is the caller's responsibility;
    this function just uses whatever string it is given.

    Each remaining source = one distinct query, executed IN PARALLEL (1
    thread per SGAI key). A key with exhausted quota is ignored by the
    pool, the others keep going.

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
        if source == "linkedin" and known_linkedin_company_url:
            continue  # known URL from the site itself, no need to guess-search
        if source == "person_linkedin" and (known_linkedin_person_url or skip_person_linkedin):
            continue  # known URL, or already deep-harvested by linkedin_lane
        if "{founder}" in template and not founder_name:
            continue
        queries[source] = template.format(company=company_name, founder=founder_name)

    results_by_source: dict = {}

    if queries:
        n_workers = max(1, min(len(_get_sgai_keys()), len(queries)))
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

    # LinkedIn company: known URL from the site itself takes priority over
    # any name-based search (see docstring above for why).
    if known_linkedin_company_url:
        hit = _sgai_scrape_linkedin_url(
            known_linkedin_company_url,
            title="LinkedIn company page (from site link)",
            prefer_profile=False,
        )
        results_by_source["linkedin"] = [hit]
    elif isinstance(results_by_source.get("linkedin"), list):
        # Fallback: only reached when the site had no declared LinkedIn link
        # — best-effort name search, can be wrong on a homonym (see above).
        results_by_source["linkedin"] = _sgai_linkedin_full_scrape(results_by_source["linkedin"])

    # LinkedIn person: same priority order as the company case above.
    # skip_person_linkedin: the caller already deep-harvested the profile
    # (linkedin_lane) — do not spend another scrape on it here.
    if skip_person_linkedin:
        pass
    elif known_linkedin_person_url:
        hit = _sgai_scrape_linkedin_url(
            known_linkedin_person_url,
            title="LinkedIn profile (from site link)",
            prefer_profile=True,
        )
        results_by_source["person_linkedin"] = [hit]
    elif isinstance(results_by_source.get("person_linkedin"), list):
        results_by_source["person_linkedin"] = _sgai_linkedin_full_scrape(results_by_source["person_linkedin"], prefer_profile=True)

    return results_by_source