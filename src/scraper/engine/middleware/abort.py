"""Abort the request immediately if the engine's signal is set."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from ...exceptions import AbortedException
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class AbortMiddleware(Middleware):
    """Raise :exc:`AbortedException` at the top of the chain when aborted."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        if self._engine.signal.aborted:
            raise AbortedException("Request aborted by signal.")
        return nxt(ctx)
