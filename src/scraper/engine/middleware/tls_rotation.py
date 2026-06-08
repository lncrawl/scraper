"""Rotate the transport's TLS cipher suite once per (non-nested) request."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..core import Engine
    from ..state import RequestState


class TlsRotationMiddleware(Middleware):
    """Ask the transport to rotate ciphers (no-op for the curl_cffi transport)."""

    def __init__(self, engine: "Engine") -> None:
        self._engine = engine

    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        if not ctx.nested:
            self._engine.transport.rotate_ciphers()
        return await nxt(ctx)
