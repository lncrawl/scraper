"""The :class:`Middleware` interface for the request pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from requests import Response

if TYPE_CHECKING:
    from ..context import RequestContext

# A handler advances the chain: it is ``lambda ctx: engine._run(ctx, i + 1)``.
NextHandler = Callable[["RequestContext"], Response]


class Middleware(ABC):
    """One concern in the onion-model request pipeline.

    ``handle`` wraps the rest of the chain: do pre-work, call ``nxt(ctx)`` (which
    descends to the next middleware and ultimately the transport), then do
    post-work on the response before returning it. Middleware mutate ``ctx`` in
    place (e.g. ``ctx.kwargs["proxies"] = ...``).
    """

    @abstractmethod
    def handle(self, ctx: RequestContext, nxt: NextHandler) -> Response:
        raise NotImplementedError
