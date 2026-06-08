"""Per-proxy clearance record store with optional disk persistence.

Each unique (domain, proxy_key) pair gets its own :class:`ClearanceRecord`.
The engine writes a record after a successful solve and reads it back on the
next request to that domain through the same proxy — avoiding a re-solve within
the ``cf_clearance`` TTL.

Disk persistence is opt-in: set ``config.cloudflare.clearance_cache_dir`` to a
directory path.  Records are stored as JSON files named by a SHA-1 hash of the
(domain, proxy_key) pair and are silently ignored when expired or corrupt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from .clearance import ClearanceResult

logger = logging.getLogger(__name__)

_Key = Tuple[str, str]  # (domain, proxy_key)


class ClearanceStore:
    """In-memory (+ optional on-disk) store for per-proxy CF clearance records."""

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._records: Dict[_Key, ClearanceResult] = {}
        if cache_dir is not None:
            self._load_from_disk()

    # -- Write ----------------------------------------------------------------

    def save(self, domain: str, result: ClearanceResult) -> None:
        key: _Key = (domain, result.proxy_key)
        with self._lock:
            self._records[key] = result
        if self._cache_dir is not None:
            self._write_disk(key, result)

    # -- Read -----------------------------------------------------------------

    def get(
        self, domain: str, proxy_key: str, refresh_buffer: float = 0.0
    ) -> Optional[ClearanceResult]:
        key: _Key = (domain, proxy_key)
        with self._lock:
            rec = self._records.get(key)
        if rec is None:
            return None
        if rec.expires > 0 and time.time() + refresh_buffer >= rec.expires:
            self._evict(key)
            return None
        return rec

    def needs_refresh(self, domain: str, proxy_key: str, refresh_buffer: float = 300.0) -> bool:
        """True when the stored clearance is absent or about to expire."""
        return self.get(domain, proxy_key, refresh_buffer=refresh_buffer) is None

    def invalidate(self, domain: str, proxy_key: str) -> None:
        self._evict((domain, proxy_key))

    # -- Internal -------------------------------------------------------------

    def _evict(self, key: _Key) -> None:
        with self._lock:
            self._records.pop(key, None)
        if self._cache_dir is not None:
            path = self._disk_path(key)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _disk_path(self, key: _Key) -> Path:
        assert self._cache_dir is not None
        digest = hashlib.sha1(f"{key[0]}|{key[1]}".encode()).hexdigest()[:16]
        return self._cache_dir / f"clearance_{digest}.json"

    def _write_disk(self, key: _Key, result: ClearanceResult) -> None:
        assert self._cache_dir is not None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "domain": key[0],
                "proxy_key": key[1],
                "cookies": result.cookies,
                "user_agent": result.user_agent,
                "expires": result.expires,
                "cf_bm_expires": result.cf_bm_expires,
            }
            path = self._disk_path(key)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not persist clearance to disk: %s", exc)

    def _load_from_disk(self) -> None:
        assert self._cache_dir is not None
        if not self._cache_dir.exists():
            return
        now = time.time()
        for path in self._cache_dir.glob("clearance_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                expires = float(data.get("expires", 0))
                if expires > 0 and now >= expires:
                    path.unlink(missing_ok=True)
                    continue
                result = ClearanceResult(
                    cookies=data["cookies"],
                    user_agent=data.get("user_agent"),
                    expires=expires,
                    cf_bm_expires=float(data.get("cf_bm_expires", 0)),
                    proxy_key=data.get("proxy_key", "direct"),
                )
                key: _Key = (data["domain"], result.proxy_key)
                self._records[key] = result
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                logger.debug("Skipping corrupt clearance cache file %s: %s", path, exc)
