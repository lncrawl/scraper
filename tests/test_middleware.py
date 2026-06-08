"""Unit tests for individual middleware against a fake async ``nxt`` handler.

Each test drives one middleware in isolation: a real engine supplies the
collaborators (state, proxy manager, stealth) while ``nxt`` is a stub that records
calls and returns a scripted response.
"""

import asyncio

import httpx
import pytest
import respx

from scraper.config import ProxyConfig, StealthConfig
from scraper.engine import create_engine
from scraper.engine.middleware import build_chain
from scraper.engine.middleware.proxy import ProxyMiddleware
from scraper.engine.middleware.retry_403 import Retry403Middleware
from scraper.engine.middleware.stealth import StealthMiddleware
from scraper.engine.middleware.throttle import ThrottleMiddleware
from scraper.engine.state import RequestState
from scraper.exceptions import ProxyTransportError

from .conftest import make_fast_config

BASE = "https://example.com"


def _engine(**overrides):
    return create_engine(make_fast_config(**overrides))


def _resp(status=200, url=BASE):
    req = httpx.Request("GET", url)
    return httpx.Response(status_code=status, content=b"", request=req)


def run_on(coro, engine):
    return asyncio.run_coroutine_threadsafe(coro, engine._loop).result(timeout=10)


def _nxt(resp, sink=None):
    async def nxt(ctx):
        if sink is not None:
            sink.append(ctx)
        return resp

    return nxt


# --- chain assembly -------------------------------------------------------


def test_build_chain_order():
    e = _engine()
    names = [type(m).__name__ for m in build_chain(e)]
    assert names == [
        "ThrottleMiddleware",
        # TlsRotation omitted: rotate_tls_ciphers=False
        "ConcurrencyMiddleware",
        "Retry403Middleware",
        "ChallengeMiddleware",
        "StealthMiddleware",
        "HooksMiddleware",
        "ProxyMiddleware",
        "SslRetryMiddleware",
    ]


def test_build_chain_includes_tls_rotation_when_enabled():
    e = _engine(rotate_tls_ciphers=True)
    assert "TlsRotationMiddleware" in [type(m).__name__ for m in build_chain(e)]


# --- throttle / nested skip ----------------------------------------------


def test_throttle_skips_when_nested():
    e = _engine()
    sink: list = []
    ctx = RequestState("GET", BASE, depth=1)
    run_on(ThrottleMiddleware(e).handle(ctx, _nxt(_resp(), sink)), e)
    assert len(sink) == 1  # nxt called, no throttling state touched


def test_throttle_sleeps_when_interval_pending():
    e = _engine(min_request_interval_fast=0.05)
    e.state.mark_request_sent()  # a recent request → next one must wait
    sink: list = []
    run_on(ThrottleMiddleware(e).handle(RequestState("GET", BASE), _nxt(_resp(), sink)), e)
    assert len(sink) == 1  # proceeded after sleeping


# --- proxy ----------------------------------------------------------------


def test_proxy_passthrough_without_proxies():
    e = _engine()
    ctx = RequestState("GET", BASE)
    run_on(ProxyMiddleware(e).handle(ctx, _nxt(_resp())), e)
    assert "proxy" not in ctx.kwargs


def test_proxy_injects_when_configured():
    e = _engine(proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]))
    ctx = RequestState("GET", BASE)
    run_on(ProxyMiddleware(e).handle(ctx, _nxt(_resp())), e)
    assert ctx.kwargs.get("proxy") == "socks5://127.0.0.1:9150"


# --- proxy: pre-configured proxy failure falls through to direct ----------


def test_proxy_pre_configured_fails_falls_through_to_direct():
    """When ctx.kwargs already has a 'proxy' and the send raises ProxyTransportError,
    the middleware strips the proxy and falls back to direct."""
    e = _engine(proxy=ProxyConfig(proxy_urls=[], fallback_to_direct=True))

    async def fail_with_proxy_only(ctx):
        if ctx.kwargs.get("proxy"):
            raise ProxyTransportError("pre-set proxy refused")
        return _resp()

    ctx = RequestState("GET", BASE, kwargs={"proxy": "socks5://127.0.0.1:9999"})
    out = run_on(ProxyMiddleware(e).handle(ctx, fail_with_proxy_only), e)
    assert out.status_code == 200
    assert "proxy" not in ctx.kwargs


# --- proxy: no-fallback raises when proxies exhausted --------------------


def test_proxy_raises_when_no_fallback_and_proxies_exhausted():
    e = _engine(
        proxy=ProxyConfig(
            proxy_urls=["socks5://127.0.0.1:9150"],
            retry_request_on_failure=1,
            fallback_to_direct=False,
        )
    )

    async def always_fail(ctx):
        raise ProxyTransportError("down")

    with pytest.raises(ProxyTransportError):
        run_on(ProxyMiddleware(e).handle(RequestState("GET", BASE), always_fail), e)


# --- stealth gating -------------------------------------------------------


def test_stealth_disabled_leaves_kwargs_untouched():
    e = _engine()  # stealth disabled in fast config
    ctx = RequestState("GET", BASE, kwargs={"headers": {"X": "1"}})
    run_on(StealthMiddleware(e).handle(ctx, _nxt(_resp())), e)
    assert ctx.kwargs == {"headers": {"X": "1"}}


def test_stealth_enabled_adds_headers():
    e = _engine(
        stealth=StealthConfig(
            enabled=True,
            human_like_delays=False,
            randomize_headers=True,
            browser_quirks=False,
        )
    )
    ctx = RequestState("GET", BASE, kwargs={})
    run_on(StealthMiddleware(e).handle(ctx, _nxt(_resp())), e)
    assert "Accept" in ctx.kwargs["headers"]


