"""Tests for the engine/challenges module.

Covers the pure :class:`CloudflareDetector` classification, its exception
mapping, the ``build_detector`` factory, and both solver backends. Everything is
offline: ``RemoteSolver`` HTTP is mocked with ``responses`` and ``BrowserSolver``
runs against a fake ``nodriver`` module (no real browser).
"""

from __future__ import annotations

import asyncio
import platform as _platform
import sys
import types

import pytest
import requests
import responses

from scraper.challenges import (
    BrowserSolver,
    ClearanceResult,
    CloudflareChallengeKind,
    CloudflareDetector,
    RemoteSolver,
    build_detector,
)
from scraper.challenges.browser_exe import _find_executables, pick_executable
from scraper.config import CloudflareConfig
from scraper.exceptions import (
    CloudflareCaptchaError,
    CloudflareChallengeError,
    CloudflareFirewallBlock,
    CloudflareSolveError,
    CloudflareTurnstileError,
)

BASE = "https://example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status=200, url=BASE, body="", headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers.update(headers or {})
    return r


def _cf_resp(status=503, body="", url=BASE):
    return _resp(status, url, body, {"Server": "cloudflare"})


_FIREWALL_BODY = '<span class="cf-error-code">1020</span>'
_TURNSTILE_BODY = '<div class="cf-turnstile" data-sitekey="abc"></div>'
_CAPTCHA_BODY = (
    '<img src="/cdn-cgi/images/trace/captcha/x.gif"><form id="challenge-form" action="/x"></form>'
)
_MANAGED_BODY = "window._cf_chl_opt = {}"


# ---------------------------------------------------------------------------
# CloudflareDetector.classify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (403, _FIREWALL_BODY, CloudflareChallengeKind.FIREWALL_BLOCK),
        (503, _TURNSTILE_BODY, CloudflareChallengeKind.TURNSTILE),
        (403, _CAPTCHA_BODY, CloudflareChallengeKind.CAPTCHA),
        (503, _MANAGED_BODY, CloudflareChallengeKind.MANAGED),
        (429, _MANAGED_BODY, CloudflareChallengeKind.MANAGED),
    ],
)
def test_classify_kinds(status, body, expected):
    assert CloudflareDetector().classify(_cf_resp(status, body)) is expected


def test_classify_clean_cf_page_is_none():
    assert (
        CloudflareDetector().classify(_cf_resp(200, "<h1>hi</h1>")) is CloudflareChallengeKind.NONE
    )


def test_classify_non_cloudflare_is_none():
    assert (
        CloudflareDetector().classify(_resp(503, body=_MANAGED_BODY))
        is CloudflareChallengeKind.NONE
    )


def test_classify_wrong_status_is_none():
    # Managed markers but a 200 status → not a challenge.
    assert (
        CloudflareDetector().classify(_cf_resp(200, _MANAGED_BODY)) is CloudflareChallengeKind.NONE
    )


def test_classify_attribute_error_is_none():
    assert CloudflareDetector().classify(object()) is CloudflareChallengeKind.NONE  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CloudflareDetector.raise_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,exc",
    [
        (CloudflareChallengeKind.FIREWALL_BLOCK, CloudflareFirewallBlock),
        (CloudflareChallengeKind.TURNSTILE, CloudflareTurnstileError),
        (CloudflareChallengeKind.CAPTCHA, CloudflareCaptchaError),
        (CloudflareChallengeKind.MANAGED, CloudflareChallengeError),
    ],
)
def test_raise_for_maps_exception(kind, exc):
    with pytest.raises(exc):
        CloudflareDetector(debug=True).raise_for(kind, _cf_resp())


# ---------------------------------------------------------------------------
# build_detector
# ---------------------------------------------------------------------------


def test_build_detector_enabled():
    det = build_detector(CloudflareConfig(debug=True))
    assert isinstance(det, CloudflareDetector)
    assert det.debug is True


def test_build_detector_disabled_returns_none():
    assert build_detector(CloudflareConfig(enabled=False)) is None


# ---------------------------------------------------------------------------
# RemoteSolver
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# BrowserSolver
# ---------------------------------------------------------------------------


