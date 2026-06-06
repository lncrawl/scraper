"""Tests for User-Agent selection and Client Hints derivation."""

import random
import re

from scraper.config import BrowserConfig
from scraper.engine.user_agent.agent import build_ua_headers
from scraper.engine.user_agent.fallback import generate_ua_fallback
from scraper.engine.user_agent.filter import (
    filter_ua_data,
    weighted_choice,
)
from scraper.engine.user_agent.helper import (
    infer_ch_platform,
    infer_platform,
)

from .conftest import make_fast_config

# --- Client Hints (tested via UserAgent headers) --------------------------


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


def test_ch_platform_mapping():
    assert infer_ch_platform("... Windows NT 10.0 ...") == "Windows"
    assert infer_ch_platform("... Macintosh; Intel Mac OS X ...") == "macOS"
    assert infer_ch_platform("... iPhone; CPU iPhone OS ...") == "iOS"
    assert infer_ch_platform("... Android 14 ...") == "Android"
    assert infer_ch_platform("... X11; Linux x86_64 ...") == "Linux"


# --- UA generation (offline fallback via conftest stub) -------------------


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
    import pytest

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
    # The `brotli` extra may not be installed; don't advertise an encoding we
    # cannot decode even when allow_brotli is True.
    import scraper.engine.user_agent.cache as ua_mod

    monkeypatch.setattr(ua_mod, "is_brotli_available", lambda: False)

    cfg = BrowserConfig(browser="chrome", allow_brotli=True)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    assert "br" not in headers.get("Accept-Encoding", "")


# --- _generate_ua_fallback platform coverage (offline, via conftest stub) --


def test_generate_ua_fallback_all_firefox_platforms():
    rng = random.Random(42)
    for platform in ("windows", "darwin", "linux", "android", "ios"):
        ua = generate_ua_fallback("firefox", platform, None, rng)
        assert ua and ("Firefox/" in ua or "FxiOS/" in ua), f"missing Firefox tag for {platform}"


def test_generate_ua_fallback_all_chrome_platforms():
    rng = random.Random(42)
    for platform in ("windows", "darwin", "linux", "android", "ios"):
        ua = generate_ua_fallback("chrome", platform, None, rng)
        assert ua and ("Chrome/" in ua or "CriOS/" in ua), f"missing Chrome tag for {platform}"


def test_generate_ua_fallback_safari_ios():
    ua = generate_ua_fallback("safari", "ios", None, random.Random(42))
    assert ua and "iPhone" in ua


def test_generate_ua_fallback_edge_all_non_windows_platforms():
    rng = random.Random(42)
    for platform, marker in [
        ("darwin", "Macintosh"),
        ("linux", "Linux"),
        ("android", "EdgA/"),
        ("ios", "EdgiOS/"),
    ]:
        ua = generate_ua_fallback("edge", platform, None, rng)
        assert ua and marker in ua, f"missing {marker!r} for edge/{platform}"


def test_generate_ua_fallback_unknown_browser_returns_none():
    assert generate_ua_fallback("opera", "windows", None, random.Random(0)) is None


def test_filter_ua_data_skips_mobile_with_wrong_device_category():
    """Android/iOS platform filters also require deviceCategory == 'mobile'."""
    data = [
        {
            "userAgent": "Mozilla/5.0 (Linux; Android 14) Chrome/130.0.0.0 Mobile Safari/537.36",
            "deviceCategory": "desktop",
            "weight": 1.0,
        }
    ]
    assert filter_ua_data(data, None, "android") == []


def test_match_version_exact_match_below_min():
    """An exact version == v passes even when v < min_version."""
    from scraper.engine.user_agent.filter import _CHROME_RE, _match_version

    assert _match_version("Chrome/90.0.0.0", _CHROME_RE, 90, 120) is True


def test_match_version_below_min_no_exact_match_rejected():
    """A version below min_version with no exact-match criteria is rejected."""
    from scraper.engine.user_agent.filter import _CHROME_RE, _match_version

    assert _match_version("Chrome/90.0.0.0", _CHROME_RE, None, 120) is False


def test_generate_ua_fallback_chrome_windows_default():
    ua = generate_ua_fallback("chrome", "windows", None, random.Random(0))
    assert ua and "Windows NT" in ua
    assert ua and "Chrome/" in ua


def test_invalid_platform_raises():
    import pytest

    with pytest.raises(RuntimeError, match="Platform"):
        cfg = BrowserConfig(browser="chrome", platform="xbox")  # type: ignore
        build_ua_headers(make_fast_config(browser=cfg))


def test_mobile_and_desktop_both_false_raises():
    import pytest

    with pytest.raises(RuntimeError, match="mobile and desktop"):
        cfg = BrowserConfig(desktop=False, mobile=False)
        build_ua_headers(make_fast_config(browser=cfg))


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
    chrome_only = filter_ua_data(data, "chrome")
    assert len(chrome_only) == 1
    assert "Chrome/" in chrome_only[0]["userAgent"]


