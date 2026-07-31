"""Paying a real browser once, so the cheap transport can be reused after.

Layers 6, 7, 9, 10 and 13 are where a pure HTTP client stops. Injected JavaScript
collects canvas and WebGL hashes, AudioContext output, font enumeration, screen
geometry and engine timing; separately, the automation control channel leaves
artifacts that survive patching surface values like ``navigator.webdriver``. That
surface is emitted, but it is high-dimensional and tightly coupled, and the
practical way to make dozens of probes agree with each other is to run something
that genuinely is a browser.

The pattern here is solve-once-and-reuse, and it exists because a challenge result
is not portable. The clearance is bound to the address, User-Agent and TLS
fingerprint that earned it, so what comes back from a solve is not just cookies
but the identity they belong to. Everything after the solve is ordinary cheap
requests on that same identity until the cookie expires.

Four details are load-bearing and easy to get wrong:

**Headless costs nothing once the browser stops announcing it.** This used to say a
headless build gives itself away through a software WebGL renderer, and to run a
virtual display on a server instead. Measured over six challenged hosts, both claims
were wrong. What gives headless away is one substring: its User-Agent says
``HeadlessChrome``. With the token left in, none of the six cleared; with it removed,
all six cleared, as fast as headed. Forcing the software renderer on a machine that
has a GPU changed nothing — all six still cleared — so the renderer was never the
tell. The solver now strips the token itself, and headless is a fair choice.

**The browser build shows through, and a virtual display does not hide it.** In a
container running Debian's ``chromium``, nothing cleared: not headless, not headless
with the User-Agent fixed, and not headed under Xvfb. That build omits the ``Google
Chrome`` brand from ``Sec-CH-UA``, which every request carries, so it is a property
of the binary rather than of how it is displayed. Install the browser a real visitor
would run; reaching for Xvfb to fix this is answering the wrong question.

**WebRTC has to be off.** A STUN request can expose the host's real address even
when every HTTP request goes through the proxy — which unbinds the identity by
leaking past it, silently.

**So does the automation flag.** Blink otherwise sets ``navigator.webdriver`` to
true, which is one boolean saying "this is automated" that every detector reads and
nothing else on the page can argue with. Measured while building the CDP solver:
without ``--disable-blink-features=AutomationControlled`` it cleared none of six
challenged hosts, spending the full 60s on each; with it, the first two cleared in
under nine seconds. A driver library may set this for you — this library does not
rely on that, since the cost of it being absent is every solve failing slowly.

The solver a site needs is not always the one bundled here, so
:class:`BrowserSolver` is a small protocol and anything satisfying it plugs in: a
patched Chromium driver, a Firefox build that drives over a non-CDP protocol, or a
paid solving service.

A browser has a second use, and it is not escalation. When a page's HTML is a shell
that JavaScript fills in, nothing is blocking and no layer is binding — a clearance
does not help, because plain HTTP with the cookie returns the same empty shell. That
is what :meth:`BrowserSolver.render` is for, and why it is not a tier.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Type, TypeVar

from .diagnosis import is_still_challenged
from .exceptions import MissingDependency, ScraperError, TierUnavailable
from .identity import Clearance, Identity

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_RENDER_POLL = 0.25
"""How often to ask whether a rendering page is done.

Short because the whole point of a ``wait_for`` selector is that the wait ends on
evidence: a page that hydrates in 200ms should cost 200ms, not a settle interval
chosen for pages with no selector at all."""

CLEARANCE_FALLBACK_TTL = 900.0
"""Assumed clearance lifetime when the cookie carries no expiry.

Deliberately shorter than the platform default, which the site operator can
configure anyway. Over-estimating means requests are sent with a dead cookie and
the resulting challenge reads as a solver failure.
"""

MAX_PROFILES = 8
"""Browser profile directories to keep under the profile root.

A profile is tens of megabytes, and a proxied exit id carries a session key — so every
rotation that reaches a solve leaves another one behind, without limit. Keying them
coarsely is not the alternative: for a pool endpoint the URL is constant while the exit
IP is not, so one shared profile would hand a fresh session the history of a burnt exit.
"""

_PROFILE_GRACE = 300.0
"""Seconds a profile is left alone regardless of the cap.

