"""Tests for engine configuration and session management."""

import pytest
import responses

from scraper.config import CloudflareConfig, ImpersonateConfig
from scraper.engine import Engine, create_engine
from scraper.engine.user_agent.helper import infer_browser

from .conftest import make_fast_config

BASE = "https://example.com"


# --- challenge detection configuration ------------------------------------


def test_default_has_detector_no_solver():
    e = create_engine(make_fast_config())
    assert e.cf_detector is not None
    assert e.cf_solver is None


def test_detection_disabled_omits_middleware():
    cfg = make_fast_config(cloudflare=CloudflareConfig(enabled=False))
    e = create_engine(cfg)
    assert e.cf_detector is None
    assert "ChallengeMiddleware" not in [type(m).__name__ for m in e.middleware]


# --- UA / impersonate family alignment ------------------------------------


def test_impersonate_aligns_ua_family():
    pytest.importorskip("curl_cffi")
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome124"))
    e = create_engine(cfg)
    assert infer_browser(e.headers["User-Agent"]) == "chrome"


def test_impersonate_custom_ua_not_overridden():
    pytest.importorskip("curl_cffi")
    from scraper import BrowserConfig

    cfg = make_fast_config(
        impersonate=ImpersonateConfig(target="chrome"),
        browser=BrowserConfig(custom="MyBot/1.0"),
    )
    e = create_engine(cfg)
    assert infer_browser(e.headers["User-Agent"]) == "chrome"


# --- refresh_session / apply_clearance / reset / close -------------------


def test_refresh_session_returns_false_on_transport_exception():
    import requests

    e = create_engine(make_fast_config())

    def boom(ctx):
        raise requests.exceptions.ConnectionError("offline")

    e.transport.send = boom  # type: ignore[method-assign]
    assert e.refresh_session(f"{BASE}/page") is False


def test_refresh_session_clears_cf_cookies_and_succeeds():
    e = create_engine(make_fast_config())
    e.cookies.set("cf_clearance", "tok", domain="example.com")

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}", status=200)
        result = e.refresh_session(f"{BASE}/page")

    assert result is True
    assert e.cookies.get("cf_clearance", domain="example.com") is None


def test_refresh_session_suppresses_keyerror_on_missing_cookie():
    e = create_engine(make_fast_config())
    e.cookies.set("unrelated", "val", domain="example.com")

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}", status=200)
        result = e.refresh_session(f"{BASE}/page")

    assert result is True


def test_apply_browser_clearance_sets_user_agent():
    e = create_engine(make_fast_config())
    e.apply_browser_clearance("example.com", user_agent="TestBot/1.0")
    assert e.headers["User-Agent"] == "TestBot/1.0"


def test_apply_browser_clearance_with_cf_clearance_and_cookies():
    e = create_engine(make_fast_config())
    e.apply_browser_clearance(
        "example.com",
        cf_clearance="tok123",
        cookies={"__cf_bm": "bm_val"},
    )
    assert e.cookies.get("cf_clearance", domain="example.com") == "tok123"
    assert e.cookies.get("__cf_bm", domain="example.com") == "bm_val"


def test_apply_browser_clearance_no_user_agent_leaves_header_unchanged():
    e = create_engine(make_fast_config())
    original_ua = e.headers.get("User-Agent")
    e.apply_browser_clearance("example.com")
    assert e.headers.get("User-Agent") == original_ua


def test_engine_reset_clears_cookies_and_headers():
    e = create_engine(make_fast_config())
    e.cookies.set("tok", "val", domain="example.com")
    e.headers["X-Custom"] = "yes"
    e.reset()
    assert list(e.cookies) == []
    assert "X-Custom" not in e.headers


def test_engine_close_does_not_raise():
    e = create_engine(make_fast_config())
    e.close()


# --- cookies / raw send ---------------------------------------------------


def test_put_cookie_visible_in_request():
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        from scraper import Scraper

        s = Scraper(origin=BASE, config=make_fast_config())
        s.put_cookie("token", "abc", domain="example.com")
        s.get(f"{BASE}/x")
        assert "token=abc" in str(rsps.calls[0].request.headers.get("Cookie", ""))


def test_perform_request_bypasses_pipeline():
    import requests

    calls = []

    def fake_send(ctx):
        r = requests.Response()
        r.status_code = 200
        r._content = b""
        r.url = ctx.url
        calls.append(ctx.url)
        return r

    e: Engine = create_engine(make_fast_config())
    e.transport.send = fake_send  # type: ignore[method-assign]
    resp = e.perform_request("GET", f"{BASE}/raw")
    assert resp.status_code == 200
    assert calls == [f"{BASE}/raw"]
