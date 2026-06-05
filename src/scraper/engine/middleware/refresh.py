"""Refresh the session when it goes stale (before a non-nested request)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class SessionRefreshMiddleware(Middleware):
    """Trigger :meth:`Engine.refresh_session` when the session age/403 state warrants it."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        if not ctx.nested:
            e = self._engine
            if e.state.needs_refresh(e.config.session_refresh_interval):
                e.refresh_session(ctx.url)
        return nxt(ctx)
