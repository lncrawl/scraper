"""Tests for the stealth header/delay layer."""

from scraper import StealthConfig
from scraper._engine.stealth import StealthMode

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)
FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0"


def no_delay_config(**overrides) -> StealthConfig:
    base = dict(min_delay=0.0, max_delay=0.0, min_delay_fast=0.0, max_delay_fast=0.0)
    base.update(overrides)
    return StealthConfig(**base)  # type: ignore[arg-type]


def test_randomize_headers_fills_defaults():
    sm = StealthMode(no_delay_config(randomize_headers=True, browser_quirks=False))
    out = sm.apply("GET", "https://x", headers={})
    headers = out["headers"]
    assert "Accept" in headers
    assert "Accept-Language" in headers


def test_randomize_headers_respects_existing():
    sm = StealthMode(no_delay_config(randomize_headers=True, browser_quirks=False))
    out = sm.apply("GET", "https://x", headers={"Accept": "custom/type"})
    assert out["headers"]["Accept"] == "custom/type"


def test_chrome_quirks_add_sec_fetch_and_order():
    sm = StealthMode(no_delay_config(randomize_headers=False, browser_quirks=True))
    out = sm.apply("GET", "https://x", headers={"User-Agent": CHROME_UA, "Accept": "*/*"})
    headers = out["headers"]
    assert headers["Sec-Fetch-Dest"] == "document"
    assert headers["Sec-Fetch-Mode"] == "navigate"
    order = list(headers.keys())
    # User-Agent must come before Accept in the Chrome ordering
    assert order.index("User-Agent") < order.index("Accept")


def test_firefox_quirks_add_upgrade_insecure():
    sm = StealthMode(no_delay_config(randomize_headers=False, browser_quirks=True))
    out = sm.apply("GET", "https://x", headers={"User-Agent": FIREFOX_UA})
    headers = out["headers"]
    assert headers["Upgrade-Insecure-Requests"] == "1"
    assert "Sec-Fetch-Dest" not in headers  # firefox quirk set has no sec-fetch


def test_delays_do_not_sleep_when_zero():
    sm = StealthMode(no_delay_config(human_like_delays=True))
    # First call: request_count == 0, returns immediately.
    sm.apply("GET", "https://x", headers={}, cf_active=True)
    # Second call exercises the cf_active delay branch (zero magnitude → no sleep).
    sm.apply("GET", "https://x", headers={}, cf_active=True)
    sm.apply("GET", "https://x", headers={}, cf_active=False)


def test_disabled_stealth_passes_headers_through():
    # StealthMode itself always processes; the engine gates it on enabled, but
    # with both toggles off apply() should be a near no-op aside from counting.
    sm = StealthMode(no_delay_config(randomize_headers=False, browser_quirks=False))
    out = sm.apply("GET", "https://x", headers={"A": "b"})
    assert out["headers"] == {"A": "b"}
