"""The solver contract, and the parts every solver shares.

This module stopped driving a browser when the driver library went: that is
`scraper.cdp`'s job now, and `tests/test_cdp.py` covers it. What is left here is the
contract — what a `SolveResult` binds to, what the base class refuses to pretend it can
do — and the profile bookkeeping, which is not solver-specific and is how a clean exit
would inherit a burnt one's session if it were wrong.
"""

from __future__ import annotations

import subprocess
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
from scraper.diagnosis import is_still_challenged
from scraper.identity import Identity

from .conftest import CHALLENGE_BODY, SERVED_WITH_JSD

CLEARED_PAGE = "<!doctype html><html><body><h1>Chapter 12</h1></body></html>"
SHELL = (
    '<!doctype html><html><body><div id="app"></div><script src="/app.js"></script></body></html>'
)


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


class TestReadingTheInterstitial:
    """The solver waits on `diagnose.is_challenge`, not on a copy of the markers.

    It used to keep its own pattern, and the copy never gained the Turnstile markers —
    so a browser watching a Turnstile page concluded on its first poll that it had
    cleared, harvested no clearance cookie, and the tier reported itself unavailable on
    the one layer it exists for.
    """

    def test_a_challenge_page_reads_as_still_challenged(self):
        assert is_still_challenged(CHALLENGE_BODY)

    def test_a_turnstile_page_does_too(self):
        assert is_still_challenged('<html><div class="cf-turnstile" data-sitekey="x"></div></html>')

    def test_the_injected_detections_script_does_not(self):
        """The `/h/` in the pattern is what separates these two bodies.

        Cloudflare injects a JavaScript-Detections script from
        `challenge-platform/scripts/…` into ordinary pages. Matching the bare path
        means the loop never reports "cleared" and burns the entire timeout on every
        solve, including the successful ones.
        """
        assert not is_still_challenged(SERVED_WITH_JSD)

    def test_a_plain_page_is_not_a_challenge(self):
        assert not is_still_challenged(CLEARED_PAGE)


def test_solve_errors_are_distinguishable():
    assert issubclass(SolveError, Exception)


def test_a_per_origin_exit_id_still_shares_one_browser_profile(tmp_path):
    """`exit_id` became `direct#<origin>` so gates and clearances are per origin.

    A Chrome profile is tens of megabytes, so following that key would leave one per
    site — twenty gigabytes for a consumer with a few hundred sources.
    """
    from scraper.browser import profile_dir_for

    a = profile_dir_for(tmp_path, "direct#a.example")
    b = profile_dir_for(tmp_path, "direct#b.example")
    assert a == b == tmp_path / "direct"
    # A real address still gets its own: a clean exit must not inherit a burnt one's
    # accumulated session.
    assert profile_dir_for(tmp_path, "res.test#s-1") != a


class TestProfilesDoNotAccumulate:
    """A proxied exit id carries a session key, so every rotation that reaches a solve
    leaves another tens-of-megabytes profile behind, and nothing used to remove one.
    """

    def _aged(self, path, seconds):
        import os
        import time

        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_the_oldest_profiles_are_removed_beyond_the_cap(self, tmp_path):
        from scraper.browser import MAX_PROFILES, profile_dir_for

        for n in range(MAX_PROFILES + 5):
            made = profile_dir_for(tmp_path, f"res.test#s-{n}")
            assert made is not None
            self._aged(made, 3600 + (MAX_PROFILES + 5 - n))

        kept = profile_dir_for(tmp_path, "res.test#s-new")
        alive = sorted(item.name for item in tmp_path.iterdir() if item.is_dir())
        assert len(alive) <= MAX_PROFILES + 1
        assert kept is not None and kept.name in alive

    def test_a_recently_used_profile_survives_the_cap(self, tmp_path):
        # Two scrapers may share a data dir, and each solver only serialises against
        # itself, so a directory another process has a browser in must not be deleted.
        from scraper.browser import MAX_PROFILES, prune_profiles

        for n in range(MAX_PROFILES + 4):
            (tmp_path / f"res.test_s-{n}").mkdir()
        assert prune_profiles(tmp_path) == 0
        assert len(list(tmp_path.iterdir())) == MAX_PROFILES + 4

    def test_nothing_is_removed_under_the_cap(self, tmp_path):
        from scraper.browser import prune_profiles

        for n in range(3):
            made = tmp_path / f"res.test_s-{n}"
            made.mkdir()
            self._aged(made, 99999)
        assert prune_profiles(tmp_path) == 0


