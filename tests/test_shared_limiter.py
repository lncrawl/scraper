"""Tests for SharedLimiter — one throttle clock + slot pool across engines."""

import threading

from scraper import Scraper, SharedLimiter, default_config


def test_create_sizes_semaphore() -> None:
    limiter = SharedLimiter.create(max_concurrent_requests=3)
    acquired = [limiter.slots.acquire(blocking=False) for _ in range(4)]
    assert acquired == [True, True, True, False]
    for _ in range(3):
        limiter.slots.release()


def test_engines_share_state_and_slots() -> None:
    limiter = SharedLimiter.create(max_concurrent_requests=2)
    a = Scraper(config=default_config())
    b = Scraper(config=default_config())
    a.adopt_limiter(limiter)
    b.adopt_limiter(limiter)
    assert a._state is b._state is limiter.state
    assert a._slots is b._slots is limiter.slots
    a.close()
    b.close()


def test_limiter_via_constructor() -> None:
    limiter = SharedLimiter.create()
    engine = Scraper(config=default_config(), limiter=limiter)
    assert engine._state is limiter.state
    assert engine._slots is limiter.slots
    engine.close()


def test_shared_slots_block_across_engines() -> None:
    limiter = SharedLimiter.create(max_concurrent_requests=1)
    a = Scraper(config=default_config())
    b = Scraper(config=default_config())
    a.adopt_limiter(limiter)
    b.adopt_limiter(limiter)

    a._acquire_slot()
    blocked = threading.Event()
    passed = threading.Event()

    def try_b() -> None:
        blocked.set()
        b._acquire_slot()  # must wait until a releases
        passed.set()

    t = threading.Thread(target=try_b, daemon=True)
    t.start()
    blocked.wait(2)
    assert not passed.wait(0.7)  # b is held back by a's slot
    a._release_slot()
    assert passed.wait(2)  # freed slot lets b through
    b._release_slot()
    t.join(2)
    a.close()
    b.close()


def test_shared_throttle_clock_spans_engines() -> None:
    limiter = SharedLimiter.create()
    a = Scraper(config=default_config())
    b = Scraper(config=default_config())
    a.adopt_limiter(limiter)
    b.adopt_limiter(limiter)

    a._state.mark_request_sent()
    # b sees a's send on the shared clock: a 10s min interval implies a wait.
    assert b._state.throttle_delay(10.0, 10.0) > 9.0
    a.close()
    b.close()
