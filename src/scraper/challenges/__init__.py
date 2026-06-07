"""Cloudflare challenge detection + pluggable solving.

The engine detects Cloudflare interstitials with :class:`CloudflareDetector` and,
when a :class:`~scraper.config.ClearanceSolver` is configured, passes them through
a real browser — either a remote service (:class:`RemoteSolver`) or an in-process
nodriver browser (:class:`BrowserSolver`).
"""

from typing import TYPE_CHECKING, Optional

from .browser_solver import BrowserSolver
from .clearance import ClearanceResult, ClearanceSolver
from .detector import CloudflareChallengeKind, CloudflareDetector
from .remote_solver import RemoteSolver

if TYPE_CHECKING:
    from ..config import CloudflareConfig

__all__ = [
    "CloudflareChallengeKind",
    "CloudflareDetector",
    "RemoteSolver",
    "BrowserSolver",
    "ClearanceSolver",
    "ClearanceResult",
    "build_detector",
]


def build_detector(cfg: "CloudflareConfig") -> Optional[CloudflareDetector]:
    """Build the detector for *cfg*, or ``None`` when detection is disabled."""
    if not cfg.enabled:
        return None
    return CloudflareDetector(debug=cfg.debug)
