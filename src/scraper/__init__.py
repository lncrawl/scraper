"""lncrawl-scraper — an HTTP scraper with Cloudflare bypass and a safe HTML wrapper.

A :class:`Scraper` is a ``requests.Session`` subclass that transparently solves
Cloudflare challenges and adds convenience helpers (``get_soup``, ``get_json``,
``get_file`` …). :class:`PageSoup` is a null-safe BeautifulSoup wrapper whose
selectors never return ``None``. Optionally route requests through a real
browser TLS/HTTP-2 fingerprint via ``ScraperConfig.impersonate``.

Example:
    >>> from scraper import Scraper
    >>> s = Scraper(origin="https://example.com")
    >>> soup = s.get_soup("https://example.com")
    >>> soup.select_one("h1").text
    'Example Domain'
"""

from importlib.metadata import PackageNotFoundError, version

from ._engine.exceptions import AbortedException, CloudflareException
from .config import (
    BrowserConfig,
    ProxyConfig,
    ScraperConfig,
    StealthConfig,
    default_config,
)
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
    "ProxyConfig",
    "StealthConfig",
    "AbortedException",
    "CloudflareException",
    "__version__",
]
