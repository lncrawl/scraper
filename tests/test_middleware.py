"""Unit tests for individual middleware against a fake `nxt` handler.

Each test drives one middleware in isolation: a real engine supplies the
collaborators (state, proxy manager, stealth) while ``nxt`` is a stub that records
calls and returns a scripted response.
"""

import pytest
import requests

from scraper import AbortedException
from scraper.config import ProxyConfig, StealthConfig
from scraper.engine import create_engine
from scraper.engine.challenges import ChallengeHandler
from scraper.engine.context import RequestContext
from scraper.engine.middleware import build_chain
from scraper.engine.middleware.abort import AbortMiddleware
from scraper.engine.middleware.challenge import ChallengeMiddleware
from scraper.engine.middleware.proxy import ProxyMiddleware
from scraper.engine.middleware.retry_403 import Retry403Middleware
from scraper.engine.middleware.stealth import StealthMiddleware
from scraper.engine.middleware.throttle import ThrottleMiddleware
from scraper.exceptions import CloudflareLoopProtection

from .conftest import make_fast_config

BASE = "https://example.com"


def _engine(**overrides):
    return create_engine(make_fast_config(**overrides))


def _resp(status=200, url=BASE):
    r = requests.Response()
    r.status_code = status
    r._content = b""
    r.url = url
    return r


def _nxt(resp, sink=None):
    def nxt(ctx):
        if sink is not None:
            sink.append(ctx)
        return resp

    return nxt


# --- chain assembly -------------------------------------------------------


def test_build_chain_order():
    e = _engine()
    names = [type(m).__name__ for m in build_chain(e)]
    assert names == [
        "AbortMiddleware",
        "ThrottleMiddleware",
        "SessionRefreshMiddleware",  # TlsRotation omitted: rotate_tls_ciphers False
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


# --- abort ----------------------------------------------------------------


def test_abort_middleware_raises_when_set():
    e = _engine()
    e.abort()
    with pytest.raises(AbortedException):
        AbortMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp()))


def test_abort_middleware_passes_through():
    e = _engine()
    sink = []
    out = AbortMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(), sink))
    assert out.status_code == 200 and len(sink) == 1


# --- throttle / nested skip ----------------------------------------------


def test_throttle_skips_when_nested():
    e = _engine()
    sink = []
    ctx = RequestContext("GET", BASE, nested=True)
    ThrottleMiddleware(e).handle(ctx, _nxt(_resp(), sink))
    assert len(sink) == 1  # nxt called, no throttling state touched


def test_throttle_sleeps_when_interval_pending():
    e = _engine(min_request_interval_fast=0.05)
    e.state.mark_request_sent()  # a recent request → next one must wait
    sink = []
    ThrottleMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(), sink))
    assert len(sink) == 1  # proceeded after sleeping


def test_throttle_aborts_when_signalled():
    e = _engine()
    e.abort()
    with pytest.raises(AbortedException):
        ThrottleMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp()))


# --- proxy ----------------------------------------------------------------


def test_proxy_passthrough_without_proxies():
    e = _engine()
    ctx = RequestContext("GET", BASE)
    ProxyMiddleware(e).handle(ctx, _nxt(_resp()))
    assert "proxies" not in ctx.kwargs


def test_proxy_injects_when_configured():
    e = _engine(proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]))
    ctx = RequestContext("GET", BASE)
    ProxyMiddleware(e).handle(ctx, _nxt(_resp()))
    assert ctx.kwargs.get("proxies")


# --- proxy: no-fallback raises when proxies exhausted --------------------


def test_proxy_raises_when_no_fallback_and_proxies_exhausted():
    from scraper.config import ProxyConfig

    e = _engine(
        proxy=ProxyConfig(
            proxy_urls=["socks5://127.0.0.1:9150"],
            failure_tolerance=0,  # disable on first failure
            retry_request_on_failure=1,
            fallback_to_direct=False,  # no direct fallback
        )
    )

    def always_fail(ctx):
        raise requests.exceptions.ProxyError("down")

    with pytest.raises(requests.exceptions.ProxyError):
        ProxyMiddleware(e).handle(RequestContext("GET", BASE), always_fail)


# --- stealth gating -------------------------------------------------------


def test_stealth_disabled_leaves_kwargs_untouched():
    e = _engine()  # stealth disabled in fast config
    ctx = RequestContext("GET", BASE, kwargs={"headers": {"X": "1"}})
    StealthMiddleware(e).handle(ctx, _nxt(_resp()))
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
    ctx = RequestContext("GET", BASE, kwargs={})
    StealthMiddleware(e).handle(ctx, _nxt(_resp()))
    assert "Accept" in ctx.kwargs["headers"]


# --- 403 retry ------------------------------------------------------------


def test_retry403_resets_on_200():
    e = _engine()
    e.state.register_403(3)  # bump the counter
    Retry403Middleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(200)))
    # a fresh 403 budget is available again
    assert e.state.register_403(3) is True


def test_retry403_returns_403_when_budget_exhausted():
    e = _engine(max_403_retries=0)
    out = Retry403Middleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(403)))
    assert out.status_code == 403


# --- 403: return None when no proxy and no refresh -----------------------


def test_retry403_returns_none_when_no_proxy_and_no_refresh():
    """When no proxy is available and auto_refresh is off, _maybe_retry returns None
    and the original 403 response is passed through."""
    e = _engine(auto_refresh_on_403=False, max_403_retries=3)
    out = Retry403Middleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(403)))
    assert out.status_code == 403


# --- challenge loop protection -------------------------------------------


class _AlwaysChallenge(ChallengeHandler):
    def is_challenge(self, response):
        return True

    def handle(self, response, *, request, perform_request, **kwargs):  # pragma: no cover
        return response


def test_challenge_loop_protection():
    e = _engine()
    handlers: list[ChallengeHandler] = [_AlwaysChallenge()]
    e.challenge_handlers = handlers
    e.chain.solve_depth = e.config.cloudflare.solve_depth  # already at the limit
    with pytest.raises(CloudflareLoopProtection):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(503)))


class _SolveOnce(ChallengeHandler):
    """Reports a challenge once, then returns a solved response via `request`."""

    def is_challenge(self, response):
        return response.status_code == 503

    def handle(self, response, *, request, perform_request, **kwargs):
        return request("GET", response.url)


def test_challenge_dispatch_solves_and_marks_cf_active():
    e = _engine()
    handlers: list[ChallengeHandler] = [_SolveOnce()]
    e.challenge_handlers = handlers
    # `request` re-enters the engine; fake the transport to return 200 the 2nd time.
    seen = {"n": 0}

    def fake_send(ctx):
        seen["n"] += 1
        return _resp(503 if seen["n"] == 1 else 200, ctx.url)

    e.transport.send = fake_send  # type: ignore[method-assign]
    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), fake_send)
    assert out.status_code == 200
    assert e.state.cf_active is True
