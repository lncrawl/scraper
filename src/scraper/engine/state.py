"""Per-request and per-session mutable state for the engine."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from curl_cffi.requests.session import HttpMethod


@dataclass
class RequestState:
    """All mutable state carried through one request chain.

    Replaces the former split between ``RequestContext`` (per-request payload)
    and ``RequestChain`` (per-thread nesting tracker).  A top-level
    :meth:`~scraper.engine.core.Engine.request` call creates one with
    ``depth=0``; nested retries (403, challenge follow-ups) receive a copy
    produced by :meth:`for_retry` with ``depth+1``.
    """

    method: HttpMethod
    url: str
    args: Tuple[Any, ...] = ()
    kwargs: Dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    solve_attempts: int = 0

    @property
    def nested(self) -> bool:
        """True for challenge follow-ups and 403/429 retries."""
        return self.depth > 0

    def for_retry(self, **kwarg_overrides: Any) -> "RequestState":
        """Return a new RequestState for a nested retry of this request."""
        kwargs = dict(self.kwargs)
        kwargs.update(kwarg_overrides)
        return RequestState(
            method=self.method,
            url=self.url,
            args=self.args,
            kwargs=kwargs,
            depth=self.depth + 1,
            solve_attempts=self.solve_attempts,
        )


class SessionState:
    """Thread-safe container for per-session counters and flags.

    All compound read-modify-write operations are performed under a single lock
    so the engine can be used from multiple threads safely.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cf_active = False
        self._retry_403 = 0
        self._last_403 = 0.0
        self._last_429 = 0.0

    # -- Cloudflare detection -------------------------------------------------

    @property
    def cf_active(self) -> bool:
        with self._lock:
            return self._cf_active

    def mark_cf_active(self) -> None:
        with self._lock:
            self._cf_active = True

    # -- Throttle -------------------------------------------------------------

    def throttle_delay(self, fast: float, slow: float) -> float:
        """Seconds to sleep to honour the min interval for the current CF state."""
        with self._lock:
            interval = slow if self._cf_active else fast
            return max(0.0, interval - (time.monotonic() - self._last_request))

    def mark_request_sent(self) -> None:
        with self._lock:
            self._last_request = time.monotonic()

    # -- Blocked-request signals (trigger session refresh) --------------------

    def register_403(self, limit: int) -> bool:
        """Record a 403 retry attempt; return True if still under *limit*."""
        with self._lock:
            if self._retry_403 >= limit:
                return False
            self._retry_403 += 1
            self._last_403 = time.monotonic()
            return True

    def reset_403(self) -> None:
        with self._lock:
            self._retry_403 = 0
            self._last_403 = 0.0

    def mark_429(self) -> None:
        with self._lock:
            self._last_429 = time.monotonic()

    def recent_block(self) -> bool:
        """True if a 403 or 429 was recorded in the last 60 seconds."""
        with self._lock:
            now = time.monotonic()
            return (self._last_403 > 0 and now - self._last_403 < 60) or (
                self._last_429 > 0 and now - self._last_429 < 60
            )
