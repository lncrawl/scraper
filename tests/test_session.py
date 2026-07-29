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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests

from scraper import ExitKind, ExitSpec, Scraper, ScraperConfig, SharedState
from scraper.browser import BrowserSolver, SolveResult
from scraper.exceptions import Aborted, Exhausted, Impassable, Poisoned
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
        assert caught.value.layer is Layer.IP_REPUTATION

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

    def test_closing_closes_the_transport(self):
        transport = FakeTransport()
        scraper_for(transport).close()
        assert transport.closed

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
