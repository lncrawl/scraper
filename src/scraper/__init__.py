"""lncrawl-scraper — a scraper that reasons about why it is being blocked.

Modern bot mitigation runs many largely independent detectors and folds them into
one trust score, and admission behaves as a near-conjunction: the weakest layer
bounds the outcome. Two things follow, and this library is built on them.

Effort spent on a layer that is not the binding constraint buys nothing. So the
pipeline diagnoses which layer is actually blocking before it changes anything, and
escalates to the cheapest capability that can reach that layer — instead of running
a fixed remedy per status code.

And what a detector *reads* decides whether it can be satisfied at all. Detectors
that read an emitted artifact — a TLS ClientHello, an HTTP/2 frame order, a header
order — are reproducible. Detectors that read a possessed property, such as
accumulated per-zone history or a private signing key, are not: the only way to
satisfy one is to hold it. That distinction is why this library will slow down and
hold an address still where a conventional scraper would rotate, and why two layers
raise instead of retrying.

Quick start::

    from scraper import Scraper

    with Scraper(origin="https://example.com") as s:
        soup = s.get_soup("https://example.com")
        print(soup.select_one("h1").text)

Add reach rather than settings. A challenged site needs a solver::

    from scraper import Scraper, ScraperConfig
    from scraper import CdpSolver

    config = ScraperConfig(browser=CdpSolver())

A scored site needs a better address::

    from scraper import ExitKind, ExitSpec, ScraperConfig

    config = ScraperConfig(
        exits=[ExitSpec(url="http://user:pass@residential:8000", kind=ExitKind.RESIDENTIAL)]
    )

When something does not work, ask::

    print(s.explain("https://example.com"))

See :mod:`scraper.layers` for the model, and ``docs/`` for the guides.
"""

from importlib.metadata import PackageNotFoundError, version

from .bidi import BidiSolver
from .botauth import BotAuthConfig, BotAuthKey
from .browser import (
    BROWSER_MODES,
    BrowserSolver,
    CallableSolver,
    RenderError,
    SolveResult,
    set_browser_slots,
)
from .browsers import (
    find_chromium,
    find_firefox,
    pick_chromium,
    pick_firefox,
)
from .cdp import CdpSolver
from .config import ResponseCheck, ScraperConfig, default_data_dir
from .diagnosis import Action, Diagnosis, diagnose, edge
from .exceptions import (
    Aborted,
    Blocked,
    ConfigError,
    Exhausted,
    Impassable,
    MissingDependency,
    Poisoned,
    ScraperError,
    TierUnavailable,
)
from .exits import ExitKind, ExitPool, ExitSpec, ExitStatus, TorPoolSpec
from .identity import Clearance, Identity
from .layers import LAYERS, Layer, LayerInfo, Stance, Trait, weakest
from .links import Link, TopicGuard, safe_links
from .memory import Memory, OriginProfile
from .pacing import Pacer, PacingPolicy, Trail
from .planner import Capability, Decision, Move, Planner
from .session import Scraper
from .soup import PageSoup
from .state import SharedState
from .transport import ImpersonateTransport, PlainTransport, Transport
from .utils.url_tools import extract_base, extract_host, validate_url

try:
    __version__ = version("lncrawl-scraper")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    # the entry point
    "Scraper",
    "ScraperConfig",
    "SharedState",
    "PageSoup",
    # the model
    "LAYERS",
    "Layer",
    "LayerInfo",
    "Stance",
    "Trait",
    "weakest",
    "Action",
    "Diagnosis",
    "diagnose",
    "edge",
    "Capability",
    "Decision",
    "Move",
    "Planner",
    # identity and addresses
    "Clearance",
    "ExitKind",
    "ExitPool",
    "ExitSpec",
    "ExitStatus",
    "Identity",
    "TorPoolSpec",
    # behaviour
    "Memory",
    "OriginProfile",
    "Pacer",
    "PacingPolicy",
    "Trail",
    # capabilities
    "BotAuthConfig",
    "BotAuthKey",
    "BidiSolver",
    "BROWSER_MODES",
    "BrowserSolver",
    "CallableSolver",
    "CdpSolver",
    "SolveResult",
    # finding a browser to drive
    "find_chromium",
    "find_firefox",
    "pick_chromium",
    "pick_firefox",
    "ImpersonateTransport",
    "PlainTransport",
    "Transport",
    # content safety
    "Link",
    "TopicGuard",
    "safe_links",
    # reading a URL the way this library keys by
    "extract_base",
    "extract_host",
    "validate_url",
    # errors
    "Aborted",
    "Blocked",
    "ConfigError",
    "Exhausted",
    "Impassable",
    "MissingDependency",
    "Poisoned",
    "RenderError",
    "ResponseCheck",
    "ScraperError",
    "TierUnavailable",
    "default_data_dir",
    "set_browser_slots",
    "__version__",
]
