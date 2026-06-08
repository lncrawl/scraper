"""Tests for RequestState and SessionState."""

import pytest

from scraper.engine.state import RequestState, SessionState

# --- RequestState ---------------------------------------------------------


def test_request_state_nested_false_by_default():
    ctx = RequestState(method="GET", url="https://example.com")
    assert ctx.nested is False
    assert ctx.depth == 0


def test_request_state_for_retry_increments_depth():
    ctx = RequestState(method="GET", url="https://example.com")
    retry = ctx.for_retry()
    assert retry.depth == 1
    assert retry.nested is True
    assert retry.method == ctx.method
    assert retry.url == ctx.url


def test_request_state_for_retry_kwarg_overrides():
    ctx = RequestState(method="GET", url="https://example.com", kwargs={"timeout": 5})
    retry = ctx.for_retry(timeout=10)
    assert retry.kwargs["timeout"] == 10


def test_request_state_for_retry_preserves_solve_attempts():
    ctx = RequestState(method="GET", url="https://example.com", solve_attempts=2)
    retry = ctx.for_retry()
    assert retry.solve_attempts == 2


# --- SessionState ---------------------------------------------------------


def test_cf_active_false_initially():
    s = SessionState()
    assert s.cf_active is False


def test_mark_cf_active_sets_flag():
    s = SessionState()
    s.mark_cf_active()
    assert s.cf_active is True


def test_throttle_delay_zero_on_cold_start():
    """No prior request → elapsed >> interval → delay is 0."""
    s = SessionState()
    assert s.throttle_delay(1.0, 2.0) == 0.0


def test_throttle_delay_nonzero_after_request(monkeypatch):
    import scraper.engine.state as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    s = SessionState()
    s.mark_request_sent()
    # time is frozen → elapsed == 0 → delay == interval exactly
    assert s.throttle_delay(1.0, 2.0) == pytest.approx(1.0)


def test_throttle_delay_uses_slow_when_cf_active(monkeypatch):
    import scraper.engine.state as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    s = SessionState()
    s.mark_cf_active()
    s.mark_request_sent()
    assert s.throttle_delay(0.5, 2.0) == pytest.approx(2.0)


def test_register_403_returns_true_under_limit():
    s = SessionState()
    assert s.register_403(3) is True
    assert s.register_403(3) is True
    assert s.register_403(3) is True


def test_register_403_returns_false_at_limit():
    s = SessionState()
    for _ in range(3):
        s.register_403(3)
    assert s.register_403(3) is False  # fourth attempt


def test_reset_403_allows_retrying():
    s = SessionState()
    s.register_403(1)
    assert s.register_403(1) is False  # at limit
    s.reset_403()
    assert s.register_403(1) is True  # counter cleared


def test_mark_429_sets_recent_block(monkeypatch):
    import scraper.engine.state as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    s = SessionState()
    s.mark_429()
    assert s.recent_block() is True


def test_recent_block_false_initially():
    s = SessionState()
    assert s.recent_block() is False


def test_recent_block_false_after_timeout(monkeypatch):
    import scraper.engine.state as mod

    now = [1_000_000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    s = SessionState()
    s.mark_429()
    # advance time past the 60s window
    now[0] += 61.0
    assert s.recent_block() is False
