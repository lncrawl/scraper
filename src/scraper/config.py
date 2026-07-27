"""Public configuration surface.

Re-exports the config dataclasses (:class:`ScraperConfig`, :class:`BrowserConfig`,
:class:`ProxyConfig`, :class:`StealthConfig`) and provides :func:`default_config`,
the recommended way to obtain tuned defaults.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from typing import Callable

from requests import Response


@dataclass
class StealthConfig:
    """Anti-detection behaviour: pacing, header randomisation, and browser quirks."""

    enabled: bool = True
    # Delays when Cloudflare is active
    min_delay: float = 1.0
    max_delay: float = 3.0
    # Delays when no CF challenge has been seen (fast path)
    min_delay_fast: float = 0.0
    max_delay_fast: float = 0.1
    human_like_delays: bool = True
    randomize_headers: bool = True
    browser_quirks: bool = True


@dataclass
class BrowserConfig:
    """Identity to spoof — drives the User-Agent and matching Client Hints."""

    # Browser engine to spoof: "chrome" | "firefox" | None (random choice).
    browser: str | None = None
    # Target platform: "windows" | "darwin" | "linux" | "android" | "ios" | None.
    platform: str | None = None
    desktop: bool = True
    mobile: bool = True
    # Explicit User-Agent string; when set, it overrides browser/platform.
    custom: str | None = None


@dataclass(frozen=True)
class ProxyUrl:
    """Proxy URL."""

    url: str


@dataclass(frozen=True)
class TorProxyUrl(ProxyUrl):
    """Tor Proxy URL for optional Tor control-port settings for rotation."""

    url: str = "socks5h://127.0.0.1:9050"
    control_host: str = "127.0.0.1"
    control_port: int = 9051
    control_password: str = "password"


@dataclass(frozen=True)
class TorPoolProxyUrl(ProxyUrl):
    """A `tor-pool <https://github.com/lncrawl/tor-pool>`_ endpoint.

    A pool runs many Tor instances behind one sticky SOCKS port: the SOCKS5
    username is a session key, and the caller stays on the same instance — and
    so the same exit IP — until it asks to rotate. Rotation goes through the
    pool's HTTP API and is near-instant, because it reassigns the session to an
    already-built instance instead of waiting out Tor's ~10s NEWNYM cooldown.

    Unlike :class:`TorProxyUrl` there is no control port and no password: the
    pool owns the control ports and never exposes them.
    """

    url: str = "socks5h://127.0.0.1:9250"
    api_url: str = "http://127.0.0.1:8080"
    # Blank generates one per ProxyManager, so two Scrapers in one process get
    # independent exit IPs by default.
    session: str = ""
    # Report 403/429/captcha/transport failures back to the pool. This is the
    # only signal that catches soft blocks: the pool cannot see inside an HTTPS
    # tunnel, so without it a burnt exit is never noticed.
    report_failures: bool = True


@dataclass
class ProxyConfig:
    """Proxy configuration."""

    fallback_to_direct: bool = True
    proxy_urls: list[TorPoolProxyUrl | TorProxyUrl | ProxyUrl | str] = field(default_factory=list)
    retry_request_on_failure: int = 3
    tor_rotation_cooldown: float = 10.0
    disable_cooldown: float = 300.0  # 5 minutes


@dataclass
class ScraperConfig:
    """Top-level scraper configuration.

    Groups challenge-handling, TLS, session, throttling, stealth, browser, and
    proxy settings. Use :func:`scraper.default_config` for tuned defaults rather
    than constructing this directly when you only need a few overrides.
    """

    # Challenge handling
    disable_v1: bool = False
    disable_v2: bool = False
    disable_v3: bool = False
    disable_turnstile: bool = False
    solve_depth: int = 3
    double_down: bool = True

    # TLS
    cipher_suite: str | None = None
    ecdh_curve: str = "prime256v1"
    source_address: str | tuple | None = None
    server_hostname: str | None = None
    ssl_context: ssl.SSLContext | None = None
    rotate_tls_ciphers: bool = True

    # Session management
    session_refresh_interval: int = 3600
    auto_refresh_on_403: bool = True
    max_403_retries: int = 3

    # Request throttling
    min_request_interval: float = 2.0  # when CF protection is active
    min_request_interval_fast: float = 0.1  # when no CF has been detected
    max_concurrent_requests: int = 1

    # Stealth
    stealth: StealthConfig = field(default_factory=StealthConfig)

    # Browser / User-Agent
    browser: BrowserConfig | dict | None = None
    allow_brotli: bool = True

    # Network fingerprint impersonation (requires the `impersonate` extra).
    # When set to a curl-impersonate target (e.g. "chrome", "firefox",
    # "chrome124"), requests are routed through curl_cffi to reproduce a real
    # browser's TLS (JA3/JA4) and HTTP/2 fingerprint instead of the urllib3
    # default. None keeps the standard transport.
    impersonate: str | None = None

    # Proxy
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Hooks — invoked as pre_hook(scraper, method, url, *args, **kwargs) and
    # post_hook(scraper, response); both receive the scraper engine instance.
    pre_hook: Callable[..., tuple] | None = None
    post_hook: Callable[..., Response] | None = None

    # SSL — set False to accept self-signed / expired certs manually;
    # the scraper also auto-retries with verify=False on SSLError for non-CF URLs.
    verify_ssl: bool = True

    # Debug
    debug: bool = False


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
