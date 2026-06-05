"""Tests for User-Agent selection and Client Hints derivation."""

import random

from scraper._engine.config import BrowserConfig
from scraper._engine.user_agent.agent import UserAgent
from scraper._engine.user_agent.fallback import generate_ua_fallback
from scraper._engine.user_agent.filter import (
    filter_ua_data,
    infer_ch_platform,
    infer_platform,
    weighted_choice,
)

# --- Client Hints (tested via UserAgent headers) --------------------------


def test_client_hints_match_chrome_version():
    import re

    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows", mobile=False))
    assert "Chrome/" in ua.headers["User-Agent"]
    match = re.search(r"Chrome/(\d+)", ua.headers["User-Agent"])
    assert match is not None
    assert f'v="{match.group(1)}"' in ua.headers["sec-ch-ua"]
    assert ua.headers["sec-ch-ua-platform"] == '"Windows"'
    assert ua.headers["sec-ch-ua-mobile"] == "?0"


def test_client_hints_mobile_flag():
    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="android", desktop=False))
    assert ua.headers["sec-ch-ua-mobile"] == "?1"
    assert ua.headers["sec-ch-ua-platform"] == '"Android"'


def test_firefox_sends_no_client_hints():
    ua = UserAgent(cfg=BrowserConfig(browser="firefox", platform="windows", mobile=False))
    assert "Firefox/" in ua.headers["User-Agent"]
    assert "sec-ch-ua" not in ua.headers


def test_ch_platform_mapping():
    assert infer_ch_platform("... Windows NT 10.0 ...") == "Windows"
    assert infer_ch_platform("... Macintosh; Intel Mac OS X ...") == "macOS"
    assert infer_ch_platform("... iPhone; CPU iPhone OS ...") == "iOS"
    assert infer_ch_platform("... Android 14 ...") == "Android"
    assert infer_ch_platform("... X11; Linux x86_64 ...") == "Linux"


# --- UA generation (offline fallback via conftest stub) -------------------


def test_chrome_ua_has_matching_client_hints():
    import re

    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows"))
    assert "Chrome/" in ua.headers["User-Agent"]
    match = re.search(r"Chrome/(\d+)", ua.headers["User-Agent"])
    assert match is not None
    assert f'v="{match.group(1)}"' in ua.headers["sec-ch-ua"]


def test_firefox_ua_has_no_client_hints():
    ua = UserAgent(cfg=BrowserConfig(browser="firefox", platform="windows", mobile=False))
    assert "Firefox/" in ua.headers["User-Agent"]
    assert "sec-ch-ua" not in ua.headers


def test_custom_user_agent_is_respected():
    custom = "MyCustomBot/1.0"
    ua = UserAgent(cfg=BrowserConfig(custom=custom))
    assert ua.headers["User-Agent"] == custom


def test_invalid_browser_raises():
    import pytest

    with pytest.raises(RuntimeError):
        UserAgent(cfg=BrowserConfig(browser="netscape"))  # type: ignore


def test_allow_brotli_toggle(monkeypatch):
    import scraper._engine.user_agent.cache as ua_mod

    monkeypatch.setattr(ua_mod, "is_brotli_available", lambda: True)

    without = UserAgent(cfg=BrowserConfig(browser="chrome", allow_brotli=False))
    assert "br" not in without.headers.get("Accept-Encoding", "")
    with_br = UserAgent(cfg=BrowserConfig(browser="chrome", allow_brotli=True))
    assert "br" in with_br.headers.get("Accept-Encoding", "")


def test_brotli_stripped_when_unavailable(monkeypatch):
    # The `brotli` extra may not be installed; don't advertise an encoding we
    # cannot decode even when allow_brotli is True.
    import scraper._engine.user_agent.cache as ua_mod

    monkeypatch.setattr(ua_mod, "is_brotli_available", lambda: False)

    ua = UserAgent(cfg=BrowserConfig(browser="chrome", allow_brotli=True))
    assert "br" not in ua.headers.get("Accept-Encoding", "")


# --- _generate_ua_fallback platform coverage (offline, via conftest stub) --


def test_generate_ua_fallback_all_firefox_platforms():
    rng = random.Random(42)
    for platform in ("windows", "darwin", "linux", "android", "ios"):
        ua = generate_ua_fallback("firefox", platform, rng)
        assert ua and ("Firefox/" in ua or "FxiOS/" in ua), f"missing Firefox tag for {platform}"


