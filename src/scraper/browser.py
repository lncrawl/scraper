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

Two details are load-bearing and easy to get wrong:

**Headless is the wrong default.** A headless build reports a software renderer
for WebGL, which is a clear indicator on its own. The default here launches
headed; on a server, run it under a virtual display rather than turning headless
back on.

**WebRTC has to be off.** A STUN request can expose the host's real address even
when every HTTP request goes through the proxy — which unbinds the identity by
leaking past it, silently.

The solver a site needs is not always the one bundled here, so
:class:`BrowserSolver` is a two-method protocol and anything satisfying it plugs
in: a patched Chromium driver, a Firefox build that drives over a non-CDP
protocol, or a paid solving service.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from .exceptions import MissingDependency, ScraperError
from .identity import Clearance, Identity

logger = logging.getLogger(__name__)

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

_STILL_CHALLENGED = re.compile(
    r"__cf_chl_|cf_chl_opt|challenge-platform/h/|just a moment|checking your browser",
    re.IGNORECASE,
)
"""Whether the page in the browser is still an interstitial.

The `/h/` matters here for a second reason beyond correctness. Cloudflare injects a
JavaScript-Detections script from `challenge-platform/scripts/…` into ordinary pages,
so matching the bare path means this never reports "cleared" — and the solve loop then
burns the entire timeout on every single solve, including the successful ones.
"""


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


class BrowserSolver:
    """Drives a real browser through a challenge.

    Implementations must honour *proxy* exactly. Solving on a different address
    than the one the requests will use produces a clearance that is dead on
    arrival — the single most common way this pattern is implemented wrong.
    """

    name = "browser"

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        raise NotImplementedError

    def close(self) -> None:
        """Release anything long-lived. Called when the scraper closes."""


class CallableSolver(BrowserSolver):
    """Wraps a plain function as a solver.

    The escape hatch for anything not bundled: a Firefox-based anti-detect build,
    a patched Playwright, a paid solving API. The callable receives the same
    arguments as :meth:`BrowserSolver.solve` and returns a :class:`SolveResult`.
    """

    def __init__(self, func: Callable[..., SolveResult], *, name: str = "callable") -> None:
        self._func = func
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


class NoDriverSolver(BrowserSolver):
    """Solves with `nodriver <https://github.com/ultrafunkamsterdam/nodriver>`_.

    Chosen as the bundled default because it drives a real Chrome over its own
    interface rather than through the standard automation path that detectors
    target directly. What it does *not* do is synthesise mouse, scroll or
    keystroke dynamics — so it clears the control-channel layer and leaves the
    behavioural one entirely to :mod:`scraper.pacing`. That division is why the
    two are separate modules.

    Args:
        headless: Left ``False`` on purpose. See the module docstring.
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
            return _run_async(self._solve(url, proxy, profile_dir, timeout))

    async def _solve(
        self,
        url: str,
        proxy: Optional[str],
        profile_dir: Optional[Path],
        timeout: float,
    ) -> SolveResult:
        try:
            import nodriver  # pyright: ignore[reportMissingImports] - absent outside 3.10-3.13
        except (ImportError, SyntaxError, TypeError) as exc:
            # Neither failure is an ImportError. Before 3.10 nodriver's module body
            # evaluates `str | Path`, a TypeError; from 3.14 its generated
            # cdp/network.py fails to tokenize on a stray non-UTF-8 byte, a
            # SyntaxError. Uncaught, both surface from inside a dependency with
            # nothing naming the supported range.
            raise MissingDependency(
                "browser", "solving a challenge with nodriver (needs Python 3.10 to 3.13)"
            ) from exc

        flags = [
            # A STUN request reaches the network directly and reports the host's
            # real address, which unbinds the identity without any request failing.
            "--disable-webrtc",
            "--disable-features=WebRtcHideLocalIpsWithMdns",
        ]
        if proxy:
            flags.append(f"--proxy-server={proxy}")
        flags.extend(self._extra)

        browser = await nodriver.start(
            headless=self._headless,
            browser_args=flags,
            user_data_dir=str(profile_dir) if profile_dir else None,
        )
        try:
            page = await browser.get(url)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await page.sleep(self._settle)
                content = await page.get_content()
                if not _STILL_CHALLENGED.search(content or ""):
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


async def _harvest_cookies(browser: object) -> "tuple[Dict[str, str], float]":
    """Collect cookies and the earliest expiry among the clearance ones."""
    jar = getattr(browser, "cookies", None)
    if jar is None:
        return {}, 0.0
    raw = await jar.get_all()  # pyright: ignore[reportAttributeAccessIssue] - nodriver is optional
    cookies: Dict[str, str] = {}
    soonest = 0.0
    for cookie in raw or []:
        name = str(getattr(cookie, "name", "") or "")
        if not name:
            continue
        cookies[name] = str(getattr(cookie, "value", "") or "")
        expires = float(getattr(cookie, "expires", 0) or 0)
        if name in _SOLVED_COOKIES and expires > 0:
            soonest = expires if soonest == 0.0 else min(soonest, expires)
    return cookies, soonest


def _run_async(coro: object) -> SolveResult:
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
    if not isinstance(value, SolveResult):
        raise SolveError("the solver returned no result")
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
