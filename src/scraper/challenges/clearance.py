"""Cloudflare challenge solver base.

Extends this base the create a challenge solver to pass a Cloudflare challenge and
harvest the ``cf_clearance`` cookie with the exact User-Agent.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ClearanceResult:
    """Cookies + User-Agent harvested by a :class:`ClearanceSolver`.

    ``cookies`` must include ``cf_clearance``; ``user_agent`` is the exact UA the
    solver's browser used (Cloudflare binds the clearance cookie to it, so the
    scraper must adopt the same UA when reusing the cookie).
    """

    cookies: dict[str, str]
    user_agent: str | None


class ClearanceSolver(ABC):
    """Strategy that solves a Cloudflare challenge and returns reusable clearance.

    Implementations drive a real browser — in-process
    (:class:`~scraper.challenges.BrowserSolver`) or a remote service
    (:class:`~scraper.challenges.RemoteSolver`) — to pass the challenge for
    ``url`` and return the resulting cookies + User-Agent, or ``None`` when solving
    failed.
    """

    def solve(
        self,
        url: str,
        *,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> Optional[ClearanceResult]:
        loop = asyncio.new_event_loop()
        try:
            coro = self.solve_async(
                url,
                proxy=proxy,
                user_agent=user_agent,
            )
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @abstractmethod
    async def solve_async(
        self,
        url: str,
        *,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> Optional[ClearanceResult]:
        raise NotImplementedError()
