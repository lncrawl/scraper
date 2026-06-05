"""The request-pipeline middleware and the ordered chain factory.

The chain is an onion: ``build_chain`` returns middleware outer-to-inner, the
engine runs them in order, and each ``handle`` wraps the next. Outer stages run
once per top-level request (throttle, TLS rotation, refresh, concurrency); inner
stages run on every send including challenge/403 follow-ups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from .abort import AbortMiddleware
from .base import Middleware, NextHandler
from .challenge import ChallengeMiddleware
from .concurrency import ConcurrencyMiddleware
from .hooks import HooksMiddleware
from .proxy import ProxyMiddleware
from .refresh import SessionRefreshMiddleware
from .retry_403 import Retry403Middleware
from .ssl_retry import SslRetryMiddleware
from .stealth import StealthMiddleware
from .throttle import ThrottleMiddleware
from .tls_rotation import TlsRotationMiddleware

if TYPE_CHECKING:
    from ..core import Engine


def build_chain(engine: "Engine") -> List[Middleware]:
    """Assemble the ordered middleware chain for *engine* (outer to inner)."""
    config = engine.config
    chain: List[Middleware] = [
        AbortMiddleware(engine),
        ThrottleMiddleware(engine),
    ]
    if config.rotate_tls_ciphers:
        chain.append(TlsRotationMiddleware(engine))
    chain.append(SessionRefreshMiddleware(engine))
    chain.append(ConcurrencyMiddleware(engine))
    chain.append(Retry403Middleware(engine))
    if engine.challenge_handlers:
        chain.append(ChallengeMiddleware(engine))
    chain.append(StealthMiddleware(engine))
    chain.append(HooksMiddleware(engine))
    chain.append(ProxyMiddleware(engine))
    chain.append(SslRetryMiddleware(engine))
    return chain


__all__ = [
    "Middleware",
    "NextHandler",
    "build_chain",
    "AbortMiddleware",
    "ThrottleMiddleware",
    "TlsRotationMiddleware",
    "SessionRefreshMiddleware",
    "ConcurrencyMiddleware",
    "Retry403Middleware",
    "ChallengeMiddleware",
    "StealthMiddleware",
    "HooksMiddleware",
    "ProxyMiddleware",
    "SslRetryMiddleware",
]
