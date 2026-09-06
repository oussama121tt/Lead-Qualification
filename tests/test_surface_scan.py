"""Public Surface Scanner regression tests.

Locks the two hard rules that make the feature trustworthy:
- a finding is only written when HTTP status AND content shape match — a 404
  or a soft-200 HTML page is NOT an exposure (the cfood false-positive);
- no check ever issues a non-GET/HEAD request (asserted on the injected
  requests layer).
"""
import pytest

import surface_scan


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.url = ""


class FakeRequests:
    """Records every method/URL touched. Any non-GET/HEAD call raises."""

    def __init__(self):
        self.calls = []
        self.routes = {}   # url -> FakeResponse
        self.raise_on = {} # url -> exception to simulate network failure
        self.default = FakeResponse()

    def route(self, url, resp):
        self.routes[url] = resp

    def raise_for(self, url, exc):
        self.raise_on[url] = exc

    def _handle(self, method, url):
        self.calls.append((method, url))
        if method not in surface_scan.ALLOWED_METHODS:
            raise AssertionError(f"forbidden HTTP method used: {method} {url}")
        if url in self.raise_on:
            raise self.raise_on[url]
        if url in self.routes:
            return self.routes[url]
        # Something not explicitly routed: a benign 200 with no finding shape.
        return self.default

    def get(self, url, **kwargs):
        return self._handle("GET", url)

    def head(self, url, **kwargs):
        return self._handle("HEAD", url)

    def post(self, url, **kwargs):
        return self._handle("POST", url)

    def put(self, url, **kwargs):
        return self._handle("PUT", url)

    def delete(self, url, **kwargs):
        return self._handle("DELETE", url)


def _plain_homepage():
    return FakeResponse(status_code=200, text="<html><body><h1>Hello</h1></body></html>")


def _find(findings, check_name):
    return [f for f in findings if f.get("check_name") == check_name]


# ---------------------------------------------------------------------------
# exposed_dotfiles — the cfood false-positive guard
# ---------------------------------------------------------------------------
def test_git_config_with_real_content_is_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.git/config", FakeResponse(status_code=200,
                 text="[core]\n\trepositoryformatversion = 0\n\tfilemode = true"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "exposed_dotfiles")
    assert len(hits) == 1
    assert hits[0]["verified"] == 1
    assert "/.git/config" in hits[0]["evidence_url"]
    assert "[core]" in hits[0]["evidence_excerpt"]


def test_git_config_404_is_not_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.git/config", FakeResponse(status_code=404,
                 text="404 Not Found"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "exposed_dotfiles") == []


def test_git_config_html_masquerading_as_200_is_not_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.git/config",
               FakeResponse(status_code=200, text="<html><body>Welcome</body></html>"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "exposed_dotfiles") == []


def test_env_content_shape_is_checked():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.env",
               FakeResponse(status_code=200, text="DATABASE_URL=postgres://secret\nAPI_KEY=abc"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "exposed_dotfiles")
    assert len(hits) == 1
    assert "/.env" in hits[0]["evidence_url"]


def test_env_with_unrelated_body_is_not_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.env", FakeResponse(status_code=200, text="just some plain text"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "exposed_dotfiles") == []


# ---------------------------------------------------------------------------
# robots.txt is honored for the well-known path checks
# ---------------------------------------------------------------------------
def test_dotfile_disallowed_by_robots_is_not_checked():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt",
               FakeResponse(status_code=200, text="User-agent: *\nDisallow: /.env\n"))
    fake.route("https://example.com/.env", FakeResponse(status_code=200,
               text="DATABASE_URL=postgres://secret"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "exposed_dotfiles") == []


# ---------------------------------------------------------------------------
# other checks
# ---------------------------------------------------------------------------
def test_security_headers_missing_is_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(status_code=200, text="<html></html>"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "security_headers")
    assert len(hits) == 1
    assert hits[0]["severity"] == "low"


def test_security_headers_present_not_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(
        status_code=200, text="<html></html>",
        headers={"Content-Security-Policy": "default-src 'self'",
                 "Strict-Transport-Security": "max-age=63072000",
                 "X-Frame-Options": "DENY",
                 "X-Content-Type-Options": "nosniff"}))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "security_headers") == []


