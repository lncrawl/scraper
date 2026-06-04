"""Tests for the public configuration surface."""

from scraper import (
    BrowserConfig,
    ProxyConfig,
    ScraperConfig,
    StealthConfig,
    default_config,
)
from scraper.config import default_config as cfg_default_config


def test_default_config_returns_fresh_instances():
    a = default_config()
    b = default_config()
    assert a is not b
    # nested objects must not be shared either
    assert a.browser is not b.browser
    assert a.stealth is not b.stealth
    assert a.proxy is not b.proxy


def test_default_config_values():
    cfg = default_config()
    assert isinstance(cfg, ScraperConfig)
    assert isinstance(cfg.browser, BrowserConfig)
    assert cfg.browser.browser == "firefox"
    assert cfg.stealth.enabled is True
    assert cfg.impersonate is None


def test_default_config_reexported_from_config_module():
    assert cfg_default_config is default_config


def test_mutating_one_config_does_not_affect_another():
    a = default_config()
    b = default_config()
    a.max_concurrent_requests = 99
    a.proxy.proxy_urls.append("http://x:1")
    assert b.max_concurrent_requests != 99
    assert b.proxy.proxy_urls == []


def test_dataclass_defaults():
    assert StealthConfig().enabled is True
    assert ProxyConfig().fallback_to_direct is True
    assert BrowserConfig().browser is None
    assert ScraperConfig().impersonate is None
