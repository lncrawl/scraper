"""Adaptive request pacing (skipped for nested challenge/retry follow-ups)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from requests import Response

from ...exceptions import AbortedException
from .base import Middleware, NextHandler

if TYPE_CHECKING:
    from ..context import RequestContext
    from ..core import Engine

logger = logging.getLogger(__name__)


class ThrottleMiddleware(Middleware):
    """Sleep to honour the configured min request interval for the CF state."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        if ctx.nested:
            return nxt(ctx)
        e = self._engine
        delay = e.state.throttle_delay(
            e.config.min_request_interval_fast, e.config.min_request_interval
        )
        if delay > 0:
            logger.debug("Throttling: sleeping %.2fs", delay)
            time.sleep(delay)
        if e.signal.aborted:
            raise AbortedException("Request aborted before sending.")
        e.state.mark_request_sent()
        return nxt(ctx)
