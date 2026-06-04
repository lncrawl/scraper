"""Public configuration surface.

Re-exports the config dataclasses (:class:`ScraperConfig`, :class:`BrowserConfig`,
:class:`ProxyConfig`, :class:`StealthConfig`) and provides :func:`default_config`,
the recommended way to obtain tuned defaults.
"""

from __future__ import annotations

from ._engine.config import (
    BrowserConfig,
    ProxyConfig,
    ScraperConfig,
    StealthConfig,
)

__all__ = [
    "ScraperConfig",
    "BrowserConfig",
    "ProxyConfig",
    "StealthConfig",
    "default_config",
]


def default_config() -> ScraperConfig:
    """Build a fresh :class:`ScraperConfig` with the library's tuned defaults.

    A new instance is returned on every call so that each :class:`~scraper.Scraper`
    owns its config (and nested proxy/stealth objects) instead of sharing a single
    mutable module-level instance.
    """
    return ScraperConfig(
        min_request_interval=2.0,
        min_request_interval_fast=0.1,
        max_concurrent_requests=1,
        rotate_tls_ciphers=True,
        auto_refresh_on_403=False,
        max_403_retries=3,
        session_refresh_interval=300,
        stealth=StealthConfig(
            enabled=True,
            min_delay=1.0,
            max_delay=3.0,
            min_delay_fast=0.0,
            max_delay_fast=0.1,
            human_like_delays=True,
            randomize_headers=True,
            browser_quirks=True,
        ),
        browser=BrowserConfig(
            browser="firefox",
            platform="windows",
            desktop=True,
            mobile=False,
        ),
        proxy=ProxyConfig(
            fallback_to_direct=True,
        ),
    )