def test_cors_wildcard_with_credentials_is_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    # CORS check issues a second GET with an Origin header — same URL, so the
    # route for "/" applies. Return the wildcard headers there.
    plain = _plain_homepage()
    plain.headers["Access-Control-Allow-Origin"] = "*"
    plain.headers["Access-Control-Allow-Credentials"] = "true"
    fake.route("https://example.com/", plain)
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "cors_wildcard")
    assert len(hits) == 1
    assert hits[0]["severity"] == "medium"


def test_open_api_endpoint_returning_json_is_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(
        status_code=200, text='<html><script>fetch("/api/users")</script></html>'))
    fake.route("https://example.com/api/users",
               FakeResponse(status_code=200, text='[{"id":1,"email":"a@b.com"}]',
                            headers={"Content-Type": "application/json"}))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "open_api_endpoints")
    assert len(hits) == 1
    assert hits[0]["severity"] == "high"
    assert "/api/users" in hits[0]["evidence_url"]


def test_open_api_endpoint_404_is_not_flagged():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(
        status_code=200, text='<html><script>fetch("/api/users")</script></html>'))
    fake.route("https://example.com/api/users", FakeResponse(status_code=404, text="Not Found"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "open_api_endpoints") == []


def test_public_llm_endpoint_is_informational():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(
        status_code=200, text='<html><script src="https://widget.intercom.io/"></script></html>'))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    hits = _find(findings, "public_llm_endpoint")
    assert len(hits) == 1
    assert hits[0]["severity"] == "info"


# ---------------------------------------------------------------------------
# the method contract
# ---------------------------------------------------------------------------
def test_only_get_and_head_are_ever_used():
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    surface_scan.scan_site("https://example.com", module=fake,
                           timeout=1, per_domain_delay=0, max_findings=20)
    methods = {method for method, _url in fake.calls}
    assert methods and methods.issubset(set(surface_scan.ALLOWED_METHODS))


def test_scan_requires_verified_and_excerpt_before_returning():
    # The scanner stamps verified=1 only after status+shape pass; a caller
    # that strips the excerpt must not produce a persisted row (enforced in
    # the DB layer, mirrored here for the contract).
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/.git/config", FakeResponse(
        status_code=200, text="[core]\n\trepositoryformatversion = 0"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert all(f.get("verified") and f.get("evidence_excerpt") for f in findings)


def test_runtime_guard_rejects_any_non_get_head_dispatch():
    # _dispatch is the ONLY network funnel in surface_scan and it checks the
    # method before touching the module: a POST/PUT/DELETE cannot even be
    # started, even if a future check tries to pass it through.
    fake = FakeRequests()
    for bad in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        with pytest.raises(ValueError):
            surface_scan._dispatch(bad, fake, "https://example.com/",
                                   timeout=1, host="example.com", per_domain_delay=0)
    # And a GET requesting something that would 404 still works -- guard is
    # about the METHOD, not the target.
    fake.route("https://example.com/", _plain_homepage())
    resp = surface_scan._dispatch("GET", fake, "https://example.com/",
                                  timeout=1, host="example.com", per_domain_delay=0)
    assert resp.status_code == 200
    assert surface_scan._head is not None  # HEAD wrapper exists for future use


# ---------------------------------------------------------------------------
# hardening: per-check isolation, body cap, network failures, ref caps
# ---------------------------------------------------------------------------
def _boom():
    raise RuntimeError("check exploded")


def test_run_isolates_a_failing_check():
    assert surface_scan._run(_boom) == []
    assert surface_scan._run(_plain_homepage) is not None  # normal fn unaffected


def test_failing_check_does_not_lose_other_findings():
    # A raising check yields [] and the already-collected findings from other
    # checks survive -- _run swallows per-check, scan_site keeps everything.
    good = surface_scan._security_headers("https://example.com", {})
    assert len(good) == 1  # sanity: the control actually produces a finding

    def flaky():
        raise ValueError("pathological page")

    surviving = []
    surviving += surface_scan._run(lambda: good)
    surviving += surface_scan._run(flaky)
    assert len(surviving) == 1


def test_body_text_caps_huge_pages():
    huge = FakeResponse(status_code=200, text="A" * (surface_scan._BODY_CAP + 10_000))
    capped = surface_scan._body_text(huge)
    assert len(capped) == surface_scan._BODY_CAP