Two scrapers can share a data dir, and each solver only serialises against itself, so
the directory being deleted must not be one another process has a browser in.
"""

_SOLVED_COOKIES = ("cf_clearance", "__cf_bm", "cf_chl_rc_ni")

HEADLESS_TOKEN = "HeadlessChrome/"
"""The substring a headless Chrome puts in its own User-Agent.

The entire headless penalty, measured: with it present none of a corpus of
challenged hosts cleared, and with it gone all of them did. See the module docstring.
"""


def launch_flags(
    proxy: Optional[str] = None,
    *,
    user_agent: str = "",
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """The command line every bundled solver starts Chrome with.

    Shared rather than copied per solver because the WebRTC pair is load-bearing and
    omitting it fails silently: nothing errors, no request is refused, the address
    simply stops being the one the identity was built on.
    """
    flags = [
        # A STUN request reaches the network directly and reports the host's
        # real address, which unbinds the identity without any request failing.
        "--disable-webrtc",
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        # Without this Blink sets `navigator.webdriver` to true, which is a single
        # boolean saying "automated" that every detector reads. Measured: a browser
        # driven over CDP without it cleared none of three challenged hosts in 60s
        # each, and all three with it.
        "--disable-blink-features=AutomationControlled",
    ]
    if user_agent:
        flags.append(f"--user-agent={user_agent}")
    if proxy:
        flags.append(f"--proxy-server={proxy}")
    flags.extend(extra or ())
    return flags


def honest_user_agent(reported: str) -> str:
    """*reported* with the headless giveaway taken out.

    Applied as a launch flag by every solver here, never through
    ``Network.setUserAgentOverride`` — the override looks equivalent and is not, since
    it suppresses ``Sec-CH-UA`` outright and trades a browser that admits to being
    headless for one that claims to be Chrome and sends no brands at all.
    """
    return reported.replace(HEADLESS_TOKEN, "Chrome/")


def clearance_deadline(expiries: Dict[str, float]) -> float:
    """The soonest expiry among the cookies a clearance actually rests on, or ``0``.

    Shared between solvers because disagreeing here gives the clearance the wrong
    lifetime, and the expensive direction is quiet: too long, and every request after
    the real expiry goes out with a dead cookie, so the challenge that comes back
    reads as the solver having failed rather than as the clock having run out.
    """
    soonest = 0.0
    for name, expires in expiries.items():
        if name in _SOLVED_COOKIES and expires > 0:
            soonest = expires if soonest == 0.0 else min(soonest, expires)
    return soonest


class SolveResult(NamedTuple):
    """What a browser brings back from a challenge.

    Args:
        user_agent: The browser's exact User-Agent. Required, not optional: the
            clearance is bound to it, so replaying the cookies without
            reproducing this string cannot work.
        expires_at: UNIX seconds, ``0`` when unknown.
    """

    cookies: Dict[str, str]
    user_agent: str
    expires_at: float = 0.0

    @property
    def cleared(self) -> bool:
        return any(name in self.cookies for name in _SOLVED_COOKIES)

    def as_clearance(self, origin: str, identity: Identity) -> Clearance:
        """Bind this result to the identity that will replay it."""
        pinned = identity.pin(self.user_agent)
        return Clearance(
            origin=origin,
            cookies=dict(self.cookies),
            identity_token=pinned.token(),
            user_agent=self.user_agent,
            expires_at=self.expires_at or (time.time() + CLEARANCE_FALLBACK_TTL),
        )


class SolveError(ScraperError):
    """A browser ran but did not come back with a clearance."""


class RenderError(ScraperError):
    """A browser ran but the page never produced what was asked for."""


class BrowserSolver:
    """Drives a real browser through a challenge, and renders a page on request.

    Implementations must honour *proxy* exactly. Solving on a different address
    than the one the requests will use produces a clearance that is dead on
    arrival — the single most common way this pattern is implemented wrong.
    """

    name = "browser"

    interactive = False
    """Whether a person can reach this browser and finish a challenge by hand.

    The solve loop already detects success by polling, and does not care who cleared
    the page — so a human needs no protocol of their own, only enough time. What this
    buys is :attr:`ScraperConfig.interactive_solve_timeout` instead of the unattended
    budget. True of a visible window on a desktop; false of a server, a container, and
    of a solving service that has no window at all.
    """

    impersonation = "chrome"
    """Which impersonation profile a clearance from this solver binds to.

    Read by :meth:`ScraperConfig.profile` to decide what every request to every origin
    then presents, because a clearance is bound to a TLS fingerprint as much as to a
    User-Agent and the two have to agree. Override it in a solver driving something
    other than Chrome; the default is what every bundled solver drives, and the
    conservative guess for a service whose browser is not knowable from here.
    """

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        raise NotImplementedError

    def render(
        self,
        url: str,
        *,
        wait_for: Optional[str] = None,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> str:
        """Return *url*'s HTML after the page has run.

        Args:
            wait_for: A CSS selector the content is behind. Polled for until it
                exists, and :class:`RenderError` if it never does — returning the
                shell instead would hand the caller a page that parses to nothing
                and reports no error, which is the failure this exists to prevent.

        Optional, unlike :meth:`solve`: a solving service can answer a challenge
        without being able to render anything, and it says so here rather than
        pretending with an empty page.
        """
        raise TierUnavailable(self.name, "this solver cannot render a page", url)

    def close(self) -> None:
        """Release anything long-lived. Called when the scraper closes."""


class CallableSolver(BrowserSolver):
    """Wraps a plain function as a solver.

    The escape hatch for anything not bundled: a Firefox-based anti-detect build,
    a patched Playwright, a paid solving API. The callable receives the same
    arguments as :meth:`BrowserSolver.solve` and returns a :class:`SolveResult`.

    Args:
        renderer: Optional, and separate because the two capabilities are
            independent — a solving API answers challenges and renders nothing,
            while a headless browser may do the reverse.
    """

    def __init__(
        self,
        func: Callable[..., SolveResult],
        *,
        name: str = "callable",
        renderer: Optional[Callable[..., str]] = None,
    ) -> None:
        self._func = func
        self._renderer = renderer
        self.name = name

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        return self._func(url, proxy=proxy, profile_dir=profile_dir, timeout=timeout)

    def render(
        self,
        url: str,
        *,
        wait_for: Optional[str] = None,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> str:
        if self._renderer is None:
            return super().render(
                url, wait_for=wait_for, proxy=proxy, profile_dir=profile_dir, timeout=timeout
            )
        return self._renderer(
            url, wait_for=wait_for, proxy=proxy, profile_dir=profile_dir, timeout=timeout
        )


class NoDriverSolver(BrowserSolver):
    """Solves with `nodriver <https://github.com/ultrafunkamsterdam/nodriver>`_.

    Chosen as the bundled default because it drives a real Chrome over its own
    interface rather than through the standard automation path that detectors
    target directly. What it does *not* do is synthesise mouse, scroll or
    keystroke dynamics — so it clears the control-channel layer and leaves the
    behavioural one entirely to :mod:`scraper.pacing`. That division is why the
    two are separate modules.

    Args:
        headless: Still ``False`` by default, but no longer because headless cannot
            clear — see the module docstring. A headed window is the one a person can
            reach in and solve, which is worth keeping as the default where there is
            a display; on a server there is nobody to reach in and headless is right.
        args: Extra command-line flags, appended after the defaults.
    """

    name = "nodriver"

    def __init__(
        self,
        *,
        headless: bool = False,
        args: Optional[List[str]] = None,
        settle: float = 3.0,
    ) -> None:
        self._headless = headless
        self._extra = list(args or [])
        self._settle = settle
        self._lock = threading.Lock()
        self._user_agent: Optional[str] = None
        # A visible window is one somebody can solve by hand. Whether anybody is in
        # front of it is a property of the deployment, not of any single call, which
        # is why this is decided once here rather than passed to `solve`.
        self.interactive = not headless

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        # One browser at a time per solver: two headed Chromes racing for the same
        # profile directory corrupt it, and the profile is what carries the
        # accumulated history forward.
        with self._lock:
            return _run_async(self._solve(url, proxy, profile_dir, timeout), SolveResult)

    def render(
        self,
        url: str,
        *,
        wait_for: Optional[str] = None,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> str:
        with self._lock:
            return _run_async(self._render(url, wait_for, proxy, profile_dir, timeout), str)

    def _flags(self, proxy: Optional[str], user_agent: str = "") -> List[str]:
        return launch_flags(proxy, user_agent=user_agent, extra=self._extra)

    async def _honest_user_agent(self) -> str:
        """The User-Agent to launch headless under, or ``""`` when headed.

        A headless Chrome announces itself: its User-Agent says ``HeadlessChrome``,
        and that one substring is the whole of the headless penalty. Measured over
        six challenged hosts on a desktop, twice each: headless cleared none of them
        and cleared all of them once the token was gone, at the same speed as headed.

        Applied as a launch flag rather than through ``Network.setUserAgentOverride``,
        which looks equivalent and is not — the override suppresses the ``Sec-CH-UA``
        request header outright, so it trades a browser that admits to being headless
        for one that claims to be Chrome and sends no brands at all.

        Learned by launching once and reading it, because the flag has to be set
        before the browser exists and the string is specific to this build and
        platform. Cached for the life of the solver, so the extra launch is paid once
        per process, and never when running headed.
        """
        if not self._headless:
            return ""
        if self._user_agent is None:
            nodriver = _import_nodriver()
            browser = await nodriver.start(headless=True, browser_args=self._flags(None))
            try:
                page = await browser.get("about:blank")
                reported = str(await page.evaluate("navigator.userAgent") or "")
            finally:
                try:
                    browser.stop()
                except Exception:  # noqa: BLE001 - see _solve
                    logger.debug("nodriver did not shut down cleanly", exc_info=True)
            self._user_agent = honest_user_agent(reported)
            if not reported:
                logger.debug("could not read the browser's User-Agent; launching as-is")
        return self._user_agent

    async def _render(
        self,
        url: str,
        wait_for: Optional[str],
        proxy: Optional[str],
        profile_dir: Optional[Path],
        timeout: float,
    ) -> str:
        nodriver = _import_nodriver()
        user_agent = await self._honest_user_agent()
        browser = await nodriver.start(
            headless=self._headless,
            browser_args=self._flags(proxy, user_agent),
            user_data_dir=str(profile_dir) if profile_dir else None,
        )
        try:
            page = await browser.get(url)
            if wait_for is None:
                # Nothing to poll for, so the only thing standing in for "the page
                # has run" is time. With a selector the wait is evidence-based and
                # this delay is dead time.
                await page.sleep(self._settle)

            deadline = time.monotonic() + timeout
            content = ""
            while True:
                content = str(await page.get_content() or "")
                if not is_still_challenged(content):
                    if wait_for is None:
                        return content
                    found = await page.evaluate(f"!!document.querySelector({json.dumps(wait_for)})")
                    if found:
                        return content
                if time.monotonic() >= deadline:
                    break
                await page.sleep(_RENDER_POLL)
        finally:
            try:
                browser.stop()
            except Exception:  # noqa: BLE001 - a browser that will not close must not mask the result
                logger.debug("nodriver did not shut down cleanly", exc_info=True)

        missing = f"{wait_for} never appeared" if wait_for else "it was still a challenge"
        raise RenderError(f"{url} did not render after {timeout:.0f}s: {missing}")

    async def _solve(
        self,
        url: str,
        proxy: Optional[str],
        profile_dir: Optional[Path],
        timeout: float,
    ) -> SolveResult:
        nodriver = _import_nodriver()
        user_agent = await self._honest_user_agent()
        browser = await nodriver.start(
            headless=self._headless,
            browser_args=self._flags(proxy, user_agent),
            user_data_dir=str(profile_dir) if profile_dir else None,
        )
        try:
            page = await browser.get(url)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await page.sleep(self._settle)
                content = await page.get_content()
                if not is_still_challenged(content or ""):
                    break
            user_agent = str(await page.evaluate("navigator.userAgent") or "")
            cookies, expires_at = await _harvest_cookies(browser)
        finally:
            try:
                browser.stop()
            except Exception:  # noqa: BLE001 - a browser that will not close must not mask the result
                logger.debug("nodriver did not shut down cleanly", exc_info=True)

        result = SolveResult(cookies=cookies, user_agent=user_agent, expires_at=expires_at)
        if not user_agent:
            raise SolveError(
                "the browser did not report a User-Agent; the clearance is bound to it "
                "and cannot be replayed without it"
            )
        return result


def _import_nodriver() -> Any:
    try:
        import nodriver  # pyright: ignore[reportMissingImports] - absent outside 3.10-3.13
    except (ImportError, SyntaxError, TypeError) as exc:
        # Neither failure is an ImportError. Before 3.10 nodriver's module body
        # evaluates `str | Path`, a TypeError; from 3.14 its generated
        # cdp/network.py fails to tokenize on a stray non-UTF-8 byte, a
        # SyntaxError. Uncaught, both surface from inside a dependency with
        # nothing naming the supported range.
        raise MissingDependency(
            "browser", "driving a browser with nodriver (needs Python 3.10 to 3.13)"
        ) from exc
    return nodriver


async def _harvest_cookies(browser: object) -> "tuple[Dict[str, str], float]":
    """Collect cookies and the earliest expiry among the clearance ones."""
    jar = getattr(browser, "cookies", None)
    if jar is None:
        return {}, 0.0
    raw = await jar.get_all()  # pyright: ignore[reportAttributeAccessIssue] - nodriver is optional
    cookies: Dict[str, str] = {}
    expiries: Dict[str, float] = {}
    for cookie in raw or []:
        name = str(getattr(cookie, "name", "") or "")
        if not name:
            continue
        cookies[name] = str(getattr(cookie, "value", "") or "")
        expiries[name] = float(getattr(cookie, "expires", 0) or 0)
    return cookies, clearance_deadline(expiries)


def _run_async(coro: object, expect: Type[_T]) -> _T:
    """Run *coro* to completion from synchronous code.

    Always on a private thread with a private loop. A caller may already be inside
    an event loop — a scraper driven from an async server, for instance — and
    ``asyncio.run`` on the current thread would raise there.
    """
    import asyncio

    box: Dict[str, object] = {}

    def target() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(coro)  # pyright: ignore[reportArgumentType]
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=target, name="scraper-solve", daemon=True)
    thread.start()
    thread.join()

    error = box.get("error")
    if isinstance(error, BaseException):
        raise error
    value = box.get("value")
    if not isinstance(value, expect):
        raise SolveError("the browser returned no result")
    return value


def profile_dir_for(root: Optional[Path], exit_id: str) -> Optional[Path]:
    """The browser profile directory to use for *exit_id*.

    One directory per address, which is the point rather than a detail. Cookie and
    session age are behavioural signals, so a profile reused across a run
    accumulates the history that makes the session look established — and sharing
    one profile between addresses would attach that history to whichever address
    happens to be in use, which is how a clean exit inherits a burnt one's
    session.
    """
    if root is None:
        return None
    # Keyed on the address, not the exit id. An id is `direct#<origin>` so the
    # concurrency gate and stored clearances can be per origin, but a Chrome profile is
    # tens of megabytes and a consumer with a few hundred sources would keep one each.
    address = "direct" if exit_id.startswith("direct#") else (exit_id or "direct")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", address)[:64]
    path = Path(root) / safe
    path.mkdir(parents=True, exist_ok=True)
    prune_profiles(Path(root), keep=path)
    return path


def prune_profiles(root: Path, *, keep: Optional[Path] = None) -> int:
    """Delete the least recently used profiles beyond :data:`MAX_PROFILES`.

    Returns how many were removed. Anything touched within :data:`_PROFILE_GRACE`
    is left alone even when over the cap, since it may be open in another process.
    """
    try:
        found = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return 0
    if len(found) <= MAX_PROFILES:
        return 0

    def touched(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    now = time.time()
    found.sort(key=touched, reverse=True)
    removed = 0
    for stale in found[MAX_PROFILES:]:
        if stale == keep or now - touched(stale) < _PROFILE_GRACE:
            continue
        shutil.rmtree(stale, ignore_errors=True)
        removed += 1
    if removed:
        logger.debug("removed %d stale browser profile(s) from %s", removed, root)
    return removed
