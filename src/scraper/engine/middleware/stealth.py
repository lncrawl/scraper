"""Apply stealth headers and human-like pacing to outgoing requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class StealthMiddleware(Middleware):
    """Delegate to :class:`~scraper.engine.stealth.StealthMode` when stealth is enabled."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        e = self._engine
        if e.config.stealth.enabled:
            headers = dict(ctx.kwargs.pop("headers", {}) or {})
            ctx.kwargs = e.stealth.apply(
                ctx.method,
                ctx.url,
                cf_active=e.state.cf_active,
                headers=headers,
                **ctx.kwargs,
            )
        return nxt(ctx)
