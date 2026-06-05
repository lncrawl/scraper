"""lncrawl-scraper — an HTTP scraper with Cloudflare bypass and a safe HTML wrapper.

A :class:`Scraper` composes a Cloudflare-bypass engine that transparently solves
challenges and adds convenience helpers (``get_soup``, ``get_json``, ``get_file`` …).
By default requests ride a real browser TLS/HTTP-2 fingerprint via curl_cffi
(``ScraperConfig.impersonate``), falling back to a urllib3 transport when curl_cffi
is unavailable. :class:`PageSoup` is a null-safe BeautifulSoup wrapper whose
selectors never return ``None``.

Example:
    >>> from scraper import Scraper
    >>> s = Scraper(origin="https://example.com")
    >>> soup = s.get_soup("https://example.com")
    >>> soup.select_one("h1").text
    'Example Domain'
"""

from importlib.metadata import PackageNotFoundError, version

from .config import (
    BrowserConfig,
    CloudflareConfig,
    HttpVersion,
    ImpersonateConfig,
    ProxyConfig,
    ProxyUrl,
    ScraperConfig,
    StealthConfig,
    TorProxyUrl,
    default_config,
)

# Shared domain types first — the engine, transport, and middleware all depend up
# on these, so importing them before .session/.soup keeps the graph acyclic.
from .exceptions import AbortedException, CloudflareException
from .session import Scraper
from .soup import PageSoup

try:
    __version__ = version("lncrawl-scraper")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "Scraper",
    "PageSoup",
    "default_config",
    "ScraperConfig",
    "BrowserConfig",
    "CloudflareConfig",
    "StealthConfig",
    "ProxyConfig",
    "ProxyUrl",
    "TorProxyUrl",
    "ImpersonateConfig",
    "HttpVersion",
    "AbortedException",
    "CloudflareException",
    "__version__",
]
