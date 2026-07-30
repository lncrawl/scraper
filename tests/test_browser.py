"""The solver contract, and the driving loop around the browser.

A real Chrome is never launched here. `nodriver` is imported inside `_solve`, so a stub
module in `sys.modules` exercises the whole path — the flags, the interstitial loop, the
cookie harvest, the shutdown — on every supported Python, including 3.9 where the real
package cannot be imported at all. What the stub cannot tell us is whether Chrome clears
a challenge; that belongs to `livetest/`. What it does tell us is whether this module
drives it correctly, and the binding it produces is the part that goes wrong in practice.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from scraper.browser import (
    _STILL_CHALLENGED,
    CLEARANCE_FALLBACK_TTL,
    BrowserSolver,
    CallableSolver,
    NoDriverSolver,
    SolveError,
    SolveResult,
    _harvest_cookies,
    _run_async,
    profile_dir_for,
)
from scraper.exceptions import MissingDependency
from scraper.identity import Identity

from .conftest import CHALLENGE_BODY, SERVED_WITH_JSD

CLEARED_PAGE = "<!doctype html><html><body><h1>Chapter 12</h1></body></html>"


class FakeCookie:
    def __init__(self, name: str, value: str = "v", expires: float = 0.0) -> None:
        self.name = name
        self.value = value
        self.expires = expires


class FakeJar:
    def __init__(self, cookies: Optional[List[FakeCookie]]) -> None:
        self._cookies = cookies

    async def get_all(self) -> Optional[List[FakeCookie]]:
        return self._cookies


class FakePage:
    """Serves *contents* one per poll, repeating the last one forever."""

    def __init__(self, contents: List[str], user_agent: str = "Mozilla/5.0 Chrome/141.0.0.0"):
        self._contents = list(contents)
        self._user_agent = user_agent
        self.settles = 0

    async def sleep(self, seconds: float) -> None:
        self.settles += 1

    async def get_content(self) -> str:
        return self._contents.pop(0) if len(self._contents) > 1 else self._contents[0]

    async def evaluate(self, expression: str) -> str:
        return self._user_agent


class FakeBrowser:
    def __init__(
        self,
        page: FakePage,
        cookies: Optional[List[FakeCookie]] = None,
        *,
        stop_raises: bool = False,
        has_jar: bool = True,
    ) -> None:
        self._page = page
        if has_jar:
            self.cookies = FakeJar(cookies if cookies is not None else [])
        self._stop_raises = stop_raises
        self.stopped = False
        self.visited = ""

    async def get(self, url: str) -> FakePage:
        self.visited = url
        return self._page

    def stop(self) -> None:
        self.stopped = True
        if self._stop_raises:
            raise RuntimeError("chrome would not exit")


def install_nodriver(monkeypatch: pytest.MonkeyPatch, browser: FakeBrowser) -> Dict[str, Any]:
    """Stand a stub `nodriver` up in `sys.modules`; returns the captured launch kwargs."""
    captured: Dict[str, Any] = {}
    module = types.ModuleType("nodriver")

    async def start(**kwargs: Any) -> FakeBrowser:
        captured.update(kwargs)
        return browser

    setattr(module, "start", start)
    monkeypatch.setitem(sys.modules, "nodriver", module)
    return captured


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

    def test_an_error_inside_the_solver_reaches_the_caller(self):
        def explode(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
            raise RuntimeError("browser would not start")

        with pytest.raises(RuntimeError, match="would not start"):
            CallableSolver(explode).solve("https://example.com/")


class TestRunningTheSolverSynchronously:
    """`_run_async` — always a private thread, never the caller's loop."""

    def test_a_result_comes_back_from_the_private_thread(self):
        async def work() -> SolveResult:
            return SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua")

        assert _run_async(work()).cleared

    def test_it_works_from_inside_a_running_event_loop(self):
        # A scraper driven from an async server is a normal deployment. `asyncio.run`
        # on the calling thread raises there, which is why the work goes to a thread
        # of its own rather than to whichever loop happens to be running.
        async def work() -> SolveResult:
            return SolveResult(cookies={"cf_clearance": "x"}, user_agent="ua")

        async def main() -> SolveResult:
            return _run_async(work())

        assert asyncio.run(main()).cleared

    def test_an_error_on_the_private_thread_is_re_raised_on_the_caller(self):
        # Otherwise a browser that will not start reads as a solver returning nothing,
        # and the caller retries something that cannot work.
        async def work() -> SolveResult:
            raise RuntimeError("browser would not start")

        with pytest.raises(RuntimeError, match="would not start"):
            _run_async(work())

    def test_a_coroutine_that_returns_nothing_is_a_solve_error(self):
        async def work() -> None:
            return None

        with pytest.raises(SolveError, match="no result"):
            _run_async(work())


