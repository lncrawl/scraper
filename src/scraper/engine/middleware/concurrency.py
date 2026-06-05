"""Bound in-flight requests with an abortable concurrency slot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests import Response

from ...exceptions import AbortedException
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine


class ConcurrencyMiddleware(Middleware):
    """Acquire a slot before sending and release it after.

    Skipped for nested requests: a challenge follow-up or 403 retry already holds
    the slot, and re-acquiring at concurrency 1 would deadlock.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        if ctx.nested:
            return nxt(ctx)
        e = self._engine
        while not e.slots.acquire(timeout=0.5):
            if e.signal.aborted:
                raise AbortedException("Request aborted while waiting for a concurrency slot.")
        try:
            return nxt(ctx)
        finally:
            try:
                e.slots.release()
            except ValueError:
                pass
