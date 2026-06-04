"""Tests for the EventLock concurrency primitive."""

import threading

import pytest

from scraper import AbortedException
from scraper._utils.event_lock import EventLock


def test_context_manager_acquires_and_releases():
    lock = EventLock(concurrency=1)
    with lock:
        pass  # acquire on enter, release on exit
    # usable again afterwards
    with lock:
        pass


def test_acquire_and_release_directly():
    lock = EventLock(concurrency=1)
    assert lock.acquire() is True
    lock.release()


def test_abort_sets_signal_and_blocks_acquire():
    lock = EventLock(concurrency=1)
    lock.abort()
    # default signal is set → acquire returns False immediately
    assert lock.acquire() is False


def test_using_external_signal():
    signal = threading.Event()
    lock = EventLock(concurrency=1)
    returned = lock.using(signal)
    assert returned is lock
    signal.set()
    assert lock.acquire() is False


def test_using_none_keeps_default_signal():
    lock = EventLock(concurrency=1)
    assert lock.using(None) is lock  # falsy signal → default is kept
    assert lock.acquire() is True
    lock.release()


def test_enter_raises_when_signalled():
    signal = threading.Event()
    signal.set()
    lock = EventLock(concurrency=1)
    with pytest.raises(AbortedException):
        with lock.using(signal):
            pass


def test_acquire_loops_when_semaphore_unavailable():
    lock = EventLock(concurrency=1)
    lock._sema.acquire()  # exhaust the single slot

    calls = {"n": 0}

    class _Signal:
        def is_set(self) -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # False first (enter loop), True second (exit)

    lock._signal = _Signal()  # type: ignore[assignment]
    # first iteration: semaphore times out → loops back → signal now set → False
    assert lock.acquire() is False
    assert calls["n"] >= 2