class TestReadingTheInterstitial:
    def test_a_challenge_page_reads_as_still_challenged(self):
        assert _STILL_CHALLENGED.search(CHALLENGE_BODY)

    def test_the_injected_detections_script_does_not(self):
        """The `/h/` in the pattern is what separates these two bodies.

        Cloudflare injects a JavaScript-Detections script from
        `challenge-platform/scripts/…` into ordinary pages. Matching the bare path
        means the loop never reports "cleared" and burns the entire timeout on every
        solve, including the successful ones.
        """
        assert not _STILL_CHALLENGED.search(SERVED_WITH_JSD)


class TestHarvestingCookies:
    def test_a_browser_with_no_cookie_jar_yields_nothing(self):
        assert asyncio.run(_harvest_cookies(object())) == ({}, 0.0)

    def test_an_empty_jar_is_not_an_error(self):
        page = FakePage([CLEARED_PAGE])
        assert asyncio.run(_harvest_cookies(FakeBrowser(page, None))) == ({}, 0.0)

    def test_a_jar_that_answers_with_nothing_is_not_an_error(self):
        browser = FakeBrowser(FakePage([CLEARED_PAGE]))
        browser.cookies = FakeJar(None)
        assert asyncio.run(_harvest_cookies(browser)) == ({}, 0.0)

    def test_every_cookie_travels_not_just_the_clearance_ones(self):
        # The per-session cookie is set alongside the clearance and dropping it makes
        # the pair incomplete.
        jar = [FakeCookie("cf_clearance", "a"), FakeCookie("__cf_bm", "b"), FakeCookie("sid", "c")]
        cookies, _ = asyncio.run(_harvest_cookies(FakeBrowser(FakePage([CLEARED_PAGE]), jar)))
        assert cookies == {"cf_clearance": "a", "__cf_bm": "b", "sid": "c"}

    def test_only_a_clearance_cookies_expiry_bounds_the_window(self):
        # An unrelated session cookie expiring in a minute must not shorten the
        # clearance to a minute: the result is how long the cheap transport may keep
        # reusing this identity, and under-reading it re-launches a browser for nothing.
        jar = [FakeCookie("cf_clearance", "a", 5000.0), FakeCookie("sid", "c", 60.0)]
        _, soonest = asyncio.run(_harvest_cookies(FakeBrowser(FakePage([CLEARED_PAGE]), jar)))
        assert soonest == 5000.0

    def test_the_earliest_clearance_expiry_wins(self):
        jar = [FakeCookie("cf_clearance", "a", 5000.0), FakeCookie("__cf_bm", "b", 3000.0)]
        _, soonest = asyncio.run(_harvest_cookies(FakeBrowser(FakePage([CLEARED_PAGE]), jar)))
        assert soonest == 3000.0

    def test_a_nameless_cookie_is_skipped(self):
        jar = [FakeCookie("", "a"), FakeCookie("cf_clearance", "b")]
        cookies, _ = asyncio.run(_harvest_cookies(FakeBrowser(FakePage([CLEARED_PAGE]), jar)))
        assert cookies == {"cf_clearance": "b"}


