"""Persistence of what was learned — the only way the possess side can accrue."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scraper.identity import Clearance, Identity
from scraper.layers import Layer
from scraper.memory import SCHEMA, Memory, OriginProfile


def test_an_origin_is_keyed_by_host_not_by_url():
    # The behavioural model is per zone, so http and https pages of one site, and
    # every path on it, are one thing to remember.
    memory = Memory()
    assert memory.key("https://www.example.com/a/b?c=1") == memory.key("http://example.com/x")


def test_a_profile_is_created_on_first_sight():
    memory = Memory()
    profile = memory.profile("https://example.com/page")
    assert profile.origin == "example.com"
    assert profile.binding is None
    assert memory.profile("https://example.com/other") is profile


class TestTheLedger:
    def test_a_success_records_the_tier_and_clears_the_streak(self):
        memory = Memory()
        memory.record_failure("https://example.com/", Layer.MANAGED_CHALLENGE)
        memory.record_success("https://example.com/", tier="clearance")
        profile = memory.profile("https://example.com/")
        assert profile.tier == "clearance"
        assert profile.successes == 1
        assert profile.consecutive_failures == 0

    def test_failures_accumulate_so_recurrence_is_visible(self):
        # The planner promotes a diagnosis on recurrence, and this counter is the
        # only place that evidence lives.
        memory = Memory()
        for _ in range(3):
            memory.record_failure("https://example.com/", Layer.SUPER_BOT_FIGHT)
        assert memory.profile("https://example.com/").consecutive_failures == 3

    def test_the_binding_layer_survives_a_round_trip_as_a_number(self):
        memory = Memory()
        memory.record_failure("https://example.com/", Layer.CDP)
        assert memory.profile("https://example.com/").binding is Layer.CDP

    def test_an_unknown_layer_number_degrades_to_no_knowledge(self):
        # Written by a newer version that added a layer. A cold start is slow but
        # correct; guessing a neighbouring layer would not be.
        profile = OriginProfile(origin="example.com", binding_layer=999)
        assert profile.binding is None

    def test_the_interval_converges_down_slowly(self):
        # A site that let one request through quickly has not raised its limit, and
        # snapping to the fast value is how a run earns a throttle it then blames on
        # the address.
        memory = Memory()
        memory.record_success("https://example.com/", tier="direct", interval=10.0)
        memory.record_success("https://example.com/", tier="direct", interval=1.0)
        assert 8.0 < memory.profile("https://example.com/").interval < 10.0

    def test_a_failure_only_widens_the_interval(self):
        memory = Memory()
        memory.record_failure("https://example.com/", Layer.BEHAVIOURAL, interval=20.0)
        memory.record_failure("https://example.com/", Layer.BEHAVIOURAL, interval=5.0)
        assert memory.profile("https://example.com/").interval == 20.0


class TestClearances:
    def test_a_clearance_round_trips_through_the_store(self, tmp_path: Path):
        identity = Identity(exit_id="e1").pin("UA/1")
        clearance = Clearance(
            origin="https://example.com/",
            cookies={"cf_clearance": "abc", "__cf_bm": "def"},
            identity_token=identity.token(),
            user_agent="UA/1",
            expires_at=time.time() + 600,
        )
        path = tmp_path / "origins.json"
        with Memory(path, flush_every=0.0) as memory:
            memory.profile("https://example.com/").remember_clearance(clearance)
            memory.touch()

        restored = (
            Memory(path).profile("https://example.com/").clearance_for("https://example.com/")
        )
        assert restored is not None
        assert restored.cookies == clearance.cookies
        assert restored.usable_by(identity)

    def test_an_expired_clearance_is_not_handed_back(self):
        memory = Memory()
        profile = memory.profile("https://example.com/")
        profile.remember_clearance(
            Clearance(
                origin="https://example.com/",
                cookies={"cf_clearance": "abc"},
                identity_token="t",
                expires_at=time.time() - 1,
            )
        )
        assert profile.clearance_for("https://example.com/") is None

    def test_a_malformed_stored_clearance_is_ignored_not_raised(self):
        profile = OriginProfile(origin="example.com", clearance={"expires_at": "soon"})
        assert profile.clearance_for("https://example.com/") is None

    def test_no_clearance_is_a_clean_none(self):
        assert OriginProfile(origin="example.com").clearance_for("https://example.com/") is None


class TestEndpointsAndDecoys:
    def test_an_endpoint_is_recorded_once(self):
        profile = OriginProfile(origin="example.com")
        assert profile.note_endpoint("https://example.com/api/v1/items")
        assert not profile.note_endpoint("https://example.com/api/v1/items")

    def test_endpoints_are_bounded(self):
        profile = OriginProfile(origin="example.com")
        for index in range(100):
            profile.note_endpoint(f"https://example.com/api/{index}")
        assert len(profile.endpoints) <= 32
        assert profile.endpoints[-1].endswith("/99")

    def test_a_decoy_is_remembered_across_runs(self, tmp_path: Path):
        # The trap gives no error, so the memory of it is the whole defence.
        path = tmp_path / "origins.json"
        with Memory(path, flush_every=0.0) as memory:
            memory.profile("https://example.com/").note_decoy("https://example.com/maze/1")
            memory.touch()
        assert Memory(path).profile("https://example.com/").is_decoy("https://example.com/maze/1")

    def test_hitting_the_same_decoy_twice_does_not_fill_the_list_with_it(self):
        # The list is capped, so duplicates would evict genuinely distinct decoys —
        # and a maze serves the same URL back repeatedly.
        profile = Memory().profile("https://example.com/")
        for _ in range(5):
            profile.note_decoy("https://example.com/maze/1")
        assert profile.decoys == ["https://example.com/maze/1"]


class TestTheFile:
    def test_it_is_written_owner_only(self, tmp_path: Path):
        path = tmp_path / "origins.json"
        with Memory(path, flush_every=0.0) as memory:
            memory.record_success("https://example.com/", tier="direct")
        assert path.exists()
        if os.name != "nt":
            assert oct(path.stat().st_mode)[-3:] == "600"

    def test_nothing_is_written_without_a_path(self):
        memory = Memory()
        memory.record_success("https://example.com/", tier="direct")
        memory.close()  # must not raise

    def test_an_unreadable_file_degrades_to_a_cold_start(self, tmp_path: Path):
        path = tmp_path / "origins.json"
        path.write_text("{not json", "utf-8")
        assert Memory(path).profile("https://example.com/").binding is None

    def test_a_foreign_schema_is_discarded_rather_than_interpreted(self, tmp_path: Path):
        path = tmp_path / "origins.json"
        path.write_text(json.dumps({"schema": SCHEMA + 99, "profiles": {"x": {}}}), "utf-8")
        memory = Memory(path)
        assert memory.profile("https://x/").successes == 0

    def test_unknown_fields_in_a_profile_are_dropped(self, tmp_path: Path):
        # Forward compatibility in the direction that matters: an older reader must
        # not crash on a file written by a newer writer.
        path = tmp_path / "origins.json"
        path.write_text(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "profiles": {
                        "example.com": {"origin": "example.com", "successes": 4, "future": 1}
                    },
                }
            ),
            "utf-8",
        )
        assert Memory(path).profile("https://example.com/").successes == 4

    def test_a_profiles_block_of_the_wrong_shape_is_a_cold_start(self, tmp_path: Path):
        path = tmp_path / "origins.json"
        path.write_text(json.dumps({"schema": SCHEMA, "profiles": ["example.com"]}), "utf-8")
        assert Memory(path).profile("https://example.com/").successes == 0

    def test_one_corrupt_profile_does_not_discard_the_rest_of_the_file(self, tmp_path: Path):
        # The store is the only way the possess side accrues anything. Throwing the
        # whole file away over one bad entry restarts every origin's history.
        path = tmp_path / "origins.json"
        path.write_text(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "profiles": {
                        "broken.test": "not a profile at all",
                        "example.com": {"origin": "example.com", "successes": 7},
                    },
                }
            ),
            "utf-8",
        )
        memory = Memory(path)
        assert memory.profile("https://example.com/").successes == 7
        assert memory.profile("https://broken.test/").successes == 0

    def test_a_write_that_cannot_land_warns_rather_than_failing_the_scrape(self, tmp_path: Path):
        # Losing the store costs a cold start, not correctness, so a read-only or
        # occupied path must not take the run down with it.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory", "utf-8")
        memory = Memory(blocker / "sub" / "origins.json", flush_every=0.0)
        memory.record_success("https://example.com/", tier="direct")
        memory.close()
