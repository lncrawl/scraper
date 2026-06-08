"""Public configuration surface.

Defines every configuration dataclass (:class:`ScraperConfig` and its nested
:class:`BrowserConfig`, :class:`StealthConfig`, :class:`ProxyConfig`,
:class:`ImpersonateConfig`) plus :func:`default_config`, the recommended way to
obtain tuned defaults.

These live at the package root as shared domain types: the engine, transport, and
middleware all depend *up* on this module, never the other way around.
"""

from __future__ import annotations

import enum
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Literal, Optional, Union

from .challenges.clearance import ClearanceSolver

if TYPE_CHECKING:
    from curl_cffi.const import CurlHttpVersion
    from curl_cffi.requests.impersonate import BrowserTypeLiteral, ExtraFingerprints, ExtraFpDict
    from curl_cffi.requests.utils import HttpVersionLiteral
    from httpx import Response

BrowserType = Literal["chrome", "firefox", "edge", "safari"]
PlatformType = Literal["windows", "darwin", "linux", "android", "ios"]
ArchitectureType = Literal["x86", "arm"]
BitnessType = Literal["32", "64"]


@dataclass
class BrowserConfig:
    """Identity to spoof — drives the User-Agent and matching Client Hints."""

    allow_brotli: bool = True
    browser: BrowserType | None = None
    platform: PlatformType | None = None
    version: int | None = None
    architecture: ArchitectureType | None = None
    bitness: BitnessType | None = None
    desktop: bool = True
    mobile: bool = True
    custom: str | None = None


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
    # ±fraction jitter applied to throttle delays (0.2 = ±20%)
    throttle_jitter: float = 0.2


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


@dataclass
class ProxyConfig:
    """Proxy configuration."""

    fallback_to_direct: bool = True
    proxy_urls: list[TorProxyUrl | ProxyUrl | str] = field(default_factory=list)
    retry_request_on_failure: int = 3
    tor_rotation_cooldown: float = 10.0
    disable_cooldown: float = 300.0  # 5 minutes


class HttpVersion(enum.IntEnum):
    """HTTP protocol version for curl_cffi sessions.

    Values mirror ``curl_cffi.const.CurlHttpVersion`` so callers don't need to
    import curl_cffi just to pick a version.
    """

    DEFAULT = 0
    V1_0 = 1
    V1_1 = 2
    V2 = 3
    V2_TLS = 4
    V2_PRIOR_KNOWLEDGE = 5
    V3 = 30
    V3_ONLY = 31


@dataclass
class ImpersonateConfig:
    """curl_cffi impersonation options — used by the curl_cffi transport.

    Groups the browser target with all curl_cffi session-level fingerprint and
    network options that have no urllib3 equivalent. When ``target`` is set and
    curl_cffi is available, requests route through the curl_cffi transport for a
    real browser TLS (JA3/JA4) and HTTP/2 fingerprint.
    """

    target: Optional[BrowserTypeLiteral] = None
    ja3: Optional[str] = None
    akamai: Optional[str] = None
    perk: Optional[str] = None
    extra_fp: Optional[Union[ExtraFingerprints, ExtraFpDict]] = None
    default_headers: bool = True
    curl_options: Optional[dict] = None
    curl_infos: Optional[list] = None
    http_version: Optional[Union[CurlHttpVersion, HttpVersionLiteral]] = None
    interface: Optional[str] = None
    cert: Optional[Union[str, tuple[str, str]]] = None
    trust_env: bool = True


@dataclass
class CloudflareConfig:
    """Cloudflare challenge detection and optional auto-solve.

    Modern Cloudflare challenges (managed challenge / Turnstile) cannot be solved
    in pure Python. The engine *detects* them and, when ``solvers`` is non-empty,
    drives each in order until one obtains a ``cf_clearance`` cookie, then retries
    the request transparently. With no solvers it raises a clear exception instead.
    """

    enabled: bool = True
    """Detect Cloudflare challenges at all. When False, challenge responses pass
    through untouched for the caller to handle."""

    debug: bool = False
    """Log detection/solve decisions."""

    solvers: List[ClearanceSolver] = field(default_factory=list)
    """Ordered solver chain: each is tried in turn until one succeeds."""

    max_solve_attempts: int = 1
    """Bounded retries per request chain before giving up (avoids solve loops)."""

    clearance_cache_dir: Optional[Path] = None
    """Directory for on-disk clearance cache. ``None`` disables persistence."""

    clearance_refresh_buffer: float = 300.0
    """Seconds before ``cf_clearance`` expiry to proactively re-solve."""


@dataclass
class ScraperConfig:
    """Top-level scraper configuration.

    Groups challenge-handling, TLS, session, throttling, stealth, browser,
    impersonation, and proxy settings. Use :func:`default_config` for tuned
    defaults rather than constructing this directly when you only need a few
    overrides.
    """

    # Challenge handling
    cloudflare: CloudflareConfig = field(default_factory=CloudflareConfig)

    # TLS (httpx fallback transport only)
    cipher_suite: str | None = None
    ecdh_curve: str = "prime256v1"
    source_address: str | tuple | None = None
    server_hostname: str | None = None
    ssl_context: ssl.SSLContext | None = None
    rotate_tls_ciphers: bool = True

    # SSL — set False to accept self-signed / expired certs;
    # the scraper also auto-retries with verify=False on SSLError for non-CF URLs.
    verify_ssl: bool = True

    # Session management
    auto_refresh_on_403: bool = True
    max_403_retries: int = 3
    max_429_backoff: float = 60.0

    # Request throttling
    min_request_interval: float = 2.0  # when CF protection is active
    min_request_interval_fast: float = 0.1  # when no CF has been detected
    max_concurrent_requests: int = 1

    # Stealth
    stealth: StealthConfig = field(default_factory=StealthConfig)

    # Browser / User-Agent
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    # Network fingerprint impersonation (curl_cffi). When target is set and
    # curl_cffi is installed, requests route through the curl_cffi transport;
    # otherwise the engine falls back to the httpx transport.
    impersonate: ImpersonateConfig = field(default_factory=ImpersonateConfig)

    # Proxy
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Hooks — invoked as pre_hook(engine, method, url, *args, **kwargs) and
    # post_hook(engine, response); both receive the engine instance.
    pre_hook: Callable[..., tuple] | None = None
    post_hook: "Callable[..., Response] | None" = None


def default_config() -> ScraperConfig:
    """Build a fresh :class:`ScraperConfig` with the library's tuned defaults.

    A new instance is returned on every call so that each :class:`~scraper.Scraper`
    owns its config (and nested proxy/stealth/impersonate objects) instead of
    sharing a single mutable module-level instance.

    Impersonation is enabled by default (``impersonate.target = "chrome"``): when
    curl_cffi is installed the request rides a real browser fingerprint, and when
    it is not the engine transparently falls back to the httpx transport.
    """
    return ScraperConfig(
        min_request_interval=2.0,
        min_request_interval_fast=0.1,
        max_concurrent_requests=1,
        rotate_tls_ciphers=True,
        auto_refresh_on_403=False,
        max_403_retries=3,
        stealth=StealthConfig(
            enabled=True,
            min_delay=1.0,
            max_delay=3.0,
            min_delay_fast=0.0,
            max_delay_fast=0.1,
            human_like_delays=True,
            randomize_headers=True,
            browser_quirks=True,
            throttle_jitter=0.2,
        ),
        impersonate=ImpersonateConfig(
            target="chrome",
        ),
        browser=BrowserConfig(
            desktop=True,
            mobile=False,
        ),
        proxy=ProxyConfig(
            fallback_to_direct=True,
        ),
    )
