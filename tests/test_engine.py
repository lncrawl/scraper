"""Tests for engine configuration and session management."""

import httpx
import pytest
import respx

from scraper.config import CloudflareConfig, ImpersonateConfig
from scraper.engine import Engine, create_engine
from scraper.engine.user_agent.helper import infer_browser

from .conftest import make_fast_config

BASE = "https://example.com"


def _resp(status=200, url=BASE, body=b""):
    req = httpx.Request("GET", url)
    return httpx.Response(status_code=status, content=body, request=req)


# --- challenge detection configuration ------------------------------------


def test_default_has_detector_no_solver():
    e = create_engine(make_fast_config())
    assert e.cf_detector is not None


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
    e = create_engine(make_fast_config())

    async def boom(ctx):
        raise ConnectionError("offline")

    e.transport.send = boom  # type: ignore[method-assign]
    result = asyncio_run_on_engine(e._refresh_session(f"{BASE}/page"), e)
    assert result is False


def asyncio_run_on_engine(coro, engine):
    import asyncio

    return asyncio.run_coroutine_threadsafe(coro, engine._loop).result(timeout=5)


@respx.mock
def test_refresh_session_clears_cf_cookies_and_succeeds():
    respx.get(BASE).mock(return_value=httpx.Response(200))
    e = create_engine(make_fast_config())
    e.put_cookie("cf_clearance", "tok", domain="example.com")

    result = asyncio_run_on_engine(e._refresh_session(f"{BASE}/page"), e)
    assert result is True
    assert e.cookies.get("cf_clearance") is None


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
    e.put_cookie("tok", "val", domain="example.com")
    e.headers["X-Custom"] = "yes"
    e.reset()
    assert list(e.cookies) == []
    assert "X-Custom" not in e.headers


def test_engine_close_does_not_raise():
    e = create_engine(make_fast_config())
    e.close()


# --- cookies / raw send ---------------------------------------------------


@respx.mock
def test_put_cookie_visible_in_request():
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, content=b"ok"))
    from scraper import Scraper

    s = Scraper(origin=BASE, config=make_fast_config())
    s.put_cookie("token", "abc", domain="example.com")
    s.get(f"{BASE}/x")
    cookie_header = str(route.calls[0].request.headers.get("cookie", ""))
    assert "token=abc" in cookie_header


def test_perform_request_bypasses_pipeline():
    calls = []

    async def fake_send(ctx):
        calls.append(ctx.url)
        return _resp(200, ctx.url)

    e: Engine = create_engine(make_fast_config())
    e.transport.send = fake_send  # type: ignore[method-assign]
    resp = e.perform_request("GET", f"{BASE}/raw")
    assert resp.status_code == 200
    assert calls == [f"{BASE}/raw"]


def test_perform_request_injects_proxy_when_configured():
    """perform_request injects the active proxy into ctx.kwargs when no proxy is pre-set."""
    from scraper.config import ProxyConfig

    injected: list = []

    async def fake_send(ctx):
        injected.append(ctx.kwargs.get("proxy"))
        return _resp(200, ctx.url)

    e = create_engine(make_fast_config(proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"])))
    e.transport.send = fake_send  # type: ignore[method-assign]
    e.perform_request("GET", f"{BASE}/raw")
    assert injected[0] == "socks5://127.0.0.1:9150"


def test_perform_request_skips_proxy_injection_when_proxy_already_set():
    """perform_request does not overwrite a proxy already in kwargs."""
    from scraper.config import ProxyConfig

    injected: list = []

    async def fake_send(ctx):
        injected.append(ctx.kwargs.get("proxy"))
        return _resp(200, ctx.url)

    e = create_engine(make_fast_config(proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"])))
    e.transport.send = fake_send  # type: ignore[method-assign]
    e.perform_request("GET", f"{BASE}/raw", proxy="socks5://10.0.0.1:1080")
    assert injected[0] == "socks5://10.0.0.1:1080"


# --- close exception swallowing -------------------------------------------


def test_close_swallows_transport_aclose_exception():
    """close() completes without raising even when transport.aclose() fails."""
    e = create_engine(make_fast_config())

    async def _boom() -> None:
        raise RuntimeError("transport crashed")

    e.transport.aclose = _boom  # type: ignore[method-assign]
    e.close()  # must not propagate the RuntimeError


# --- cancel token integration with engine.request -------------------------


@respx.mock
def test_engine_request_with_cancel_token_succeeds():
    """Passing a CancelToken binds it to the future (core.py:144)."""
    from scraper.utils.cancel_token import CancelToken

    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200, content=b"ok"))
    e = create_engine(make_fast_config())
    token = CancelToken()
    resp = e.request("GET", f"{BASE}/x", cancel_token=token)
    assert resp.status_code == 200


def test_engine_request_cancelled_token_raises_aborted():
    """Pre-cancelled token + slow transport → AbortedException (core.py:149)."""
    import asyncio

    from scraper.exceptions import AbortedException
    from scraper.utils.cancel_token import CancelToken

    e = create_engine(make_fast_config())

    async def _hang(ctx):  # type: ignore[misc]
        await asyncio.sleep(30)
        return _resp(200)

    e.transport.send = _hang  # type: ignore[method-assign]

    token = CancelToken()
    token.cancel()  # pre-cancel before the request starts

    with pytest.raises(AbortedException):
        e.request("GET", f"{BASE}/slow", cancel_token=token)


# --- abort_on signal -------------------------------------------------------


def test_abort_on_triggers_abort_when_signal_set():
    import threading
    import time

    e = create_engine(make_fast_config())
    event = threading.Event()
    e.abort_on(event)
    assert not e._aborted
    event.set()
    deadline = time.monotonic() + 2.0
    while not e._aborted and time.monotonic() < deadline:
        time.sleep(0.01)
    assert e._aborted
