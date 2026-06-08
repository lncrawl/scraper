"""Cloudflare challenge solver base.

Extends this base the create a challenge solver to pass a Cloudflare challenge and
harvest the ``cf_clearance`` cookie with the exact User-Agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ClearanceResult:
    """Cookies + User-Agent harvested by a :class:`ClearanceSolver`.

    ``cookies`` must include ``cf_clearance``; ``user_agent`` is the exact UA the
    solver's browser used (Cloudflare binds the clearance cookie to it, so the
    scraper must adopt the same UA when reusing the cookie).

    ``expires`` is the Unix timestamp at which ``cf_clearance`` expires (derived
    from the cookie's ``expires`` attribute).  Zero means unknown.
    ``cf_bm_expires`` is the ``__cf_bm`` expiry (30 min from issue, typically).
    """

    cookies: Dict[str, str]
    user_agent: Optional[str]
    expires: float = 0.0
    cf_bm_expires: float = 0.0
    proxy_key: str = "direct"

    @property
    def cf_clearance(self) -> Optional[str]:
        return self.cookies.get("cf_clearance")

    @property
    def cf_bm(self) -> Optional[str]:
        return self.cookies.get("__cf_bm")


class ClearanceSolver(ABC):
    """Strategy that solves a Cloudflare challenge and returns reusable clearance.

    Implementations drive a real browser - in-process
    (:class:`~scraper.challenges.BrowserSolver`) or a remote service
    (:class:`~scraper.challenges.RemoteSolver`) - to pass the challenge for
    ``url`` and return the resulting cookies + User-Agent, or ``None`` when solving
    failed.
    """

    @abstractmethod
    async def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[ClearanceResult]:
        raise NotImplementedError()