def test_generate_ua_fallback_all_chrome_platforms():
    rng = random.Random(42)
    for platform in ("windows", "darwin", "linux", "android", "ios"):
        ua = generate_ua_fallback("chrome", platform, rng)
        assert ua and ("Chrome/" in ua or "CriOS/" in ua), f"missing Chrome tag for {platform}"


def test_generate_ua_fallback_chrome_windows_default():
    ua = generate_ua_fallback("chrome", "windows", random.Random(0))
    assert ua and "Windows NT" in ua
    assert ua and "Chrome/" in ua


def test_invalid_platform_raises():
    import pytest

    with pytest.raises(RuntimeError, match="Platform"):
        UserAgent(cfg=BrowserConfig(browser="chrome", platform="xbox"))  # type: ignore


def test_mobile_and_desktop_both_false_raises():
    import pytest

    with pytest.raises(RuntimeError, match="mobile and desktop"):
        UserAgent(cfg=BrowserConfig(desktop=False, mobile=False))


# --- _infer_platform ------------------------------------------------------


def test_infer_platform_mobile_ios():
    assert infer_platform("... iPhone ...") == "ios"
    assert infer_platform("... iPad ...") == "ios"


def test_infer_platform_mobile_android():
    assert infer_platform("Android 14") == "android"


def test_infer_platform_desktop_windows():
    assert infer_platform("Windows NT") == "windows"


def test_infer_platform_desktop_mac():
    assert infer_platform("Macintosh; Intel Mac OS X") == "darwin"


def test_infer_platform_desktop_linux():
    assert infer_platform("X11; Linux x86_64") == "linux"


# --- _filter_ua_data and _weighted_choice (offline) -----------------------


def test_filter_ua_data_browser_filter():
    data = [
        {"userAgent": "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36", "weight": 1.0},
        {"userAgent": "Mozilla/5.0 (rv:140.0) Gecko/20100101 Firefox/140.0", "weight": 1.0},
    ]
    chrome_only = filter_ua_data(data, "chrome", None)
    assert len(chrome_only) == 1
    assert "Chrome/" in chrome_only[0]["userAgent"]


def test_filter_ua_data_excludes_old_versions():
    data = [
        {"userAgent": "Mozilla/5.0 Chrome/90.0.0.0", "weight": 1.0},
        {"userAgent": "Mozilla/5.0 Chrome/130.0.0.0", "weight": 1.0},
    ]
    result = filter_ua_data(data, None, None)
    assert len(result) == 1
    assert "130" in result[0]["userAgent"]


def test_filter_ua_data_by_platform():
    data = [
        {
            "userAgent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/130.0.0.0 Mobile Safari/537.36",
            "weight": 1.0,
        },
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36",
            "weight": 1.0,
        },
    ]
    windows_only = filter_ua_data(data, None, "windows")
    assert len(windows_only) == 1
    assert "Windows" in windows_only[0]["userAgent"]


def test_filter_ua_data_includes_edge():
    data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36 Edg/130.0",
            "weight": 1.0,
        },
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            "weight": 1.0,
        },
    ]
    edge_only = filter_ua_data(data, "edge", None)
    assert len(edge_only) == 1
    assert "Edg/" in edge_only[0]["userAgent"]


def test_filter_ua_data_excludes_firefox_when_chrome_requested():
    data = [
        {"userAgent": "Mozilla/5.0 Chrome/130.0.0.0 Edg/130.0", "weight": 1.0},
        {"userAgent": "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36", "weight": 1.0},
    ]
    result = filter_ua_data(data, "firefox", None)
    assert result == []


def test_weighted_choice_returns_ua_string():
    data = [{"userAgent": "A", "weight": 1.0}, {"userAgent": "B", "weight": 1.0}]
    chosen = weighted_choice(data, random.Random(0))
    assert chosen in ("A", "B")


def test_weighted_choice_returns_from_list():
    data = [{"userAgent": "A", "weight": 2.0}, {"userAgent": "B", "weight": 1.0}]
    rng = random.Random(99)
    for _ in range(20):
        chosen = weighted_choice(data, rng)
        assert chosen in ("A", "B")


# --- UserAgent.load with UA data present ----------------------------------


