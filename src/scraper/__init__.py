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
