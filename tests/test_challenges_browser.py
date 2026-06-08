"""Tests for BrowserSolver and browser_exe helpers."""

from __future__ import annotations

import asyncio
import platform as _platform
import sys
import types

import pytest

from scraper.challenges import (
    BrowserSolver,
    ClearanceResult,
)
from scraper.challenges.browser_exe import _find_executables, pick_executable
from scraper.exceptions import CloudflareSolveError

BASE = "https://example.com"


def solve(solver, *args, **kwargs):
    return asyncio.run(solver.solve(*args, **kwargs))


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
    monkeypatch.setitem(sys.modules, "nodriver", None)
    with pytest.raises(CloudflareSolveError, match="browser"):
        solve(BrowserSolver(), BASE)


def test_browser_solver_success(monkeypatch):
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    result = solve(BrowserSolver(timeout=5), BASE)
    assert isinstance(result, ClearanceResult)
    assert result.cookies["cf_clearance"] == "TOK"
    assert result.user_agent == "FakeUA/1.0"


def test_browser_solver_no_clearance_returns_none(monkeypatch):
    _install_fake_nodriver(monkeypatch, cookies={"other": "x"})
    assert solve(BrowserSolver(timeout=0), BASE) is None


def test_browser_solver_invalid_url(monkeypatch):
    """Invalid URL raises ValueError before the browser is started."""
    _install_fake_nodriver(monkeypatch, cookies={})
    with pytest.raises(ValueError, match="Invalid URL"):
        solve(BrowserSolver(), "not-a-url")


def test_browser_solver_with_proxy(monkeypatch):
    """Proxy URL is forwarded as a browser arg."""
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    result = solve(BrowserSolver(timeout=5), BASE, proxy="http://p:8080")
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"


def test_browser_solver_app_mode_false_fetches_tab(monkeypatch):
    """app_mode=False â†’ browser.get(url) called to obtain the tab."""
    _install_fake_nodriver(monkeypatch, cookies={"cf_clearance": "TOK"})
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    result = solve(BrowserSolver(app_mode=False, timeout=5), BASE)
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"


def test_browser_solver_start_failure_raises(monkeypatch):
    """uc.start() raising â†’ wrapped as CloudflareSolveError."""
    module = types.ModuleType("nodriver")

    async def _boom(**kwargs):
        raise RuntimeError("no Chrome found")

    module.start = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nodriver", module)
    monkeypatch.setattr("scraper.challenges.browser_solver.pick_executable", lambda: None)
    with pytest.raises(CloudflareSolveError, match="Failed to start"):
        solve(BrowserSolver(timeout=5), BASE)


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

    result = solve(BrowserSolver(timeout=5), BASE)
    assert result is not None
    assert result.cookies["cf_clearance"] == "TOK"
    assert call_count[0] >= 2


def test_browser_solver_exception_in_loop_raises(monkeypatch):
    """Unexpected exception in the poll loop â†’ CloudflareSolveError."""

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
        solve(BrowserSolver(timeout=5), BASE)


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
    """evaluate() returning a falsy value â†’ None returned."""

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
# browser_exe helpers
# ---------------------------------------------------------------------------


def test_find_executables_linux_branch(monkeypatch):
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
    monkeypatch.setattr(
        "scraper.challenges.browser_exe.find_all_chromium_executables",
        lambda: [],
    )
    assert pick_executable() is None


def test_pick_executable_returns_shortest(monkeypatch):
    monkeypatch.setattr(
        "scraper.challenges.browser_exe.find_all_chromium_executables",
        lambda: ["/usr/bin/chromium-browser", "/usr/bin/chrome"],
    )
    assert pick_executable() == "/usr/bin/chrome"
