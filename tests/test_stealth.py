"""Tests for the stealth header/delay layer."""

from scraper import StealthConfig
from scraper.engine.stealth import StealthMode

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


def test_chrome_quirks_preserves_non_order_headers():
    """Headers not in the quirks order list are still included in the output."""
    sm = StealthMode(no_delay_config(randomize_headers=False, browser_quirks=True))
    out = sm.apply(
        "GET",
        "https://x",
        headers={"User-Agent": CHROME_UA, "X-Custom-Header": "keep-me"},
    )
    assert out["headers"]["X-Custom-Header"] == "keep-me"


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


def test_delay_computed_when_large_enough():
    cfg = StealthConfig(
        min_delay=0.5,
        max_delay=0.5,
        min_delay_fast=0.5,
        max_delay_fast=0.5,
        human_like_delays=True,
        randomize_headers=False,
        browser_quirks=False,
    )
    sm = StealthMode(cfg)
    d1 = sm.compute_delay(False)  # count=0 → returns 0.0
    sm.apply("GET", "https://x", headers={})  # increments request count
    d2 = sm.compute_delay(False)  # count=1 → returns delay ≥ 0.5
    assert d1 == 0.0
    assert d2 >= 0.5


def test_extreme_delay_scaled_and_capped(monkeypatch):
    # Force the 10 % jitter branch to always fire
    monkeypatch.setattr("random.uniform", lambda a, b: b)
    monkeypatch.setattr("random.random", lambda: 0.0)  # < 0.1 → jitter taken

    cfg = StealthConfig(
        min_delay=5.0,
        max_delay=5.0,
        min_delay_fast=0.0,
        max_delay_fast=0.0,
        human_like_delays=True,
        randomize_headers=False,
        browser_quirks=False,
    )
    sm = StealthMode(cfg)
    d1 = sm.compute_delay(True)  # count=0 → returns 0.0
    sm.apply("GET", "https://x", headers={})  # increment request count
    # cf_active=True + random.random()=0.0 < 0.1 → jitter: delay = min(5.0*1.5, 10.0)
    d2 = sm.compute_delay(True)
    assert d1 == 0.0
    assert d2 <= 10.0


def test_accept_language_already_present_not_overwritten():
    # Exercises the 143->145 branch: Accept-Language is already set so line 143
    # condition is False and we jump to line 145 (the DNT randomization).
    sm = StealthMode(no_delay_config(randomize_headers=True, browser_quirks=False))
    out = sm.apply("GET", "https://x", headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert out["headers"]["Accept-Language"] == "de-DE,de;q=0.9"


def test_disabled_stealth_passes_headers_through():
    # StealthMode itself always processes; the engine gates it on enabled, but
    # with both toggles off apply() should be a near no-op aside from counting.
    sm = StealthMode(no_delay_config(randomize_headers=False, browser_quirks=False))
    out = sm.apply("GET", "https://x", headers={"A": "b"})
    assert out["headers"] == {"A": "b"}


def test_delay_cf_active_no_jitter_branch(monkeypatch):
    """Branch 101->105: cf_active=True + random.random() >= 0.1 → no jitter applied."""
    monkeypatch.setattr("random.uniform", lambda a, b: b)  # always returns max_delay
    monkeypatch.setattr("random.random", lambda: 0.5)  # >= 0.1 → jitter NOT taken

    cfg = StealthConfig(
        min_delay=0.5,
        max_delay=0.5,
        min_delay_fast=0.0,
        max_delay_fast=0.0,
        human_like_delays=True,
        randomize_headers=False,
        browser_quirks=False,
    )
    sm = StealthMode(cfg)
    sm._request_count = 1  # skip the count==0 early return
    d = sm.compute_delay(True)  # cf_active=True, random.random()=0.5 ≥ 0.1
    assert d == 0.5  # no jitter, plain delay returned
