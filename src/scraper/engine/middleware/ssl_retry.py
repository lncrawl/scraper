"""Retry once without TLS verification on SSL errors (non-CF URLs only)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests
from requests import Response

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine

logger = logging.getLogger(__name__)


class SslRetryMiddleware(Middleware):
    """Innermost middleware: default ``verify=False`` when configured, and retry an
    SSL failure unverified unless the URL is a Cloudflare endpoint."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        e = self._engine
        if not e.config.verify_ssl:
            ctx.kwargs.setdefault("verify", False)
        try:
            return nxt(ctx)
        except requests.exceptions.SSLError:
            if "/cdn-cgi/" in ctx.url or e.state.cf_active:
                raise
            logger.warning(
                "SSL verification failed for %s — retrying without verification.", ctx.url
            )
            ctx.kwargs["verify"] = False
            return nxt(ctx)
