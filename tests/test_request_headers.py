"""Tests for RequestHeaders — the case-insensitive header dict."""

from scraper.utils import RequestHeaders


def test_getitem_case_insensitive():
    d = RequestHeaders({"Content-Type": "text/html"})
    assert d["content-type"] == "text/html"
    assert d["CONTENT-TYPE"] == "text/html"


def test_contains_case_insensitive():
    d = RequestHeaders({"Accept": "*/*"})
    assert "accept" in d
    assert "ACCEPT" in d
    assert "Missing" not in d


def test_get_case_insensitive():
    d = RequestHeaders({"X-Token": "abc"})
    assert d.get("x-token") == "abc"
    assert d.get("missing", "default") == "default"


def test_pop_case_insensitive():
    d = RequestHeaders({"Authorization": "Bearer xyz"})
    val = d.pop("authorization")
    assert val == "Bearer xyz"
    assert "Authorization" not in d


def test_pop_missing_with_default():
    d = RequestHeaders()
    assert d.pop("missing", "fallback") == "fallback"


def test_update_from_dict():
    d = RequestHeaders()
    d.update({"content-type": "application/json", "Accept": "*/*"})
    assert d.get("Content-Type") == "application/json"
    assert d.get("accept") == "*/*"


def test_update_from_kwargs():
    d = RequestHeaders()
    d.update(accept="text/html")
    assert d.get("Accept") == "text/html"


def test_update_from_iterable():
    d = RequestHeaders()
    d.update([("content-length", "42")])
    assert d.get("Content-Length") == "42"


def test_contains_non_string_key():
    d = RequestHeaders({"Accept": "*/*"})
    assert 42 not in d


def test_setdefault_normalises_key():
    d = RequestHeaders()
    d.setdefault("content-type", "text/plain")
    assert d["Content-Type"] == "text/plain"
    d.setdefault("content-type", "application/json")
    assert d["Content-Type"] == "text/plain"


def test_keys_are_stored_title_cased():
    d = RequestHeaders()
    d["x-custom-header"] = "val"
    assert list(d.keys()) == ["X-Custom-Header"]
