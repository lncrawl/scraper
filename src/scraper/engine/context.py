"""The request context threaded through the middleware pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from requests import Response


@dataclass
class RequestContext:
    """Mutable per-request state carried through the middleware chain.

    Middleware read and mutate this in place: the proxy middleware injects
    ``kwargs["proxies"]``, stealth rewrites ``kwargs["headers"]``, and the
    innermost transport call populates :attr:`response`. ``nested`` is captured at
    engine entry — challenge follow-ups and 403 retries re-enter the pipeline with
    ``nested=True`` so the once-per-request stages (throttle, rotation, refresh,
    concurrency slot) skip themselves.
    """

    method: str
    url: str
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    nested: bool = False
    response: Optional[Response] = None
