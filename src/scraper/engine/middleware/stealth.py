"""Apply stealth headers and human-like pacing to outgoing requests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState

logger = logging.getLogger(__name__)


class StealthMiddleware(Middleware):
    """Delegate to :class:`~scraper.engine.stealth.StealthMode` when stealth is enabled."""

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        e = self._engine
        if not e.config.stealth.enabled:
            return await nxt(ctx)

        cf_active = e.state.cf_active
        delay = e.stealth.compute_delay(cf_active=cf_active)
        headers = dict(ctx.kwargs.pop("headers", {}) or {})
        ctx.kwargs = e.stealth.apply(
            ctx.method,
            ctx.url,
            cf_active=cf_active,
            headers=headers,
            **ctx.kwargs,
        )
        if delay > 0:
            logger.debug("Stealth delay: %.2fs (cf_active=%s)", delay, cf_active)
            await asyncio.sleep(delay)
        return await nxt(ctx)
