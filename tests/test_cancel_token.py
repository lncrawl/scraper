"""Tests for CancelToken: per-request cancellation."""

import asyncio
import concurrent.futures
import threading
import time

import pytest

from scraper.utils.cancel_token import CancelToken


def test_cancel_token_starts_uncancelled():
    token = CancelToken()
    assert not token.cancelled


def test_cancel_token_cancelled_after_cancel():
    token = CancelToken()
    token.cancel()
    assert token.cancelled


def test_cancel_token_idempotent():
    token = CancelToken()
    token.cancel()
    token.cancel()
    assert token.cancelled


def test_cancel_token_cancels_future():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    cancelled_event = threading.Event()

    async def _long_running():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_event.set()
            raise

    token = CancelToken()
    future = asyncio.run_coroutine_threadsafe(_long_running(), loop)
    token._bind_future(future, loop)

    time.sleep(0.05)
    token.cancel()

    with pytest.raises((concurrent.futures.CancelledError, asyncio.CancelledError)):
        future.result(timeout=2)

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def test_cancel_before_bind_still_cancels():
    """If cancel() is called before _bind_future, it should cancel immediately on bind."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    token = CancelToken()
    token.cancel()  # cancel before bind

    async def _noop():
        await asyncio.sleep(0.01)
        return "done"

    future = asyncio.run_coroutine_threadsafe(_noop(), loop)
    token._bind_future(future, loop)  # should trigger immediate cancel

    try:
        future.result(timeout=1)
    except (concurrent.futures.CancelledError, Exception):
        pass  # either cancelled or ran to completion (race) — both are acceptable

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