def test_load_with_ua_data_picks_ua(monkeypatch):
    """When load_ua_data returns data, load() should use it instead of fallback."""
    import scraper._engine.user_agent.cache as ua_mod

    fake_data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            "weight": 1.0,
        }
    ]
    monkeypatch.setattr(ua_mod, "load_ua_data", lambda: fake_data)

    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows", mobile=False))
    assert "Chrome/130" in ua.headers["User-Agent"]


def test_load_with_ua_data_falls_back_when_filter_empty(monkeypatch):
    """When filtered data yields no platform match, fallback generator is used."""
    import scraper._engine.user_agent.cache as ua_mod

    fake_data = [
        {
            # No platform tokens → _infer_platform returns "linux"; won't match "windows"
            "userAgent": "Mozilla/5.0 Chrome/130.0.0.0",
            "weight": 1.0,
        }
    ]
    monkeypatch.setattr(ua_mod, "load_ua_data", lambda: fake_data)

    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows"))
    assert "Chrome/" in ua.headers["User-Agent"]


# --- _ch_platform edge cases -----------------------------------------------


def test_ch_platform_chromeos():
    assert infer_ch_platform("Mozilla/5.0 (X11; CrOS x86_64 14469.0.0)") == "Chrome OS"


# --- UserAgent.load -------------------


def test_load_with_string_browser_arg():
    # Pin a desktop platform to avoid the iOS CriOS/ UA family (testing skill gotcha).
    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows", mobile=False))
    # Reload with a plain BrowserConfig to exercise load()
    ua.load(cfg=BrowserConfig(browser="chrome"))
    user_agent = str(ua.headers.get("User-Agent", ""))
    assert "Chrome/" in user_agent or "CriOS/" in user_agent


# --- fallback platform vs mobile/desktop constraint --------


def test_load_mobile_platform_with_mobile_false_redirects_to_desktop():
    # platform="android" (mobile) + mobile=False → engine picks a desktop platform
    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="android", mobile=False))
    ua_str = str(ua.headers.get("User-Agent", ""))
    # the engine overrides to a desktop platform so the result is not Android
    assert "Android" not in ua_str


def test_load_desktop_platform_with_desktop_false_redirects_to_mobile():
    # platform="windows" (desktop) + desktop=False → engine picks a mobile platform
    ua = UserAgent(cfg=BrowserConfig(browser="chrome", platform="windows", desktop=False))
    ua_str = str(ua.headers.get("User-Agent", ""))
    # engine overrides to mobile platform
    assert "Windows" not in ua_str


# --- _filter_ua_data branch coverage ----------------------------------------


def test_filter_ua_data_skips_old_firefox():
    data = [{"userAgent": "Mozilla/5.0 (rv:90.0) Gecko/20100101 Firefox/90.0", "weight": 1.0}]
    assert filter_ua_data(data, None, None) == []


def test_filter_ua_data_skips_old_safari():
    data = [
        {
            "userAgent": "Mozilla/5.0 AppleWebKit/605.1.15 Version/16.0 Safari/605.1.15",
            "weight": 1.0,
        }
    ]
    assert filter_ua_data(data, None, None) == []


def test_filter_ua_data_includes_modern_safari():
    data = [
        {
            "userAgent": "Mozilla/5.0 AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
            "weight": 1.0,
        }
    ]
    result = filter_ua_data(data, "safari", None)
    assert len(result) == 1


def test_filter_ua_data_excludes_unknown_browsers():
    data = [
        {
            "userAgent": "Mozilla/5.0 (compatible; MSIE 11.0; Windows NT 6.1; Trident/7.0)",
            "weight": 1.0,
        }
    ]
    assert filter_ua_data(data, None, None) == []


def test_filter_ua_data_platform_filter():
    data = [{"userAgent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0.0.0", "weight": 1.0}]
    # Only windows accepted → linux entry skipped
    assert filter_ua_data(data, None, "windows") == []


# --- _read_cache on missing/corrupt cache file ----------------------------


def test_read_cache_returns_none_when_file_missing(tmp_path, monkeypatch):
    import scraper._engine.user_agent.cache as cache

    monkeypatch.setattr(cache, "_CACHE_PATH", tmp_path / "no_such_file.json.gz")
    assert cache._read_cache() is None
