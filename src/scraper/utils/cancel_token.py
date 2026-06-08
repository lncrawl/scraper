"""Per-request cancellation token.

A CancelToken is created by the caller, passed to Scraper.get/post/etc., and
can be cancelled from any thread at any time. Cancellation propagates to the
underlying asyncio Task (which owns the live HTTP socket), so the connection is
closed almost immediately rather than waiting for the current recv to complete.

Usage::

    token = CancelToken()
    threading.Thread(target=lambda: (time.sleep(2), token.cancel())).start()
    scraper.get(url, cancel_token=token)   # raises AbortedException after ~2 s
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Optional


class CancelToken:
    """Thread-safe per-request cancel handle.

    Pass an instance to any Scraper request method.  Call :meth:`cancel` from
    any thread to abort the in-flight request.  One token may be shared across
    several requests; cancellation affects all of them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._future: Optional[concurrent.futures.Future] = None  # type: ignore[type-arg]
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- Internal (called by Engine) ------------------------------------------

    def _bind_future(
        self,
        fut: concurrent.futures.Future,  # type: ignore[type-arg]
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Register the CF Future that wraps the asyncio Task for this request."""
        with self._lock:
            self._future = fut
            self._loop = loop
            if self._cancelled:
                self._schedule_cancel()

    def _schedule_cancel(self) -> None:
        if self._future is not None and self._loop is not None:
            # call_soon_threadsafe schedules fut.cancel() in the event loop thread,
            # which via _chain_future propagates to asyncio Task.cancel() — the
            # genuine "stop the socket" cancellation path.
            self._loop.call_soon_threadsafe(self._future.cancel)

    # -- Public API -----------------------------------------------------------

    def cancel(self) -> None:
        """Cancel the associated request(s).  Safe to call from any thread."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._schedule_cancel()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled
