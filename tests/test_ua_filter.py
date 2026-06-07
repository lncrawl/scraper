"""Tests for filter_ua_data, weighted_choice, and _match_version."""

import random

from scraper.engine.user_agent.filter import (
    filter_ua_data,
    weighted_choice,
)


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


def test_match_version_no_regex_match_returns_false():
    from scraper.engine.user_agent.filter import _CHROME_RE, _match_version

    assert _match_version("Mozilla/5.0 Firefox/130.0", _CHROME_RE, None, 120) is False


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
    assert filter_ua_data(data, None, "windows") == []


def test_filter_ua_data_includes_modern_firefox():
    data = [{"userAgent": "Mozilla/5.0 (rv:135.0) Gecko/20100101 Firefox/135.0", "weight": 1.0}]
    result = filter_ua_data(data, "firefox", None)
    assert len(result) == 1


def test_filter_ua_data_skips_old_edge():
    data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/90.0.0.0 Safari/537.36 Edg/90.0",
            "weight": 1.0,
        }
    ]
    assert filter_ua_data(data, "edge", None) == []


def test_filter_ua_data_includes_modern_chrome():
    data = [
        {
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36",
            "weight": 1.0,
        }
    ]
    result = filter_ua_data(data, "chrome", None)
    assert len(result) == 1
    assert "Chrome/130" in result[0]["userAgent"]


def test_filter_ua_data_else_skips_unrecognised_browser(monkeypatch):
    """The else: continue guard rejects entries whose inferred browser is not
    one of the four known families (edge/firefox/safari/chrome)."""
    import scraper.engine.user_agent.filter as filter_mod

    monkeypatch.setattr(filter_mod, "infer_browser", lambda ua: "opera")
    data = [{"userAgent": "Opera/9.80 (Windows NT 6.1) Presto/2.12", "weight": 1.0}]
    assert filter_ua_data(data, None, None) == []