class TestDrivingNoDriver:
    def test_a_solve_returns_what_the_browser_held(self, monkeypatch: pytest.MonkeyPatch):
        page = FakePage([CLEARED_PAGE])
        browser = FakeBrowser(page, [FakeCookie("cf_clearance", "abc", 4000.0)])
        install_nodriver(monkeypatch, browser)

        result = NoDriverSolver(settle=0.0).solve("https://site.test/deep")

        assert result.cleared
        assert result.cookies == {"cf_clearance": "abc"}
        assert result.user_agent == "Mozilla/5.0 Chrome/141.0.0.0"
        assert result.expires_at == 4000.0
        assert browser.visited == "https://site.test/deep"
        assert browser.stopped, "a browser left running holds the profile directory"

    def test_the_flags_disable_webrtc(self, monkeypatch: pytest.MonkeyPatch):
        """A STUN request reaches the network directly and reports the host's real
        address, which unbinds the identity without any request failing."""
        browser = FakeBrowser(FakePage([CLEARED_PAGE]), [FakeCookie("cf_clearance", "x")])
        captured = install_nodriver(monkeypatch, browser)

        NoDriverSolver(args=["--extra-flag"], settle=0.0).solve(
            "https://site.test/", proxy="http://p.test:1"
        )

        flags = captured["browser_args"]
        assert "--disable-webrtc" in flags
        assert "--disable-features=WebRtcHideLocalIpsWithMdns" in flags
        assert "--proxy-server=http://p.test:1" in flags
        assert flags[-1] == "--extra-flag", "caller flags come after the defaults"
        # Headless reports a software renderer for WebGL, which is a clear indicator on
        # its own, so the default has to be headed.
        assert captured["headless"] is False

    def test_no_proxy_means_no_proxy_flag(self, monkeypatch: pytest.MonkeyPatch):
        browser = FakeBrowser(FakePage([CLEARED_PAGE]), [FakeCookie("cf_clearance", "x")])
        captured = install_nodriver(monkeypatch, browser)

        NoDriverSolver(settle=0.0).solve("https://site.test/")

        assert not any(flag.startswith("--proxy-server") for flag in captured["browser_args"])
        assert captured["user_data_dir"] is None

    def test_the_profile_directory_is_handed_to_chrome(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The profile is what carries accumulated session history forward, and a solve
        # that does not use it throws that away on every launch.
        browser = FakeBrowser(FakePage([CLEARED_PAGE]), [FakeCookie("cf_clearance", "x")])
        captured = install_nodriver(monkeypatch, browser)

        NoDriverSolver(settle=0.0).solve("https://site.test/", profile_dir=tmp_path)

        assert captured["user_data_dir"] == str(tmp_path)

    def test_the_loop_polls_while_the_page_is_still_an_interstitial(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        page = FakePage([CHALLENGE_BODY, CHALLENGE_BODY, CLEARED_PAGE])
        browser = FakeBrowser(page, [FakeCookie("cf_clearance", "x")])
        install_nodriver(monkeypatch, browser)

        NoDriverSolver(settle=0.0).solve("https://site.test/")

        assert page.settles == 3

    def test_a_cleared_page_stops_the_loop_immediately(self, monkeypatch: pytest.MonkeyPatch):
        # Not a performance nicety: the detections script Cloudflare injects into
        # ordinary pages used to keep this loop running for the whole timeout, on every
        # successful solve.
        page = FakePage([SERVED_WITH_JSD])
        install_nodriver(monkeypatch, FakeBrowser(page, [FakeCookie("cf_clearance", "x")]))

        NoDriverSolver(settle=0.0).solve("https://site.test/")

        assert page.settles == 1

    def test_an_exhausted_timeout_still_returns_what_the_browser_has(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A page that never stops looking challenged may still have set the cookie, and
        # discarding it would re-launch a browser to earn one we already hold.
        page = FakePage([CHALLENGE_BODY])
        install_nodriver(monkeypatch, FakeBrowser(page, [FakeCookie("cf_clearance", "x")]))

        result = NoDriverSolver(settle=0.0).solve("https://site.test/", timeout=0.0)

        assert result.cleared
        assert page.settles == 0

    def test_a_browser_that_reports_no_user_agent_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The clearance is bound to the User-Agent. Returning the cookies without it
        # produces a clearance that is rejected on first use, which reads as the solver
        # being broken rather than as the result being incomplete.
        page = FakePage([CLEARED_PAGE], user_agent="")
        install_nodriver(monkeypatch, FakeBrowser(page, [FakeCookie("cf_clearance", "x")]))

        with pytest.raises(SolveError, match="User-Agent"):
            NoDriverSolver(settle=0.0).solve("https://site.test/")

    def test_a_browser_that_will_not_close_does_not_lose_the_clearance(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        page = FakePage([CLEARED_PAGE])
        browser = FakeBrowser(page, [FakeCookie("cf_clearance", "x")], stop_raises=True)
        install_nodriver(monkeypatch, browser)

        assert NoDriverSolver(settle=0.0).solve("https://site.test/").cleared

    def test_a_solve_holds_the_solvers_lock(self, monkeypatch: pytest.MonkeyPatch):
        """Two browsers racing for one profile directory corrupt it.

        The profile is what carries accumulated history forward, so the serialisation
        is not a nicety. Asserted from inside the launch, which is the window that
        matters.
        """
        solver = NoDriverSolver(settle=0.0)
        browser = FakeBrowser(FakePage([CLEARED_PAGE]), [FakeCookie("cf_clearance", "x")])
        held: Dict[str, bool] = {}
        module = types.ModuleType("nodriver")

        async def start(**kwargs: Any) -> FakeBrowser:
            # The coroutine runs on a private thread, so a held lock is visible here.
            held["locked"] = solver._lock.locked()  # noqa: SLF001 - the lock is under test
            return browser

        setattr(module, "start", start)
        monkeypatch.setitem(sys.modules, "nodriver", module)

        solver.solve("https://site.test/")

        assert held["locked"] is True
        assert not solver._lock.locked(), "the lock has to be released afterwards"  # noqa: SLF001

    def test_a_missing_nodriver_names_the_version_floor(self, monkeypatch: pytest.MonkeyPatch):
        """The message has to say *why* it is unavailable, not just that it is.

        Before Python 3.10 nodriver cannot be imported at all, and the raw failure is a
        TypeError from inside the dependency with nothing pointing at the version.
        """
        monkeypatch.setitem(sys.modules, "nodriver", None)

        with pytest.raises(MissingDependency) as info:
            NoDriverSolver().solve("https://site.test/")

        assert "browser" in str(info.value)
        assert "3.10" in str(info.value)


def test_solve_errors_are_distinguishable():
    assert issubclass(SolveError, Exception)


def test_a_nodriver_that_cannot_be_imported_names_the_supported_range(monkeypatch):
    """Neither failure is an ImportError.

    Below 3.10 nodriver's module body evaluates `str | Path`; from 3.14 its generated
    cdp/network.py fails to tokenize on a stray non-UTF-8 byte. Both used to surface
    raw from inside a dependency.
    """
    import builtins

    from scraper.browser import NoDriverSolver
    from scraper.exceptions import MissingDependency

    real_import = builtins.__import__
    for error in (
        ImportError("no nodriver"),
        SyntaxError("Non-UTF-8 code"),
        TypeError("str | Path"),
    ):

        def broken(name, *args, _error=error, **kwargs):
            if name == "nodriver":
                raise _error
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", broken)
        with pytest.raises(MissingDependency, match="3.10 to 3.13"):
            NoDriverSolver().solve("https://example.com/")
