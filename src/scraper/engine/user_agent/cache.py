"""Remote UA dataset: fetch, ETag-based cache, and brotli availability check."""

from __future__ import annotations

import gzip
import json
import logging
import tempfile
import time
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_URL = "https://raw.githubusercontent.com/intoli/user-agents/main/src/user-agents.json.gz"
_CACHE_PATH = Path(tempfile.gettempdir()) / "lncrawl_ua_cache.json.gz"
_ETAG_PATH = Path(tempfile.gettempdir()) / "lncrawl_ua_cache.etag"
_FAST_TTL = 3600  # seconds — skip network entirely if cache is this fresh


@lru_cache(maxsize=1)
def is_brotli_available() -> bool:
    """Whether a brotli decoder (the optional `brotli` extra) is importable."""
    try:
        import brotli  # noqa: F401

        return True
    except ImportError:
        try:
            import brotlicffi  # type: ignore[import-not-found]  # noqa: F401

            return True
        except ImportError:
            return False


def _read_cache() -> list[dict] | None:
    # Deferred so tests can monkeypatch scraper.engine.user_agent.cache._CACHE_PATH.

    try:
        with gzip.open(_CACHE_PATH) as f:
            return json.loads(f.read())
    except Exception:
        return None


def load_ua_data() -> list[dict] | None:
    """Return the intoli UA dataset, downloading/validating via ETag as needed."""
    import httpx

    # Fast path: no network call if cache is fresh.
    if _CACHE_PATH.exists():
        if time.time() - _CACHE_PATH.stat().st_mtime < _FAST_TTL:
            return _read_cache()

    # Conditional GET using stored ETag (avoids re-downloading unchanged data).
    headers: dict[str, str] = {}
    if _ETAG_PATH.exists():
        headers["If-None-Match"] = _ETAG_PATH.read_text().strip()

    try:
        resp = httpx.get(_CACHE_URL, headers=headers, timeout=10, follow_redirects=True)
        if resp.status_code == 304:
            # Unchanged; reset mtime so fast path applies next time.
            _CACHE_PATH.touch()
            return _read_cache()
        resp.raise_for_status()
        _CACHE_PATH.write_bytes(resp.content)
        if etag := resp.headers.get("ETag"):
            _ETAG_PATH.write_text(etag)
        return _read_cache()
    except Exception:
        logger.debug("UA data fetch failed; trying stale cache or fallback.")
        return _read_cache()
