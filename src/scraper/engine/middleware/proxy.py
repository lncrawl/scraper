"""Inject the active proxy and recover from proxy/connection errors."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from ...exceptions import ProxyTransportError
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState

logger = logging.getLogger(__name__)


class ProxyMiddleware(Middleware):
    """Set ``ctx.kwargs["proxy"]`` from the manager, then rotate (and optionally
    fall back to direct) when the send raises a :exc:`ProxyTransportError`."""

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        e = self._engine
        pm = e.proxy_manager

        attempt = 0
        max_retry = pm.config.retry_request_on_failure

        # if there is a pre-configured proxy try with this one first
        if ctx.kwargs.get("proxy"):
            attempt += 1
            try:
                return await nxt(ctx)
            except ProxyTransportError:
                ctx.kwargs.pop("proxy", None)

        while pm.has_proxy and attempt < max_retry:
            try:
                ctx.kwargs["proxy"] = pm.get_proxy()
                return await nxt(ctx)
            except ProxyTransportError:
                ctx.kwargs.pop("proxy", None)
                e.rotate_proxy(disable_current=True)

        if not pm.config.fallback_to_direct:
            raise ProxyTransportError("No proxy available")

        return await nxt(ctx)
