"""Public Surface Scanner.

Deterministic checks on the *publicly visible* surface of a lead's website —
nothing the target hasn't already published to the open internet. Hard rules:

- GET/HEAD only. No writes, no auth attempts, no fuzzing, no payloads. Every
  request is a URL any browser could open.
- Verify before flagging: a 404 is NOT an exposure. A finding is written only
  when the HTTP status AND the content shape both match (this is the exact
  cfood false-positive guard: /.git/config returning 404 must never be flagged;
  a soft-200 HTML error page must never be flagged either).
- Polite: per-domain throttle (reuses site_fetcher's), robots.txt honored for
  the well-known-path checks.

Findings are dicts; the DB layer drops any row without verified=1 and an
evidence_excerpt (see db.save_lead_public_findings). known_vulnerable_deps is
deferred (needs a public CVE feed) — the 9th check is intentionally omitted.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests

from site_fetcher import UA, _throttle_domain

ALLOWED_METHODS = ("GET", "HEAD")

SECURITY_HEADERS = (
    "Content-Security-Policy",
    "Strict-Transport-Security",
    "X-Frame-Options",
    "X-Content-Type-Options",
)

DOTFILE_PATHS = ("/.env", "/.git/config", "/.aws/credentials")

# The false-positive guard: status 200 ALONE is never enough. The body must
# match what the file really looks like.
DOTFILE_SHAPES = {
    "/.env": re.compile(r"(?im)^\s*[a-z0-9_.-]+\s*=\s*\S"),
    "/.git/config": re.compile(r"\[core\]|repositoryformatversion", re.I),
    "/.aws/credentials": re.compile(r"\[default\]|aws_access_key_id", re.I),
}

_SERVER_VERSION_RE = re.compile(r"\d+(\.\d+)+")
_SOURCE_MAP_RE = re.compile(r"sourceMappingURL=([^\s\"'\)]+\.map)", re.I)
_MAP_REF_RE = re.compile(r"[\"'(]([^\s\"'()]+\.map)[\"')]", re.I)
_OPEN_API_RE = re.compile(
    r"""["'(](https?://[^"'\s)]*/api[^"'\s)]*|/(?:api|graphql|v\d)[^"'\s)]*)["')]""",
    re.I,
)
_CHAT_WIDGET_RE = re.compile(
    r"[^\"']*intercom|drift|tawk\.to|crisp\.chat|widget\.crisp|/api/(?:chat|ask)",
    re.I,
)
_STORAGE_REF_RE = re.compile(
    r"[\"'](https?://[^\"']*(?:s3[^\"']*\.amazonaws\.com|storage\.googleapis\.com|supabase\.co/storage)[^\"']*)[\"']",
    re.I,
)
_BUCKET_LISTING_MARKERS = ("ListBucketResult", "<Key>", "<Contents>")


def _ensure_scheme(url: str) -> str:
    return url if "://" in url else "https://" + url


def _origin(url: str) -> str:
    u = _ensure_scheme(url or "")
    p = urlparse(u)
    if not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _is_same_origin(url: str, origin: str) -> bool:
    p, o = urlparse(url), urlparse(origin)
    return p.scheme == o.scheme and p.netloc == o.netloc


def _dispatch(method: str, module, url: str, timeout: float, host: str,
              per_domain_delay: float, headers: dict | None = None,
              allow_redirects: bool = True):
    """MUST be a GET/HEAD. Runtime-enforced: `method` is checked against
    ALLOWED_METHODS before ANY network call, so a future check cannot smuggle
    in a POST/PUT/DELETE through the requests-like seam — the guard raises
    before the module is ever touched (structurally incapable, not just
    tested). `module` is the requests-like seam injected by tests."""
    if method not in ALLOWED_METHODS:
        raise ValueError(
            f"surface_scan only permits {ALLOWED_METHODS}, got {method!r}"
        )
    _throttle_domain(host, per_domain_delay)
    hdrs = dict(headers or {"User-Agent": UA})
    call = getattr(module, method.lower())
    return call(url, headers=hdrs, timeout=timeout, allow_redirects=allow_redirects)


def _get(module, url: str, timeout: float, host: str, per_domain_delay: float,
         headers: dict | None = None, allow_redirects: bool = True):
    return _dispatch("GET", module, url, timeout, host, per_domain_delay,
                     headers=headers, allow_redirects=allow_redirects)


