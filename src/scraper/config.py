"""Configuration, shaped as policy rather than as knobs.

The previous generation of this library had thirty-odd settings, and most of them
were levers on layers that were rarely the binding constraint — cipher rotation,
header randomisation, per-request User-Agent choice. Tuning those is the activity
the bound says is wasted: it moves a term that is not the minimum.

So what is configurable here is the set of *capabilities* available and how patient
the run is allowed to be. Which capability gets used, and when, is decided from
evidence at runtime.

Everything has a working default. The two settings that most change what this
library can do are :attr:`ScraperConfig.exits` and :attr:`ScraperConfig.browser`,
because they are the two that add reach rather than adjust it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

import requests

from .botauth import BotAuthConfig
from .browser import BrowserSolver
from .diagnosis import Diagnosis
from .exits import ExitSpec
from .pacing import PacingPolicy
from .tiers.managed import Provider
from .transport import Transport

if TYPE_CHECKING:
    from .tiers import Tier

APP_DIR_NAME = "lncrawl-scraper"

ResponseCheck = Callable[[requests.Response, str], Optional[Diagnosis]]
"""Reads a response this library found nothing wrong with, and may overrule it.

Given the response and the decoded body already peeked at, so an implementation
neither re-reads a consumed stream nor guesses an encoding. Returns ``None`` to let
the response stand, or a :class:`~scraper.Diagnosis` to treat it as that failure
instead — after which everything downstream behaves as though the detection were
this library's own: the layer is attributed, the failure is recorded against the
origin, and the planner rotates or escalates as the diagnosis directs.

