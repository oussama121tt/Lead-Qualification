"""Builder-subdomain near-proof signal (Task 2).

Locks the fix that (a) the free-first scrape path passes homepage_url so
on_builder_subdomain actually fires, and (b) a known builder subdomain also
populates app_builder_fingerprint (a STRONG segment signal downstream).
"""
from scraper import BUILDER_SUBDOMAIN_SUFFIXES, extract_technical_signals


def test_known_builder_subdomain_sets_app_builder_fingerprint():
    signals = extract_technical_signals(
        raw_html="<html><body>hi</body></html>",
        all_links=[],
        homepage_url="https://myapp.bubbleapps.io",
    )
    assert signals["on_builder_subdomain"] is True
    assert signals["on_builder_subdomain_builder"] == "bubble"
    assert signals["app_builder_fingerprint"] == "bubble"
    assert signals["generator_fingerprint"] == "bubble"


def test_existing_html_fingerprint_takes_precedence_over_subdomain():
    # If the HTML already names a different product builder, the subdomain
    # must not overwrite it.
    signals = extract_technical_signals(
        raw_html="<html><body>built with bolt.new</body></html>",
        all_links=[],
        homepage_url="https://myapp.bubbleapps.io",
    )
    assert signals["on_builder_subdomain"] is True
    assert signals["on_builder_subdomain_builder"] == "bubble"
    assert signals["app_builder_fingerprint"] == "bolt"


def test_unknown_subdomain_does_not_fire():
    signals = extract_technical_signals(
        raw_html="<html><body>hi</body></html>",
        all_links=[],
        homepage_url="https://myapp.example.com",
    )
    assert signals["on_builder_subdomain"] is False
    assert signals["app_builder_fingerprint"] is None


def test_missing_homepage_url_does_not_fire_subdomain():
    # Guards the original plumbing bug: the free-first scrape path omitted
    # homepage_url, silently disabling this near-proof signal.
    signals = extract_technical_signals(
        raw_html="<html><body>hi</body></html>",
        all_links=[],
    )
    assert signals["on_builder_subdomain"] is False


def test_app_builder_subdomain_maps_to_app_builder():
    # Replit is an APP builder (in APP_BUILDER_FINGERPRINTS), so a replit.app
    # subdomain yields app_builder_fingerprint, unlike a site-builder host.
    assert ".replit.app" in BUILDER_SUBDOMAIN_SUFFIXES.get("replit", "")
    signals = extract_technical_signals(
        raw_html="<html><body>hi</body></html>",
        all_links=[],
        homepage_url="https://team.replit.app",
    )
    assert signals["on_builder_subdomain"] is True
    assert signals["on_builder_subdomain_builder"] == "replit"
    assert signals["app_builder_fingerprint"] == "replit"
