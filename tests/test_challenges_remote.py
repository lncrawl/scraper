"""Tests for RemoteSolver."""

import pytest
import responses

from scraper.challenges import ClearanceResult, RemoteSolver
from scraper.exceptions import CloudflareSolveError

BASE = "https://example.com"


@responses.activate
def test_remote_solver_success():
    responses.add(
        responses.POST,
        "http://svc/v1",
        json={
            "status": "ok",
            "solution": {
                "cookies": [{"name": "cf_clearance", "value": "TOK"}],
                "userAgent": "UA/1.0",
            },
        },
    )
    result = RemoteSolver("http://svc/").solve(BASE)
    assert isinstance(result, ClearanceResult)
    assert result.cookies == {"cf_clearance": "TOK"}
    assert result.user_agent == "UA/1.0"


@responses.activate
def test_remote_solver_not_ok_raises():
    responses.add(responses.POST, "http://svc/v1", json={"status": "error", "message": "boom"})
    with pytest.raises(CloudflareSolveError, match="boom"):
        RemoteSolver("http://svc").solve(BASE)


@responses.activate
def test_remote_solver_http_error_raises():
    responses.add(responses.POST, "http://svc/v1", status=500)
    with pytest.raises(CloudflareSolveError):
        RemoteSolver("http://svc").solve(BASE)


@responses.activate
def test_remote_solver_sends_proxy_and_session():
    responses.add(
        responses.POST,
        "http://svc/v1",
        json={"status": "ok", "solution": {"cookies": [], "userAgent": ""}},
    )
    RemoteSolver("http://svc", session="sess").solve(BASE, proxy="http://p:8080")
    body = responses.calls[0].request.body
    text = body.decode() if isinstance(body, bytes) else str(body)
    assert "sess" in text
    assert "p:8080" in text
