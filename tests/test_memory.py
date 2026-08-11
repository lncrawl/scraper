"""Persistence of what was learned — the only way the possess side can accrue."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scraper.identity import Clearance, Identity
from scraper.layers import Layer
from scraper.memory import DECOY_TTL, SCHEMA, Memory, OriginProfile


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
        assert list(profile.decoys) == ["https://example.com/maze/1"]

    def test_a_decoy_verdict_expires(self):
        # A wrong verdict must cost a re-check rather than the URL. Nothing else in the
        # store forgets a conclusion this specific, so it is worth pinning.
        profile = Memory().profile("https://example.com/")
        profile.note_decoy("https://example.com/maze/1")
        assert profile.is_decoy("https://example.com/maze/1")

        profile.decoys["https://example.com/maze/1"] = time.time() - DECOY_TTL - 1
        assert not profile.is_decoy("https://example.com/maze/1")

    def test_noting_a_decoy_drops_the_ones_that_expired(self):
        profile = Memory().profile("https://example.com/")
        profile.decoys["https://example.com/old"] = time.time() - DECOY_TTL - 1
        profile.note_decoy("https://example.com/new")
        assert list(profile.decoys) == ["https://example.com/new"]


class TestValidators:
    def test_a_pair_round_trips_per_endpoint(self):
        profile = OriginProfile(origin="example.com")
        assert profile.note_validators("https://example.com/a", etag='W/"1"')
        assert profile.note_validators("https://example.com/b", last_modified="Mon, 01 Jan 2026")
        assert profile.validators_for("https://example.com/a") == {
            "etag": 'W/"1"',
            "last_modified": "",
        }
        assert profile.validators_for("https://example.com/c") == {}

    def test_recording_the_same_pair_twice_is_not_a_change(self):
        # The caller uses the return value to decide whether the store needs writing,
        # and a table of contents that has not moved is the common case.
        profile = OriginProfile(origin="example.com")
        assert profile.note_validators("https://example.com/a", etag='W/"1"')
        assert not profile.note_validators("https://example.com/a", etag='W/"1"')
        assert profile.note_validators("https://example.com/a", etag='W/"2"')

    def test_a_response_with_neither_validator_records_nothing(self):
        profile = OriginProfile(origin="example.com")
        assert not profile.note_validators("https://example.com/a")
        assert profile.validators == {}

    def test_the_store_is_bounded_and_refreshing_keeps_an_endpoint_alive(self):
        from scraper.memory import MAX_VALIDATORS

        profile = OriginProfile(origin="example.com")
        profile.note_validators("https://example.com/first", etag='W/"first"')
        for index in range(MAX_VALIDATORS):
            profile.note_validators(f"https://example.com/{index}", etag=f'W/"{index}"')
            # Recording it again has to move it to the back, or the endpoint being
            # polled most often is the one evicted.
            profile.note_validators("https://example.com/first", etag='W/"first"')
        assert len(profile.validators) == MAX_VALIDATORS
        assert "https://example.com/first" in profile.validators


class TestTheBound:
    def test_an_origin_unseen_for_too_long_is_dropped(self, tmp_path: Path):
        # A stored profile is a conclusion about a site's current configuration. A
        # month-old one is worth less than the cold start that replaces it, and an
        # edge that has been turned off would otherwise keep sending every later run
        # up the ladder.
        path = tmp_path / "origins.json"
        with Memory(path, flush_every=0.0) as memory:
            memory.record_failure("https://old.test/", Layer.TURNSTILE)
            memory.record_failure("https://fresh.test/", Layer.TURNSTILE)
            memory.profile("https://old.test/").last_seen = time.time() - 40 * 86400
            memory.touch()

        reloaded = Memory(path)
        assert reloaded.origins() == ["fresh.test"]

    def test_the_store_does_not_grow_past_its_cap(self):
        memory = Memory(max_origins=3)
        for index in range(10):
            memory.record_success(f"https://host{index}.test/", tier="direct")
        assert memory.count == 3

    def test_the_least_recently_seen_go_first(self):
        memory = Memory(max_origins=2)
        memory.record_success("https://a.test/", tier="direct")
        memory.record_success("https://b.test/", tier="direct")
        memory.profile("https://a.test/").last_seen = time.time() - 60
        memory.record_success("https://c.test/", tier="direct")
        assert set(memory.origins()) == {"b.test", "c.test"}

    def test_the_origin_being_asked_for_is_never_the_one_evicted(self):
        # profile() hands back a live object the retrieval loop then mutates. Evicting
        # the entry it just created would silently discard everything that retrieval
        # learns, including the clearance a browser was launched for.
        memory = Memory(max_origins=1)
        memory.record_success("https://first.test/", tier="direct")
        profile = memory.profile("https://second.test/")
        assert memory.origins() == ["second.test"]
        assert memory.profile("https://second.test/") is profile

    def test_forgetting_everything_keeps_the_store_usable(self):
        memory = Memory()
        memory.record_success("https://example.com/", tier="direct")
        memory.clear()
        assert memory.count == 0
        assert memory.profile("https://example.com/").successes == 0


class TestInventory:
    def test_origins_come_back_most_recently_seen_first(self):
        memory = Memory()
        memory.record_success("https://old.test/", tier="direct")
        memory.record_success("https://new.test/", tier="direct")
        memory.profile("https://old.test/").last_seen = time.time() - 600
        assert memory.origins() == ["new.test", "old.test"]

    def test_a_caller_cannot_edit_the_store_through_the_snapshot(self):
        memory = Memory()
        memory.profile("https://example.com/").note_decoy("https://example.com/maze")
        snapshot = memory.profiles()
        snapshot[0].decoys.clear()
        snapshot[0].successes = 99
        assert memory.profile("https://example.com/").is_decoy("https://example.com/maze")
        assert memory.profile("https://example.com/").successes == 0

    def test_an_export_carries_the_clearance_window_but_not_the_cookies(self):
        # The cookies are the one secret in the store, and a status page asks whether a
        # clearance is held and for how long — not what it is.
        memory = Memory()
        memory.profile("https://example.com/").remember_clearance(
            Clearance(
                origin="https://example.com/",
                cookies={"cf_clearance": "secret-value"},
                identity_token="t",
                user_agent="UA/1",
                expires_at=1234.0,
            )
        )
        exported = memory.export()
        assert json.dumps(exported)
        assert exported["example.com"]["clearance"] == {
            "expires_at": 1234.0,
            "user_agent": "UA/1",
        }
        assert "secret-value" not in json.dumps(exported)

    def test_an_origin_with_no_clearance_exports_none(self):
        memory = Memory()
        memory.record_success("https://example.com/", tier="direct")
        assert memory.export()["example.com"]["clearance"] is None

    def test_forgetting_one_origin_reports_whether_there_was_anything(self, tmp_path: Path):
        memory = Memory(tmp_path / "origins.json", flush_every=0.0)
        memory.record_failure("https://example.com/", Layer.IP_REPUTATION)
        assert memory.forget("https://example.com/page")
        assert not memory.forget("https://example.com/page")
        assert memory.count == 0


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
