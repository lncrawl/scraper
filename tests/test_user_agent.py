"""Tests for User-Agent selection and Client Hints derivation."""

from scraper._engine.user_agent import UserAgent, _ch_platform, _client_hints

# --- Client Hints ---------------------------------------------------------


def test_client_hints_match_chrome_version():
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36"
    hints = _client_hints(ua)
    assert 'v="145"' in hints["sec-ch-ua"]
    assert hints["sec-ch-ua-platform"] == '"Windows"'
    assert hints["sec-ch-ua-mobile"] == "?0"


def test_client_hints_mobile_flag():
    ua = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/145.0.0.0 Mobile Safari/537.36"
    hints = _client_hints(ua)
    assert hints["sec-ch-ua-mobile"] == "?1"
    assert hints["sec-ch-ua-platform"] == '"Android"'


def test_firefox_sends_no_client_hints():
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
    assert _client_hints(ua) == {}


def test_ch_platform_mapping():
    assert _ch_platform("... Windows NT 10.0 ...") == "Windows"
    assert _ch_platform("... Macintosh; Intel Mac OS X ...") == "macOS"
    assert _ch_platform("... iPhone; CPU iPhone OS ...") == "iOS"
    assert _ch_platform("... Android 14 ...") == "Android"
    assert _ch_platform("... X11; Linux x86_64 ...") == "Linux"


# --- UA generation (offline fallback via conftest stub) -------------------


def test_chrome_ua_has_matching_client_hints():
    ua = UserAgent(browser={"browser": "chrome", "platform": "windows"})
    assert "Chrome/" in ua.headers["User-Agent"]
    # the hint version must match the UA version
    import re

    match = re.search(r"Chrome/(\d+)", ua.headers["User-Agent"])
    assert match is not None
    assert f'v="{match.group(1)}"' in ua.headers["sec-ch-ua"]


def test_firefox_ua_has_no_client_hints():
    ua = UserAgent(browser={"browser": "firefox", "platform": "windows", "mobile": False})
    assert "Firefox/" in ua.headers["User-Agent"]
    assert "sec-ch-ua" not in ua.headers


def test_custom_user_agent_is_respected():
    custom = "MyCustomBot/1.0"
    ua = UserAgent(browser={"custom": custom})
    assert ua.headers["User-Agent"] == custom


def test_invalid_browser_raises():
    import pytest

    with pytest.raises(RuntimeError):
        UserAgent(browser={"browser": "netscape"})


def test_allow_brotli_toggle():
    without = UserAgent(allow_brotli=False, browser={"browser": "chrome"})
    assert "br" not in without.headers.get("Accept-Encoding", "")
    with_br = UserAgent(allow_brotli=True, browser={"browser": "chrome"})
    assert "br" in with_br.headers.get("Accept-Encoding", "")


def test_brotli_stripped_when_unavailable(monkeypatch):
    # The `brotli` extra may not be installed; don't advertise an encoding we
    # cannot decode even when allow_brotli is True.
    from scraper._engine import user_agent as ua_mod

    monkeypatch.setattr(ua_mod, "_brotli_available", lambda: False)
    ua = UserAgent(allow_brotli=True, browser={"browser": "chrome"})
    assert "br" not in ua.headers.get("Accept-Encoding", "")
