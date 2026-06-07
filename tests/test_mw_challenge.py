"""Tests for ChallengeMiddleware: detection, solving, and edge cases."""

import pytest
import requests

from scraper.challenges import ClearanceResult, ClearanceSolver
from scraper.config import CloudflareConfig, ProxyConfig
from scraper.engine import create_engine
from scraper.engine.context import RequestContext
from scraper.engine.middleware.challenge import ChallengeMiddleware
from scraper.exceptions import CloudflareChallengeError, CloudflareSolveError

from .conftest import make_fast_config

BASE = "https://example.com"

_MANAGED_BODY = "window._cf_chl_opt = {}"


def _resp(status=200, url=BASE):
    r = requests.Response()
    r.status_code = status
    r._content = b""
    r.url = url
    return r


def _cf_resp(status=503, url=BASE, body=_MANAGED_BODY):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers["Server"] = "cloudflare"
    return r


def _nxt(resp):
    return lambda ctx: resp


class _FakeSolver(ClearanceSolver):
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def solve_async(self, url, *, proxy=None, user_agent=None):
        self.calls.append(url)
        return self.result


def _engine(**overrides):
    return create_engine(make_fast_config(**overrides))


def test_challenge_detect_raises_without_solver():
    e = _engine()
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
    e.transport.send = lambda ctx: _resp(200, ctx.url)  # type: ignore[method-assign]

    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert out.status_code == 200
    assert solver.calls == [BASE]
    assert e.state.cf_active is True
    assert e.cookies.get("cf_clearance") == "TOKEN"
    assert e.transport._forced_user_agent == "UA/1.0"


def test_challenge_solver_no_clearance_raises():
    solver = _FakeSolver(None)
    e = _engine(cloudflare=CloudflareConfig(solver=solver))
    with pytest.raises(CloudflareSolveError, match="did not obtain a cf_clearance"):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))


def test_challenge_max_attempts_exhausted_raises():
    solver = _FakeSolver(ClearanceResult(cookies={"cf_clearance": "T"}, user_agent="UA"))
    e = _engine(cloudflare=CloudflareConfig(solver=solver, max_solve_attempts=1))
    e.chain.solve_attempts = 1
    with pytest.raises(CloudflareSolveError, match="challenge persisted"):
        ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))


def test_challenge_middleware_no_detector_passes_through():
    e = _engine()
    e.cf_detector = None
    out = ChallengeMiddleware(e).handle(RequestContext("GET", BASE), _nxt(_cf_resp()))
    assert out.status_code == 503


def test_challenge_solver_receives_proxy():
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
