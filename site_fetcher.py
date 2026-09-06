"""Free-first website fetching via requests + BeautifulSoup.

Ported from the proven lead_tool enrichment project (pipeline/website_enrich.py)
and adapted to this codebase's contracts:

- Returns raw HTML (needed by scraper.extract_technical_signals — the free
  fetch actually gives BETTER raw HTML than Firecrawl's rawHtml format).
- Preserves <blockquote> blocks as markdown "> " lines so the downstream
  testimonial tagging (scraper._tag_attributed_content) keeps working on
  free-fetched text exactly as it does on Firecrawl markdown.
- Detects JS-heavy/app-shell pages so the caller can escalate ONLY those to
  Firecrawl (paid) instead of paying for every page of every lead.

~1 req/sec per domain enforced process-wide (thread-safe).
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Per-domain last-hit timestamps (thread-safe) to enforce ~1 req/sec/domain.
_domain_last: dict[str, float] = {}
_domain_lock = threading.Lock()


def _throttle_domain(host: str, min_gap: float) -> None:
    with _domain_lock:
        last = _domain_last.get(host, 0.0)
        wait = min_gap - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    with _domain_lock:
        _domain_last[host] = time.monotonic()


def looks_js_heavy(html: str, text: str) -> bool:
    """True when the server-rendered HTML is an app shell with no real text —
    the case where the free fetch is useless and Firecrawl (JS rendering)
    is worth its credit."""
    if len(text or "") >= 400:
        return False
    if re.search(r'id=["\'](root|__next|app)["\']', html or "", re.I):
        return True
    scripts = len(re.findall(r"<script", html or "", re.I))
    return scripts > 8 and len(text or "") < 200


def _blockquotes_to_markdown(soup: BeautifulSoup) -> None:
    """Rewrites <blockquote> content so the extracted text keeps the markdown
    '> ' convention the testimonial tagger relies on."""
    for bq in soup.find_all("blockquote"):
        quoted = bq.get_text(separator="\n").strip()
        if not quoted:
            continue
        marked = "\n".join("> " + line for line in quoted.splitlines() if line.strip())
        bq.string = "\n" + marked + "\n"


def _clean_text(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_page(url: str, timeout: float = 15.0, per_domain_delay: float = 1.0) -> dict:
    """Fetches one page for free. Returns:
        {
            "ok": bool,             # HTTP < 400 and parseable
            "status": int | None,   # HTTP status (None on network error)
            "html": str,            # raw HTML ("" on failure)
            "text": str,            # clean visible text, blockquotes kept as '> '
            "links": [str, ...],    # absolute hrefs found on the page
            "js_heavy": bool,       # app shell — caller should escalate to paid fetch
            "error": str | None,
        }
    Never raises: any exception becomes ok=False with the error message.
    """
    out = {"ok": False, "status": None, "html": "", "text": "",
           "links": [], "js_heavy": False, "error": None}
    host = urlparse(url).netloc
    try:
        _throttle_domain(host, per_domain_delay)
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                            allow_redirects=True)
    except requests.RequestException as e:
        out["error"] = f"free fetch failed: {e.__class__.__name__}"
        return out

    out["status"] = resp.status_code
    if resp.status_code >= 400:
        out["error"] = f"HTTP {resp.status_code}"
        return out

    try:
        html = resp.text
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        _blockquotes_to_markdown(soup)
        text = _clean_text(soup.get_text(separator="\n"))

        links = []
        from urllib.parse import urljoin
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            if urlparse(href).scheme in ("http", "https"):
                links.append(href)

        out.update({
            "ok": True,
            "html": html,
            "text": text,
            "links": links,
            "js_heavy": looks_js_heavy(html, text),
        })
    except Exception as e:
        out["error"] = f"parse failed: {e}"
    return out
