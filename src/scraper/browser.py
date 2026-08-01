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
tell. The solver now strips the token itself, and **headless is the default** — most
places this runs have no display to put a window on, and the ones that do get one by
asking. A visible window is worth opening only where somebody can reach into it, which
is why asking for it also buys the interactive solve budget.

**In a container, set the clock before blaming anything else.** A container is UTC
unless told otherwise, and a browser whose timezone disagrees with where its address
geolocates is read as automation. Same six challenged hosts, same binary, same egress
address: **1 of 6 under UTC and 6 of 6 with ``TZ`` matching the exit.** Nothing else
measured came close — a full font set was worth one host, and the browser's version
none at all.

**The browser build shows through too, and a virtual display does not hide it.**
Debian's ``chromium`` cleared 1 of the same 6 even with the clock corrected, because
it omits the ``Google Chrome`` brand from ``Sec-CH-UA``, which every request carries.
That is a property of the binary rather than of how it is displayed, so reaching for
Xvfb is answering the wrong question. Install the browser a real visitor would run.

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

This module defines the contract and the parts every solver shares; the bundled
implementation that speaks to a browser is :mod:`scraper.cdp`. :class:`BrowserSolver`
is a small protocol and anything satisfying it plugs in — a patched Chromium build, a
Firefox driven over a non-CDP protocol, or a paid solving service.

A browser has a second use, and it is not escalation. When a page's HTML is a shell
that JavaScript fills in, nothing is blocking and no layer is binding — a clearance
does not help, because plain HTTP with the cookie returns the same empty shell. That
is what :meth:`BrowserSolver.render` is for, and why it is not a tier.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from .exceptions import ScraperError, TierUnavailable
from .identity import Clearance, Identity

logger = logging.getLogger(__name__)

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


def chrome_proxy(url: str) -> str:
    """*url* in the form Chrome's ``--proxy-server`` accepts, or refuse to guess.

    Two ways a perfectly good proxy URL is not one Chrome can use, and neither says so:

    **``socks5h`` is not a scheme it knows.** The flag is rejected whole and every
    navigation then fails with ``ERR_NO_SUPPORTED_PROXIES``. Safe to translate, because
    Chrome's ``socks5`` already resolves names at the proxy — which is all the ``h``
    ever asked for.

    **Credentials cannot travel at all.** Chrome implements no SOCKS5
    username/password authentication, and userinfo in the flag makes it reject the
    whole thing regardless of scheme — measured, including for ``http://``. Dropping
    the credentials and launching anyway would be worse than failing: for a pool
    endpoint the username *is* the session key, so the browser would leave by a
    different exit than the requests that go on to replay its clearance, and the
    clearance would be dead on arrival. That reads as "the solver does not work", and
    the retry it provokes re-solves forever.
    """
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise TierUnavailable(
            "browser",
            "this proxy needs credentials and a browser cannot send them; solve "
            "through an address that does not, or turn the browser tier off",
        )
    scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(parsed.scheme, parsed.scheme)
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "", "", ""))


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
        flags.append(f"--proxy-server={chrome_proxy(proxy)}")
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
