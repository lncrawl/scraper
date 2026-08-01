"""Describing a failure in words, without prescribing what to do about it.

The line this module is built on: everything here is derived from this package's own
types, so two consumers writing it themselves would disagree about the same failure.
Advice is the other side of that line — "configure a proxy in the crawler settings" names
one application's UI — and its absence here is the thing most likely to erode, so it is
asserted rather than assumed.
"""

from __future__ import annotations

import pytest
from PIL import UnidentifiedImageError
from requests import HTTPError, RequestException, Response

from scraper import failure
from scraper.browser import RenderError, SolveError
from scraper.exceptions import (
    Aborted,
    Blocked,
    Exhausted,
    Impassable,
    MissingDependency,
    Poisoned,
    TierUnavailable,
)
from scraper.layers import LAYERS, Layer, Trait


def http(code: int) -> HTTPError:
    error = HTTPError("boom")
    response = Response()
    response.status_code = code
    error.response = response
    return error


class TestNamingTheFailure:
    @pytest.mark.parametrize(
        "error,expected",
        [
            (Impassable(Layer.WEB_BOT_AUTH, "no bypass", "u"), failure.IMPASSABLE),
            (Exhausted(Layer.BOT_MANAGEMENT, "gave up", "u"), failure.EXHAUSTED),
            (Blocked(Layer.IP_REPUTATION, "refused", "u"), failure.BLOCKED),
            (Poisoned("u", "decoy"), failure.POISONED),
            (TierUnavailable("clearance", "none", "u"), failure.TIER_UNAVAILABLE),
            (MissingDependency("Pillow", "images"), failure.MISSING_DEPENDENCY),
            (RenderError("never appeared"), failure.RENDER_FAILED),
            (SolveError("no cookie"), failure.SOLVE_FAILED),
            (Aborted("cancelled"), failure.ABORTED),
            (http(404), failure.HTTP_ERROR),
            (UnidentifiedImageError("bad"), failure.BAD_IMAGE),
            (RequestException("reset"), failure.UNREACHABLE),
            (RuntimeError("other"), failure.FAILED),
        ],
    )
    def test_each_class_of_failure_has_a_stable_name(self, error, expected):
        # Consumers key health tallies and API fields on these, so a drifting name
        # silently splits one failure into two wherever it is counted.
        assert failure.failure_kind(error) == expected

    def test_a_block_with_no_layer_is_unreachable_not_blocked(self):
        # "Refused" and "never arrived" are different things to act on.
        assert failure.failure_kind(Blocked(None, "no route", "u")) == failure.UNREACHABLE

    def test_every_name_is_listed(self):
        assert set(failure.FAILURE_KINDS) == {
            failure.failure_kind(e)
            for e in (
                Impassable(Layer.WEB_BOT_AUTH, "x", "u"),
                Exhausted(Layer.BOT_MANAGEMENT, "x", "u"),
                Blocked(Layer.IP_REPUTATION, "x", "u"),
                Blocked(None, "x", "u"),
                Poisoned("u", "x"),
                TierUnavailable("t", "x", "u"),
                MissingDependency("p", "w"),
                RenderError("x"),
                SolveError("x"),
                Aborted("x"),
                http(500),
                UnidentifiedImageError("x"),
                RuntimeError("x"),
            )
        }


class TestWhatIsPermanent:
    def test_a_held_secret_and_decoy_content_are_permanent(self):
        assert failure.is_permanent(Impassable(Layer.WEB_BOT_AUTH, "x", "u"))
        assert failure.is_permanent(Poisoned("u", "x"))

    def test_exhausted_is_not(self):
        # It says every tier *this configuration* reaches was spent. A proxy or a browser
        # may still get through, so calling it permanent would retire a source over a
        # setting.
        assert not failure.is_permanent(Exhausted(Layer.BOT_MANAGEMENT, "x", "u"))


class TestReadingTheLayer:
    def test_the_layer_comes_from_the_error_not_the_status(self):
        blocked = Blocked(Layer.BOT_MANAGEMENT, "x", "u")
        assert failure.blocking_layer(blocked) is Layer.BOT_MANAGEMENT
        assert failure.blocking_layer(http(403)) is None

    def test_every_trait_has_a_clause(self):
        for trait in Trait:
            assert failure.reads(trait)

    def test_reads_is_a_clause_not_a_sentence(self):
        # It has to sit inside "It reads {…}, so {the caller's remedy}." — a full stop
        # here would make the seam visible the first time either side rephrased.
        for trait in Trait:
            clause = failure.reads(trait)
            assert not clause.endswith(".")
            assert clause[0].islower()


class TestTheProseItProduces:
    def test_the_headline_names_what_happened(self):
        assert (
            "refused"
            in failure.summarise(Blocked(Layer.IP_REPUTATION, "x", "u"), url="https://s.test/")[0]
        )

    def test_the_url_and_status_ride_on_the_headline(self):
        first = failure.summarise(http(503), url="https://s.test/p")[0]
        assert "(HTTP 503)" in first and "for https://s.test/p" in first

    def test_a_layer_contributes_its_summary(self):
        parts = failure.summarise(Blocked(Layer.IP_REPUTATION, "x", "u"))
        assert any(str(Layer.IP_REPUTATION) in p for p in parts)
        assert any(LAYERS[Layer.IP_REPUTATION].summary in p for p in parts)

    def test_an_unattributed_block_says_so_rather_than_guessing(self):
        parts = failure.summarise(Blocked(None, "x", "u"))
        assert failure.NO_LAYER_NOTE in parts

    def test_the_remedy_sentence_is_left_to_the_caller(self):
        """The contract of the split, stated as a structure rather than a word list.

        An earlier version of this test scanned the prose for words like "configure",
        which failed twice for the right reason: a layer's own `summary` may tell you to
        configure something *in scraper*, and "No configured capability can serve this
        request" is a statement of fact. Neither is advice. What actually must not happen
        is this module completing the sentence — "It reads {trait}, so {remedy}." — whose
        second half names a particular application's settings.
        """
        for layer in Layer:
            parts = failure.summarise(Blocked(layer, "x", "u"))
            assert parts[-1].startswith(str(layer)), parts[-1]
            assert not any(p.startswith("It reads ") for p in parts)

    def test_this_module_holds_no_remedy_table(self):
        # The mapping from stance to what-to-do belongs to the consumer. Its absence here
        # is the whole reason the objective half could move into a library at all.
        assert not hasattr(failure, "_REMEDY")
        assert not hasattr(failure, "_ADVICE")
        assert all("Stance" not in name for name in dir(failure))

    def test_a_status_note_carries_no_inference_about_why(self):
        # That a 404 means a source's URLs moved is a guess only the consumer can make.
        assert failure.status_note(404) == "The page is not there."
        assert failure.status_note(418) is None

    def test_a_detail_is_not_repeated_when_the_error_folds_it_in(self):
        blocked = Blocked(Layer.IP_REPUTATION, "refused at L1", "https://s.test/")
        parts = failure.summarise(blocked)
        assert sum(1 for p in parts if "refused at L1" in p) <= 1