def _head(module, url: str, timeout: float, host: str, per_domain_delay: float,
          headers: dict | None = None, allow_redirects: bool = True):
    return _dispatch("HEAD", module, url, timeout, host, per_domain_delay,
                     headers=headers, allow_redirects=allow_redirects)


def _robots_disallow(module, origin: str, host: str, timeout: float, per_domain_delay: float) -> set:
    """Returns the set of robot-disallowed path prefixes (wildcard UA section
    only). Empty robots/404 = nothing disallowed."""
    resp = _get(module, urljoin(origin + "/", "robots.txt"), timeout, host, per_domain_delay)
    if resp.status_code != 200:
        return set()
    disallowed: set = set()
    wildcard_section = False
    for line in (resp.text or "").splitlines():
        low = (line or "").strip().lower()
        if low.startswith("user-agent:"):
            wildcard_section = line.split(":", 1)[1].strip() == "*"
        elif wildcard_section and low.startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path and not re.match(r"^https?://", path, re.I):
                disallowed.add(path)
    return disallowed


def _path_disallowed(path: str, disallowed: set) -> bool:
    return any(path.startswith(prefix) for prefix in disallowed if prefix)


def _security_headers(origin: str, headers: dict) -> list:
    missing = [h for h in SECURITY_HEADERS if not headers.get(h)]
    if not missing:
        return []
    return [{
        "check_name": "security_headers",
        "severity": "low",
        "evidence_url": origin,
        "evidence_excerpt": "Missing security headers on homepage response: " + ", ".join(missing) + ".",
    }]


def _framework_disclosure(origin: str, headers: dict, html: str) -> list:
    leaked = []
    powered = headers.get("X-Powered-By")
    if powered:
        leaked.append(f"X-Powered-By: {powered}")
    server = headers.get("Server")
    if server and _SERVER_VERSION_RE.search(server):
        leaked.append(f"Server: {server}")
    if re.search(r"__NEXT_DATA__", html or "", re.I):
        leaked.append("__NEXT_DATA__ disclosure (framework/build metadata on homepage)")
    if not leaked:
        return []
    return [{
        "check_name": "framework_disclosure",
        "severity": "low",
        "evidence_url": origin,
        "evidence_excerpt": "; ".join(leaked) + ".",
    }]


def _cors_wildcard(module, origin: str, host: str, timeout: float, per_domain_delay: float) -> list:
    resp = _get(module, origin + "/", timeout, host, per_domain_delay,
                headers={"User-Agent": UA, "Origin": origin}, allow_redirects=False)
    acao = resp.headers.get("Access-Control-Allow-Origin")
    acac = resp.headers.get("Access-Control-Allow-Credentials", "").lower() == "true"
    if acao == "*" and acac:
        return [{
            "check_name": "cors_wildcard",
            "severity": "medium",
            "evidence_url": origin,
            "evidence_excerpt": "Access-Control-Allow-Origin: * combined with Access-Control-Allow-Credentials: true.",
        }]
    return []


def _dotfile(module, origin: str, path: str, host: str, timeout: float, per_domain_delay: float) -> list:
    url = urljoin(origin + "/", path.lstrip("/"))
    resp = _get(module, url, timeout, host, per_domain_delay)
    if resp.status_code != 200:
        return []  # 404 (or any non-200) is not an exposure
    body = resp.text or ""
    shape = DOTFILE_SHAPES.get(path)
    if shape is None or not shape.search(body):
        return []  # soft 200 / HTML error page / wrong content: not an exposure
    first_line = (body.splitlines() or [""])[0][:80]
    return [{
        "check_name": "exposed_dotfiles",
        "severity": "medium",
        "evidence_url": url,
        "evidence_excerpt": f"{path} served with HTTP 200 and {path} content shape (first line: {first_line!r}).",
    }]


