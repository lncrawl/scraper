"""Retry 403/429 responses via proxy rotation or session refresh."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState

logger = logging.getLogger(__name__)


class Retry403Middleware(Middleware):
    """On 403/429 (within the retry budget), rotate the proxy or soft-refresh and retry.

    403 handling:
      1. Rotate the active proxy and retry (if proxies are configured and budget remains).
      2. If no proxy, or once the proxy budget is exhausted, attempt a soft session refresh.
      3. Return the 403 response as-is if neither strategy succeeds.

    429 handling:
      Back off for ``min(config.max_429_backoff, base + depth_factor)`` seconds,
      then retry.  Only one 429 retry is attempted per request depth.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        response = await nxt(ctx)
        if response.status_code == 200:
            self._engine.state.reset_403()
            return response
        if response.status_code == 429 and not ctx.nested:
            return await self._handle_429(ctx, response)
        if response.status_code == 403:
            retried = await self._handle_403(ctx, response)
            if retried is not None:
                return retried
        return response

    async def _handle_429(self, ctx: "RequestState", response: httpx.Response) -> httpx.Response:
        e = self._engine
        e.state.mark_429()
        backoff = min(e.config.max_429_backoff, 10.0 + 10.0 * (ctx.depth + 1))
        logger.warning("429 Too Many Requests — backing off %.0fs before retry.", backoff)
        await asyncio.sleep(backoff)
        return await e._run_pipeline(ctx.for_retry())

    async def _handle_403(
        self, ctx: "RequestState", response: httpx.Response
    ) -> Optional[httpx.Response]:
        e = self._engine
        pm = e.proxy_manager
        if pm.has_proxy and e.state.register_403(e.config.max_403_retries):
            e.rotate_proxy()
            logger.debug("403 received — rotating proxy and retrying.")
            return await e._run_pipeline(ctx.for_retry())
        if e.config.auto_refresh_on_403:
            logger.debug("403 received — triggering soft session refresh.")
            if await e._refresh_session(ctx.url):
                return await e._run_pipeline(ctx.for_retry())
        return None
