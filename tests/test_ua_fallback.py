"""Tests for the UA fallback generator (generate_ua_fallback)."""

import random

import pytest

from scraper.engine.user_agent.fallback import generate_ua_fallback


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
    with pytest.raises(ValueError, match="Unknown browser: opera"):
        generate_ua_fallback("opera", "windows", None, random.Random(0))


def test_generate_ua_fallback_chrome_windows_default():
    ua = generate_ua_fallback("chrome", "windows", None, random.Random(0))
    assert ua and "Windows NT" in ua
    assert ua and "Chrome/" in ua


def test_generate_ua_fallback_edge_windows():
    ua = generate_ua_fallback("edge", "windows", None, random.Random(0))
    assert ua and "Windows NT" in ua and "Edg/" in ua
