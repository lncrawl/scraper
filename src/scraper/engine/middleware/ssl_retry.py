"""Retry once without TLS verification on SSL errors (non-CF URLs only)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from ...exceptions import SSLTransportError
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState

logger = logging.getLogger(__name__)


class SslRetryMiddleware(Middleware):
    """Innermost middleware: apply ``verify=False`` when configured globally, and
    retry an :exc:`SSLTransportError` unverified unless the URL is a Cloudflare
    endpoint."""

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        e = self._engine
        if not e.config.verify_ssl:
            ctx.kwargs.setdefault("verify", False)
        try:
            return await nxt(ctx)
        except SSLTransportError:
            if "/cdn-cgi/" in ctx.url or e.state.cf_active:
                raise
            logger.warning(
                "SSL verification failed for %s — retrying without verification.", ctx.url
            )
            return await nxt(ctx.for_retry(verify=False))
