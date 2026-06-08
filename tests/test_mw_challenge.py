"""Tests for ChallengeMiddleware: detection, solving, and edge cases."""

import asyncio

import httpx
import pytest

from scraper.challenges import ClearanceResult, ClearanceSolver
from scraper.config import CloudflareConfig, ProxyConfig
from scraper.engine import create_engine
from scraper.engine.middleware.challenge import ChallengeMiddleware
from scraper.engine.state import RequestState
from scraper.exceptions import CloudflareChallengeError, CloudflareSolveError

from .conftest import make_fast_config

BASE = "https://example.com"

_MANAGED_BODY = "window._cf_chl_opt = {}"


def _resp(status=200, url=BASE, body=b"", headers=None):
    req = httpx.Request("GET", url)
    return httpx.Response(
        status_code=status,
        content=body,
        headers=dict(headers or {}),
        request=req,
    )


def _cf_resp(status=503, url=BASE, body=_MANAGED_BODY):
    return _resp(status, url, body.encode(), {"server": "cloudflare"})


def run_on(coro, engine):
    return asyncio.run_coroutine_threadsafe(coro, engine._loop).result(timeout=10)


class _FakeSolver(ClearanceSolver):
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def solve(self, url, *, proxy=None, user_agent=None):
        self.calls.append(url)
        return self.result


def _engine(**overrides):
    return create_engine(make_fast_config(**overrides))


def test_challenge_detect_raises_without_solver():
    e = _engine()

    async def nxt(c):
        return _cf_resp()

    with pytest.raises(CloudflareChallengeError):
        run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)
    assert e.state.cf_active is True


def test_challenge_clean_response_passes_through():
    e = _engine()

    async def nxt(c):
        return _resp(200)

    out = run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)
    assert out.status_code == 200


def test_challenge_solver_solves_and_retries():
    solver = _FakeSolver(ClearanceResult(cookies={"cf_clearance": "TOKEN"}, user_agent="UA/1.0"))
    e = _engine(cloudflare=CloudflareConfig(solver=solver))

    calls = [0]

    async def fake_send(ctx):
        calls[0] += 1
        return _resp(200, ctx.url)

    e.transport.send = fake_send  # type: ignore[method-assign]

    async def nxt(c):
        return _cf_resp()

    out = run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)
    assert out.status_code == 200
    assert solver.calls == [BASE]
    assert e.state.cf_active is True
    assert e.cookies.get("cf_clearance") == "TOKEN"
    assert e.transport._forced_user_agent == "UA/1.0"


def test_challenge_solver_no_clearance_raises():
    solver = _FakeSolver(None)
    e = _engine(cloudflare=CloudflareConfig(solver=solver))

    async def nxt(c):
        return _cf_resp()

    with pytest.raises(CloudflareSolveError, match="did not obtain a cf_clearance"):
        run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)


def test_challenge_max_attempts_exhausted_raises():
    solver = _FakeSolver(ClearanceResult(cookies={"cf_clearance": "T"}, user_agent="UA"))
    e = _engine(cloudflare=CloudflareConfig(solver=solver, max_solve_attempts=1))

    async def nxt(c):
        return _cf_resp()

    ctx = RequestState("GET", BASE, solve_attempts=1)
    with pytest.raises(CloudflareSolveError, match="challenge persisted"):
        run_on(ChallengeMiddleware(e).handle(ctx, nxt), e)


def test_challenge_middleware_no_detector_passes_through():
    e = _engine()
    e.cf_detector = None

    async def nxt(c):
        return _cf_resp()

    out = run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)
    assert out.status_code == 503


def test_challenge_solver_receives_proxy():
    received: list = []

    class _TrackSolver(ClearanceSolver):
        async def solve(self, url, *, proxy=None, user_agent=None):
            received.append(proxy)
            return ClearanceResult(cookies={"cf_clearance": "T"}, user_agent=None)

    e = _engine(
        cloudflare=CloudflareConfig(solver=_TrackSolver()),
        proxy=ProxyConfig(proxy_urls=["https://p:8080"]),
    )

    async def fake_send(ctx):
        return _resp(200, ctx.url)

    e.transport.send = fake_send  # type: ignore[method-assign]

    async def nxt(c):
        return _cf_resp()

    run_on(ChallengeMiddleware(e).handle(RequestState("GET", BASE), nxt), e)
    assert received[0] is not None
    assert "p:8080" in received[0]
