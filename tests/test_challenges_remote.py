"""Tests for RemoteSolver."""

import asyncio

import httpx
import pytest
import respx

from scraper.challenges import ClearanceResult, RemoteSolver
from scraper.exceptions import CloudflareSolveError

BASE = "https://example.com"


def solve(solver, *args, **kwargs):
    return asyncio.run(solver.solve(*args, **kwargs))


@respx.mock
def test_remote_solver_success():
    respx.post("http://svc/v1").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "cookies": [{"name": "cf_clearance", "value": "TOK"}],
                    "userAgent": "UA/1.0",
                },
            },
        )
    )
    result = solve(RemoteSolver("http://svc/"), BASE)
    assert isinstance(result, ClearanceResult)
    assert result.cookies == {"cf_clearance": "TOK"}
    assert result.user_agent == "UA/1.0"


@respx.mock
def test_remote_solver_not_ok_raises():
    respx.post("http://svc/v1").mock(
        return_value=httpx.Response(200, json={"status": "error", "message": "boom"})
    )
    with pytest.raises(CloudflareSolveError, match="boom"):
        solve(RemoteSolver("http://svc"), BASE)


@respx.mock
def test_remote_solver_http_error_raises():
    respx.post("http://svc/v1").mock(return_value=httpx.Response(500))
    with pytest.raises(CloudflareSolveError):
        solve(RemoteSolver("http://svc"), BASE)


@respx.mock
def test_remote_solver_sends_proxy_and_session():
    route = respx.post("http://svc/v1").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "solution": {"cookies": [], "userAgent": ""}}
        )
    )
    solve(RemoteSolver("http://svc", session="sess"), BASE, proxy="http://p:8080")
    body = route.calls[0].request.content.decode()
    assert "sess" in body
    assert "p:8080" in body
