"""Bound in-flight requests with a concurrency slot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState


class ConcurrencyMiddleware(Middleware):
    """Acquire an async semaphore slot before sending and release it after.

    Skipped for nested requests: a challenge follow-up or 403 retry already holds
    the slot, and re-acquiring at concurrency 1 would deadlock.

    ``engine.slots`` is an :class:`asyncio.Semaphore` created on the engine's
    event loop during :meth:`~scraper.engine.core.Engine._async_init`.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        if ctx.nested:
            return await nxt(ctx)
        async with self._engine.slots:
            return await nxt(ctx)
