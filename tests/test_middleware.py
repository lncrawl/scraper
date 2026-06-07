"""Unit tests for individual middleware against a fake `nxt` handler.

Each test drives one middleware in isolation: a real engine supplies the
collaborators (state, proxy manager, stealth) while ``nxt`` is a stub that records
calls and returns a scripted response.
"""

import pytest
import requests

from scraper import AbortedException
from scraper.challenges import ClearanceResult, ClearanceSolver
from scraper.config import CloudflareConfig, ProxyConfig, StealthConfig
from scraper.engine import create_engine
from scraper.engine.context import RequestContext
from scraper.engine.middleware import build_chain
from scraper.engine.middleware.abort import AbortMiddleware
from scraper.engine.middleware.challenge import ChallengeMiddleware
from scraper.engine.middleware.proxy import ProxyMiddleware
from scraper.engine.middleware.retry_403 import Retry403Middleware
from scraper.engine.middleware.stealth import StealthMiddleware
from scraper.engine.middleware.throttle import ThrottleMiddleware
from scraper.exceptions import CloudflareChallengeError, CloudflareSolveError

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
    sink: list = []
    out = AbortMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(), sink))
    assert out.status_code == 200 and len(sink) == 1


# --- throttle / nested skip ----------------------------------------------


def test_throttle_skips_when_nested():
    e = _engine()
    sink: list = []
    ctx = RequestContext("GET", BASE, nested=True)
    ThrottleMiddleware(e).handle(ctx, _nxt(_resp(), sink))
    assert len(sink) == 1  # nxt called, no throttling state touched


def test_throttle_sleeps_when_interval_pending():
    e = _engine(min_request_interval_fast=0.05)
    e.state.mark_request_sent()  # a recent request → next one must wait
    sink: list = []
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


# --- challenge detection + auto-solve ------------------------------------

_MANAGED_BODY = "window._cf_chl_opt = {}"


def _cf_resp(status=503, url=BASE, body=_MANAGED_BODY):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers["Server"] = "cloudflare"
    return r


class _FakeSolver(ClearanceSolver):
    """Returns a scripted ClearanceResult (or None) and records calls."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def solve_async(self, url, *, proxy=None, user_agent=None):
        self.calls.append(url)
        return self.result


def test_challenge_detect_raises_without_solver():
    e = _engine()  # cloudflare.solver is None by default
    with pytest.raises(CloudflareChallengeError):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert e.state.cf_active is True


def test_challenge_clean_response_resets_attempts():
    e = _engine()
    e.chain.solve_attempts = 5
    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_resp(200)))
    assert out.status_code == 200
    assert e.chain.solve_attempts == 0


def test_challenge_solver_solves_and_retries():
    solver = _FakeSolver(ClearanceResult(cookies={"cf_clearance": "TOKEN"}, user_agent="UA/1.0"))
    e = _engine(cloudflare=CloudflareConfig(solver=solver))
    # The retry re-enters the engine via e.request → fake the transport to 200.
    e.transport.send = lambda ctx: _resp(200, ctx.url)  # type: ignore[method-assign]

    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert out.status_code == 200
    assert solver.calls == [BASE]
    assert e.state.cf_active is True
    assert e.cookies.get("cf_clearance") == "TOKEN"
    # The solver's exact UA is pinned on the transport for clearance reuse.
    assert e.transport._forced_user_agent == "UA/1.0"


def test_challenge_solver_no_clearance_raises():
    solver = _FakeSolver(None)
    e = _engine(cloudflare=CloudflareConfig(solver=solver))
    with pytest.raises(CloudflareSolveError, match="did not obtain a cf_clearance"):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))


def test_challenge_max_attempts_exhausted_raises():
    solver = _FakeSolver(ClearanceResult(cookies={"cf_clearance": "T"}, user_agent="UA"))
    e = _engine(cloudflare=CloudflareConfig(solver=solver, max_solve_attempts=1))
    e.chain.solve_attempts = 1  # already at the limit
    with pytest.raises(CloudflareSolveError, match="challenge persisted"):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))


def test_challenge_middleware_no_detector_passes_through():
    """When cf_detector is None the response is returned unchanged."""
    e = _engine()
    e.cf_detector = None
    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert out.status_code == 503


def test_challenge_solver_receives_proxy():
    """_current_proxy forwards the https proxy URL into solver.solve."""
    received: list = []

    class _TrackSolver(ClearanceSolver):
        async def solve_async(self, url, *, proxy=None, user_agent=None):
            received.append(proxy)
            return ClearanceResult(cookies={"cf_clearance": "T"}, user_agent=None)

    e = _engine(
        cloudflare=CloudflareConfig(solver=_TrackSolver()),
        proxy=ProxyConfig(proxy_urls=["https://p:8080"]),
    )
    e.transport.send = lambda ctx: _resp(200, ctx.url)  # type: ignore[method-assign]
    ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert received[0] is not None
    assert "p:8080" in received[0]
