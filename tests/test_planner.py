"""The policy: what gets changed, given what is actually binding.

The three tests that matter most are in :class:`TestPossessIsNeverRotated`,
:class:`TestRotationNeedsSomewhereBetter` and :class:`TestEscalationGoesSomewhereUseful`.
Each of them is a case where the conventional status-code table does the wrong
thing, and each is the reason this module exists rather than a dictionary.
"""

from __future__ import annotations

import pytest

from scraper.diagnosis import Action, Diagnosis
from scraper.layers import Layer
from scraper.planner import (
    Capability,
    Context,
    Move,
    Planner,
    default_capabilities,
)

RESIDENTIAL = frozenset({Layer.IP_REPUTATION})
DATACENTER = frozenset()


def full_planner(**kwargs) -> Planner:
    return Planner(
        default_capabilities(archive=False, browser=True, managed=True),
        **kwargs,
    )


def direct_only() -> Planner:
    return Planner(default_capabilities())


class TestWhereToStart:
    def test_a_cold_origin_starts_cheap(self):
        assert full_planner().start().name == "direct"

    def test_a_known_challenge_starts_at_the_tier_that_handles_it(self):
        # The point of remembering: rediscovering this costs a failed request, and
        # failed requests are what the behavioural layer counts.
        chosen = full_planner().start(binding=Layer.MANAGED_CHALLENGE)
        assert chosen.name == "clearance"

    def test_it_starts_at_the_cheapest_tier_that_reaches_not_the_strongest(self):
        chosen = full_planner().start(binding=Layer.SUPER_BOT_FIGHT)
        assert chosen.name == "direct"

    def test_a_remembered_tier_is_honoured_when_it_still_reaches(self):
        chosen = full_planner().start(binding=Layer.CDP, preferred="managed")
        assert chosen.name == "managed"

    def test_a_remembered_tier_that_cannot_reach_is_ignored(self):
        chosen = full_planner().start(binding=Layer.CDP, preferred="direct")
        assert chosen.name == "clearance"

    def test_an_impassable_binding_does_not_pick_a_tier_to_fight_it(self):
        assert full_planner().start(binding=Layer.ACCESS).name == "direct"

    def test_the_archive_is_first_when_enabled(self):
        planner = Planner(default_capabilities(archive=True))
        assert planner.start().name == "archive"

    def test_a_planner_needs_something_to_plan_with(self):
        with pytest.raises(ValueError):
            Planner([])


class TestPossessIsNeverRotated:
    """The single most consequential rule in the library.

    A layer that reads accumulated history is reset by discarding identity. The
    naive reflex — new address, try again — guarantees the history never gets long
    enough to pass, so the run looks busy forever.
    """

    def test_a_throttle_slows_down(self):
        decision = full_planner().react(
            Diagnosis(Action.BACKOFF, Layer.BEHAVIOURAL, "rate limited", retry_after=7.0),
            Context(tier="direct", exit_reach=RESIDENTIAL, interval=2.0),
        )
        assert decision.move is Move.BACKOFF
        assert decision.wait == 7.0

    def test_even_a_rotate_verdict_is_downgraded_when_the_layer_is_possessed(self):
        # Whatever standing this address has built is the only asset in play.
        decision = full_planner().react(
            Diagnosis(Action.ROTATE, Layer.BEHAVIOURAL, "looks automated"),
            Context(tier="direct", exit_reach=RESIDENTIAL, interval=3.0),
        )
        assert decision.move is Move.ACCUMULATE
        assert decision.move is not Move.ROTATE

    def test_arriving_cold_is_fixed_by_arriving_warm(self):
        decision = full_planner().react(
            Diagnosis(Action.BACKOFF, Layer.BEHAVIOURAL, "rate limited"),
            Context(tier="direct", exit_reach=RESIDENTIAL, warmed=False),
        )
        assert decision.move is Move.WARM

    def test_the_wait_falls_back_to_the_learned_interval(self):
        decision = full_planner().react(
            Diagnosis(Action.BACKOFF, Layer.BEHAVIOURAL, "rate limited"),
            Context(tier="direct", exit_reach=RESIDENTIAL, interval=12.0),
        )
        assert decision.wait == 12.0


