"""Pluggable HTTP transports for the engine.

:func:`build_transport` selects the primary curl_cffi transport when an
impersonation target is configured and curl_cffi is importable, and otherwise
returns the legacy urllib3 transport (also used as the automatic fallback).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import Transport

if TYPE_CHECKING:
    from ...config import ScraperConfig

logger = logging.getLogger(__name__)


def build_transport(config: ScraperConfig) -> Transport:
    """Return the transport for *config*.

    Uses :class:`~scraper.engine.transport.curl.CurlCffiTransport` (curl_cffi) when
    ``config.impersonate.target`` is set and curl_cffi is installed; otherwise (or
    if curl_cffi is missing) falls back to
    :class:`~scraper.engine.transport.urllib.UrllibTransport`.
    """
    if config.impersonate.target:
        try:
            from .curl import CurlCffiTransport

            return CurlCffiTransport(config)
        except ImportError:
            logger.warning(
                "curl_cffi is unavailable — falling back to the urllib3 transport "
                "(weaker TLS fingerprint). Install curl_cffi to enable impersonation."
            )

    # The following transport is just for legacy support.
    # It may never be used since curl-cffi is a direct dependency.
    from .urllib import UrllibTransport

    return UrllibTransport(config)


__all__ = [
    "Transport",
    "build_transport",
]
