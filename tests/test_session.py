"""The retrieval loop end to end, against a fake transport.

These are the tests that prove the model is actually wired to behaviour rather than
merely described in docstrings. The ones worth reading first:

- :class:`TestSolveOnceReuseMany` — the expensive tier runs once, not per request.
- :class:`TestRotationIsEarned` — a throttle does not spend an address, and a
  blocked address is not swapped for an equally blocked one.
- :class:`TestNothingToRetry` — the two layers that read a secret raise.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests

from scraper import ExitKind, ExitSpec, Scraper, ScraperConfig, SharedState
from scraper.browser import BrowserSolver, SolveResult
from scraper.exceptions import Aborted, Blocked, Exhausted, Impassable, Poisoned, ScraperError
from scraper.exits import TorPoolSpec
from scraper.layers import Layer

from .conftest import BLOCK_BODY, CHALLENGE_BODY, FakeTransport, make_response

PAGE = "<html><body><h1>Chapter One</h1><a href='/next'>next</a></body></html>"
URL = "https://example.com/novel/chapter-1"


class RecordingSolver(BrowserSolver):
    """A solver that never launches anything and counts how often it was asked."""

    name = "recording"

    def __init__(self, user_agent: str = "Mozilla/5.0 Chrome/140.0.0.0") -> None:
        self.calls: List[Dict[str, Any]] = []
        self.user_agent = user_agent

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        self.calls.append({"url": url, "proxy": proxy, "profile_dir": profile_dir})
        return SolveResult(
            cookies={"cf_clearance": f"cleared-{len(self.calls)}", "__cf_bm": "bm"},
            user_agent=self.user_agent,
        )


def scraper_for(transport: FakeTransport, **overrides: Any) -> Scraper:
    from scraper import PacingPolicy

    settings: Dict[str, Any] = {
        "transport": transport,
        "pacing": PacingPolicy(interval=0.0, floor=0.0, warmup=False, pause_chance=0.0),
        "remember": False,
        "guard_topic": False,
        "raise_for_status": False,
    }
    settings.update(overrides)
    return Scraper(origin="https://example.com", config=ScraperConfig(**settings))


class TestAJavaScriptRedirect:
    """A 200 whose whole body is `window.location.replace(...)`.

    Followed rather than escalated, because the destination is emitted in the HTML —
    running the script would produce the URL already sitting there in plain text.
    """

    STUB = (
        "<html><head><title>Loading...</title></head><body><script>"
        "window.location.replace('https://example.com/real?js=TOKEN');"
        "</script></body></html>"
    )

    def _hop_once(self) -> FakeTransport:
        def handler(method: str, url: str, kwargs: Any) -> Any:
            if "js=TOKEN" in url:
                return make_response(body=PAGE, url=url)
            return make_response(body=self.STUB, url=url)

        return FakeTransport(handler=handler)

    def test_the_destination_is_fetched_and_returned(self):
        transport = self._hop_once()
        with scraper_for(transport) as scraper:
            response = scraper.get(URL)
        assert "Chapter One" in response.text
        assert transport.urls == [URL, "https://example.com/real?js=TOKEN"]

    def test_what_is_learned_is_filed_under_the_url_that_was_asked_for(self, tmp_path: Path):
        """Otherwise every run starts cold on a site it already solved.

        The hop moves the request target, not the caller's address. Keying memory on
        the destination instead left `knows(url)` empty, so the working tier was never
        recalled and the redirect was re-walked from scratch every time. An HTTP
        redirect already behaves this way.
        """
        transport = self._hop_once()
        # A real data_dir, but the test's own: `remember=True` with the default path
        # writes into the developer's cache directory.
        with scraper_for(transport, remember=True, data_dir=tmp_path) as scraper:
            scraper.get(URL)
            assert scraper.knows(URL).tier == "direct"

    def test_a_redirect_loop_stops_instead_of_spinning(self):
        # A hop is deliberately not charged as an attempt, so without its own cap two
        # pages pointing at each other would never terminate.
        forever = FakeTransport(handler=lambda m, u, k: make_response(body=self.STUB, url=u))
        with scraper_for(forever) as scraper:
            with pytest.raises(Blocked) as caught:
                scraper.get(URL)
        assert "redirect" in str(caught.value).lower()
        assert len(forever.urls) <= 5


class TestTheEasyPath:
    def test_a_clean_page_comes_back(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            response = scraper.get(URL)
        assert response.status_code == 200
        assert "Chapter One" in response.text
        assert transport.urls == [URL]

    def test_the_cheapest_tier_is_used_when_nothing_blocks(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport, browser=RecordingSolver()) as scraper:
            scraper.get(URL)
            assert scraper.knows(URL).tier == "direct"

    def test_soup_json_and_files_all_go_through_the_same_loop(self, tmp_path: Path):
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body='{"ok": true}' if "api" in url else PAGE,
                url=url,
                headers={"content-type": "application/json" if "api" in url else "text/html"},
            )
        )
        with scraper_for(transport) as scraper:
            assert scraper.get_soup(URL).select_one("h1").text == "Chapter One"
            assert scraper.get_json("https://example.com/api/x") == {"ok": True}
            target = scraper.get_file("https://example.com/cover.jpg", tmp_path / "c.jpg")
        assert target.read_text("utf-8") == PAGE

    def test_a_navigation_records_the_referrer_for_the_next_one(self):
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=PAGE, url=url)
        )
        with scraper_for(transport) as scraper:
            scraper.get("https://example.com/list")
            scraper.get(URL)
        assert transport.headers_of(1)["referer"] == "https://example.com/list"

    def test_a_sub_resource_stays_out_of_the_chain(self):
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=PAGE, url=url)
        )
        with scraper_for(transport) as scraper:
            scraper.get("https://example.com/list")
            scraper.get("https://example.com/cover.jpg", navigation=False)
            scraper.get(URL)
        # The image did not become the referrer for the page that followed it.
        assert transport.headers_of(2)["referer"] == "https://example.com/list"

    def test_a_404_is_returned_rather_than_diagnosed(self):
        # It is the site's answer about a path and says nothing about the client.
        transport = FakeTransport([make_response(404, "gone", url=URL)])
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 404
            assert scraper.knows(URL).binding is None

    def test_raise_for_status_is_honoured_when_asked_for(self):
        transport = FakeTransport([make_response(404, "gone", url=URL)])
        with scraper_for(transport, raise_for_status=True) as scraper:
            with pytest.raises(requests.HTTPError):
                scraper.get(URL)


class TestSolveOnceReuseMany:
    """A challenge costs one browser launch, not one per request."""

    def test_a_challenge_escalates_to_the_solver_and_then_succeeds(self):
        transport = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        solver = RecordingSolver()
        with scraper_for(transport, browser=solver) as scraper:
            assert "Chapter One" in scraper.get(URL).text
        assert len(solver.calls) == 1
        assert scraper.knows(URL).tier == "clearance"

    def test_the_clearance_is_reused_on_later_requests(self):
        transport = FakeTransport(
            handler=lambda method, url, kwargs: (
                make_response(403, CHALLENGE_BODY, url=url)
                if "cf_clearance" not in (kwargs.get("cookies") or {})
                else make_response(body=PAGE, url=url)
            )
        )
        solver = RecordingSolver()
        with scraper_for(transport, browser=solver) as scraper:
            for index in range(5):
                assert scraper.get(f"{URL}?p={index}").status_code == 200
        assert len(solver.calls) == 1, "the browser ran more than once"

    def test_the_browsers_user_agent_is_reproduced_afterwards(self):
        # The clearance is bound to it, so replaying the cookies under any other
        # User-Agent cannot work.
        transport = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        solver = RecordingSolver(user_agent="Mozilla/5.0 Chrome/141.2.3.4")
        with scraper_for(transport, browser=solver) as scraper:
            scraper.get(URL)
        assert transport.headers_of(1)["user-agent"] == "Mozilla/5.0 Chrome/141.2.3.4"
        # And the client hints were re-derived from it rather than left contradicting.
        assert '"141"' in transport.headers_of(1)["sec-ch-ua"]

    def test_the_clearance_cookies_are_sent_with_the_replay(self):
        transport = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        with scraper_for(transport, browser=RecordingSolver()) as scraper:
            scraper.get(URL)
        assert transport.calls[1][2]["cookies"]["cf_clearance"] == "cleared-1"

    def test_the_solve_runs_on_the_address_the_requests_will_use(self):
        # Solving on one exit and fetching from another produces a clearance that is
        # dead on arrival, which then reads as the solver being broken.
        transport = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        exits = [ExitSpec(url="http://user:pw@res.test:8000", kind=ExitKind.RESIDENTIAL)]
        solver = RecordingSolver()
        with scraper_for(transport, browser=solver, exits=exits) as scraper:
            scraper.get(URL)
        assert solver.calls[0]["proxy"] == "http://user:pw@res.test:8000"

    def test_turnstile_is_solved_by_the_same_tier(self):
        from .conftest import TURNSTILE_BODY

        transport = FakeTransport(
            [make_response(200, TURNSTILE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        solver = RecordingSolver()
        with scraper_for(transport, browser=solver) as scraper:
            assert scraper.get(URL).status_code == 200
        assert len(solver.calls) == 1

    def test_a_challenge_behind_a_200_is_not_mistaken_for_content(self):
        # Without this the scrape reports success and collects an interstitial.
        transport = FakeTransport([make_response(200, CHALLENGE_BODY, url=URL)])
        with scraper_for(transport, max_attempts=2) as scraper:
            with pytest.raises(Exhausted) as caught:
                scraper.get(URL)
        assert caught.value.layer is Layer.MANAGED_CHALLENGE

    def test_without_a_solver_the_error_names_what_is_missing(self):
        transport = FakeTransport([make_response(403, CHALLENGE_BODY, url=URL)])
        with scraper_for(transport) as scraper:
            with pytest.raises(Exhausted, match="browser solver"):
                scraper.get(URL)


class TestRotationIsEarned:
    def test_a_throttle_slows_down_and_keeps_the_address(self):
        transport = FakeTransport(
            [
                make_response(429, "slow down", url=URL, headers={"retry-after": "0"}),
                make_response(body=PAGE, url=URL),
            ]
        )
        exits = [ExitSpec(url="http://res.test:1", kind=ExitKind.RESIDENTIAL)]
        with scraper_for(transport, exits=exits) as scraper:
            before = scraper.exits.lease("example.com").exit_id
            assert scraper.get(URL).status_code == 200
            assert scraper.exits.lease("example.com").exit_id == before

    def test_a_throttle_widens_the_learned_interval(self):
        transport = FakeTransport(
            [
                make_response(429, "slow", url=URL, headers={"retry-after": "0"}),
                make_response(body=PAGE, url=URL),
            ]
        )
        with scraper_for(transport) as scraper:
            scraper.pacer.learn("example.com", 1.0)
            scraper.get(URL)
            assert scraper.pacer.interval_for("example.com") > 1.0

    def test_a_firewall_block_rotates_when_a_better_address_exists(self):
        transport = FakeTransport(
            [make_response(403, BLOCK_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        exits = [
            ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL, label="a"),
            ExitSpec(url="http://b.test:1", kind=ExitKind.RESIDENTIAL, label="b"),
        ]
        with scraper_for(transport, exits=exits) as scraper:
            before = scraper.exits.lease("example.com").exit_id
            assert scraper.get(URL).status_code == 200
            assert scraper.exits.lease("example.com").exit_id != before

    def test_rotating_between_published_ranges_stops_with_an_explanation(self):
        # Every Tor exit is on the same lists, so the replacement is blocked for the
        # same reason. Saying so beats burning the pool to find out.
        transport = FakeTransport([make_response(403, BLOCK_BODY, url=URL)])
        with scraper_for(transport, exits=[TorPoolSpec(api_url="http://127.0.0.1:1")]) as scraper:
            with pytest.raises(Exhausted) as caught:
                scraper.get(URL)
        assert caught.value.layer is Layer.IP_REPUTATION
        assert "residential" in caught.value.detail

    def test_a_transport_failure_through_a_proxy_blames_the_exit(self):
        def explode(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            raise requests.ConnectionError("connection reset")

        transport = FakeTransport(handler=explode)
        exits = [
            ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL, label="a"),
            ExitSpec(url="http://b.test:1", kind=ExitKind.RESIDENTIAL, label="b"),
        ]
        with scraper_for(transport, exits=exits, max_attempts=3, max_rotations=1) as scraper:
            with pytest.raises(Exhausted) as caught:
                scraper.get(URL)
        # The address is spent, but nothing is attributed: the site never answered.
        assert caught.value.layer is None
        assert "exit unusable" in str(caught.value)

    def test_a_transport_failure_with_no_proxy_just_retries(self):
        attempts = {"n": 0}

        def flaky(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise requests.ConnectionError("dns hiccup")
            return make_response(body=PAGE, url=url)

        with scraper_for(FakeTransport(handler=flaky)) as scraper:
            assert scraper.get(URL).status_code == 200
        assert attempts["n"] == 2


class TestNothingToRetry:
    def test_a_login_gate_raises_immediately(self):
        transport = FakeTransport([make_response(401, "sign in", url=URL)])
        with scraper_for(transport) as scraper:
            with pytest.raises(Impassable) as caught:
                scraper.get(URL)
        assert caught.value.layer is Layer.ACCESS
        assert "account" in str(caught.value)
        assert len(transport.calls) == 1, "a secret-bearing layer must not be retried"

    def test_a_required_signature_names_the_only_route(self):
        transport = FakeTransport(
            [make_response(401, "", url=URL, headers={"www-authenticate": "Signature"})]
        )
        with scraper_for(transport) as scraper:
            with pytest.raises(Impassable) as caught:
                scraper.get(URL)
        assert caught.value.layer is Layer.WEB_BOT_AUTH

    def test_a_rejected_proxy_credential_is_not_reported_as_a_site_problem(self):
        transport = FakeTransport([make_response(407, "proxy auth required", url=URL)])
        with scraper_for(transport) as scraper:
            with pytest.raises(Exhausted) as caught:
                scraper.get(URL)
        assert "proxy" in caught.value.detail

    def test_attempts_are_bounded(self):
        transport = FakeTransport([make_response(502, "bad gateway", url=URL)])
        with scraper_for(transport, max_attempts=3) as scraper:
            with pytest.raises(Exhausted):
                scraper.get(URL)
        assert len(transport.calls) == 3


class TestWarmUp:
    def test_a_deep_page_is_preceded_by_the_homepage(self):
        from scraper import PacingPolicy

        transport = FakeTransport(
            handler=lambda method, url, kwargs: (
                make_response(body=PAGE, url=url)
                if url == "https://example.com/"
                else make_response(429, "slow", url=url, headers={"retry-after": "0"})
                if len(transport.calls) < 2
                else make_response(body=PAGE, url=url)
            )
        )
        config = ScraperConfig(
            transport=transport,
            pacing=PacingPolicy(interval=0.0, floor=0.0, warmup=True, warmup_ttl=0.0),
            remember=False,
            guard_topic=False,
            raise_for_status=False,
        )
        with Scraper(origin="https://example.com", config=config) as scraper:
            scraper.get(URL)
        assert "https://example.com/" in transport.urls


class TestDecoyContent:
    def test_off_topic_content_can_be_made_to_raise(self):
        # The one layer with no error response, so the check has to run on the way
        # out rather than on demand.
        on_topic = (
            "<html><body>chapter translation novel protagonist cultivation sect "
            "elder disciple sword qi realm</body></html>"
        )
        off_topic = (
            "<html><body>quarterly amortisation schedules reconciled against "
            "depreciating municipal bond covenants actuarial tables</body></html>"
        )
        pages = [on_topic] * 6 + [off_topic]
        index = {"n": 0}

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            body = pages[min(index["n"], len(pages) - 1)]
            index["n"] += 1
            return make_response(body=body, url=url)

        with scraper_for(
            FakeTransport(handler=serve), guard_topic=True, on_decoy="raise"
        ) as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            with pytest.raises(Poisoned):
                scraper.get("https://example.com/maze/1")

    def test_a_known_decoy_is_not_fetched_again(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.knows(URL).note_decoy(URL)
            with pytest.raises(Poisoned):
                scraper.get(URL)
        assert transport.calls == []

    def test_the_guard_is_off_by_configuration(self):
        transport = FakeTransport([make_response(body="totally unrelated words", url=URL)])
        with scraper_for(transport, guard_topic=False) as scraper:
            assert scraper.get(URL).status_code == 200

    def test_recorded_decoys_are_filtered_out_of_the_link_frontier(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            response = scraper.get(URL)
            scraper.knows(URL).note_decoy("https://example.com/next")
            assert [link.url for link in scraper.links(response.text, URL)] == []


class TestLearning:
    def test_the_conclusion_survives_into_the_next_run(self, tmp_path: Path):
        # Rediscovering the binding layer costs a failed request every run, and
        # failed requests are what the behavioural layer counts.
        from scraper import PacingPolicy

        def config_with(transport: FakeTransport, solver: BrowserSolver) -> ScraperConfig:
            return ScraperConfig(
                transport=transport,
                pacing=PacingPolicy(interval=0.0, floor=0.0, warmup=False),
                data_dir=tmp_path,
                guard_topic=False,
                raise_for_status=False,
                browser=solver,
            )

        first = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        with Scraper(config=config_with(first, RecordingSolver())) as scraper:
            scraper.get(URL)

        second = FakeTransport([make_response(body=PAGE, url=URL)])
        with Scraper(config=config_with(second, RecordingSolver())) as scraper:
            assert scraper.knows(URL).binding is Layer.MANAGED_CHALLENGE
            assert scraper.knows(URL).tier == "clearance"

    def test_state_can_be_shared_between_scrapers(self):
        # Two scrapers on one host must not look like two contradictory visitors.
        from scraper import PacingPolicy

        config = ScraperConfig(
            transport=FakeTransport(
                handler=lambda method, url, kwargs: make_response(body=PAGE, url=url)
            ),
            pacing=PacingPolicy(interval=0.0, floor=0.0, warmup=False),
            remember=False,
            guard_topic=False,
        )
        state = SharedState.create(config)
        one = Scraper(config=config, state=state)
        two = Scraper(config=config, state=state)
        try:
            one.get(URL)
            assert two.knows(URL).successes == 1
            assert one.exits.lease("example.com") is two.exits.lease("example.com")
        finally:
            one.close()
            two.close()
            state.close()

    def test_two_stores_on_one_path_do_not_merge(self, tmp_path: Path):
        # Found while auditing a consumer that built one state per domain: each store
        # holds every origin it knows and flush() writes the whole file, so the second
        # flush deletes what the first had learned. The remedy is one Memory, not one
        # state — hence the memory= argument below.
        from scraper.memory import Memory

        path = tmp_path / "origins.json"
        first = Memory(path, flush_every=0.0)
        second = Memory(path, flush_every=0.0)
        first.record_success("https://a.test/", tier="direct")
        second.record_success("https://b.test/", tier="direct")
        assert Memory(path).origins() == ["b.test"]

    def test_one_memory_can_back_several_states(self, tmp_path: Path):
        from scraper.memory import Memory

        path = tmp_path / "origins.json"
        config = ScraperConfig(data_dir=tmp_path)
        memory = Memory(path, flush_every=0.0)
        one = SharedState.create(config, memory=memory)
        two = SharedState.create(config, memory=memory)
        one.memory.record_success("https://a.test/", tier="direct")
        two.memory.record_success("https://b.test/", tier="direct")
        assert set(Memory(path).origins()) == {"a.test", "b.test"}


class TestControl:
    def test_an_abort_stops_the_next_request(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.abort()
            with pytest.raises(Aborted):
                scraper.get(URL)
        assert transport.calls == []

    def test_a_download_stops_mid_stream(self, tmp_path: Path):
        # The guarantee worth pinning down: a long download must not have to finish
        # before a cancelled job notices.
        signal_holder: Dict[str, threading.Event] = {}

        class Chunked(FakeTransport):
            @contextmanager
            def stream(self, method: str, url: str, **kwargs: Any):
                response = make_response(body="", url=url)

                def chunks():
                    yield b"first"
                    signal_holder["signal"].set()
                    for _ in range(1000):
                        yield b"more"

                yield response, chunks()

        transport = Chunked()
        with scraper_for(transport) as scraper:
            signal_holder["signal"] = scraper.signal
            with pytest.raises(Aborted):
                scraper.get_file("https://example.com/big.bin", tmp_path / "big.bin")
        # The partial file was never left behind: the write is atomic.
        assert not (tmp_path / "big.bin").exists()

    def test_one_request_can_be_cancelled_without_stopping_the_others(self):
        # The scraper is meant to be shared by an origin's callers, so cancelling one
        # job through the shared attribute cancelled all of them — which is what made
        # a consumer keep a scraper per thread and lose the per-origin state that
        # sharing exists for.
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            cancelled = threading.Event()
            cancelled.set()
            with pytest.raises(Aborted):
                scraper.get(URL, signal=cancelled)
            assert transport.calls == []
            assert scraper.get(URL).status_code == 200

    def test_an_abort_still_stops_a_request_carrying_its_own_signal(self):
        # Combined, not substituted: abort() is the one lever that has to stop
        # everything, including work that brought its own switch.
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.abort()
            with pytest.raises(Aborted):
                scraper.get(URL, signal=threading.Event())
        assert transport.calls == []

    def test_a_per_request_signal_stops_a_download_mid_stream(self, tmp_path: Path):
        holder: Dict[str, threading.Event] = {"signal": threading.Event()}

        class Chunked(FakeTransport):
            @contextmanager
            def stream(self, method: str, url: str, **kwargs: Any):
                response = make_response(body="", url=url)

                def chunks():
                    yield b"\x89PNG\r\n\x1a\n"
                    holder["signal"].set()
                    for _ in range(1000):
                        yield b"more"

                yield response, chunks()

        with scraper_for(Chunked()) as scraper:
            with pytest.raises(Aborted):
                scraper.get_file(URL, tmp_path / "big.bin", signal=holder["signal"])
        assert not (tmp_path / "big.bin").exists()

    def test_a_per_request_signal_interrupts_the_pacing_wait(self):
        # The wait is where a cancelled job spends most of its life: the interval is
        # drawn from a distribution with a tail measured in tens of seconds.
        from scraper import PacingPolicy

        slow = PacingPolicy(interval=30.0, floor=30.0, warmup=False, pause_chance=0.0)
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport, pacing=slow) as scraper:
            scraper.get(URL)  # first request is unpaced; the next one waits
            cancelled = threading.Event()
            cancelled.set()
            started = time.monotonic()
            with pytest.raises(Aborted):
                scraper.get(URL, signal=cancelled)
            assert time.monotonic() - started < 1.0

    def test_a_challenge_is_never_written_to_the_download_target(self, tmp_path: Path):
        """A challenge answers 200, so the status cannot be what decides this.

        Written after finding that `_download` blanked the body before diagnosis ran,
        so an interstitial was streamed to the caller's path and accepted — a chapter
        image that is really a Cloudflare page, indistinguishable once the response
        is gone.
        """

        class Interstitial(FakeTransport):
            @contextmanager
            def stream(self, method: str, url: str, **kwargs: Any):
                response = make_response(200, CHALLENGE_BODY, url=url)

                def chunks():
                    yield CHALLENGE_BODY.encode()

                yield response, chunks()

        target = tmp_path / "cover.jpg"
        with scraper_for(Interstitial()) as scraper:
            with pytest.raises(ScraperError):
                scraper.get_file(URL, target)
        assert not target.exists()

    def test_a_clean_body_still_reaches_the_target(self, tmp_path: Path):
        class Asset(FakeTransport):
            @contextmanager
            def stream(self, method: str, url: str, **kwargs: Any):
                response = make_response(body="", url=url)

                def chunks():
                    yield b"\x89PNG\r\n\x1a\n"
                    yield b"payload"

                yield response, chunks()

        target = tmp_path / "cover.png"
        with scraper_for(Asset()) as scraper:
            assert scraper.get_file(URL, target) == target
        assert target.read_bytes() == b"\x89PNG\r\n\x1a\npayload"

    def test_a_transport_the_tier_owns_is_closed(self):
        from scraper.tiers.direct import DirectTier

        transport = FakeTransport()
        DirectTier(transport).close()
        assert transport.closed

    def test_an_injected_transport_is_left_open(self):
        # Two scrapers over one ScraperConfig(transport=...) is a supported shape, and
        # the first close() used to take the transport out from under the second.
        transport = FakeTransport()
        one = scraper_for(transport)
        two = scraper_for(transport)
        one.close()
        assert not transport.closed
        assert two.get(URL).status_code == 200
        two.close()
        assert not transport.closed, "the injector owns it, and closes it itself"

    def test_explain_names_the_binding_layer_and_the_ladder(self):
        transport = FakeTransport(
            [make_response(403, CHALLENGE_BODY, url=URL), make_response(body=PAGE, url=URL)]
        )
        with scraper_for(transport, browser=RecordingSolver()) as scraper:
            scraper.get(URL)
            report = scraper.explain(URL)
        assert "example.com" in report
        assert "L9" in report
        assert "clearance" in report
        assert "ladder" in report

    def test_explain_works_before_anything_has_happened(self):
        with scraper_for(FakeTransport()) as scraper:
            assert "nothing has blocked yet" in scraper.explain(URL)


class TestTheHelperSurface:
    def test_every_helper_goes_through_the_same_loop(self, tmp_path: Path):
        # So a challenged site is handled identically whether you asked for HTML or a
        # cover image.
        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            if "api" in url:
                return make_response(
                    body='{"items": [1, 2]}',
                    url=url,
                    headers={"content-type": "application/json"},
                )
            return make_response(body=PAGE, url=url)

        transport = FakeTransport(handler=serve)
        with scraper_for(transport) as scraper:
            assert scraper.get_json("https://example.com/api/x")["items"] == [1, 2]
            assert scraper.post_json("https://example.com/api/x", data="{}")["items"] == [1, 2]
            assert scraper.post_soup(URL, data={"q": "x"}).select_one("h1").text == "Chapter One"
            assert scraper.submit_form(URL, data={"q": "x"}).status_code == 200
            assert scraper.ping(URL).status_code == 200
            assert scraper.get_file(URL, tmp_path / "page.html").exists()

    def test_a_form_post_declares_its_content_type(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.submit_form(URL, data={"q": "x"})
            assert "x-www-form-urlencoded" in transport.headers_of(0)["content-type"]
            scraper.submit_form(URL, data={"q": "x"}, multipart=True)
            assert transport.headers_of(1)["content-type"] == "multipart/form-data"

    def test_default_headers_are_applied_to_every_request(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.headers["accept-language"] = "en-GB"
            scraper.get(URL)
        assert transport.headers_of(0)["accept-language"] == "en-GB"

    def test_a_per_request_header_wins_over_a_default(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.headers["accept-language"] = "en-GB"
            scraper.get(URL, headers={"accept-language": "fr-FR"})
        assert transport.headers_of(0)["accept-language"] == "fr-FR"

    def test_a_data_uri_image_needs_no_request(self):
        pytest.importorskip("PIL")
        transport = FakeTransport()
        tiny = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
        with scraper_for(transport) as scraper:
            assert scraper.get_image(tiny).size == (1, 1)
        assert transport.calls == []

    def test_soup_can_be_made_without_fetching(self):
        with scraper_for(FakeTransport()) as scraper:
            assert scraper.make_soup("<p>hi</p>").select_one("p").text == "hi"

    def test_a_cookie_can_be_installed_on_the_transport(self):
        transport = FakeTransport()
        with scraper_for(transport) as scraper:
            scraper.set_cookie("a", "b", domain="example.com")
            assert scraper.cookies.get("a") == "b"


class TestTierUnavailable:
    def test_a_tier_that_cannot_serve_escalates_without_blaming_the_site(self):
        # An archive with no snapshot says nothing about the site's defences, so nothing
        # may be attributed to a layer or written to memory.
        index = "[]"

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            if "web.archive.org" in url:
                return make_response(body=index, url=url)
            return make_response(body=PAGE, url=url)

        transport = FakeTransport(handler=serve)
        with scraper_for(transport, archive=True) as scraper:
            assert scraper.get(URL).status_code == 200
            assert scraper.knows(URL).binding is None
            assert scraper.knows(URL).tier == "direct"


class TestSigning:
    def test_a_configured_key_signs_every_request(self):
        pytest.importorskip("cryptography")
        from scraper import BotAuthConfig, BotAuthKey

        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        config = BotAuthConfig(key=BotAuthKey.generate(), agent="https://crawler.test/")
        with scraper_for(transport, botauth=config) as scraper:
            scraper.get(URL)
        headers = transport.headers_of(0)
        assert "signature" in headers
        assert headers["signature-agent"] == "https://crawler.test/"

    def test_nothing_is_signed_without_a_key(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.get(URL)
        assert "signature" not in transport.headers_of(0)


class TestDownloading:
    def test_an_error_page_is_not_written_to_the_callers_file(self, tmp_path: Path):
        # A challenge interstitial is a body, not a status. Writing it where the
        # caller asked for a chapter leaves a file that looks like a successful
        # download and is not one.
        target = tmp_path / "cover.jpg"
        transport = FakeTransport([make_response(403, BLOCK_BODY, url=URL)])
        with scraper_for(transport, archive=False) as scraper:
            with pytest.raises(Exhausted):
                scraper.get_file(URL, target)
        assert not target.exists()

    def test_an_abort_mid_stream_stops_the_download(self, tmp_path: Path):
        target = tmp_path / "big.bin"
        transport = FakeTransport([make_response(body="x" * 1000, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.abort()
            with pytest.raises(Aborted):
                scraper.get_file(URL, target)
        assert not target.exists()

    def test_an_image_is_fetched_through_the_same_loop(self):
        # So a cover behind a challenge is handled exactly like a chapter behind one.
        pytest.importorskip("PIL")
        import base64

        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        response = make_response(url=URL, headers={"content-type": "image/gif"})
        response._content = gif  # noqa: SLF001
        transport = FakeTransport([response])
        with scraper_for(transport) as scraper:
            assert scraper.get_image(URL).size == (1, 1)
        assert transport.headers_of(0)["accept"].startswith("image/")

    def test_a_caller_header_survives_the_image_accept_header(self):
        pytest.importorskip("PIL")
        import base64

        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        response = make_response(url=URL, headers={"content-type": "image/gif"})
        response._content = gif  # noqa: SLF001
        with scraper_for(FakeTransport([response])) as scraper:
            transport = scraper.transport
            scraper.get_image(URL, headers={"Referer": "https://example.com/novel/"})
        assert transport.headers_of(0)["referer"] == "https://example.com/novel/"  # type: ignore[attr-defined]


class TestTheTopicGuardInTheLoop:
    ON_TOPIC = (
        "<html><body>chapter translation novel protagonist cultivation sect "
        "elder disciple sword qi realm</body></html>"
    )
    OFF_TOPIC = (
        "<html><body>quarterly amortisation schedules reconciled against "
        "depreciating municipal bond covenants actuarial tables</body></html>"
    )

    def serve_then_stray(self, on_topic_count: int = 6):
        pages = [self.ON_TOPIC] * on_topic_count + [self.OFF_TOPIC]
        index = {"n": 0}

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            body = pages[min(index["n"], len(pages) - 1)]
            index["n"] += 1
            return make_response(body=body, url=url)

        return serve

    def test_a_warning_policy_returns_the_page_and_says_so(self, caplog):
        # The default for a caller who wants to make the judgement themselves. Raising
        # is right for a pipeline that writes what it fetches; warning is right for
        # one that inspects first.
        transport = FakeTransport(handler=self.serve_then_stray())
        with scraper_for(transport, guard_topic=True, on_decoy="warn") as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            with caplog.at_level("WARNING", logger="scraper.session"):
                response = scraper.get("https://example.com/maze/1")
        assert response.status_code == 200
        assert "decoy" in caplog.text

    def test_a_decoy_is_recorded_even_when_it_is_not_raised(self):
        transport = FakeTransport(handler=self.serve_then_stray())
        with scraper_for(transport, guard_topic=True, on_decoy="warn") as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            scraper.get("https://example.com/maze/1")
            assert scraper.knows(URL).is_decoy("https://example.com/maze/1")

    def test_a_binary_body_is_not_measured_for_vocabulary(self):
        # An image has no prose, and scoring one would drive the overlap to zero and
        # accuse every cover on the site.
        image = make_response(body="\x00\x01binary", url=URL, headers={"content-type": "image/png"})
        transport = FakeTransport(handler=self.serve_then_stray())
        with scraper_for(transport, guard_topic=True, on_decoy="raise") as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            scraper.transport.replies = [image]  # type: ignore[attr-defined]
            scraper.transport.handler = None  # type: ignore[attr-defined]
            assert scraper.get(f"{URL}/cover.png").status_code == 200

    def test_an_empty_body_is_not_measured_either(self):
        empty = make_response(body="", url=URL)
        transport = FakeTransport(handler=self.serve_then_stray())
        with scraper_for(transport, guard_topic=True, on_decoy="raise") as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            scraper.transport.replies = [empty]  # type: ignore[attr-defined]
            scraper.transport.handler = None  # type: ignore[attr-defined]
            assert scraper.get(f"{URL}?p=empty").status_code == 200

    def test_a_parser_that_is_not_installed_falls_back_to_raw_words(self):
        # `parser` is a caller setting, and one naming a parser that is not installed
        # would otherwise take down every fetch rather than just the markup cleanup.
        from scraper.session import _visible_text

        assert "hello" in _visible_text("<p>hello</p>", "a-parser-nobody-installed")

    def test_script_content_is_not_part_of_the_vocabulary(self):
        """Site JavaScript is identical on every page, including the decoys.

        Measuring it makes every page look on-topic, which turns the guard off
        without anyone changing a setting.
        """
        from scraper.session import _visible_text

        text = _visible_text("<script>var telemetry=1</script><p>chapter one</p>", "lxml")
        assert "telemetry" not in text
        assert "chapter one" in text

    def test_the_guard_reports_what_it_has_learned(self):
        transport = FakeTransport(handler=self.serve_then_stray())
        with scraper_for(transport, guard_topic=True) as scraper:
            for number in range(3):
                scraper.get(f"{URL}?p={number}")
            assert "topic guard" in scraper.explain(URL)


class TestWiringFaults:
    def test_a_tier_the_scraper_never_built_is_a_wiring_bug_not_a_block(self):
        # The planner only names capabilities built from the same config, so this can
        # only be a programming error — and it must not read as a site refusing us.
        with scraper_for(FakeTransport()) as scraper:
            with pytest.raises(KeyError, match="no tier named"):
                scraper._tier("clearance")  # noqa: SLF001

    def test_a_tier_that_will_not_close_does_not_break_the_context_manager(self):
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        scraper = scraper_for(transport)

        def explode() -> None:
            raise RuntimeError("this tier is stuck")

        with scraper:
            scraper.get(URL)
            for tier in scraper._tiers.values():  # noqa: SLF001
                tier.close = explode  # type: ignore[method-assign]

    def test_a_managed_provider_is_wired_when_one_is_configured(self):
        def provider(method: str, url: str, **options: Any) -> requests.Response:
            return make_response(body=PAGE, url=url)

        with scraper_for(FakeTransport(), managed=provider) as scraper:
            assert scraper._tier("managed") is not None  # noqa: SLF001

    def test_links_with_nothing_to_resolve_against_still_come_back(self):
        # No origin, no previous page and no explicit base: there is no profile to ask
        # about recorded decoys, and the answer is the links, not an empty frontier.
        config = ScraperConfig(transport=FakeTransport(), remember=False, guard_topic=False)
        with Scraper(origin="", config=config) as scraper:
            found = scraper.links('<a href="https://example.com/next">next</a>')
        assert [link.url for link in found] == ["https://example.com/next"]

    def test_a_body_that_cannot_be_read_is_not_a_failed_fetch(self):
        """A consumed or broken stream costs the topic check, not the response.

        The guard is a heuristic backstop; a body it could not read is a reason to
        skip it, not to fail a request that already succeeded.
        """
        from scraper.session import Scraper as _Scraper

        class Unreadable(requests.Response):
            @property
            def content(self) -> bytes:  # type: ignore[override]
                raise RuntimeError("the stream was already consumed")

        assert _Scraper._peek(Unreadable()) == ""  # noqa: SLF001

    def test_a_wait_the_server_asked_for_is_actually_waited_out(self):
        # A 503 with a retry-after is the origin naming when it will be back, and it
        # is not a detection event — retrying immediately spends an attempt on a
        # certainty and looks like exactly the impatience the behavioural layer reads.
        calls = {"n": 0}

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return make_response(503, "maintenance", url=url, headers={"retry-after": "0.05"})
            return make_response(body=PAGE, url=url)

        started = time.monotonic()
        with scraper_for(FakeTransport(handler=serve)) as scraper:
            assert scraper.get(URL).status_code == 200
        assert calls["n"] == 2
        assert time.monotonic() - started >= 0.05

    def test_an_abort_while_queued_behind_another_request_is_honoured(self):
        # Concurrency is capped per address, so a request can be waiting on a slot
        # rather than on the network. Cancellation has to be seen there too, or a
        # cancelled job sits until whoever holds the slot is done.
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        raised: List[BaseException] = []
        with scraper_for(transport, max_sessions_per_exit=1) as scraper:
            gate = scraper.exits.slot(scraper.exits.lease(scraper.memory.key(URL)))
            assert gate.acquire(timeout=1)

            def fetch() -> None:
                try:
                    scraper.get(URL)
                except BaseException as exc:  # noqa: BLE001 - reported on the main thread
                    raised.append(exc)

            worker = threading.Thread(target=fetch, daemon=True)
            worker.start()
            time.sleep(0.1)
            scraper.abort()
            worker.join(timeout=5)
            gate.release()

        assert raised and isinstance(raised[0], Aborted)
        assert transport.calls == [], "the request must never have gone out"

    def test_a_clearance_earned_by_a_previous_identity_is_never_sent(self):
        """Kept out of the call rather than sent and rejected.

        A clearance under the wrong identity produces a challenge, and a challenge
        there reads as the solver having failed rather than as the address having
        changed underneath it — which starts a re-solve loop on a fresh exit each
        time.
        """
        transport = FakeTransport([make_response(body=PAGE, url=URL)])
        with scraper_for(transport) as scraper:
            scraper.knows(URL).clearance = {
                "origin": "https://example.com/",
                "cookies": {"cf_clearance": "earned-by-someone-else"},
                "identity_token": "a-token-from-a-retired-exit",
                "expires_at": time.time() + 600,
            }
            scraper.get(URL)
        assert "cookies" not in transport.calls[0][2]

    def test_a_decoy_can_be_recorded_without_warning_or_raising(self, caplog):
        on_topic = (
            "<html><body>chapter translation novel protagonist cultivation sect "
            "elder disciple sword qi realm</body></html>"
        )
        off_topic = (
            "<html><body>quarterly amortisation schedules reconciled against "
            "depreciating municipal bond covenants actuarial tables</body></html>"
        )
        pages = [on_topic] * 6 + [off_topic]
        index = {"n": 0}

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            body = pages[min(index["n"], len(pages) - 1)]
            index["n"] += 1
            return make_response(body=body, url=url)

        with scraper_for(
            FakeTransport(handler=serve), guard_topic=True, on_decoy="ignore"
        ) as scraper:
            for number in range(6):
                scraper.get(f"{URL}?p={number}")
            with caplog.at_level("WARNING", logger="scraper.session"):
                scraper.get("https://example.com/maze/1")
            # Still written to the profile: the next run's frontier needs to know,
            # even for a caller who does not want to hear about it now.
            assert scraper.knows(URL).is_decoy("https://example.com/maze/1")
        assert caplog.text == ""


class TestWarmUpTolerance:
    def test_a_homepage_that_will_not_load_does_not_stop_the_deep_page(self):
        """A soft signal must not become a hard stop.

        The homepage failing is worth knowing about, but the page the caller actually
        asked for may still work, and refusing to try guarantees it does not.
        """
        from scraper import PacingPolicy

        deep = {"n": 0}

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            if url == "https://example.com/":
                return make_response(403, BLOCK_BODY, url=url)
            deep["n"] += 1
            if deep["n"] == 1:
                # The first refusal is what makes the planner warm up at all.
                return make_response(429, "slow", url=url, headers={"retry-after": "0"})
            return make_response(body=PAGE, url=url)

        transport = FakeTransport(handler=serve)
        config = ScraperConfig(
            transport=transport,
            pacing=PacingPolicy(interval=0.0, floor=0.0, warmup=True, warmup_ttl=0.0),
            remember=False,
            guard_topic=False,
            raise_for_status=False,
        )
        with Scraper(origin="https://example.com", config=config) as scraper:
            assert scraper.get(URL).status_code == 200
        assert "https://example.com/" in transport.urls, "the warm-up has to have been tried"


class TestWhatAFailedResponseTeaches:
    """The ledger has to mean something, and it only does if a refusal is not filed
    as a success. Every case below used to write the opposite of what happened.
    """

    def test_an_unmatched_client_error_is_not_a_success(self):
        # A site answering 439 to everything: no diagnosis matches, so the planner
        # proceeds and the response is handed back for `raise_for_status`. What must
        # not happen is memory concluding that the tier it just used works.
        transport = FakeTransport([make_response(439, "nope", url=URL)])
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 439
            profile = scraper.knows(URL)
            assert profile.successes == 0
            assert profile.tier == ""

    def test_a_hostile_status_counts_against_the_origin_without_naming_a_layer(self):
        transport = FakeTransport([make_response(402, "pay up", url=URL)])
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 402
            profile = scraper.knows(URL)
            assert profile.failures == 1
            assert profile.successes == 0
            # No layer: 402 does not say which one, and naming one would retire a
            # healthy exit.
            assert profile.binding is None

    def test_a_missing_page_is_neither_a_success_nor_a_failure(self):
        # A 404 is the site's answer about a path. It says nothing about this
        # visitor, so it must not move the ledger in either direction.
        transport = FakeTransport([make_response(404, "gone", url=URL)])
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 404
            profile = scraper.knows(URL)
            assert profile.successes == 0
            assert profile.failures == 0

    def test_a_redirect_still_counts_as_reached(self):
        transport = FakeTransport(
            [make_response(301, "", url=URL, headers={"location": "/elsewhere"})]
        )
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 301
            assert scraper.knows(URL).successes == 1
            assert scraper.knows(URL).tier == "direct"

    def test_a_throttle_is_counted_once(self):
        # `_apply` records the throttle with the widened interval; the loop used to
        # record it again, so `promote_after=3` tripped on the second 429 and
        # escalated to a tier the caller never configured.
        transport = FakeTransport(
            [
                make_response(429, "slow", url=URL, headers={"retry-after": "0"}),
                make_response(body=PAGE, url=URL),
            ]
        )
        with scraper_for(transport) as scraper:
            assert scraper.get(URL).status_code == 200
            assert scraper.knows(URL).failures == 1

    def test_a_failure_with_nothing_to_attribute_keeps_the_known_layer(self):
        from scraper.memory import Memory

        memory = Memory()
        memory.record_failure(URL, Layer.MANAGED_CHALLENGE)
        assert memory.profile(URL).binding is Layer.MANAGED_CHALLENGE
        memory.record_failure(URL, None)
        assert memory.profile(URL).binding is Layer.MANAGED_CHALLENGE
        assert memory.profile(URL).failures == 2


class TestRotationIsReachable:
    """A dead exit is not a reputation block, and a pool of them can still move.

    `ExitKind.TOR.reach` is empty and honestly so — Tor exit lists are published — but
    the planner asked that question before every rotation, so with a tor-pool
    configured `Move.ROTATE` was never emitted and `ExitPool.rotate`/`report` were
    unreachable from `fetch`.
    """

    def test_a_pool_of_tor_exits_rotates_off_a_dead_one(self):
        def explode(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            raise requests.ConnectionError("connection reset")

        transport = FakeTransport(handler=explode)
        exits = [TorPoolSpec(url="socks5h://pool.test:9250", api_url="http://pool.test:1")]
        with scraper_for(transport, exits=exits, max_attempts=3, max_rotations=2) as scraper:
            before = scraper.exits.lease("example.com").exit_id
            with pytest.raises(Exhausted):
                scraper.get(URL)
            assert scraper.exits.lease("example.com").exit_id != before, (
                "the pool never moved, so rotate() was never reached"
            )

    def test_a_reputation_block_still_refuses_to_rotate_among_published_ranges(self):
        # The half of the old check that was right, kept: every Tor address is in a
        # published range, so replacing one with another cannot clear layer 1.
        transport = FakeTransport([make_response(403, "banned", url=URL, headers={"cf-ray": "x"})])
        exits = [TorPoolSpec(url="socks5h://pool.test:9250", api_url="http://pool.test:1")]
        with scraper_for(transport, exits=exits, max_attempts=2, max_rotations=2) as scraper:
            with pytest.raises(Blocked) as caught:
                scraper.get(URL)
        assert "reputation" in str(caught.value) or caught.value.layer is not None


class TestAnOptionalExtraNamesItself:
    def test_get_image_without_pillow_names_the_extra(self, monkeypatch: Any):
        import builtins

        from scraper.exceptions import MissingDependency

        real_import = builtins.__import__

        def no_pillow(name: str, *args: Any, **kwargs: Any):
            if name == "PIL":
                raise ImportError("No module named 'PIL'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pillow)
        with scraper_for(FakeTransport()) as scraper:
            with pytest.raises(MissingDependency, match=r"lncrawl-scraper\[image\]"):
                scraper.get_image("https://example.com/cover.jpg")
