"""Pluggable HTTP transports for the engine.

:func:`build_transport` selects the primary curl_cffi transport when an
impersonation target is configured and curl_cffi is importable, and otherwise
returns the httpx-based fallback transport.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import Transport

if TYPE_CHECKING:
    from ...config import ScraperConfig

logger = logging.getLogger(__name__)


def build_transport(config: "ScraperConfig") -> Transport:
    """Return the transport for *config*.

    Uses :class:`~scraper.engine.transport.curl.CurlCffiTransport` (curl_cffi)
    when ``config.impersonate.target`` is set and curl_cffi is installed;
    otherwise falls back to
    :class:`~scraper.engine.transport.httpx_transport.HttpxTransport`.
    """
    if config.impersonate.target:
        try:
            from .curl import CurlCffiTransport

            return CurlCffiTransport(config)
        except Exception:
            logger.warning(
                "curl_cffi transport is unavailable - "
                "falling back to the httpx transport (weaker TLS fingerprint)."
            )

    from .httpx import HttpxTransport

    return HttpxTransport(config)


__all__ = [
    "Transport",
    "build_transport",
]
