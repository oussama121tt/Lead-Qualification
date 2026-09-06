"""Free-first fetcher: JS-shell detection and blockquote preservation
(the testimonial tagger downstream depends on the '> ' markers)."""
from bs4 import BeautifulSoup

from site_fetcher import _blockquotes_to_markdown, looks_js_heavy


def test_js_heavy_app_shell_detected():
    html = '<div id="root"></div>' + "<script></script>" * 3
    assert looks_js_heavy(html, "")


def test_rich_server_rendered_page_not_js_heavy():
    text = "word " * 200   # >= 400 chars of real text
    assert not looks_js_heavy("<div id='root'></div>", text)


def test_many_scripts_and_no_text_is_js_heavy():
    html = "<script></script>" * 10
    assert looks_js_heavy(html, "tiny")


def test_blockquotes_become_markdown_quotes():
    soup = BeautifulSoup(
        "<blockquote>I built my MVP with vibe coding\nand it collapsed</blockquote>"
        "<p>Jane D — Founder, Acme</p>",
        "lxml",
    )
    _blockquotes_to_markdown(soup)
    text = soup.get_text(separator="\n")
    assert "> I built my MVP with vibe coding" in text
    assert "> and it collapsed" in text
    # The attribution stays OUTSIDE the quote markers.
    assert "> Jane D" not in text
