"""Invoke the configured pre/post request hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState


class HooksMiddleware(Middleware):
    """Run ``pre_hook`` before sending and ``post_hook`` on the raw response.

    ``pre_hook(engine, method, url, *args, **kwargs) -> (method, url, args, kwargs)``
    and ``post_hook(engine, response) -> response`` both receive the engine.
    """

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        e = self._engine
        if e.config.pre_hook:
            ctx.method, ctx.url, ctx.args, ctx.kwargs = e.config.pre_hook(
                e, ctx.method, ctx.url, *ctx.args, **ctx.kwargs
            )
        response = await nxt(ctx)
        if e.config.post_hook:
            response = e.config.post_hook(e, response) or response
        return response
