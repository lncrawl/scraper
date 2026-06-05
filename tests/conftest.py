"""Shared fixtures.

Keeps tests fast and offline:
- UA dataset fetching is stubbed so construction never hits the network and
  always uses the deterministic embedded generator.
- `fast_config` disables stealth delays and throttling and selects the urllib
  transport (so `responses` can intercept requests — curl_cffi bypasses it).
"""

import pytest

from scraper import ImpersonateConfig, ScraperConfig, StealthConfig


@pytest.fixture(autouse=True)
def _offline_user_agent(monkeypatch):
    """Force the embedded UA fallback generator (no network, deterministic)."""
    monkeypatch.setattr(
        "scraper.engine.user_agent.cache.load_ua_data",
        lambda: None,
    )


def make_fast_config(**overrides) -> ScraperConfig:
    """A ScraperConfig tuned for tests: no delays, no throttling, urllib transport."""
    config = ScraperConfig(
        min_request_interval=0.0,
        min_request_interval_fast=0.0,
        max_concurrent_requests=4,
        session_refresh_interval=10**9,
        rotate_tls_ciphers=False,
        stealth=StealthConfig(
            enabled=False,
            human_like_delays=False,
            randomize_headers=False,
            browser_quirks=False,
        ),
        # No impersonation target → UrllibTransport, which `responses` can mock.
        impersonate=ImpersonateConfig(
            target=None,
        ),
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


@pytest.fixture
def fast_config():
    return make_fast_config()
