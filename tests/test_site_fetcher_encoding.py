"""Free fetcher decodes pages as the site meant (UTF-8 without a declared
charset used to come back as latin-1 mojibake and break quote grounding)."""
import site_fetcher


class _Resp:
    def __init__(self, body: bytes, ctype: str):
        self.content = body
        self.headers = {"content-type": ctype}
        self.encoding = "ISO-8859-1"
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self.content.decode(self.encoding, errors="replace")


def test_utf8_without_charset_is_decoded_as_utf8():
    body = "<p>스포츠 분석: 스포츠의 모든 것 — café</p>".encode("utf-8")
    html = site_fetcher.decode_html(_Resp(body, "text/html"))
    assert "스포츠 분석" in html and "café" in html


def test_declared_charset_is_respected():
    body = "<p>café</p>".encode("latin-1")
    html = site_fetcher.decode_html(_Resp(body, "text/html; charset=ISO-8859-1"))
    assert "café" in html


def test_mojibake_is_repaired_when_it_slipped_through():
    r = _Resp("<p>café — naïve</p>".encode("utf-8"), "text/html; charset=ISO-8859-1")
    html = site_fetcher.decode_html(r)     # declared charset is wrong: repair kicks in
    assert "café" in html and "naïve" in html
