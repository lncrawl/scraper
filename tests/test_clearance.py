"""Tests for cookie handling and the browser-clearance hook."""

import pytest

from scraper import BrowserConfig, Scraper

from .conftest import make_fast_config


def test_put_cookie_sets_on_session(fast_config):
    s = Scraper(config=fast_config)
    s.put_cookie("a", "1")
    assert s.cookies.get("a") == "1"


def test_set_cookie_decodes_bytes(fast_config):
    s = Scraper(config=fast_config)
    s.set_cookie("b", b"2")
    assert s.cookies.get("b") == "2"


def test_apply_browser_clearance_sets_cookie_and_ua(fast_config):
    s = Scraper(config=fast_config)
    s.apply_browser_clearance(
        "https://protected.example.com/x",
        cf_clearance="TOKEN",
        user_agent="BrowserUA/1.0",
        cookies={"__cf_bm": "BM"},
    )
    assert s.headers["User-Agent"] == "BrowserUA/1.0"
    assert s.cookies.get("cf_clearance", domain="protected.example.com") == "TOKEN"
    assert s.cookies.get("__cf_bm", domain="protected.example.com") == "BM"


def test_apply_browser_clearance_accepts_bare_host(fast_config):
    s = Scraper(config=fast_config)
    s.apply_browser_clearance("example.com", cf_clearance="T")
    assert s.cookies.get("cf_clearance", domain="example.com") == "T"


# --- impersonation cookie sync (needs curl_cffi) --------------------------

pytest.importorskip("curl_cffi")


def test_cookies_propagate_to_impersonation_jar():
    config = make_fast_config(impersonate="chrome", browser=BrowserConfig(browser="chrome"))
    s = Scraper(config=config)
    s.set_cookie("sid", "xyz")
    assert s._impersonate is not None
    assert s._impersonate.cookies.get("sid") == "xyz"


def test_reset_clears_impersonation_jar():
    config = make_fast_config(impersonate="chrome", browser=BrowserConfig(browser="chrome"))
    s = Scraper(config=config)
    s.set_cookie("sid", "xyz")
    s.reset()
    assert list(s.cookies) == []
    assert s._impersonate is not None
    assert s._impersonate.cookies.get("sid") is None
