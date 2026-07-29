"""The solver contract, minus the browser itself.

`NoDriverSolver` needs a real Chrome and a display, so it is exercised by hand rather
than here. What *is* testable is the contract around it — and the binding it produces is
the part that goes wrong in practice, not the browser automation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scraper.browser import (
    CLEARANCE_FALLBACK_TTL,
    BrowserSolver,
    CallableSolver,
    SolveError,
    SolveResult,
    profile_dir_for,
)
from scraper.identity import Identity

try:
    import nodriver
except Exception:  # noqa: BLE001 - nodriver fails with TypeError before Python 3.10
    nodriver = None  # type: ignore[assignment]

needs_nodriver = pytest.mark.skipif(nodriver is None, reason="nodriver needs Python 3.10 or newer")


class TestSolveResult:
    def test_a_clearance_cookie_means_the_challenge_cleared(self):
        assert SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua").cleared
        assert SolveResult(cookies={"__cf_bm": "x"}, user_agent="ua").cleared

    def test_no_clearance_cookie_means_it_did_not(self):
        assert not SolveResult(cookies={"session": "x"}, user_agent="ua").cleared
        assert not SolveResult(cookies={}, user_agent="ua").cleared

    def test_the_clearance_is_bound_to_the_browsers_user_agent(self):
        # The browser is the source of truth once it has solved: the cookie is bound to
        # its exact User-Agent, so the identity has to adopt it.
        identity = Identity(impersonate="chrome", exit_id="e1")
        result = SolveResult(cookies={"cf_clearance": "x"}, user_agent="Chrome/141")
        clearance = result.as_clearance("https://example.com/", identity)

        assert not clearance.usable_by(identity), "the pre-solve identity is a different one"
        assert clearance.usable_by(identity.pin("Chrome/141"))

    def test_a_cookie_expiry_is_honoured_when_the_browser_reports_one(self):
        result = SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua", expires_at=1234.0)
        assert result.as_clearance("https://example.com/", Identity()).expires_at == 1234.0

    def test_an_unknown_expiry_falls_back_conservatively(self):
        # Over-estimating means requests go out with a dead cookie, and the resulting
        # challenge reads as a solver failure.
        import time

        clearance = SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua").as_clearance(
            "https://example.com/", Identity()
        )
        assert clearance.expires_at <= time.time() + CLEARANCE_FALLBACK_TTL + 1

    def test_all_the_cookies_travel_not_just_the_clearance(self):
        # The per-session cookie is set alongside it, and dropping it makes the pair
        # incomplete.
        result = SolveResult(cookies={"cf_clearance": "a", "__cf_bm": "b"}, user_agent="ua")
        assert result.as_clearance("https://example.com/", Identity()).cookies == {
            "cf_clearance": "a",
            "__cf_bm": "b",
        }


class TestCallableSolver:
    def test_a_plain_function_satisfies_the_protocol(self):
        seen = {}

        def solve(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
            seen.update({"url": url, "proxy": proxy, "timeout": timeout})
            return SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua")

        solver = CallableSolver(solve, name="mine")
        result = solver.solve("https://example.com/", proxy="http://p:1", timeout=5.0)
        assert result.cleared
        assert solver.name == "mine"
        assert seen == {"url": "https://example.com/", "proxy": "http://p:1", "timeout": 5.0}

    def test_the_base_class_is_abstract_in_practice(self):
        with pytest.raises(NotImplementedError):
            BrowserSolver().solve("https://example.com/")

    def test_closing_a_solver_that_holds_nothing_is_fine(self):
        BrowserSolver().close()


class TestProfileDirectories:
    def test_one_directory_per_address(self, tmp_path: Path):
        # Cookie and session age are behavioural signals and they belong to the address
        # that accrued them; sharing a profile is how a clean exit inherits a burnt
        # one's session.
        first = profile_dir_for(tmp_path, "pool#s-aaa")
        second = profile_dir_for(tmp_path, "pool#s-bbb")
        assert first != second
        assert first is not None and first.is_dir()

    def test_the_same_address_reuses_its_directory(self, tmp_path: Path):
        assert profile_dir_for(tmp_path, "e1") == profile_dir_for(tmp_path, "e1")

    def test_an_exit_identifier_cannot_escape_the_root(self, tmp_path: Path):
        # Exit labels come from configuration, and a path separator in one would
        # otherwise write outside the profile root.
        made = profile_dir_for(tmp_path, "../../etc/passwd")
        assert made is not None
        assert made.parent == tmp_path

    def test_no_root_means_no_directory(self):
        assert profile_dir_for(None, "e1") is None

    def test_a_missing_identifier_still_gets_a_home(self, tmp_path: Path):
        made = profile_dir_for(tmp_path, "")
        assert made is not None and made.name == "direct"


class TestRunningTheSolverSynchronously:
    def test_an_error_inside_the_solver_reaches_the_caller(self):
        def explode(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
            raise RuntimeError("browser would not start")

        with pytest.raises(RuntimeError, match="would not start"):
            CallableSolver(explode).solve("https://example.com/")

    def test_a_solver_is_usable_from_inside_an_event_loop(self):
        # A scraper driven from an async server is a normal deployment, and the
        # asyncio-based solver has to work there.
        import asyncio

        result: dict = {}

        async def main() -> None:
            def solve(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
                return SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua")

            result["value"] = CallableSolver(solve).solve("https://example.com/")

        asyncio.run(main())
        assert result["value"].cleared


def test_solve_errors_are_distinguishable():
    assert issubclass(SolveError, Exception)


@needs_nodriver
def test_the_nodriver_flags_disable_webrtc(monkeypatch):
    """A STUN request reports the host's real address past the proxy, silently.

    Asserted on the constructed flag list rather than by launching a browser, because
    the value of the check is that the flag is present at all.
    """
    from scraper.browser import NoDriverSolver

    captured: dict = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        raise SolveError("stopping before a browser is launched")

    monkeypatch.setattr(nodriver, "start", fake_start)
    solver = NoDriverSolver(args=["--extra-flag"])
    with pytest.raises(SolveError):
        solver.solve("https://example.com/", proxy="http://p.test:1")

    flags = captured["browser_args"]
    assert "--disable-webrtc" in flags
    assert "--proxy-server=http://p.test:1" in flags
    assert "--extra-flag" in flags
    # Headless reports a software renderer for WebGL, which is a clear indicator on its
    # own, so the default has to be headed.
    assert captured["headless"] is False


@needs_nodriver
def test_a_solve_holds_the_solvers_lock(monkeypatch):
    """Two browsers racing for one profile directory corrupt it.

    The profile is what carries accumulated history forward, so the serialisation is not
    a nicety. Asserted from inside the launch, which is the window that matters.
    """
    from scraper.browser import NoDriverSolver

    solver = NoDriverSolver()
    held = {}

    async def fake_start(**kwargs):
        # The coroutine runs on a private thread, so a held lock is visible from here.
        held["locked"] = solver._lock.locked()  # noqa: SLF001 - the lock is under test
        raise SolveError("stopping before a browser is launched")

    monkeypatch.setattr(nodriver, "start", fake_start)
    with pytest.raises(SolveError):
        solver.solve("https://example.com/")
    assert held["locked"] is True


def test_a_missing_solver_dependency_names_the_version_floor():
    """The message has to say *why* it is unavailable, not just that it is.

    Before Python 3.10 nodriver cannot be imported at all, and the raw failure is a
    TypeError from inside the dependency with nothing pointing at the version.
    """
    from scraper.exceptions import MissingDependency

    error = MissingDependency("browser", "solving a challenge with nodriver (needs Python 3.10)")
    assert "browser" in str(error)
    assert "3.10" in str(error)
