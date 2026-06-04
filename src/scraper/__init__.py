from importlib.metadata import PackageNotFoundError, version

from .cloudscraper import CloudScraper
from .cloudscraper.exceptions import AbortedException, CloudflareException
from .config import (
    BrowserConfig,
    CloudScraperConfig,
    ProxyConfig,
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
    "CloudScraper",
    "default_config",
    "CloudScraperConfig",
    "BrowserConfig",
    "ProxyConfig",
    "StealthConfig",
    "AbortedException",
    "CloudflareException",
    "__version__",
]