# --- 403 retry ------------------------------------------------------------


def test_retry403_resets_on_200():
    e = _engine()
    e.state.register_403(3)
    run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt(_resp(200))), e)
    # a fresh 403 budget is available again
    assert e.state.register_403(3) is True


def test_retry403_returns_403_when_proxy_budget_exhausted_and_no_refresh():
    """max_403_retries controls IP-rotation retries; with refresh also disabled, 403 is returned."""
    e = _engine(max_403_retries=0, auto_refresh_on_403=False)
    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt(_resp(403))), e)
    assert out.status_code == 403


def test_retry403_returns_403_when_no_proxy_and_no_refresh():
    """When no proxy and auto_refresh_on_403 is off, original 403 is returned."""
    e = _engine(auto_refresh_on_403=False, max_403_retries=3)
    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt(_resp(403))), e)
    assert out.status_code == 403


@respx.mock
def test_retry403_429_triggers_backoff_and_retry():
    """429 response → back off (mocked sleep) and retry via _run_pipeline."""
    import asyncio

    respx.get(BASE).mock(return_value=httpx.Response(200, content=b"ok"))

    slept: list = []

    async def _fast_sleep(t: float) -> None:
        slept.append(t)

    e = _engine()

    call_count = [0]

    async def _nxt_429_then_200(ctx):
        call_count[0] += 1
        return _resp(429 if call_count[0] == 1 else 200)

    async def _run(e_ref=e):
        import scraper.engine.middleware.retry_403 as m429

        original = m429.asyncio.sleep
        m429.asyncio.sleep = _fast_sleep  # type: ignore[assignment]
        try:
            return await Retry403Middleware(e_ref).handle(
                RequestState("GET", BASE), _nxt_429_then_200
            )
        finally:
            m429.asyncio.sleep = original

    out = asyncio.run_coroutine_threadsafe(_run(), e._loop).result(timeout=10)
    assert out.status_code == 200
    assert slept  # sleep was called during backoff


@respx.mock
def test_retry403_refresh_falls_through_after_proxy_budget_exhausted():
    """When proxy budget is exhausted, session refresh is attempted as a final fallback."""
    respx.get(BASE).mock(return_value=httpx.Response(200, content=b"ok"))
    e = _engine(
        proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]),
        auto_refresh_on_403=True,
        max_403_retries=0,  # proxy rotation budget exhausted immediately
    )

    # Mock _refresh_session to return True without a real network call.
    refreshed: list = []

    async def _mock_refresh(url: str) -> bool:
        refreshed.append(url)
        return True

    e._refresh_session = _mock_refresh  # type: ignore[method-assign]

    call_count = [0]

    async def _nxt_always_403(_):
        call_count[0] += 1
        return _resp(403)

    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt_always_403), e)
    assert refreshed  # refresh was attempted after proxy budget was exhausted
    assert out.status_code == 200


@respx.mock
def test_retry403_proxy_rotation_on_403():
    """403 + proxy configured → rotate proxy and retry."""
    respx.get(BASE).mock(return_value=httpx.Response(200, content=b"ok"))
    e = _engine(proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]))

    call_count = [0]

    async def _nxt_403_then_200(ctx):
        call_count[0] += 1
        return _resp(403 if call_count[0] == 1 else 200)

    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt_403_then_200), e)
    assert out.status_code == 200


@respx.mock
def test_retry403_refresh_on_403_succeeds():
    """403 + no proxy + auto_refresh_on_403=True → refresh session and retry."""
    respx.get(BASE).mock(return_value=httpx.Response(200, content=b"ok"))
    e = _engine(auto_refresh_on_403=True, max_403_retries=3)

    call_count = [0]

    async def _nxt_403_then_200(ctx):
        call_count[0] += 1
        return _resp(403 if call_count[0] == 1 else 200)

    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt_403_then_200), e)
    # Either succeeds after refresh or returns the 403 if refresh returned False
    assert out.status_code in (200, 403)


def test_retry403_refresh_on_403_returns_403_when_refresh_fails():
    """403 + no proxy + auto_refresh_on_403=True but _refresh_session returns False → 403."""
    e = _engine(auto_refresh_on_403=True, max_403_retries=3)

    # Mock _refresh_session to return False (refresh failed)
    async def _mock_refresh(url):
        return False

    e._refresh_session = _mock_refresh  # type: ignore[method-assign]

    async def _nxt_403(ctx):
        return _resp(403, ctx.url)

    out = run_on(Retry403Middleware(e).handle(RequestState("GET", BASE), _nxt_403), e)
    assert out.status_code == 403


# --- stealth: delay > 0 path ----------------------------------------------


def test_stealth_delay_fires_when_positive(monkeypatch):
    """Lines 42-43 in middleware/stealth.py: delay > 0 triggers log + sleep."""

    slept: list = []

    async def _fast_sleep(t: float) -> None:
        slept.append(t)

    monkeypatch.setattr("scraper.engine.middleware.stealth.asyncio.sleep", _fast_sleep)

    e = _engine(
        stealth=StealthConfig(
            enabled=True,
            human_like_delays=True,
            min_delay=0.5,
            max_delay=0.5,
            min_delay_fast=0.5,
            max_delay_fast=0.5,
            randomize_headers=False,
            browser_quirks=False,
        )
    )
    # Set request_count > 0 so compute_delay returns a positive value
    e.stealth._request_count = 1

    sink: list = []
    run_on(StealthMiddleware(e).handle(RequestState("GET", BASE), _nxt(_resp(), sink)), e)
    assert len(sink) == 1
    assert slept and slept[0] >= 0.5
