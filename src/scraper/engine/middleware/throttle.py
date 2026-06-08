"""Adaptive request pacing (skipped for nested challenge/retry follow-ups)."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState

logger = logging.getLogger(__name__)


class ThrottleMiddleware(Middleware):
    """Sleep to honour the configured min request interval for the CF state.

    ±:attr:`~scraper.config.StealthConfig.throttle_jitter` random variance is
    applied to the computed delay so that the inter-request timing doesn't read
    as machine-regular.  Only runs for top-level requests (``ctx.nested`` is
    False); challenge follow-ups and 403/429 retries skip this stage.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        if ctx.nested:
            return await nxt(ctx)
        e = self._engine
        delay = e.state.throttle_delay(
            e.config.min_request_interval_fast,
            e.config.min_request_interval,
        )
        if delay > 0:
            jitter = e.config.stealth.throttle_jitter
            delay = max(0.0, delay * (1 + random.uniform(-jitter, jitter)))
            logger.debug("Throttling: sleeping %.2fs", delay)
            await asyncio.sleep(delay)
        e.state.mark_request_sent()
        return await nxt(ctx)
