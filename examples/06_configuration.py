"""Configuring the scraper: defaults, throttling, stealth, and browser identity.

Run:
    uv run python examples/06_configuration.py
"""

import json

from scraper import (
    BrowserConfig,
    Scraper,
    ScraperConfig,
    StealthConfig,
    default_config,
)


def from_scratch() -> Scraper:
    """Build a fully explicit config."""
    config = ScraperConfig(
        # Throttling
        min_request_interval=2.0,  # min seconds between requests when CF is active
        min_request_interval_fast=0.1,  # min seconds when no CF challenge seen
        max_concurrent_requests=1,
        # Retry
        max_403_retries=3,
        # Stealth
        stealth=StealthConfig(
            enabled=True,
            min_delay=1.0,
            max_delay=3.0,
            human_like_delays=True,
            randomize_headers=True,
            browser_quirks=True,
        ),
        # Spoofed browser identity (drives UA + matching Client Hints)
        browser=BrowserConfig(
            browser="chrome",
            platform="darwin",
            desktop=True,
            mobile=False,
            architecture="arm",
            bitness="64",
        ),
    )
    return Scraper(origin="https://example.com", config=config)


def from_defaults() -> Scraper:
    """Start from the library's tuned defaults and tweak a couple of fields.

    default_config() returns a FRESH instance every call, so mutating it never
    leaks into other scrapers.
    """
    config = default_config()
    config.max_concurrent_requests = 4
    config.browser = BrowserConfig(browser="chrome", platform="darwin")
    return Scraper(config=config)


def main() -> None:
    a = from_defaults()
    print("with impersonate:\n", json.dumps(dict(a.headers), indent=4))

    b = from_scratch()
    print("without impersonate:\n", json.dumps(dict(b.headers), indent=4))


if __name__ == "__main__":
    main()