def _install_fake_nodriver(monkeypatch, *, cookies):
    class _Cookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class _Cookies:
        async def get_all(self):
            return [_Cookie(n, v) for n, v in cookies.items()]

    class _Tab:
        async def evaluate(self, expr):
            return "FakeUA/1.0"

        async def find(self, selector, timeout=3):
            return None

    class _Browser:
        def __init__(self):
            self.cookies = _Cookies()
            self.main_tab = _Tab()

        async def get(self, url):
            return self.main_tab

        def stop(self):
            pass

    async def _start(**kwargs):
        return _Browser()

    module = types.ModuleType("nodriver")
    module.start = _start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nodriver", module)


def test_browser_solver_missing_nodriver(monkeypatch):
    # None in sys.modules forces `import nodriver` to raise ImportError.
    monkeypatch.setitem(sys.modules, "nodriver", None)
    with pytest.raises(CloudflareSolveError, match="browser"):
        BrowserSolver().solve(BASE)


def test_browser_solver_success(monkeypatch):
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    result = BrowserSolver(timeout=5).solve(BASE)
    assert isinstance(result, ClearanceResult)
    assert result.cookies["cf_clearance"] == "TOK"
    assert result.user_agent == "FakeUA/1.0"


def test_browser_solver_no_clearance_returns_none(monkeypatch):
    _install_fake_nodriver(monkeypatch, cookies={"other": "x"})
    assert BrowserSolver(timeout=0).solve(BASE) is None


# ---------------------------------------------------------------------------
# browser_exe._find_executables  (lines 20-26, 28->42, 35->29, 257)
# ---------------------------------------------------------------------------


def test_find_executables_linux_branch(monkeypatch):
    """Posix/Linux platform branch is exercised."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    result = _find_executables(
        windows_path=[],
        mac_app_path=["/nonexistent/mac"],
        linux_app_path=["/nonexistent/linux"],
        posix_app_name=["google-chrome"],
        windows_exe_name=[],
    )
    assert isinstance(result, list)


def test_find_executables_darwin_branch(monkeypatch):
    """mac_app_path branch is exercised under Darwin."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    result = _find_executables(
        windows_path=[],
        mac_app_path=["/nonexistent/mac/chrome"],
        linux_app_path=[],
        posix_app_name=["google-chrome"],
        windows_exe_name=[],
    )
    assert isinstance(result, list)


def test_find_executables_windows_missing_env_var(monkeypatch):
    """Absent Windows env var → that path iteration is skipped gracefully."""
    monkeypatch.delenv("PROGRAMW6432", raising=False)
    result = _find_executables(
        windows_path=["Google/Chrome/Application"],
        mac_app_path=[],
        linux_app_path=[],
        posix_app_name=[],
        windows_exe_name=["chrome.exe"],
    )
    assert isinstance(result, list)


def test_pick_executable_returns_none_when_empty(monkeypatch):
    """When no executables are found, None is returned."""
    monkeypatch.setattr(
        "scraper.challenges.browser_exe.find_all_chromium_executables",
        lambda: [],
    )
    assert pick_executable() is None


def test_pick_executable_returns_shortest(monkeypatch):
    """When multiple executables are found, the shortest path wins."""
    monkeypatch.setattr(
        "scraper.challenges.browser_exe.find_all_chromium_executables",
        lambda: ["/usr/bin/chromium-browser", "/usr/bin/chrome"],
    )
    assert pick_executable() == "/usr/bin/chrome"


# ---------------------------------------------------------------------------
# BrowserSolver additional edge cases
# ---------------------------------------------------------------------------


def test_browser_solver_invalid_url(monkeypatch):
    """Invalid URL raises ValueError before the browser is started."""
    _install_fake_nodriver(monkeypatch, cookies={})
    with pytest.raises(ValueError, match="Invalid URL"):
        BrowserSolver().solve("not-a-url")


def test_browser_solver_with_proxy(monkeypatch):
    """Proxy URL is forwarded as a browser arg."""
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    result = BrowserSolver(timeout=5).solve(BASE, proxy="http://p:8080")
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"


def test_browser_solver_app_mode_false_fetches_tab(monkeypatch):
    """app_mode=False → browser.get(url) called to obtain the tab."""
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    result = BrowserSolver(app_mode=False, timeout=5).solve(BASE)
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"


