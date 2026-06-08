"""The scraper engine: a middleware pipeline over a pluggable transport.

This is a public extension surface. Most users only touch :class:`~scraper.Scraper`
and :func:`~scraper.default_config`, but custom transports and middleware can be
built against the primitives re-exported here.
"""

from __future__ import annotations

from typing import Optional

from ..config import ScraperConfig
from .core import Engine
from .middleware import Middleware, build_chain
from .proxy_manager import ProxyManager
from .state import RequestState
from .transport import Transport, build_transport


def create_engine(config: Optional[ScraperConfig] = None) -> Engine:
    """Build an :class:`Engine` for *config* (curl_cffi transport when available)."""
    return Engine(config)


__all__ = [
    "Engine",
    "create_engine",
    "RequestState",
    "Transport",
    "ProxyManager",
    "build_transport",
    "Middleware",
    "build_chain",
]
