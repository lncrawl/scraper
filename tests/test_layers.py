"""The model itself: the table, the bound, and the transport group."""

from __future__ import annotations

import pytest

from scraper.layers import (
    FORGEABLE,
    IMPASSABLE,
    LAYERS,
    TRANSPORT_LAYERS,
    Layer,
    Stance,
    Trait,
    expand,
    info,
    is_forgeable,
    is_impassable,
    marginal_gain,
    stance,
    trait,
    weakest,
)


def test_every_layer_is_described():
    # The table is what the rest of the package reads to decide what to do, so a
    # layer missing from it is a KeyError at the worst possible moment.
    assert set(LAYERS) == set(Layer)
    for layer, described in LAYERS.items():
        assert described.layer is layer
        assert described.title and described.summary


def test_layers_are_numbered_in_request_order():
    assert [layer.value for layer in Layer] == list(range(1, 20))


def test_only_secret_bearing_layers_are_impassable():
    # The whole point of the distinction: these two read something held, everything
    # else reads something reproducible or accumulable.
    assert IMPASSABLE == {Layer.WEB_BOT_AUTH, Layer.ACCESS}
    for layer in IMPASSABLE:
        assert LAYERS[layer].stance is Stance.REFUSE


def test_forgeable_means_emit():
    assert FORGEABLE == {layer for layer in Layer if trait(layer) is Trait.EMIT}
    assert is_forgeable(Layer.TLS_FINGERPRINT)
    assert not is_forgeable(Layer.BEHAVIOURAL)
    assert is_impassable(Layer.ACCESS)


def test_possess_layers_are_the_hard_ones():
    possess = {layer for layer in Layer if trait(layer) is Trait.POSSESS}
    assert possess == {Layer.BEHAVIOURAL, Layer.WEB_BOT_AUTH, Layer.ACCESS}


class TestTransportGroup:
    def test_the_group_is_layers_two_to_five(self):
        assert TRANSPORT_LAYERS == {
            Layer.TLS_FINGERPRINT,
            Layer.POST_QUANTUM,
            Layer.HTTP_FRAMES,
            Layer.HEADER_ORDER,
        }

    def test_declaring_one_declares_all_four(self):
        # A tier that reproduces a browser's ClientHello reproduces its frame order
        # too; listing them separately in a reach set would let the planner think a
        # stronger tier was needed for something already covered.
        assert expand({Layer.TLS_FINGERPRINT}) == TRANSPORT_LAYERS

    def test_unrelated_layers_are_left_alone(self):
        assert expand({Layer.BEHAVIOURAL}) == {Layer.BEHAVIOURAL}

    def test_the_group_travels_with_its_company(self):
        assert expand({Layer.HEADER_ORDER, Layer.CDP}) == TRANSPORT_LAYERS | {Layer.CDP}


class TestTheBound:
    def test_the_weakest_layer_wins(self):
        odds = {Layer.IP_REPUTATION: 0.1, Layer.TLS_FINGERPRINT: 0.99}
        assert weakest(odds) == (Layer.IP_REPUTATION, 0.1)

    def test_a_tie_resolves_to_the_layer_met_first(self):
        odds = {Layer.BEHAVIOURAL: 0.2, Layer.TLS_FINGERPRINT: 0.2}
        assert weakest(odds) == (Layer.TLS_FINGERPRINT, 0.2)

    def test_nothing_to_weigh(self):
        assert weakest({}) is None

    def test_fixing_the_wrong_layer_gains_nothing(self):
        # The arithmetic behind the policy in the planner: a strategy already failing
        # on address reputation does not improve by perfecting its TLS profile.
        odds = {Layer.IP_REPUTATION: 0.05, Layer.TLS_FINGERPRINT: 0.6}
        assert marginal_gain(odds, Layer.TLS_FINGERPRINT, 1.0) == 0.0

    def test_fixing_the_binding_layer_moves_the_bound(self):
        odds = {Layer.IP_REPUTATION: 0.05, Layer.TLS_FINGERPRINT: 0.6}
        assert marginal_gain(odds, Layer.IP_REPUTATION, 0.9) == pytest.approx(0.55)

    def test_the_gain_stops_at_the_next_weakest(self):
        # Raising the minimum only helps up to whatever becomes the minimum next,
        # which is why "just improve everything" is not a plan.
        odds = {Layer.IP_REPUTATION: 0.05, Layer.BEHAVIOURAL: 0.3}
        assert marginal_gain(odds, Layer.IP_REPUTATION, 1.0) == pytest.approx(0.25)

    def test_a_gain_is_never_negative(self):
        odds = {Layer.IP_REPUTATION: 0.5}
        assert marginal_gain(odds, Layer.IP_REPUTATION, 0.1) == 0.0

    def test_no_odds_means_no_gain(self):
        assert marginal_gain({}, Layer.TLS_FINGERPRINT, 1.0) == 0.0


def test_layers_render_readably():
    assert str(Layer.BEHAVIOURAL).startswith("L8 ")


def test_a_layer_carries_its_own_description():
    # The table is the package's documentation of the model, and it is read at
    # runtime — an exception's message is built from it.
    found = info(Layer.BEHAVIOURAL)
    assert found is LAYERS[Layer.BEHAVIOURAL]
    assert found.trait is Trait.POSSESS
    assert found.title and found.summary


def test_a_layer_reports_what_this_library_does_about_it():
    # The stance is what the planner is ultimately reading: rotating away from a
    # possessed-property layer discards the very history it measures.
    assert stance(Layer.IP_REPUTATION) is Stance.LEASE
    assert stance(Layer.BEHAVIOURAL) is Stance.ACCUMULATE
    assert stance(Layer.ACCESS) is Stance.REFUSE