def test_network_failure_on_a_dotfile_is_skipped_not_flagged_or_fatal():
    from requests.exceptions import ConnectionError as ReqConnErr
    fake = FakeRequests()
    fake.route("https://example.com/", _plain_homepage())
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    # /.env "connection refused": _dispatch returns None, _dotfile skips, and
    # the other checks still run to completion.
    fake.raise_for("https://example.com/.env", ReqConnErr("connection refused"))
    fake.route("https://example.com/.git/config", FakeResponse(status_code=404))
    fake.route("https://example.com/.aws/credentials", FakeResponse(status_code=404))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    assert _find(findings, "exposed_dotfiles") == []
    assert len(_find(findings, "security_headers")) == 1  # other work survived


def test_network_failure_yields_none_from_dispatch():
    from requests.exceptions import Timeout as ReqTimeout
    fake = FakeRequests()
    fake.raise_for("https://example.com/", ReqTimeout("timed out"))
    resp = surface_scan._dispatch("GET", fake, "https://example.com/",
                                  timeout=1, host="example.com", per_domain_delay=0)
    assert resp is None


def test_refs_checked_are_capped():
    fake = FakeRequests()
    maps = " ".join(f'sourceMappingURL={i}.map' for i in range(20))
    fake.route("https://example.com/", FakeResponse(status_code=200, text=f"<html>{maps}</html>"))
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    # All the .map URLs exist as routes so the loop actually probes them
    # (a cap that merely stopped before the routes were reached would be a
    # cap-on-regex-hits, not a cap-on-requests).
    probes = [f"https://example.com/{i}.map" for i in range(20)]
    for url in probes:
        fake.route(url, FakeResponse(status_code=200, text="not a real source map"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    map_probes = [u for m, u in fake.calls if u.endswith(".map")]
    assert len(map_probes) <= surface_scan._MAX_REFS_CHECKED
    assert _find(findings, "source_maps_exposed") == []


def test_findings_sorted_high_severity_first_before_truncation():
    fake = FakeRequests()
    fake.route("https://example.com/", FakeResponse(
        status_code=200, text='<html><script>fetch("/api/users")</script></html>'))
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    fake.route("https://example.com/.env", FakeResponse(status_code=404))
    fake.route("https://example.com/.git/config", FakeResponse(status_code=404))
    fake.route("https://example.com/.aws/credentials", FakeResponse(status_code=404))
    fake.route("https://example.com/api/users",
               FakeResponse(status_code=200, text='[{"id":1,"email":"a@b.com"}]',
                            headers={"Content-Type": "application/json"}))
    # max_findings tiny: only the single highest-severity row may survive.
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=1)
    assert len(findings) == 1
    assert findings[0]["check_name"] == "open_api_endpoints"  # high beats low
    assert findings[0]["severity"] == "high"


def test_storage_bucket_refs_checked_are_capped():
    fake = FakeRequests()
    urls = [f"https://bucket{i}.storage.googleapis.com/a" for i in range(20)]
    html = "<html>" + " ".join(f'"{u}"' for u in urls) + "</html>"
    fake.route("https://example.com/", FakeResponse(status_code=200, text=html))
    fake.route("https://example.com/robots.txt", FakeResponse(status_code=404))
    # Benign listings for the first N buckets; the marker body ONLY appears on
    # buckets beyond the cap -> if the cap were not enforced, the scan would
    # reach bucket 15 and find it.
    for u in urls[:surface_scan._MAX_REFS_CHECKED]:
        fake.route(u, FakeResponse(status_code=200, text="<xml><Name>n</Name></xml>"))
    for u in urls[surface_scan._MAX_REFS_CHECKED:]:
        fake.route(u, FakeResponse(status_code=200,
                                   text="<ListBucketResult><Key>secret.txt</Key></ListBucketResult>"))
    findings = surface_scan.scan_site("https://example.com", module=fake,
                                      timeout=1, per_domain_delay=0, max_findings=20)
    bucket_probes = [u for m, u in fake.calls if "storage.googleapis.com" in u]
    assert len(bucket_probes) <= surface_scan._MAX_REFS_CHECKED
    assert len(bucket_probes) == surface_scan._MAX_REFS_CHECKED  # cap bites exactly
    # The only listing that would have matched is beyond the cap.
    assert _find(findings, "storage_bucket_listing") == []