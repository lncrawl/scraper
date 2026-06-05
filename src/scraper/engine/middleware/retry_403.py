"""Retry 403 responses via proxy rotation or session refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from requests import Response

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class Retry403Middleware(Middleware):
    """On a 403 (under the retry budget), rotate the proxy or refresh and re-request."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        response = nxt(ctx)
        if response.status_code == 200:
            self._engine.state.reset_403()
        retried = self._maybe_retry(ctx, response)
        return retried if retried is not None else response

    def _maybe_retry(self, ctx: RequestContext, response: Response) -> Optional[Response]:
        e = self._engine
        pm = e.proxy_manager
        if response.status_code != 403:
            return None
        if not e.state.register_403(e.config.max_403_retries):
            return None
        pm.report_failure()
        if pm.has_proxy:
            return e.request(ctx.method, ctx.url, *ctx.args, **ctx.kwargs)
        if e.config.auto_refresh_on_403 and e.refresh_session(ctx.url):
            return e.request(ctx.method, ctx.url, *ctx.args, **ctx.kwargs)
        return None
