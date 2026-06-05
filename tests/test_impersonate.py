"""Tests for the curl_cffi impersonation transport and UA alignment."""

import pytest

from scraper import Scraper, ScraperConfig
from scraper._engine.impersonate import ImpersonateTransport, build_transport
from scraper._engine.session import _impersonate_family

from .conftest import make_fast_config

curl_cffi = pytest.importorskip("curl_cffi")


# --- helpers (no network) -------------------------------------------------


def test_build_transport_disabled_returns_none():
    assert build_transport(None, True) is None
    assert build_transport("", True) is None


def test_build_transport_enabled():
    t = build_transport("chrome", True)
    assert isinstance(t, ImpersonateTransport)
    assert t.target == "chrome"


@pytest.mark.parametrize(
    "target,family",
    [
        ("chrome", "chrome"),
        ("chrome124", "chrome"),
        ("firefox", "firefox"),
        ("firefox135", "firefox"),
        ("safari17_0", "chrome"),  # unknown families fall back to chrome
        (None, "chrome"),
    ],
)
def test_impersonate_family_mapping(target, family):
    assert _impersonate_family(target) == family


# --- response adaptation --------------------------------------------------


class _FakeCookies:
    jar: list = []


class _FakeResp:
    status_code = 200
    content = b"<h1>hi</h1>"
    url = "https://example.com/"
    reason = "OK"
    encoding = "utf-8"
    headers = {"Content-Type": "text/html"}
    cookies = _FakeCookies()


def test_adapt_builds_requests_response():
    out = ImpersonateTransport._adapt("get", "https://example.com/", {"A": "b"}, _FakeResp())
    assert out.status_code == 200
    assert out.content == b"<h1>hi</h1>"
    assert out.headers["Content-Type"] == "text/html"
    # a minimal request record is attached for challenge-redirect handling
    assert out.request.method == "GET"
    assert out.request.url == "https://example.com/"


# --- engine integration (constructs a real curl_cffi session, no network) -


def test_impersonate_aligns_ua_family_over_default():
    from scraper import BrowserConfig

    # A configured firefox family must be overridden to chrome by impersonate.
    # Pin a desktop platform so the UA is deterministically "Chrome/...".
    config = make_fast_config(
        impersonate="chrome",
        browser=BrowserConfig(browser="firefox", platform="windows", mobile=False),
    )
    s = Scraper(config=config)
    assert "Chrome/" in str(s.headers["User-Agent"])
    assert s.headers.get("sec-ch-ua")
    assert s._impersonate is not None


def test_impersonate_firefox_target():
    from scraper import BrowserConfig

    config = ScraperConfig(
        impersonate="firefox",
        browser=BrowserConfig(platform="windows", mobile=False),
    )
    s = Scraper(config=config)
    assert "Firefox/" in str(s.headers["User-Agent"])
    assert "sec-ch-ua" not in s.headers


def test_custom_ua_is_not_overridden_by_impersonation():
    from scraper import BrowserConfig

    config = ScraperConfig(impersonate="chrome", browser=BrowserConfig(custom="MyBot/9"))
    s = Scraper(config=config)
    assert s.headers["User-Agent"] == "MyBot/9"


def test_no_impersonation_by_default(fast_config):
    s = Scraper(config=fast_config)
    assert s._impersonate is None
