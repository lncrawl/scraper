"""Tests for UA inference helpers: infer_platform, infer_ch_platform, infer_browser,
and _add_client_hints."""

from requests.structures import CaseInsensitiveDict

from scraper.config import BrowserConfig
from scraper.engine.user_agent.agent import _add_client_hints
from scraper.engine.user_agent.helper import (
    infer_browser,
    infer_ch_platform,
    infer_platform,
)


def test_ch_platform_mapping():
    assert infer_ch_platform("... Windows NT 10.0 ...") == "Windows"
    assert infer_ch_platform("... Macintosh; Intel Mac OS X ...") == "macOS"
    assert infer_ch_platform("... iPhone; CPU iPhone OS ...") == "iOS"
    assert infer_ch_platform("... Android 14 ...") == "Android"
    assert infer_ch_platform("... X11; Linux x86_64 ...") == "Linux"


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


def test_ch_platform_chromeos():
    assert infer_ch_platform("Mozilla/5.0 (X11; CrOS x86_64 14469.0.0)") == "Chrome OS"


def test_ch_platform_linux_fallback():
    assert infer_ch_platform("Mozilla/5.0 (X11; Linux x86_64)") == "Linux"


def test_ch_platform_none_ua_returns_none():
    assert infer_ch_platform(None) is None


def test_infer_browser_chromium_ua_returns_none():
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/130.0.0.0 Safari/537.36"
    assert infer_browser(ua) is None


def test_add_client_hints_no_chrome_version_skips_hints():
    headers = CaseInsensitiveDict({"User-Agent": "Mozilla/5.0 Chrome/NoVersion Safari/537.36"})
    _add_client_hints(BrowserConfig(browser="chrome"), headers)
    assert "sec-ch-ua" not in headers


def test_add_client_hints_edge_includes_ev():
    edge_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.4472.124 Safari/537.36 Edg/130.0.2849.52"
    )
    headers = CaseInsensitiveDict({"User-Agent": edge_ua})
    _add_client_hints(BrowserConfig(browser="edge"), headers)
    assert "sec-ch-ua" in headers
    assert "Microsoft Edge" in headers["sec-ch-ua"]
    assert "130" in headers["sec-ch-ua"]


def test_add_client_hints_sets_arch_and_bitness():
    chrome_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    cfg = BrowserConfig(browser="chrome", architecture="x86", bitness="64")
    headers = CaseInsensitiveDict({"User-Agent": chrome_ua})
    _add_client_hints(cfg, headers)
    assert headers.get("sec-ch-ua-arch") == '"x86"'
    assert headers.get("sec-ch-ua-bitness") == '"64"'
