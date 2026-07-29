"""Timing and navigation: the shape of the traffic, not just its rate."""

from __future__ import annotations

import statistics
import threading

import pytest

from scraper.exceptions import Aborted
from scraper.pacing import Pacer, PacingPolicy, Trail, needs_warmup, warmup_url


class TestTheDistribution:
    """Gaps come from a distribution, because a constant gap is the signal.

    Perfectly regular arrivals are a stronger indicator than being fast, so these
    tests assert on variance rather than on any particular value.
    """

    def test_gaps_vary(self):
        pacer = Pacer(PacingPolicy(interval=2.0, floor=0.01, pause_chance=0.0), seed=7)
        gaps = [pacer.gap("example.com") for _ in range(200)]
        assert statistics.pstdev(gaps) > 0.3

    def test_the_mean_tracks_the_configured_interval(self):
        pacer = Pacer(PacingPolicy(interval=2.0, floor=0.01, pause_chance=0.0), seed=11)
        gaps = [pacer.gap("example.com") for _ in range(500)]
        assert 1.6 < statistics.mean(gaps) < 2.4

    def test_the_distribution_is_right_skewed_like_reading_is(self):
        pacer = Pacer(PacingPolicy(interval=3.0, floor=0.01, pause_chance=0.0), seed=13)
        gaps = sorted(pacer.gap("example.com") for _ in range(500))
        median = gaps[len(gaps) // 2]
        assert median < statistics.mean(gaps)

    def test_gaps_stay_inside_the_floor_and_ceiling(self):
        policy = PacingPolicy(interval=5.0, floor=1.0, ceiling=6.0, pause_chance=0.0)
        pacer = Pacer(policy, seed=3)
        gaps = [pacer.gap("example.com") for _ in range(300)]
        assert min(gaps) >= 1.0
        assert max(gaps) <= 6.0

    def test_reading_pauses_happen(self):
        pacer = Pacer(PacingPolicy(interval=1.0, ceiling=99.0, pause_chance=1.0, pause_scale=5.0))
        assert pacer.gap("example.com") == pytest.approx(5.0)

    def test_a_zero_interval_means_no_pacing_at_all(self):
        assert Pacer(PacingPolicy(interval=0.0)).gap("example.com") == 0.0

    def test_the_sequence_is_independent_of_the_global_random_module(self):
        # A scrape whose timing becomes reproducible because unrelated code called
        # random.seed() has lost the property this module provides.
        import random

        first = Pacer(PacingPolicy(interval=2.0))
        random.seed(1234)
        a = [first.gap("a") for _ in range(5)]
        random.seed(1234)
        b = [first.gap("a") for _ in range(5)]
        assert a != b


class TestLearningTheLimit:
    def test_a_throttle_widens_the_interval(self):
        pacer = Pacer(PacingPolicy(interval=2.0, backoff_factor=2.0))
        assert pacer.throttled("example.com") == 4.0
        assert pacer.throttled("example.com") == 8.0

    def test_a_server_supplied_delay_beats_our_guess(self):
        pacer = Pacer(PacingPolicy(interval=2.0))
        assert pacer.throttled("example.com", 30.0) == 30.0

    def test_a_short_retry_after_does_not_undo_a_learned_interval(self):
        pacer = Pacer(PacingPolicy(interval=20.0))
        assert pacer.throttled("example.com", 1.0) == 20.0

    def test_the_interval_is_capped(self):
        pacer = Pacer(PacingPolicy(interval=10.0, max_interval=30.0, backoff_factor=10.0))
        assert pacer.throttled("example.com") == 30.0

    def test_a_learned_interval_can_be_carried_in_from_a_previous_run(self):
        pacer = Pacer(PacingPolicy(interval=1.0))
        pacer.learn("example.com", 9.0)
        assert pacer.interval_for("example.com") == 9.0

    def test_learning_is_per_origin(self):
        pacer = Pacer(PacingPolicy(interval=1.0))
        pacer.throttled("slow.example")
        assert pacer.interval_for("fast.example") == 1.0

    def test_a_nonsense_learned_value_is_ignored(self):
        pacer = Pacer(PacingPolicy(interval=1.0))
        pacer.learn("example.com", 0.0)
        assert pacer.interval_for("example.com") == 1.0


class TestWaiting:
    def test_time_already_spent_counts_towards_the_gap(self):
        # A caller doing its own work between fetches should not be made to wait
        # twice for the same interval.
        pacer = Pacer(PacingPolicy(interval=0.0))
        pacer.mark("example.com")
        assert pacer.next_delay("example.com") == 0.0

    def test_an_abort_interrupts_a_long_wait(self):
        # The tail of the distribution runs to tens of seconds, and a cancelled job
        # must not wait one out.
        pacer = Pacer(PacingPolicy(interval=30.0, floor=30.0, pause_chance=0.0))
        signal = threading.Event()
        signal.set()
        with pytest.raises(Aborted):
            pacer.wait("example.com", signal)


class TestTheNavigationChain:
    def test_the_first_visit_to_an_origin_has_no_referrer(self):
        trail = Trail()
        headers = trail.headers("https://example.com/page")
        assert "referer" not in headers
        assert headers["sec-fetch-site"] == "none"

    def test_the_next_page_cites_the_previous_one(self):
        trail = Trail()
        trail.record("https://example.com/list")
        headers = trail.headers("https://example.com/list/item")
        assert headers["referer"] == "https://example.com/list"
        assert headers["sec-fetch-site"] == "same-origin"

    def test_a_page_never_cites_itself(self):
        trail = Trail()
        trail.record("https://example.com/page")
        assert trail.referer("https://example.com/page") == ""

    def test_leaving_the_site_is_marked_cross_site(self):
        trail = Trail()
        trail.record("https://example.com/page")
        assert trail.headers("https://other.test/x")["sec-fetch-site"] == "none"

    def test_a_sub_resource_is_not_a_navigation(self):
        # A referrer chain threaded through every image is not one a browser
        # produces.
        trail = Trail()
        trail.record("https://example.com/page")
        headers = trail.headers("https://example.com/cover.jpg", navigation=False)
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["sec-fetch-dest"] == "empty"
        assert "upgrade-insecure-requests" not in headers

    def test_a_navigation_carries_the_headers_a_navigation_carries(self):
        headers = Trail().headers("https://example.com/page")
        assert headers["sec-fetch-mode"] == "navigate"
        assert headers["sec-fetch-dest"] == "document"
        assert headers["upgrade-insecure-requests"] == "1"

    def test_the_chain_is_per_origin(self):
        trail = Trail()
        trail.record("https://a.test/one")
        assert trail.referer("https://b.test/two") == ""


class TestWarmUp:
    def test_a_deep_page_on_a_cold_origin_wants_a_homepage_visit_first(self):
        assert needs_warmup("https://example.com/a/b/c", 0.0, PacingPolicy())

    def test_the_homepage_itself_never_needs_one(self):
        # Or the warm-up recurses.
        assert not needs_warmup("https://example.com/", 0.0, PacingPolicy())
        assert not needs_warmup("https://example.com", 0.0, PacingPolicy())

    def test_a_recent_warm_up_still_counts(self):
        import time as _time

        assert not needs_warmup("https://example.com/deep", _time.time(), PacingPolicy())

    def test_warming_up_can_be_switched_off(self):
        assert not needs_warmup("https://example.com/deep", 0.0, PacingPolicy(warmup=False))

    def test_the_warm_up_target_is_the_origin_root(self):
        assert warmup_url("https://example.com/a/b?c=1") == "https://example.com/"


def test_a_hostile_policy_is_clamped_rather_than_trusted():
    policy = PacingPolicy(interval=-5.0, shape=0.0, floor=2.0, ceiling=1.0)
    assert policy.interval == 0.0
    assert policy.shape >= 0.5
    assert policy.ceiling >= policy.floor