def test_filter_ua_data_excludes_old_versions():
    data = [
        {"userAgent": "Mozilla/5.0 Chrome/90.0.0.0", "weight": 1.0},
        {"userAgent": "Mozilla/5.0 Chrome/130.0.0.0", "weight": 1.0},
    ]
    result = filter_ua_data(data)
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


# --- _ch_platform edge cases -----------------------------------------------


def test_ch_platform_chromeos():
    assert infer_ch_platform("Mozilla/5.0 (X11; CrOS x86_64 14469.0.0)") == "Chrome OS"


# --- fallback platform vs mobile/desktop constraint --------


def test_load_mobile_platform_with_mobile_false_redirects_to_desktop():
    # platform="android" (mobile) + mobile=False → engine picks a desktop platform
    cfg = BrowserConfig(browser="chrome", platform="android", mobile=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    ua_str = str(headers.get("User-Agent", ""))
    # the engine overrides to a desktop platform so the result is not Android
    assert "Android" not in ua_str


def test_load_desktop_platform_with_desktop_false_redirects_to_mobile():
    # platform="windows" (desktop) + desktop=False → engine picks a mobile platform
    cfg = BrowserConfig(browser="chrome", platform="windows", desktop=False)
    headers = build_ua_headers(make_fast_config(browser=cfg))
    ua_str = str(headers.get("User-Agent", ""))
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
    import scraper.engine.user_agent.cache as cache

    monkeypatch.setattr(cache, "_CACHE_PATH", tmp_path / "no_such_file.json.gz")
    assert cache._read_cache() is None


# --- _match_version: no regex match → return False (line 24) ---------------


def test_match_version_no_regex_match_returns_false():
    from scraper.engine.user_agent.filter import _CHROME_RE, _match_version

    # UA has no Chrome/ token at all → regex doesn't match → line 24
    assert _match_version("Mozilla/5.0 Firefox/130.0", _CHROME_RE, None, 120) is False


# --- infer_browser: Chromium UA returns None (line 13→18) ------------------


def test_infer_browser_chromium_ua_returns_none():
    from scraper.engine.user_agent.helper import infer_browser

    # "Chromium" in UA → the Chrome/Safari branches are skipped → returns None
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/130.0.0.0 Safari/537.36"
    assert infer_browser(ua) is None


# --- infer_ch_platform: Linux fallback (line 36) ---------------------------


def test_infer_ch_platform_linux_fallback():
    from scraper.engine.user_agent.helper import infer_ch_platform

    # Generic desktop UA with no OS token → falls through to "Linux"
    assert infer_ch_platform("Mozilla/5.0 (X11; Linux x86_64)") == "Linux"


# --- _add_client_hints: UA without Chrome/ skips hint generation (line 112) --


def test_add_client_hints_no_chrome_version_skips_hints():
    # UA that infer_browser returns "chrome" for (has Chrome/ token) but the version
    # part is non-numeric so re.search returns None → return early at line 112.
    from requests.structures import CaseInsensitiveDict

    from scraper.config import BrowserConfig
    from scraper.engine.user_agent.agent import _add_client_hints

    headers = CaseInsensitiveDict({"User-Agent": "Mozilla/5.0 Chrome/NoVersion Safari/537.36"})
    _add_client_hints(BrowserConfig(browser="chrome"), headers)
    assert "sec-ch-ua" not in headers


# --- edge client hints: ev extracted from Edg/ token (lines 126-132) -----


def test_add_client_hints_edge_includes_ev():
    from requests.structures import CaseInsensitiveDict

    from scraper.config import BrowserConfig
    from scraper.engine.user_agent.agent import _add_client_hints

    edge_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.4472.124 Safari/537.36 Edg/130.0.2849.52"
    )
    headers = CaseInsensitiveDict({"User-Agent": edge_ua})
    _add_client_hints(BrowserConfig(browser="edge"), headers)
    assert "sec-ch-ua" in headers
    assert "Microsoft Edge" in headers["sec-ch-ua"]
    # ev comes from Edg/ token
    assert "130" in headers["sec-ch-ua"]


# --- filter_ua_data: modern Firefox passes version check (branch 74→85) ---


def test_filter_ua_data_includes_modern_firefox():
    data = [{"userAgent": "Mozilla/5.0 (rv:135.0) Gecko/20100101 Firefox/135.0", "weight": 1.0}]
    result = filter_ua_data(data, "firefox", None)
    assert len(result) == 1


# --- filter_ua_data: old Edge excluded (line 72 — continue for old edge) --


def test_filter_ua_data_skips_old_edge():
    data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/90.0.0.0 Safari/537.36 Edg/90.0",
            "weight": 1.0,
        }
    ]
    assert filter_ua_data(data, "edge", None) == []


# --- build_ua_headers: unrecognized impersonate target returns early (line 153) --


def test_build_ua_headers_unrecognized_impersonate_returns_minimal():
    from scraper.config import ImpersonateConfig

    # "123" is truthy so the impersonate branch is entered, but _IMPERSONATE_TARGET_RE
    # requires at least one letter — all-digits target → findall returns [] → None →
    # early return at line 153 with just Accept-Encoding, no User-Agent set.
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="123"))
    headers = build_ua_headers(cfg)
    assert "User-Agent" not in headers
