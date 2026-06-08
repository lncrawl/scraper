"""Tests for engine request pipeline: abort, SSL retry, proxy, 403, hooks, stealth,
cipher rotation, and per-middleware unit tests."""

import asyncio

import httpx
import pytest
import respx

from scraper import AbortedException, Scraper, StealthConfig
from scraper.config import ProxyConfig
from scraper.engine import create_engine
from scraper.engine.state import RequestState
from scraper.exceptions import ProxyTransportError, SSLTransportError

from .conftest import make_fast_config

BASE = "https://example.com"


def _resp(status=200, url=BASE, body=b""):
    req = httpx.Request("GET", url)
    return httpx.Response(status_code=status, content=body, request=req)


def run_on(coro, engine):
    """Run a coroutine on the engine's event loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, engine._loop).result(timeout=10)


# --- abort ----------------------------------------------------------------


def test_abort_signal_blocks_request():
    s = Scraper(origin=BASE, config=make_fast_config())
    s.engine.abort()
    with pytest.raises(AbortedException):
        s.get(f"{BASE}/x")


# --- SSL retry ------------------------------------------------------------


def test_ssl_error_retried_without_verification():
    calls = [0]

    async def fake_send(ctx):
        calls[0] += 1
        if calls[0] == 1:
            raise SSLTransportError("cert verify failed")
        return _resp(200, ctx.url)

    e = create_engine(make_fast_config())
    e.transport.send = fake_send  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 200
    assert calls[0] == 2


def test_ssl_error_in_cdn_cgi_not_retried():
    async def fake_send(ctx):
        raise SSLTransportError("fail")

    e = create_engine(make_fast_config())
    e.transport.send = fake_send  # type: ignore[method-assign]
    with pytest.raises(SSLTransportError):
        e.request("GET", f"{BASE}/cdn-cgi/challenge")


def test_verify_ssl_false_sets_verify_kwarg():
    seen = []

    async def fake_send(ctx):
        seen.append(ctx.kwargs.get("verify"))
        return _resp(200, ctx.url)

    e = create_engine(make_fast_config(verify_ssl=False))
    e.transport.send = fake_send  # type: ignore[method-assign]
    e.request("GET", f"{BASE}/x")
    assert seen[0] is False


# --- proxy fallback -------------------------------------------------------


def test_proxy_error_uses_rotated_proxy_on_retry():
    seen_proxies = []

    async def fake_send(ctx):
        proxy = ctx.kwargs.get("proxy")
        seen_proxies.append(proxy)
        if proxy == "socks5://127.0.0.1:9150":
            raise ProxyTransportError("down")
        return _resp(200, ctx.url)

    cfg = make_fast_config(
        proxy=ProxyConfig(
            proxy_urls=["socks5://127.0.0.1:9150", "socks5://127.0.0.1:9151"],
        )
    )
    e = create_engine(cfg)
    e.transport.send = fake_send  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 200
    assert seen_proxies[0] == "socks5://127.0.0.1:9150"
    assert seen_proxies[1] == "socks5://127.0.0.1:9151"


def test_proxy_error_rotates_then_falls_back_to_direct():
    async def fake_send(ctx):
        if ctx.kwargs.get("proxy"):
            raise ProxyTransportError("down")
        return _resp(200, ctx.url)

    cfg = make_fast_config(
        proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"], fallback_to_direct=True)
    )
    e = create_engine(cfg)
    e.transport.send = fake_send  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


# --- 403 handling ---------------------------------------------------------


def test_403_retry_exhausted_returns_403():
    async def fake_send(ctx):
        return _resp(403, ctx.url)

    e = create_engine(make_fast_config(max_403_retries=0))
    e.transport.send = fake_send  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 403


def test_403_triggers_refresh_when_configured():
    calls = [0]

    async def fake_send(ctx):
        calls[0] += 1
        return _resp(403 if calls[0] == 1 else 200, ctx.url)

    cfg = make_fast_config(auto_refresh_on_403=True, max_403_retries=3)
    e = create_engine(cfg)
    e.transport.send = fake_send  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


def test_403_with_proxy_rotates_and_retries():
    calls = [0]

    async def fake_send(ctx):
        calls[0] += 1
        if calls[0] == 1 and ctx.kwargs.get("proxy"):
            return _resp(403, ctx.url)
        return _resp(200, ctx.url)

    cfg = make_fast_config(
        proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]), max_403_retries=3
    )
    e = create_engine(cfg)
    e.transport.send = fake_send  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


# --- pre / post hooks -----------------------------------------------------


@respx.mock
def test_pre_and_post_hooks_run():
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    seen = {}

    def pre(engine, method, url, *args, **kwargs):
        seen["pre"] = True
        return method, url, args, kwargs

    def post(engine, resp):
        seen["post"] = resp.status_code
        return resp

    cfg = make_fast_config(pre_hook=pre, post_hook=post)
    Scraper(origin=BASE, config=cfg).get(f"{BASE}/x")
    assert seen == {"pre": True, "post": 200}


# --- stealth / cipher rotation --------------------------------------------


@respx.mock
def test_stealth_enabled_applies_headers():
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    cfg = make_fast_config(
        stealth=StealthConfig(
            enabled=True,
            human_like_delays=False,
            min_delay=0.0,
            max_delay=0.0,
            min_delay_fast=0.0,
            max_delay_fast=0.0,
            randomize_headers=True,
            browser_quirks=False,
        )
    )
    Scraper(origin=BASE, config=cfg).get(f"{BASE}/x")
    assert "accept" in dict(route.calls[0].request.headers)


@respx.mock
def test_tls_cipher_rotation_runs_without_error():
    respx.get(f"{BASE}/a").mock(return_value=httpx.Response(200))
    respx.get(f"{BASE}/b").mock(return_value=httpx.Response(200))
    s = Scraper(origin=BASE, config=make_fast_config(rotate_tls_ciphers=True))
    s.get(f"{BASE}/a")
    s.get(f"{BASE}/b")


# --- middleware unit: tls_rotation skipped for nested requests ------------


def test_tls_rotation_skipped_for_nested():
    """rotate_ciphers() must not be called when ctx.nested is True."""
    from scraper.engine.middleware.tls_rotation import TlsRotationMiddleware

    e = create_engine(make_fast_config(rotate_tls_ciphers=True))
    mw = TlsRotationMiddleware(e)

    rotate_calls = []
    e.transport.rotate_ciphers = lambda: rotate_calls.append(1)  # type: ignore[method-assign]

    ctx = RequestState("GET", f"{BASE}/x", depth=1)

    async def nxt(c):
        return _resp(200, BASE)

    run_on(mw.handle(ctx, nxt), e)
    assert not rotate_calls


# --- middleware unit: concurrency slot acquired/released ------------------


def test_concurrency_slot_acquired_for_top_level():
    from scraper.engine.middleware.concurrency import ConcurrencyMiddleware

    e = create_engine(make_fast_config(max_concurrent_requests=1))
    mw = ConcurrencyMiddleware(e)
    ctx = RequestState("GET", f"{BASE}/x")

    async def nxt(c):
        return _resp(200, BASE)

    resp = run_on(mw.handle(ctx, nxt), e)
    assert resp.status_code == 200


def test_concurrency_slot_skipped_for_nested():
    from scraper.engine.middleware.concurrency import ConcurrencyMiddleware

    e = create_engine(make_fast_config(max_concurrent_requests=1))
    mw = ConcurrencyMiddleware(e)
    ctx = RequestState("GET", f"{BASE}/x", depth=1)  # nested

    called = []

    async def nxt(c):
        called.append(True)
        return _resp(200, BASE)

    run_on(mw.handle(ctx, nxt), e)
    assert called  # nxt was called despite slot being held