class TestBrowserSlots:
    """Concurrency limits on launching a browser.

    A per-instance lock only protects one solver object. Nothing stops a caller building
    one per thread — a scraper is documented as thread-safe, so parallel callers are
    expected — and each instance then launches its own browser. Past what the platform
    allows, Firefox refuses `session.new` and Chrome exits at once; both reach the caller
    as "the browser exited immediately", which reads as the site refusing the request
    rather than as a local limit. Findings built on that are wrong in the confident
    direction, which is why this is bounded rather than left to the caller.
    """

    @staticmethod
    def _run(engine, hold, results, tag):
        import time

        from scraper.browser import browser_slot

        with browser_slot(engine):
            results.append(("enter", engine, tag))
            time.sleep(hold)
            results.append(("exit", engine, tag))

    def test_two_solves_on_one_engine_do_not_overlap(self):
        import threading

        results = []
        threads = [
            threading.Thread(target=self._run, args=("firefox", 0.05, results, n)) for n in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Strict alternation: enter, exit, enter, exit.
        assert [step for step, _, _ in results] == ["enter", "exit", "enter", "exit"]

    def test_different_engines_run_at_the_same_time(self):
        import threading

        # Firefox's session cap says nothing about Chrome, so queueing one behind the
        # other would halve throughput for no reason.
        results = []
        threads = [
            threading.Thread(target=self._run, args=(engine, 0.05, results, 0))
            for engine in ("firefox", "chromium")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert [step for step, _, _ in results] == ["enter", "enter", "exit", "exit"]

    def test_the_limit_can_be_raised_for_one_engine(self):
        import threading

        from scraper.browser import set_browser_slots

        try:
            set_browser_slots(2, "firefox")
            results = []
            threads = [
                threading.Thread(target=self._run, args=("firefox", 0.05, results, n))
                for n in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert [step for step, _, _ in results] == ["enter", "enter", "exit", "exit"]
        finally:
            set_browser_slots(1)

    def test_a_slot_count_below_one_is_refused(self):
        from scraper.browser import set_browser_slots

        with pytest.raises(ValueError):
            set_browser_slots(0)

    def test_waiting_too_long_is_a_tier_problem_not_a_site_problem(self):
        import threading

        from scraper.browser import browser_slot
        from scraper.exceptions import TierUnavailable

        held = threading.Event()
        release = threading.Event()

        def hold():
            with browser_slot("firefox"):
                held.set()
                release.wait(2.0)

        keeper = threading.Thread(target=hold)
        keeper.start()
        held.wait(2.0)
        try:
            # Reported as the tier being busy; a caller must not read this as the origin
            # blocking us, which is exactly the confusion the limit exists to prevent.
            with pytest.raises(TierUnavailable):
                with browser_slot("firefox", timeout=0.05):
                    pass
        finally:
            release.set()
            keeper.join()

    def test_the_bundled_solvers_name_the_browser_they_drive(self):
        from scraper.bidi import BidiSolver
        from scraper.cdp import CdpSolver

        assert BidiSolver.engine == "firefox"
        assert CdpSolver.engine == "chromium"
        assert BidiSolver.engine != CdpSolver.engine


class TestRaisingTheWindow:
    """Surfacing a headed browser, so an interactive solve is actually seen.

    Observed during an interactive solve on macOS: Firefox ran with no `-headless` flag
    and the solver reported `interactive: True`, but the window opened behind everything
    and stayed there. A browser launched from a background process gets a dock icon
    without focus, so the person who is supposed to click never learns there is anything
    to click, and the solve spends its whole budget waiting.
    """

    def test_nothing_happens_off_macos(self, monkeypatch):
        import scraper.browser as browser_mod

        calls = []
        monkeypatch.setattr(browser_mod.sys, "platform", "linux")
        monkeypatch.setattr(browser_mod.subprocess, "run", lambda *a, **k: calls.append(a))
        browser_mod.raise_window("/usr/bin/firefox")
        assert calls == []

    def test_the_app_name_comes_from_the_bundle(self, monkeypatch):
        import scraper.browser as browser_mod

        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
        monkeypatch.setattr(browser_mod.subprocess, "run", fake_run)
        browser_mod.raise_window("/Applications/Firefox.app/Contents/MacOS/firefox")
        assert 'tell application "Firefox" to activate' in seen["argv"][-1]

    def test_a_path_with_no_bundle_is_left_alone(self, monkeypatch):
        import scraper.browser as browser_mod

        calls = []
        monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
        monkeypatch.setattr(browser_mod.subprocess, "run", lambda *a, **k: calls.append(a))
        browser_mod.raise_window("/opt/homebrew/bin/firefox")
        assert calls == []

    def test_failing_to_raise_never_fails_the_solve(self, monkeypatch):
        import scraper.browser as browser_mod

        def boom(*args, **kwargs):
            raise OSError("no osascript here")

        monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
        monkeypatch.setattr(browser_mod.subprocess, "run", boom)
        # Must not propagate: a cosmetic failure cannot be allowed to lose a clearance.
        browser_mod.raise_window("/Applications/Firefox.app/Contents/MacOS/firefox")


class TestRaiseWindowIsPortable:
    """`raise_window` ships to Windows, Linux and macOS, and must be inert on two of them.

    The implementation shells out to `osascript`, which exists nowhere but macOS. A guard
    that let it run elsewhere would put a missing-binary failure directly in the solve
    path, so the platform check is pinned here rather than trusted.
    """

    @pytest.mark.parametrize("platform", ["win32", "linux", "cygwin", "freebsd7"])
    def test_no_process_is_spawned_off_macos(self, platform, monkeypatch):
        import scraper.browser as browser_mod

        spawned = []
        monkeypatch.setattr(browser_mod.sys, "platform", platform)
        monkeypatch.setattr(browser_mod.subprocess, "run", lambda *a, **k: spawned.append(a))
        for path in (
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            "/usr/bin/firefox",
            "/snap/firefox/current/usr/lib/firefox/firefox",
        ):
            browser_mod.raise_window(path)
        assert spawned == []

    def test_a_chrome_bundle_is_named_correctly(self, monkeypatch):
        import scraper.browser as browser_mod

        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
        monkeypatch.setattr(browser_mod.subprocess, "run", fake_run)
        browser_mod.raise_window("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        assert 'tell application "Google Chrome" to activate' in seen[0][-1]

    def test_a_homebrew_style_macos_path_is_skipped(self, monkeypatch):
        import scraper.browser as browser_mod

        # No .app bundle to name, and guessing one would activate the wrong thing.
        spawned = []
        monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
        monkeypatch.setattr(browser_mod.subprocess, "run", lambda *a, **k: spawned.append(a))
        browser_mod.raise_window("/opt/homebrew/bin/chromium")
        assert spawned == []


class TestRaiseWindowPerPlatform:
    """Each desktop is raised the way it allows, and none of it may reach another.

    The previous version handled macOS only, which left the other two desktops with the
    same invisible-window failure: a browser launched from a background process gets a
    taskbar entry without focus, and an interactive solve then waits out its whole budget
    on a person who was never shown a window.

    Windows and Linux cannot be exercised from here, so what is pinned instead is that the
    right mechanism is chosen, no other one is touched, and every failure path stays
    silent — a cosmetic step must never cost a clearance.
    """

    def test_windows_focuses_the_first_visible_window_of_the_process(self, monkeypatch):
        import scraper.browser as browser_mod

        monkeypatch.setattr(browser_mod.sys, "platform", "win32")
        # Nothing may shell out: Windows is driven through ctypes precisely so that no
        # external tool becomes a dependency.
        monkeypatch.setattr(
            browser_mod.subprocess, "run", lambda *a, **k: pytest.fail("spawned a process")
        )
        calls = []
        monkeypatch.setattr(browser_mod, "_raise_windows", lambda pid: calls.append(pid) or True)
        browser_mod.raise_window("C:\\firefox.exe", 4321)
        assert calls == [4321]

    def test_linux_asks_the_window_manager_and_only_if_a_tool_exists(self, monkeypatch):
        import scraper.browser as browser_mod

        monkeypatch.setattr(browser_mod.sys, "platform", "linux")
        monkeypatch.setattr(browser_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            browser_mod.subprocess, "run", lambda *a, **k: pytest.fail("ran a missing tool")
        )
        # No wmctrl, no xdotool: there is no portable way to raise a window, and inventing
        # one would be worse than doing nothing.
        browser_mod.raise_window("/usr/bin/firefox", 99)

    def test_linux_prefers_wmctrl_when_it_is_installed(self, monkeypatch):
        import scraper.browser as browser_mod

        argv_seen = []

        def fake_run(argv, **kwargs):
            argv_seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        monkeypatch.setattr(browser_mod.sys, "platform", "linux")
        monkeypatch.setattr(browser_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(browser_mod.subprocess, "run", fake_run)
        browser_mod.raise_window("/usr/bin/firefox", 4321)
        assert argv_seen[0][0] == "wmctrl"
        assert "4321" in argv_seen[0]

    def test_linux_falls_back_to_xdotool(self, monkeypatch):
        import scraper.browser as browser_mod

        argv_seen = []

        def fake_run(argv, **kwargs):
            argv_seen.append(argv)
            # wmctrl present but unable to match the window: xdotool still worth a try.
            return subprocess.CompletedProcess(argv, 0 if argv[0] == "xdotool" else 1)

        monkeypatch.setattr(browser_mod.sys, "platform", "linux")
        monkeypatch.setattr(browser_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(browser_mod.subprocess, "run", fake_run)
        browser_mod.raise_window("/usr/bin/firefox", 4321)
        assert [argv[0] for argv in argv_seen] == ["wmctrl", "xdotool"]

    def test_no_pid_means_nothing_is_attempted_off_macos(self, monkeypatch):
        import scraper.browser as browser_mod

        for platform in ("win32", "linux"):
            monkeypatch.setattr(browser_mod.sys, "platform", platform)
            monkeypatch.setattr(
                browser_mod.shutil, "which", lambda name: pytest.fail("looked for a tool")
            )
            monkeypatch.setattr(
                browser_mod.subprocess, "run", lambda *a, **k: pytest.fail("spawned")
            )
            browser_mod.raise_window("/usr/bin/firefox", None)

    def test_a_windows_failure_is_swallowed(self, monkeypatch):
        import scraper.browser as browser_mod

        # ctypes is absent or the API refuses; the solve must carry on regardless.
        monkeypatch.setattr(browser_mod.sys, "platform", "win32")
        assert browser_mod._raise_windows(1234) is False


class TestBrowserModes:
    """The three ways a solver may put a browser on screen.

    `headless` is a floor rather than a preference: it forbids a window, which also
    forbids asking a person, and that is what makes it correct for a server or container
    where a window would open into a display nobody is attached to. `headed` shows one
    every time. `auto` starts hidden and shows one only after the unattended attempt
    fails — worth doing because a corrected headless browser clears everything a headed
    one does, so a window up front spends attention to buy nothing.
    """

    def test_the_boolean_still_works_and_headless_means_no_window(self):
        from scraper.browser import resolve_mode

        assert resolve_mode(None, True) == "headless"

    def test_the_old_headed_boolean_now_means_auto(self):
        from scraper.browser import resolve_mode

        # Same capability as before — a person can still be asked — without paying for a
        # window on the solves that never needed one.
        assert resolve_mode(None, False) == "auto"

    def test_an_explicit_mode_wins_over_the_boolean(self):
        from scraper.browser import resolve_mode

        assert resolve_mode("headed", True) == "headed"
        assert resolve_mode("headless", False) == "headless"

    def test_an_unknown_mode_is_refused(self):
        from scraper.browser import resolve_mode

        with pytest.raises(ValueError):
            resolve_mode("visible", False)

    @pytest.mark.parametrize(
        "mode,interactive",
        [("headless", False), ("headed", True), ("auto", True)],
    )
    def test_only_a_showable_mode_is_interactive(self, mode, interactive, monkeypatch):
        import scraper.browser as browser_mod
        from scraper.bidi import BidiSolver

        monkeypatch.setattr(browser_mod, "has_display", lambda: True)
        assert BidiSolver(executable="/x", mode=mode).interactive is interactive

    def test_no_display_means_no_window_whatever_the_mode(self, monkeypatch):
        import scraper.bidi as bidi_mod
        from scraper.bidi import BidiSolver

        # A container has no desktop, so escalating would trade a fast honest failure for
        # a slow one and still show nobody anything.
        monkeypatch.setattr(bidi_mod, "has_display", lambda: False)
        for mode in ("auto", "headed"):
            assert BidiSolver(executable="/x", mode=mode).interactive is False

    def test_display_detection_reads_the_linux_session(self, monkeypatch):
        import scraper.browser as browser_mod

        monkeypatch.setattr(browser_mod.sys, "platform", "linux")
        monkeypatch.setattr(browser_mod.os, "environ", {})
        assert browser_mod.has_display() is False
        monkeypatch.setattr(browser_mod.os, "environ", {"WAYLAND_DISPLAY": "wayland-0"})
        assert browser_mod.has_display() is True
        monkeypatch.setattr(browser_mod.os, "environ", {"DISPLAY": ":0"})
        assert browser_mod.has_display() is True

    def test_a_desktop_platform_is_assumed_to_have_one(self, monkeypatch):
        import scraper.browser as browser_mod

        for platform in ("darwin", "win32"):
            monkeypatch.setattr(browser_mod.sys, "platform", platform)
            monkeypatch.setattr(browser_mod.os, "environ", {})
            assert browser_mod.has_display() is True
