"""Tests for build_ua_headers and _get_impersonate_browser."""

import re

import pytest

from scraper.config import BrowserConfig, ImpersonateConfig
from scraper.engine.user_agent.agent import _get_impersonate_browser, build_ua_headers

from .conftest import make_fast_config


def test_client_hints_match_chrome_version():
    cfg = BrowserConfig(browser="chrome", platform="windows", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert headers["User-Agent"]
    assert "Chrome/" in headers["User-Agent"]
    match = re.search(r"Chrome/(\d+)", headers["User-Agent"])
    assert match is not None
    assert f'v="{match.group(1)}"' in headers["sec-ch-ua"]
    assert headers["sec-ch-ua-platform"] == '"Windows"'
    assert headers["sec-ch-ua-mobile"] == "?0"


def test_client_hints_mobile_flag():
    cfg = BrowserConfig(browser="chrome", platform="android", desktop=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'


def test_firefox_sends_no_client_hints():
    cfg = BrowserConfig(browser="firefox", platform="windows", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Firefox/" in headers["User-Agent"]
    assert "sec-ch-ua" not in headers


def test_chrome_ua_has_matching_client_hints():
    cfg = BrowserConfig(browser="chrome", platform="windows")
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Chrome/" in headers["User-Agent"]
    match = re.search(r"Chrome/(\d+)", headers["User-Agent"])
    assert match is not None
    assert f'v="{match.group(1)}"' in headers["sec-ch-ua"]


def test_firefox_ua_has_no_client_hints():
    cfg = BrowserConfig(browser="firefox", platform="windows", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Firefox/" in headers["User-Agent"]
    assert "sec-ch-ua" not in headers


def test_custom_user_agent_is_respected():
    custom = "MyCustomBot/1.0"
    cfg = BrowserConfig(custom=custom)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert headers["User-Agent"] == custom


def test_invalid_browser_raises():
    with pytest.raises(RuntimeError):
        cfg = BrowserConfig(browser="netscape")  # type: ignore
        build_ua_headers(make_fast_config(browser=cfg))


def test_allow_brotli_toggle(monkeypatch):
    import scraper.engine.user_agent.cache as ua_mod

    monkeypatch.setattr(ua_mod, "is_brotli_available", lambda: True)
    cfg = BrowserConfig(browser="chrome", allow_brotli=False)
    without = build_ua_headers(make_fast_config(browser=cfg))
    assert "br" not in without.get("Accept-Encoding", "")
    cfg = BrowserConfig(browser="chrome", allow_brotli=True)
    with_br = build_ua_headers(make_fast_config(browser=cfg))
    assert "br" in with_br.get("Accept-Encoding", "")


def test_brotli_stripped_when_unavailable(monkeypatch):
    import scraper.engine.user_agent.cache as ua_mod

    monkeypatch.setattr(ua_mod, "is_brotli_available", lambda: False)
    cfg = BrowserConfig(browser="chrome", allow_brotli=True)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "br" not in headers.get("Accept-Encoding", "")


def test_invalid_platform_raises():
    with pytest.raises(RuntimeError, match="Platform"):
        cfg = BrowserConfig(browser="chrome", platform="xbox")  # type: ignore
        build_ua_headers(make_fast_config(browser=cfg))


def test_mobile_and_desktop_both_false_raises():
    with pytest.raises(RuntimeError, match="mobile and desktop"):
        cfg = BrowserConfig(desktop=False, mobile=False)
        build_ua_headers(make_fast_config(browser=cfg))


def test_load_with_ua_data_picks_ua(monkeypatch):
    """When load_ua_data returns data, load() should use it instead of fallback."""
    import scraper.engine.user_agent.cache as ua_mod

    fake_data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            "weight": 1.0,
        }
    ]
    monkeypatch.setattr(ua_mod, "load_ua_data", lambda: fake_data)

    cfg = BrowserConfig(browser="chrome", platform="windows", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Chrome/130" in headers["User-Agent"]


def test_load_with_ua_data_falls_back_when_filter_empty(monkeypatch):
    """When filtered data yields no platform match, fallback generator is used."""
    import scraper.engine.user_agent.cache as ua_mod

    fake_data = [
        {
            # No platform tokens → _infer_platform returns "linux"; won't match "windows"
            "userAgent": "Mozilla/5.0 Chrome/130.0.0.0",
            "weight": 1.0,
        }
    ]
    monkeypatch.setattr(ua_mod, "load_ua_data", lambda: fake_data)

    cfg = BrowserConfig(browser="chrome", platform="windows")
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Chrome/" in headers["User-Agent"]


def test_load_mobile_platform_with_mobile_false_redirects_to_desktop():
    cfg = BrowserConfig(browser="chrome", platform="android", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Android" not in str(headers.get("User-Agent", ""))


def test_load_desktop_platform_with_desktop_false_redirects_to_mobile():
    cfg = BrowserConfig(browser="chrome", platform="windows", desktop=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "Windows" not in str(headers.get("User-Agent", ""))


def test_build_ua_headers_unrecognized_impersonate_returns_minimal():
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="123"))
    headers = build_ua_headers(cfg)
    assert "User-Agent" not in headers


def test_get_impersonate_browser_plain_name():
    result = _get_impersonate_browser("chrome")
    assert result is not None
    assert result.browser == "chrome"
    assert result.version == 0
    assert result.platform is None
    assert result.desktop is True
    assert result.mobile is False


def test_get_impersonate_browser_with_version():
    result = _get_impersonate_browser("chrome120")
    assert result is not None
    assert result.browser == "chrome"
    assert result.version == 120


def test_get_impersonate_browser_with_platform():
    result = _get_impersonate_browser("chrome120_android")
    assert result is not None
    assert result.browser == "chrome"
    assert result.version == 120
    assert result.platform == "android"
    assert result.mobile is True
    assert result.desktop is False


def test_build_ua_headers_with_valid_impersonate_target():
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome120"))
    headers = build_ua_headers(cfg)
    assert "User-Agent" in headers
    assert "Chrome/" in headers["User-Agent"]


def test_invalid_architecture_raises():
    cfg = BrowserConfig(browser="chrome", architecture="arm64")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Architecture"):
        build_ua_headers(make_fast_config(browser=cfg))


def test_invalid_bitness_raises():
    cfg = BrowserConfig(browser="chrome", bitness="128")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="Bitness"):
        build_ua_headers(make_fast_config(browser=cfg))


def test_safari_mobile_platform_mobile_false_redirects_to_darwin():
    cfg = BrowserConfig(browser="safari", platform="ios", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    ua = headers["User-Agent"]
    assert "iPhone" not in ua and "iPad" not in ua and "Android" not in ua


def test_safari_desktop_platform_desktop_false_redirects_to_ios():
    cfg = BrowserConfig(browser="safari", platform="darwin", desktop=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    ua = headers["User-Agent"]
    assert "iPhone" in ua or "iPad" in ua
