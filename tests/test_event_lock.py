"""Tests for the EventLock concurrency primitive."""

import threading

import pytest

from scraper import AbortedException
from scraper.utils.event_lock import EventLock


def test_context_manager_acquires_and_releases():
    lock = EventLock(concurrency=1)
    with lock:
        pass  # acquire on enter, release on exit
    # usable again afterwards
    with lock:
        pass


def test_acquire_and_release_directly():
    lock = EventLock(concurrency=1)
    lock.acquire(lock._signal)  # returns None, does not raise
    lock.release()


def test_abort_sets_signal_and_blocks_acquire():
    lock = EventLock(concurrency=1)
    lock.abort()
    with pytest.raises(AbortedException):
        lock.acquire(lock._signal)


def test_using_external_signal_raises_when_set():
    signal = threading.Event()
    signal.set()
    lock = EventLock(concurrency=1)
    with pytest.raises(AbortedException):
        with lock.using(signal):
            pass


def test_using_none_keeps_default_signal():
    lock = EventLock(concurrency=1)
    with lock.using(None) as ctx:
        assert ctx is lock  # yields self


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

    with pytest.raises(AbortedException):
        lock.acquire(_Signal())  # type: ignore[arg-type]

    assert calls["n"] >= 2