For refusals no general detector can see. A site that answers ``200`` with
``{"success": false}`` is, on the wire, indistinguishable from one that answers
``200`` with an article — the difference is in a schema only the caller knows. Such
a site otherwise reads as a run of perfect successes while every page comes back
empty, and the addresses being spent are never blamed, because nothing ever failed.
"""


def default_data_dir() -> Path:
    """Where learned state goes when the caller does not say.

    Honours ``SCRAPER_DATA_DIR`` first so a deployment can place it on a volume;
    otherwise the platform's cache location. It has to default to *somewhere* real
    rather than to nothing, because the layer this state exists for cannot be
    satisfied by a process that forgets everything on exit.
    """
    override = os.environ.get("SCRAPER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / APP_DIR_NAME


@dataclass
class ScraperConfig:
    """Everything a :class:`~scraper.Scraper` needs.

    Args:
        impersonate: curl-impersonate target, or ``""`` to choose one. Keep the
            family alias: a pinned profile ages into a signal of its own, and the
            library warns when it detects one older than the installed build offers.
            See :meth:`profile` for what ``""`` resolves to and why it depends on
            whether a browser solver is configured.
        prefer_http3: Offer HTTP/3 where the origin advertises it. Current browsers
            prefer it, so a client that never does is a mild mismatch — mild enough
            that it is off by default, since HTTP/3 through some proxies is worse
            than the mismatch.
        transport: Inject a transport instead of building one. The seam tests use,
            and the way to supply a client this library does not know about.
        exits: Configured addresses, best kind first after sorting. The single most
            consequential setting: reputation is not something a client emits, so
            no amount of transport fidelity substitutes for a better address.
        tiers: Extra rungs on the ladder. Each is a :class:`~scraper.tiers.Tier`
            declaring its own cost and reach, and the planner picks it on evidence like
            any other. The seam for a capability this library does not have — a
            provider that speaks a protocol of its own, a cache in front of the site.
        browser: A challenge solver. The second most consequential: without one,
            every challenged page is out of reach no matter how patient the run is.
        archive: Allow serving pages from the Wayback Machine. Off by default
            because it trades freshness for cost, which only the caller can weigh.
        managed: A provider callable for the last rung. See
            :data:`scraper.tiers.Provider`.
        remember: Persist what is learned per origin. On by default, and turning it
            off costs more than it looks like: every run then rediscovers the
            binding layer with the same number of failed requests, and those
            failures are themselves what the behavioural layer counts.
        data_dir: Where learned state and browser profiles live.
        max_attempts: Attempts for one retrieval across all tiers.
        max_rotations: Addresses to spend on one retrieval. Deliberately small —
            burning a pool one request at a time is a misdiagnosis, not bad luck.
        retry_backoff: Base seconds before a retry, doubled per attempt and capped
            at *max_retry_wait*. Only used when the response named no delay; a
            ``Retry-After`` header always wins.
        guard_topic: Watch for decoy content. The only defence against the layer
            that returns no error.
        on_decoy: ``"warn"``, ``"raise"`` or ``"ignore"``. Warning is the default
            because the check is a heuristic and a false positive should not be
            able to fail a job; ``"raise"`` is right for anything that trains on
            or republishes what it collects.
        raise_for_status: Raise :class:`requests.HTTPError` on a non-2xx that
            survived the ladder. A 404 reaches the caller either way; this only
            decides whether it arrives as a return value or an exception.
    """

    # -- transport -------------------------------------------------------------------
    impersonate: str = ""
    prefer_http3: bool = False
    verify_tls: bool = True
    transport: Optional[Transport] = None

    # -- addresses -------------------------------------------------------------------
    exits: List[ExitSpec] = field(default_factory=list)
    max_sessions_per_exit: int = 2
    allow_rotation: bool = True
    retire_exit_for: float = 600.0

    # -- behaviour -------------------------------------------------------------------
    pacing: PacingPolicy = field(default_factory=PacingPolicy)

    # -- capabilities ----------------------------------------------------------------
    tiers: List["Tier"] = field(default_factory=list)
    browser: Optional[BrowserSolver] = None
    archive: bool = False
    archive_max_age: float = 0.0
    managed: Optional[Provider] = None
    botauth: BotAuthConfig = field(default_factory=BotAuthConfig)

    # -- persistence -----------------------------------------------------------------
    remember: bool = True
    data_dir: Optional[Path] = None

    # -- patience --------------------------------------------------------------------
    max_attempts: int = 5
    max_rotations: int = 2
    promote_after: int = 3
    solve_timeout: float = 90.0
    retry_backoff: float = 1.0
    max_retry_wait: float = 30.0

    # -- content safety --------------------------------------------------------------
    guard_topic: bool = True
    on_decoy: str = "warn"
    check_response: Optional[ResponseCheck] = None

    # -- request defaults ------------------------------------------------------------
    timeout: Any = (15, 120)
    parser: str = "lxml"
    raise_for_status: bool = True

    def __post_init__(self) -> None:
        if self.on_decoy not in ("warn", "raise", "ignore"):
            raise ValueError(f"on_decoy must be warn, raise or ignore, not {self.on_decoy!r}")
        if self.data_dir is not None:
            self.data_dir = Path(self.data_dir).expanduser()

    @property
    def state_dir(self) -> Optional[Path]:
        """The directory to keep learned state in, or ``None`` when not remembering."""
        if not self.remember:
            return None
        return self.data_dir or default_data_dir()

    @property
    def memory_path(self) -> Optional[Path]:
        root = self.state_dir
        return None if root is None else root / "origins.json"

    @property
    def profile_root(self) -> Optional[Path]:
        """Where per-address browser profiles go.

        ``None`` when there is no browser, because creating profile directories for
        a solver that does not exist leaves litter and explains nothing.
        """
        root = self.state_dir
        if root is None or self.browser is None:
            return None
        return root / "profiles"

    def profile(self) -> str:
        """The impersonation target to use, resolving ``""``.

        Firefox by default, which is a measured choice rather than a taste. Over a
        random 150-host sample of the source corpus, one request each: firefox 85,
        safari 84, edge 82, chrome 81 — and against chrome, firefox won four hosts and
        lost none. Chrome being the most common real browser is a reason to expect it
        to be unremarkable, not evidence that it is the least remarkable.

        Chrome when a browser solver is configured, and that override is the whole
        reason this is a method. A clearance is bound to a User-Agent *and* a TLS
        fingerprint together. The bundled solver drives Chrome, so it earns a
        clearance under a Chrome User-Agent — and replaying that over a Firefox
        handshake presents a contradiction the binding exists to catch. Four hosts is
        not worth breaking the tier that answers challenges.

        Set ``impersonate`` explicitly to override either way; nothing here second
        guesses a caller who named a profile.
        """
        if self.impersonate:
            return self.impersonate
        return "chrome" if self.browser is not None else "firefox"

    def capabilities_enabled(self) -> Tuple[bool, bool, bool]:
        """``(archive, browser, managed)`` — which optional tiers are available."""
        return bool(self.archive), self.browser is not None, self.managed is not None
