"""The solver contract, and the parts every solver shares.

This module stopped driving a browser when the driver library went: that is
`scraper.cdp`'s job now, and `tests/test_cdp.py` covers it. What is left here is the
contract — what a `SolveResult` binds to, what the base class refuses to pretend it can
do — and the profile bookkeeping, which is not solver-specific and is how a clean exit
would inherit a burnt one's session if it were wrong.
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
