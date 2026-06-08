"""The :class:`Middleware` interface for the async request pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

if TYPE_CHECKING:
    from ..state import RequestState

# A handler advances the chain: ``lambda ctx: engine._run(ctx, i + 1)``.
NextHandler = Callable[["RequestState"], Awaitable[httpx.Response]]


class Middleware(ABC):
    """One concern in the onion-model request pipeline.

    ``handle`` wraps the rest of the chain: do pre-work, ``await nxt(ctx)`` to
    descend to the next middleware (and ultimately the transport), then do
    post-work on the response before returning it.  Middleware mutate ``ctx`` in
    place (e.g. ``ctx.kwargs["proxy"] = ...``).
    """

    @abstractmethod
    async def handle(self, ctx: "RequestState", nxt: NextHandler) -> httpx.Response:
        raise NotImplementedError
