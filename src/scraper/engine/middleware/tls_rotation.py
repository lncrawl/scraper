"""Rotate the transport's TLS cipher suite once per (non-nested) request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class TlsRotationMiddleware(Middleware):
    """Ask the transport to rotate ciphers (no-op for the curl_cffi transport)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        if not ctx.nested:
            self._engine.transport.rotate_ciphers()
        return nxt(ctx)