class TestRotationNeedsSomewhereBetter:
    def test_a_blocked_address_rotates_when_a_better_kind_exists(self):
        decision = full_planner().react(
            Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, "Cloudflare error 1020"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.ROTATE

    def test_rotating_between_published_ranges_is_refused_and_explained(self):
        # Datacenter and Tor ranges are published, so the replacement is blocklisted
        # for the same reason as the original. Saying so beats proving it one exit at
        # a time.
        decision = full_planner().react(
            Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, "Cloudflare error 1020"),
            Context(tier="direct", exit_reach=DATACENTER),
        )
        assert decision.move is Move.STOP
        assert decision.layer is Layer.IP_REPUTATION
        assert "residential" in decision.reason

    def test_rotating_with_nowhere_to_go_is_refused_immediately(self):
        """Found live: readwn.com bans this machine's ASN (Cloudflare 1005).

        With one address or none, a rotation lands on the address that was just
        refused. Spending the rotation budget to discover that is pure waste.
        """
        decision = full_planner().react(
            Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, "Cloudflare error 1005"),
            Context(tier="direct", exit_reach=RESIDENTIAL, can_rotate=False),
        )
        assert decision.move is Move.STOP
        assert "no other address configured" in decision.reason
        assert "1005" in decision.reason

    def test_rotation_is_bounded(self):
        decision = full_planner(max_rotations=1).react(
            Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, "blocked"),
            Context(tier="direct", exit_reach=RESIDENTIAL, rotations=1),
        )
        assert decision.move is Move.STOP
        assert "address kind" in decision.reason

    def test_rotation_can_be_switched_off_entirely(self):
        decision = full_planner(allow_rotation=False).react(
            Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, "blocked"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.STOP


class TestEscalationGoesSomewhereUseful:
    def test_a_challenge_escalates_to_the_solver(self):
        decision = full_planner().react(
            Diagnosis(Action.SOLVE, Layer.MANAGED_CHALLENGE, "challenge"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.ESCALATE
        assert decision.tier == "clearance"

    def test_it_stops_at_the_cheapest_tier_that_reaches(self):
        decision = full_planner().react(
            Diagnosis(Action.ESCALATE, Layer.CDP, "1010"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.tier == "clearance"

    def test_without_a_solver_the_message_names_what_is_missing(self):
        decision = direct_only().react(
            Diagnosis(Action.SOLVE, Layer.MANAGED_CHALLENGE, "challenge"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.STOP
        assert "browser solver" in decision.reason

    def test_a_challenge_under_the_solving_tier_re_solves_rather_than_escalating(self):
        # Almost always a clearance earned under a different identity, so the fix is
        # to solve again on the identity actually in force.
        decision = full_planner().react(
            Diagnosis(Action.SOLVE, Layer.MANAGED_CHALLENGE, "challenge"),
            Context(tier="managed", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.RETRY


class TestPromotion:
    def test_a_repeatedly_rejected_profile_becomes_the_per_zone_model(self):
        # The scoring tiers are indistinguishable from outside, so recurrence is the
        # only evidence there is that the zone runs the composite.
        decision = full_planner(promote_after=3).react(
            Diagnosis(Action.ESCALATE, Layer.SUPER_BOT_FIGHT, "scored as automated"),
            Context(tier="direct", exit_reach=RESIDENTIAL, consecutive_failures=3),
        )
        assert decision.move is Move.ESCALATE
        assert decision.layer is Layer.BOT_MANAGEMENT

    def test_one_failure_is_not_evidence_of_anything(self):
        decision = full_planner(promote_after=3).react(
            Diagnosis(Action.ESCALATE, Layer.SUPER_BOT_FIGHT, "scored as automated"),
            Context(tier="direct", exit_reach=RESIDENTIAL, consecutive_failures=1),
        )
        assert decision.layer is Layer.SUPER_BOT_FIGHT

    def test_a_declared_crawler_is_never_promoted(self):
        # That block is about the User-Agent, and no number of repetitions turns it
        # into a machine-learning verdict.
        decision = full_planner(promote_after=1).react(
            Diagnosis(Action.ESCALATE, Layer.AI_BOT_BLOCKER, "declared crawler"),
            Context(tier="direct", exit_reach=RESIDENTIAL, consecutive_failures=9),
        )
        assert decision.layer is Layer.AI_BOT_BLOCKER


class TestStopping:
    @pytest.mark.parametrize("layer", [Layer.WEB_BOT_AUTH, Layer.ACCESS])
    def test_a_layer_that_reads_a_secret_stops_immediately(self, layer: Layer):
        decision = full_planner().react(
            Diagnosis(Action.REFUSE, layer, "no bypass"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.STOP

    def test_a_secret_bearing_layer_stops_even_when_the_action_says_otherwise(self):
        decision = full_planner().react(
            Diagnosis(Action.ESCALATE, Layer.ACCESS, "login"),
            Context(tier="direct", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.STOP

    def test_attempts_are_bounded(self):
        decision = full_planner(max_attempts=3).react(
            Diagnosis(Action.RETRY, None, "flaky"),
            Context(tier="direct", attempt=3),
        )
        assert decision.move is Move.STOP
        assert "3 attempts" in decision.reason

    def test_the_strongest_tier_failing_says_so_with_a_hint(self):
        decision = full_planner().react(
            Diagnosis(Action.ESCALATE, Layer.BOT_MANAGEMENT, "composite"),
            Context(tier="managed", exit_reach=RESIDENTIAL),
        )
        assert decision.move is Move.STOP
        assert "managed" in decision.reason

    def test_a_configured_tier_that_failed_is_not_advised_to_be_configured(self):
        """Found live: a browser solver ran, produced no clearance, and the stop said
        "Configure a browser solver".

        That sends the reader to check their configuration instead of the solver's own
        output, which is where the reason actually is.
        """
        planner = Planner(default_capabilities(browser=True))
        decision = planner.react(
            Diagnosis(
                Action.ESCALATE,
                Layer.MANAGED_CHALLENGE,
                "nodriver finished without a clearance cookie",
            ),
            Context(tier="clearance", attempt=2),
        )
        assert decision.move is Move.STOP
        assert "Configure a browser solver" not in decision.reason
        assert "clearance, which ran and did not succeed" in decision.reason
        assert "without a clearance cookie" in decision.reason

    def test_a_layer_genuinely_out_of_reach_still_gets_the_hint(self):
        # The hint is right when the capability is actually absent.
        decision = direct_only().react(
            Diagnosis(Action.ESCALATE, Layer.CDP, "1010"),
            Context(tier="direct", attempt=2),
        )
        assert decision.move is Move.STOP
        assert "browser solver" in decision.reason


class TestPlainMoves:
    def test_a_clean_response_proceeds(self):
        decision = full_planner().react(Diagnosis(Action.ACCEPT), Context(tier="direct"))
        assert decision.move is Move.PROCEED

    def test_a_transient_error_retries_on_the_same_tier(self):
        decision = full_planner().react(
            Diagnosis(Action.RETRY, None, "502", retry_after=1.5),
            Context(tier="direct"),
        )
        assert decision.move is Move.RETRY
        assert decision.tier == "direct"
        assert decision.wait == 1.5


class TestCapabilities:
    def test_the_exits_reach_is_folded_in_rather_than_claimed_by_the_tier(self):
        # A tier does not change where packets come from; the exit does.
        direct = default_capabilities()[0]
        assert Layer.IP_REPUTATION not in direct.reach
        assert Layer.IP_REPUTATION in direct.with_reach(RESIDENTIAL).reach

    def test_declaring_one_transport_layer_declares_the_group(self):
        direct = default_capabilities()[0]
        assert Layer.HEADER_ORDER in direct.reach
        assert Layer.POST_QUANTUM in direct.reach

    def test_no_tier_claims_a_layer_that_reads_a_secret(self):
        for cap in default_capabilities(archive=True, browser=True, managed=True):
            assert Layer.WEB_BOT_AUTH not in cap.reach
            assert Layer.ACCESS not in cap.reach

    def test_the_ladder_is_ordered_by_cost(self):
        planner = full_planner()
        costs = [cap.cost for cap in planner.ladder()]
        assert costs == sorted(costs)

    def test_covers_treats_no_layer_as_covered(self):
        assert Capability(name="x", cost=1).covers(None)
