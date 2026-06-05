"""Inject proxies and recover from proxy/connection errors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from requests import Response
from requests.exceptions import ConnectionError, ProxyError

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine

logger = logging.getLogger(__name__)


class ProxyMiddleware(Middleware):
    """Set ``proxies`` from the manager, then rotate (and optionally fall back to
    direct) when the send raises a proxy/connection error."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        e = self._engine
        pm = e.proxy_manager

        # pre-configured proxy
        if ctx.kwargs.get("proxies"):
            return nxt(ctx)

        # use our defined proxy manager and try until success
        attempt = 0
        max_retry = pm.config.retry_request_on_failure
        while pm.has_proxy and attempt < max_retry:
            attempt += 1
            try:
                ctx.kwargs["proxies"] = pm.get_proxy()
                res = nxt(ctx)
                pm.report_success()
                return res
            except (ProxyError, ConnectionError):
                pm.report_failure()

        # check if default fallback is allowed
        if not pm.config.fallback_to_direct:
            raise ProxyError("No proxy available")

        # direct fallback
        ctx.kwargs.pop("proxies", None)
        return nxt(ctx)
