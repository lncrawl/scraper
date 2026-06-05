"""Tests for SessionState — thread-safe per-session counters."""

from scraper._engine.session_state import SessionState


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


def test_throttle_delay_nonzero_after_request():
    s = SessionState()
    s.mark_request_sent()
    delay = s.throttle_delay(1.0, 2.0)  # cf_active=False → fast interval
    assert delay > 0.9


def test_throttle_delay_uses_slow_when_cf_active():
    s = SessionState()
    s.mark_cf_active()
    s.mark_request_sent()
    delay = s.throttle_delay(0.5, 2.0)
    assert delay > 1.9


def test_next_cipher_rotation_increments():
    s = SessionState()
    assert s.next_cipher_rotation() == 1
    assert s.next_cipher_rotation() == 2
    assert s.next_cipher_rotation() == 3


def test_needs_refresh_false_when_fresh():
    s = SessionState()
    assert not s.needs_refresh(10**9)


def test_needs_refresh_true_when_aged():
    s = SessionState()
    assert s.needs_refresh(-1)  # max_age=-1 → immediately stale


def test_needs_refresh_true_with_recent_403():
    s = SessionState()
    s.register_403(10)
    # recent_403=True overrides even a huge max_age
    assert s.needs_refresh(10**9)


def test_reset_session_clock_restarts_age_counter():
    s = SessionState()
    s.reset_session_clock()
    assert not s.needs_refresh(10**9)


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
