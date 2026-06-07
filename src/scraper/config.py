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
from typing import Callable, Literal, Optional, TypedDict

from requests import Response

from .challenges.clearance import ClearanceSolver

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


@dataclass(frozen=True)
class ProxyUrl:
    """Proxy URL."""

    url: str
    http_only: bool = False


@dataclass(frozen=True)
class TorProxyUrl(ProxyUrl):
    """Tor Proxy URL for optional Tor control-port settings for rotation."""

    url: str = "socks5://127.0.0.1:9150"
    control_host: str = "127.0.0.1"
    control_port: int = 9151
    control_password: str = ""


@dataclass
class ProxyConfig:
    """Proxy configuration."""

    fallback_to_direct: bool = True
    proxy_urls: list[TorProxyUrl | ProxyUrl | str] = field(default_factory=list)
    retry_request_on_failure: int = 3
    failure_tolerance: int = 3
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


class ExtraFingerprints(TypedDict, total=False):
    """Extra TLS/HTTP2 fingerprint overrides for curl_cffi.

    All keys are optional. Mirrors ``curl_cffi.requests.impersonate.ExtraFingerprints``.
    """

    tls_min_version: int
    tls_grease: bool
    tls_permute_extensions: bool
    tls_cert_compression: Literal["zlib", "brotli"]
    tls_signature_algorithms: list[str]
    tls_delegated_credential: str
    tls_record_size_limit: int
    http2_stream_weight: int
    http2_stream_exclusive: int
    http2_no_priority: bool
    http3_sig_hash_algs: str
    http3_tls_extension_order: str


# source: https://curl-cffi.readthedocs.io/en/latest/impersonate/targets.html
ImersonateTargetType = Literal[
    "chrome100",
    "chrome101",
    "chrome104",
    "chrome107",
    "chrome110",
    "chrome116",
    "chrome119",
    "chrome120",
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome131_android",
    "chrome133a",
    "chrome136",
    "chrome142",
    "chrome145",
    "chrome146",
    "chrome99",
    "chrome99_android",
    "edge101",
    "edge99",
    "firefox133",
    "firefox135",
    "firefox144",
    "firefox147",
    "safari153",
    "safari155",
    "safari170",
    "safari172_ios",
    "safari180",
    "safari180_ios",
    "safari184",
    "safari184_ios",
    "safari260",
    "safari260_ios",
    "safari2601",
    "tor145",
]


@dataclass
class ImpersonateConfig:
    """curl_cffi impersonation options — used by the curl_cffi transport.

    Groups the browser target with all curl_cffi session-level fingerprint and
    network options that have no urllib3 equivalent. When ``target`` is set and
    curl_cffi is available, requests route through the curl_cffi transport for a
    real browser TLS (JA3/JA4) and HTTP/2 fingerprint.
    """

    target: ImersonateTargetType | str | None = None
    http_version: HttpVersion | int | None = None
    ja3: str | None = None
    akamai: str | None = None
    perk: str | None = None
    extra_fp: ExtraFingerprints | None = None
    default_headers: bool = True
    trust_env: bool = True
    curl_options: dict | None = None


@dataclass
class CloudflareConfig:
    """Cloudflare challenge detection and optional auto-solve.

    Modern Cloudflare challenges (managed challenge / Turnstile) cannot be solved
    in pure Python. The engine *detects* them and, when a ``solver`` is set,
    drives it to obtain a ``cf_clearance`` cookie and retries the request
    transparently. With no solver it raises a clear exception instead — pair the
    default browser impersonation with
    :meth:`~scraper.Scraper.apply_browser_clearance` for those sites.
    """

    enabled: bool = True
    """Detect Cloudflare challenges at all. When False, challenge responses pass
    through untouched for the caller to handle."""

    debug: bool = False
    """Log detection/solve decisions."""

    solver: Optional[ClearanceSolver] = None
    """Set to a :class:`ClearanceSolver` to auto-solve detected challenges. Its
    presence is the auto-solve toggle."""

    max_solve_attempts: int = 1
    """Bounded retries per request chain before giving up (avoids solve loops)."""


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

    # TLS (urllib transport only)
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
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    # Network fingerprint impersonation (curl_cffi). When target is set and
    # curl_cffi is installed, requests route through the curl_cffi transport;
    # otherwise the engine falls back to the urllib3 transport.
    impersonate: ImpersonateConfig = field(default_factory=ImpersonateConfig)

    # Proxy
    proxy: ProxyConfig = field(default_factory=ProxyConfig)

    # Hooks — invoked as pre_hook(engine, method, url, *args, **kwargs) and
    # post_hook(engine, response); both receive the engine instance.
    pre_hook: Callable[..., tuple] | None = None
    post_hook: Callable[..., Response] | None = None

    # SSL — set False to accept self-signed / expired certs manually;
    # the scraper also auto-retries with verify=False on SSLError for non-CF URLs.
    verify_ssl: bool = True


def default_config() -> ScraperConfig:
    """Build a fresh :class:`ScraperConfig` with the library's tuned defaults.

    A new instance is returned on every call so that each :class:`~scraper.Scraper`
    owns its config (and nested proxy/stealth/impersonate objects) instead of
    sharing a single mutable module-level instance.

    Impersonation is enabled by default (``impersonate.target = "chrome"``): when
    curl_cffi is installed the request rides a real browser fingerprint, and when
    it is not the engine transparently falls back to the urllib3 transport.
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
