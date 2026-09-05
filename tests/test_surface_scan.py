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
        self.default = FakeResponse()

    def route(self, url, resp):
        self.routes[url] = resp

    def _handle(self, method, url):
        self.calls.append((method, url))
        if method not in surface_scan.ALLOWED_METHODS:
            raise AssertionError(f"forbidden HTTP method used: {method} {url}")
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