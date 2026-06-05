"""User-Agent selection for the scraper engine.

On first use, fetches a current UA dataset from intoli/user-agents (GitHub raw),
caches it locally with ETag-based validation so re-downloads only happen when the
remote dataset actually changes, and filters to modern browser versions.
Falls back to an embedded generator when the network is unavailable.
"""

from __future__ import annotations

from .agent import UserAgent

__all__ = [
    "UserAgent",
]
