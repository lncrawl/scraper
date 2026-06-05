"""Detect and solve Cloudflare challenges via the handler registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from ...exceptions import CloudflareLoopProtection
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class ChallengeMiddleware(Middleware):
    """Run each challenge handler against the response and solve the first match.

    Handlers receive ``request`` (the full engine pipeline, for challenge submits
    and follow-up redirects) and ``perform_request`` (the raw transport, for the
    doubleDown bypass). Recursion depth is bounded by ``config.solve_depth``.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        e = self._engine
        response = nxt(ctx)
        cfg = e.config.cloudflare
        for handler in e.challenge_handlers:
            if not handler.is_challenge(response):
                continue
            chain = e.chain
            if chain.solve_depth >= cfg.solve_depth:
                chain.solve_depth = 0
                raise CloudflareLoopProtection(
                    f"Loop protection triggered after {cfg.solve_depth} attempts."
                )
            chain.solve_depth += 1
            e.state.mark_cf_active()
            return handler.handle(
                response,
                request=e.request,
                perform_request=e.perform_request,
                **ctx.kwargs,
            )
        if not response.is_redirect and response.status_code not in (429, 503):
            e.chain.solve_depth = 0
        return response
