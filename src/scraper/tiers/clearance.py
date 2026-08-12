"""Solve once with a real browser, then reuse on the cheap transport.

This is the dominant production pattern for a challenged site, and its correctness
rests on one fact: a clearance is bound to the address, User-Agent and TLS
fingerprint that earned it. So the browser and the requests that follow are not two
independent things that happen to run in sequence — they are one identity, and the
solve is an expensive way of upgrading it.

Which is why this tier does not own a transport. It owns a *solver*, and delegates
every actual request to the direct tier. The alternative shape, where the browser
tier fetches pages itself, both wastes a browser on pages that no longer need one
and quietly allows the solve and the fetch to run on different identities.

The failure this structure makes impossible is worth naming, because it is the usual
way the pattern is implemented wrong: solving on one exit and fetching from another
produces a clearance that is rejected on first use, which reads as "the solver does
not work" and leads to re-solving forever.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

import requests

from ..browser import BrowserSolver, profile_dir_for
from ..exceptions import TierUnavailable
from ..identity import Clearance, Identity
from ..utils.url_tools import extract_base
from .base import Call, Tier
from .direct import DirectTier

logger = logging.getLogger(__name__)

MAX_HELD = 64
"""Origins to keep a solved clearance in memory for.

Every solve added an entry keyed by origin and nothing ever removed one, so a
long-running process accumulated one per site it had ever cleared, each holding cookies
long past their expiry. The cap is a backstop; expiry does the real work.
"""


class ClearanceTier(Tier):
    """Obtains a clearance with a browser, then serves through :class:`DirectTier`.

    Args:
        solver: What drives the browser.
        direct: The tier every request actually goes through, before and after the
            solve.
        store: Called with each new clearance so it outlives the process. A solve
            is the most expensive thing this library does, and repeating it every
            run is the difference between one browser launch and one per session.
        profile_root: Root for per-address browser profile directories. One
            directory per exit, because cookie and session age are behavioural
            signals and they belong to the address that accrued them.
        solve_timeout: How long to let a browser work before giving up.
        interactive_solve_timeout: The budget instead of *solve_timeout* when the
            solver says a person can reach its window.
    """

    name = "clearance"

    def __init__(
        self,
        solver: BrowserSolver,
        direct: DirectTier,
        *,
        store: Optional[Callable[[str, Clearance], None]] = None,
        profile_root: Optional[Path] = None,
        solve_timeout: float = 90.0,
        interactive_solve_timeout: float = 300.0,
    ) -> None:
        self.solver = solver
        self.direct = direct
        self._store = store
        self._profile_root = profile_root
        self._solve_timeout = solve_timeout
        self._interactive_solve_timeout = interactive_solve_timeout
        self._lock = threading.Lock()
        self._held: dict = {}

    def send(self, call: Call) -> requests.Response:
        return self.direct.send(self._cleared(call))

    @contextmanager
    def stream(self, call: Call) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        with self.direct.stream(self._cleared(call)) as pair:
            yield pair

    def close(self) -> None:
        self.solver.close()

    # -- the solve ------------------------------------------------------------------

    def _cleared(self, call: Call) -> Call:
        """Return *call* carrying a clearance valid for its identity."""
        existing = call.clearance
        if existing is not None and existing.usable_by(call.identity):
            return call

        if existing is not None:
            logger.debug("re-solving %s: %s", call.url, existing.why_not(call.identity))

        clearance, identity = self.solve(
            call.url,
            call.identity,
            proxies=call.proxies,
            browser_proxy=call.browser_proxy,
        )
        call.clearance = clearance
        # The identity changed, because the browser's User-Agent is now part of it.
        # Handing back the old one would send the clearance under a token it was not
        # issued for, which is the exact mismatch this tier exists to prevent.
        call.identity = identity
        return call

    def solve(
        self,
        url: str,
        identity: Identity,
        *,
        proxies: Optional[dict] = None,
        browser_proxy: Optional[Callable[[], Optional[str]]] = None,
    ) -> Tuple[Clearance, Identity]:
        """Run the browser and return the clearance with the identity it belongs to.

        Serialised per tier: two browsers racing for one profile directory corrupt
        it, and the profile is what carries accumulated history forward.
        """
        origin = extract_base(url)
        proxy = self._browser_address(url, proxies, browser_proxy)

        with self._lock:
            # Another thread may have solved this origin while this one waited, and
            # a solve is expensive enough that checking is worth the branch.
            held = self._held.get(origin)
            if held is not None:
                clearance, pinned = held
                if clearance.usable_by(pinned):
                    return clearance, pinned

            # Only the budget is decided here. A solver may open its window at the start,
            # at the end, or not at all — an `auto` one shows it only after an unattended
            # attempt has failed — so announcing it from out here would have been a guess,
            # and usually a wrong one. The solver says so at the moment it opens one.
            interactive = bool(getattr(self.solver, "interactive", False))
            result = self.solver.solve(
                url,
                proxy=proxy,
                profile_dir=profile_dir_for(self._profile_root, identity.exit_id),
                timeout=(self._interactive_solve_timeout if interactive else self._solve_timeout),
            )
            if not result.cleared:
                raise TierUnavailable(
                    self.name,
                    f"{self.solver.name} finished without a clearance cookie",
                    url,
                )
            clearance = result.as_clearance(origin, identity)
            if clearance.expired:
                raise TierUnavailable(
                    self.name,
                    f"{self.solver.name} returned a clearance that had already expired",
                    url,
                )
            pinned = identity.pin(result.user_agent)
            self._prune_locked()
            self._held[origin] = (clearance, pinned)

        if self._store is not None:
            self._store(origin, clearance)
        logger.info(
            "solved %s with %s; clearance valid for %.0fs on %s",
            origin,
            self.solver.name,
            max(0.0, clearance.expires_at - clearance.issued_at),
            pinned.describe(),
        )
        return clearance, pinned

    def _browser_address(
        self,
        url: str,
        proxies: Optional[dict],
        resolve: Optional[Callable[[], Optional[str]]],
    ) -> Optional[str]:
        """Where the browser leaves from, or a refusal to launch one.

        The lease's own proxy URL is not always usable: a pool endpoint carries the
        session key as userinfo and no browser can send one. Asking the exit pool is
        what turns that into a credential-free address *on the same instance*, which
        is the part that matters — an address that merely works would earn a
        clearance the requests replaying it cannot use.

        So when there is no such address, this tier is unavailable rather than
        launched anyway. Solving from somewhere the clearance will not be replayed
        from produces a cookie rejected on first use, which reads as a broken solver
        and provokes re-solving forever.
        """
        if resolve is None:
            # Nobody offered to answer — `solve()` called directly, or a caller
            # assembling its own Call. The lease URL is the honest best guess, and
            # `chrome_proxy` refuses it if it carries a credential.
            return (proxies or {}).get("https") or (proxies or {}).get("http")
        address = resolve()
        if address is None:
            raise TierUnavailable(
                self.name,
                "no address a browser can leave by: this exit needs a credential and a "
                "browser cannot send one",
                url,
            )
        return address

    def _prune_locked(self) -> None:
        """Forget expired clearances, then the oldest, until under the cap."""
        for origin in [key for key, (held, _) in self._held.items() if held.expired]:
            del self._held[origin]
        while len(self._held) >= MAX_HELD:
            self._held.pop(next(iter(self._held)))