def test_browser_solver_start_failure_raises(monkeypatch):
    """uc.start() raising → wrapped as CloudflareSolveError."""
    module = types.ModuleType("nodriver")

    async def _boom(**kwargs):
        raise RuntimeError("no Chrome found")

    module.start = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nodriver", module)
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    with pytest.raises(CloudflareSolveError, match="Failed to start"):
        BrowserSolver(timeout=5).solve(BASE)


def test_browser_solver_polls_until_clearance(monkeypatch):
    """Loop sleeps when cf_clearance not yet present, then returns it."""
    call_count = [0]

    async def _instant_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    class _Cookie:
        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value

    class _DynamicCookies:
        async def get_all(self):
            call_count[0] += 1
            if call_count[0] == 1:
                return []
            return [_Cookie("cf_clearance", "TOK")]

    class _Tab:
        async def evaluate(self, expr):
            return "FakeUA/1.0"

    class _Browser:
        def __init__(self):
            self.cookies = _DynamicCookies()
            self.main_tab = _Tab()

        async def get(self, url):
            return self.main_tab

        def stop(self):
            pass

    async def _start(**kwargs):
        return _Browser()

    module = types.ModuleType("nodriver")
    module.start = _start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nodriver", module)
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)

    result = BrowserSolver(timeout=5).solve(BASE)
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"
    assert call_count[0] >= 2


def test_browser_solver_exception_in_loop_raises(monkeypatch):
    """Unexpected exception in the poll loop → CloudflareSolveError."""

    class _BoomCookies:
        async def get_all(self):
            raise RuntimeError("browser crashed mid-poll")

    class _Tab:
        async def evaluate(self, expr):
            return None

    class _Browser:
        def __init__(self):
            self.cookies = _BoomCookies()
            self.main_tab = _Tab()

        async def get(self, url):
            return self.main_tab

        def stop(self):
            pass

    async def _start(**kwargs):
        return _Browser()

    module = types.ModuleType("nodriver")
    module.start = _start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nodriver", module)
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    with pytest.raises(CloudflareSolveError, match="Failed to obtain"):
        BrowserSolver(timeout=5).solve(BASE)


def test_browser_solver_read_cookies_static():
    """_read_cookies filters out empty cookie values."""

    class _C:
        def __init__(self, name: str, value: str) -> None:
            self.name = name
            self.value = value

    class _FakeBrowser:
        class _FakeCookies:
            async def get_all(self):
                return [_C("cf_clearance", "TOK"), _C("empty_val", "")]

        cookies = _FakeCookies()

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(BrowserSolver._read_cookies(_FakeBrowser()))  # type: ignore[arg-type]
    loop.close()
    assert result == {"cf_clearance": "TOK"}


def test_browser_solver_read_user_agent_falsy():
    """evaluate() returning a falsy value → None returned."""

    class _FalsyTab:
        async def evaluate(self, expr):
            return ""

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(BrowserSolver._read_user_agent(_FalsyTab()))  # type: ignore[arg-type]
    loop.close()
    assert result is None


def test_browser_solver_read_user_agent_exception():
    """_read_user_agent returns None when evaluate() raises."""

    class _BadTab:
        async def evaluate(self, expr):
            raise RuntimeError("JS engine unavailable")

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(BrowserSolver._read_user_agent(_BadTab()))  # type: ignore[arg-type]
    loop.close()
    assert result is None


# ---------------------------------------------------------------------------
# CloudflareDetector extra branches
# ---------------------------------------------------------------------------


def test_classify_cf_server_challenge_status_no_known_marker():
    """CF server + challenge status + no matching marker → NONE."""
    assert (
        CloudflareDetector().classify(_cf_resp(503, "<h1>Generic error</h1>"))
        is CloudflareChallengeKind.NONE
    )


def test_raise_for_none_kind_hits_fallback():
    """raise_for(NONE) triggers the final fallback CloudflareChallengeError."""
    with pytest.raises(CloudflareChallengeError):
        CloudflareDetector().raise_for(CloudflareChallengeKind.NONE, _cf_resp())