def _source_maps(module, origin: str, html: str, host: str, timeout: float, per_domain_delay: float) -> list:
    refs = re.findall(_MAP_REF_RE, html or "") + re.findall(_SOURCE_MAP_RE, html or "")
    seen = set()
    for ref in refs:
        url = urljoin(origin, ref)
        if not _is_same_origin(url, origin) or url in seen:
            continue
        seen.add(url)
        resp = _get(module, url, timeout, host, per_domain_delay)
        if resp.status_code != 200:
            continue
        body = (resp.text or "")[:2000]
        if '"version"' in body and '"sources"' in body:
            return [{
                "check_name": "source_maps_exposed",
                "severity": "medium",
                "evidence_url": url,
                "evidence_excerpt": "Source map served in production; reveals original source layout.",
            }]
    return []


def _open_api_endpoints(module, origin: str, html: str, host: str, timeout: float, per_domain_delay: float) -> list:
    refs = re.findall(_OPEN_API_RE, html or "")
    seen = set()
    for ref in refs:
        url = urljoin(origin, ref)
        if not _is_same_origin(url, origin) or url in seen:
            continue
        seen.add(url)
        resp = _get(module, url, timeout, host, per_domain_delay)
        if resp.status_code != 200:
            continue
        ctype = resp.headers.get("Content-Type", "")
        body = (resp.text or "").strip()
        is_json = "json" in ctype.lower() or body[:1] in ("{", "[")
        if is_json and body:
            excerpt = body[:200].replace("\n", " ")
            return [{
                "check_name": "open_api_endpoints",
                "severity": "high",
                "evidence_url": url,
                "evidence_excerpt": f"Referenced data endpoint returns JSON without visible auth (HTTP {resp.status_code}): {excerpt}",
            }]
    return []


def _public_llm_endpoint(origin: str, html: str) -> list:
    if html:
        match = _CHAT_WIDGET_RE.search(html)
        if match:
            return [{
                "check_name": "public_llm_endpoint",
                "severity": "info",
                "evidence_url": origin,
                "evidence_excerpt": "Chat widget marker present in homepage HTML — potential spend-cap surface.",
            }]
    return []


def _storage_bucket_listing(module, origin: str, html: str, host: str, timeout: float, per_domain_delay: float) -> list:
    refs = re.findall(_STORAGE_REF_RE, html or "")
    seen = set()
    for url in refs:
        if url in seen:
            continue
        seen.add(url)
        resp = _get(module, url, timeout, host, per_domain_delay)
        if resp.status_code != 200:
            continue
        body = resp.text or ""
        if any(marker in body for marker in _BUCKET_LISTING_MARKERS):
            return [{
                "check_name": "storage_bucket_listing",
                "severity": "medium",
                "evidence_url": url,
                "evidence_excerpt": "Public object-storage bucket returns an index listing (ListBucketResult).",
            }]
    return []


def scan_site(homepage_url: str, *, timeout: float = 10.0, per_domain_delay: float = 1.0,
              max_findings: int = 8, module=None) -> list:
    """Scans one lead's public surface. Returns a list of finding dicts, each
    already verified (HTTP status AND content shape) with an evidence_excerpt.

    `module` is the requests-like seam (defaults to the real requests module).
    """
    module = module or __import__("requests")
    origin = _origin(homepage_url)
    if not origin:
        return []
    host = urlparse(origin).netloc
    findings: list = []

    intro = _get(module, origin + "/", timeout, host, per_domain_delay)
    status = intro.status_code
    html = ""
    if status and status < 400:
        html = intro.text or ""
    headers = dict(intro.headers or {})

    robots = _robots_disallow(module, origin, host, timeout, per_domain_delay)

    if status and status < 400:
        findings += _security_headers(origin, headers)
        findings += _framework_disclosure(origin, headers, html)
        findings += _cors_wildcard(module, origin, host, timeout, per_domain_delay)

    for path in DOTFILE_PATHS:
        if _path_disallowed(path, robots):
            continue
        findings += _dotfile(module, origin, path, host, timeout, per_domain_delay)

    if status and status < 400:
        findings += _source_maps(module, origin, html, host, timeout, per_domain_delay)
        findings += _open_api_endpoints(module, origin, html, host, timeout, per_domain_delay)
        findings += _public_llm_endpoint(origin, html)
        findings += _storage_bucket_listing(module, origin, html, host, timeout, per_domain_delay)

    # Every finding returned here already passed BOTH gates (HTTP status AND
    # content shape), so all are stamped verified for the DB hard rule.
    for finding in findings:
        finding["verified"] = 1
    return findings[:max_findings]