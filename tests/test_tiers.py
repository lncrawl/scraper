"""Individual tiers: the archive, the baseline, and delegation."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Dict, List, Optional

import pytest
import requests

from scraper import Scraper
from scraper.browser import CallableSolver, SolveResult
from scraper.exceptions import ConfigError, TierUnavailable
from scraper.identity import Clearance, Identity
from scraper.layers import Layer
from scraper.tiers import ArchiveTier, Call, DirectTier, ManagedTier, Tier, http_provider
from scraper.tiers.archive import SOURCE_HEADER
from scraper.tiers.clearance import ClearanceTier

from .conftest import FakeTransport, make_response

URL = "https://example.com/novel/chapter-1"


def call_for(url: str = URL, **kwargs: Any) -> Call:
    kwargs.setdefault("identity", Identity(impersonate="chrome", exit_id="e1"))
    return Call(method=kwargs.pop("method", "GET"), url=url, **kwargs)


def stamp(days_ago: int) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return moment.strftime("%Y%m%d%H%M%S")


class TestDirectTier:
    def test_the_profiles_headers_are_left_alone_by_default(self):
        # Header order is read, not just header values, and an impersonation profile
        # already emits a complete correctly ordered set.
        transport = FakeTransport([make_response()])
        DirectTier(transport).send(call_for())
        assert transport.headers_of(0) == {}

    def test_request_headers_from_the_caller_go_through(self):
        transport = FakeTransport([make_response()])
        DirectTier(transport).send(call_for(headers={"accept": "application/json"}))
        assert transport.headers_of(0)["accept"] == "application/json"

    def test_a_pinned_user_agent_is_sent_with_matching_hints(self):
        transport = FakeTransport([make_response()])
        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140.0.0.0")
        DirectTier(transport).send(call_for(identity=identity))
        headers = transport.headers_of(0)
        assert headers["user-agent"].endswith("Chrome/140.0.0.0")
        assert '"140"' in headers["sec-ch-ua"]

    def test_a_callers_header_wins_over_the_identitys(self):
        transport = FakeTransport([make_response()])
        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140.0.0.0")
        DirectTier(transport).send(call_for(identity=identity, headers={"user-agent": "mine/1"}))
        assert transport.headers_of(0)["user-agent"] == "mine/1"

    def test_the_lease_proxies_are_passed_straight_through(self):
        transport = FakeTransport([make_response()])
        proxies = {"http": "http://p.test:1", "https": "http://p.test:1"}
        DirectTier(transport).send(call_for(proxies=proxies))
        assert transport.proxies_of(0) == proxies

    def test_a_clearance_travels_per_request_not_in_the_jar(self):
        # A jar outlives identities; a clearance belongs to exactly one, so leaving it
        # installed is how a rotated address keeps presenting the old one's cookie.
        from scraper.identity import Clearance

        transport = FakeTransport([make_response()])
        identity = Identity(exit_id="e1")
        clearance = Clearance(
            origin="https://example.com/",
            cookies={"cf_clearance": "abc"},
            identity_token=identity.token(),
        )
        DirectTier(transport).send(call_for(identity=identity, clearance=clearance))
        assert transport.calls[0][2]["cookies"] == {"cf_clearance": "abc"}
        assert len(transport.cookies) == 0

    def test_an_abort_is_honoured_before_anything_is_sent(self):
        import threading

        from scraper.exceptions import Aborted

        signal = threading.Event()
        signal.set()
        transport = FakeTransport([make_response()])
        with pytest.raises(Aborted):
            DirectTier(transport).send(call_for(signal=signal))
        assert transport.calls == []

    def test_streaming_goes_through_the_same_preparation(self):
        transport = FakeTransport([make_response(body="payload")])
        with DirectTier(transport).stream(call_for(headers={"accept": "*/*"})) as (resp, chunks):
            assert resp.status_code == 200
            assert b"".join(chunks) == b"payload"
        assert transport.headers_of(0)["accept"] == "*/*"


class TestArchiveTier:
    def _index(self, rows) -> str:
        return json.dumps([["timestamp", "original"], *rows])

    def test_a_capture_is_served_with_the_original_url(self):
        # Relative links resolved against a web.archive.org base point back into the
        # archive, which silently turns a scrape of a site into a scrape of a
        # snapshot of a site.
        index = self._index([[stamp(1), URL]])

        def serve(method: str, url: str, kwargs: Dict[str, Any]) -> requests.Response:
            if "cdx" in url:
                return make_response(body=index, url=url)
            return make_response(body="<html>archived</html>", url=url)

        transport = FakeTransport(handler=serve)
        response = ArchiveTier(transport).send(call_for())
        assert response.url == URL
        assert "archived" in response.text

    def test_the_capture_timestamp_is_reported(self):
        when = stamp(2)
        index = self._index([[when, URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body=index if "cdx" in url else "<html>x</html>", url=url
            )
        )
        assert ArchiveTier(transport).send(call_for()).headers[SOURCE_HEADER] == when

    def test_the_raw_capture_is_requested_not_the_rewritten_one(self):
        when = stamp(1)
        index = self._index([[when, URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body=index if "cdx" in url else "x", url=url
            )
        )
        ArchiveTier(transport).send(call_for())
        assert f"/{when}id_/" in transport.urls[1]

    def test_the_newest_acceptable_capture_wins(self):
        index = self._index([[stamp(30), URL], [stamp(1), URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body=index if "cdx" in url else "x", url=url
            )
        )
        ArchiveTier(transport).send(call_for())
        assert stamp(1)[:8] in transport.urls[1]

    def test_a_capture_older_than_asked_for_is_refused(self):
        index = self._index([[stamp(400), URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        with pytest.raises(TierUnavailable):
            ArchiveTier(transport, max_age=86400).send(call_for())

    def test_latest_reports_the_capture_send_would_have_used(self):
        # The lookahead a caller uses to decide whether the archive is worth trying at
        # all, so it has to apply the same age limit `send` does.
        when = stamp(1)
        index = self._index([[stamp(400), URL], [when, URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        assert ArchiveTier(transport).latest(URL) == (when, URL)

    def test_latest_is_empty_when_nothing_is_acceptable(self):
        index = self._index([[stamp(400), URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        assert ArchiveTier(transport, max_age=86400).latest(URL) is None

    def test_an_index_answering_with_the_wrong_shape_did_not_answer(self):
        # The CDX API replies with an object rather than an array of rows when it is
        # unhappy. That is the index declining, not the URL being unarchived, and
        # conflating the two stops the caller ever trying the archive again.
        transport = FakeTransport([make_response(body=json.dumps({"error": "blocked"}))])
        with pytest.raises(TierUnavailable, match="did not answer"):
            ArchiveTier(transport).send(call_for())

    def test_a_truncated_index_row_is_skipped_rather_than_unpacked(self):
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body=json.dumps([["timestamp", "original"], [stamp(1)], "junk"]), url=url
            )
        )
        with pytest.raises(TierUnavailable, match="no capture on record"):
            ArchiveTier(transport).send(call_for())

    def test_no_capture_escalates_rather_than_blaming_the_site(self):
        # An archive gap says nothing about the site's defences, so recording it as a
        # block would teach the memory something false.
        transport = FakeTransport([make_response(body="[]")])
        with pytest.raises(TierUnavailable, match="no capture on record"):
            ArchiveTier(transport).send(call_for())

    def test_an_index_that_will_not_answer_is_reported_as_such(self):
        """Found live: the index rate-limits, and a 503 read as "never archived".

        The caller then stops considering the archive for a URL it does hold, which is
        the same misleading-message class as the negative-limit bug.
        """
        transport = FakeTransport([make_response(503, "Service Unavailable")])
        with pytest.raises(TierUnavailable, match="did not answer"):
            ArchiveTier(transport, retry_after=0.0).send(call_for())

    def test_the_index_is_retried_once_before_giving_up(self):
        index = self._index([[stamp(1), URL]])
        replies = [
            make_response(503, "Service Unavailable"),
            make_response(body=index),
            make_response(body="<html>archived</html>"),
        ]
        transport = FakeTransport(replies)
        response = ArchiveTier(transport, retry_after=0.0).send(call_for())
        assert response.status_code == 200

    def test_an_age_limit_is_reported_differently_from_an_empty_index(self):
        # Two different problems: one is a caller policy, the other is coverage.
        index = self._index([[stamp(400), URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        with pytest.raises(TierUnavailable, match="within the age limit"):
            ArchiveTier(transport, max_age=86400).send(call_for())

    def test_a_post_has_no_snapshot(self):
        transport = FakeTransport([make_response(body="[]")])
        with pytest.raises(TierUnavailable, match="only serves GET"):
            ArchiveTier(transport).send(call_for(method="POST"))

    def test_the_index_is_never_asked_for_a_negative_row_count(self):
        """A negative CDX ``limit`` returns an empty body once a filter is applied.

        Found live: the tier reported "no usable capture" for every URL, which is
        indistinguishable from a URL the archive has genuinely never seen. Asserted on
        the request rather than the response, because a stubbed transport will happily
        answer a broken query.
        """
        transport = FakeTransport([make_response(body="[]")])
        with pytest.raises(TierUnavailable):
            ArchiveTier(transport).send(call_for())
        params = transport.calls[0][2]["params"]
        assert "limit" not in params or int(params["limit"]) > 0

    def test_the_query_is_always_bounded(self):
        """Found live: an unbounded query on a popular URL times out.

        A timeout is indistinguishable from a URL the archive has never seen, so an
        unbounded query makes the tier look permanently empty rather than slow.
        """
        index = self._index([[stamp(1), URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(
                body=index if "cdx" in url else "x", url=url
            )
        )
        # Both with and without a caller-supplied maximum age.
        ArchiveTier(transport, max_age=30 * 86400).send(call_for())
        assert len(transport.calls[0][2]["params"]["from"]) == 8
        ArchiveTier(transport).send(call_for())
        assert len(transport.calls[-2][2]["params"]["from"]) == 8

    def test_the_newest_rows_are_kept_when_the_index_is_long(self):
        rows = [[stamp(n), URL] for n in range(40, 0, -1)]
        index = self._index(rows)
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        found = ArchiveTier(transport).captures(URL, limit=5)
        assert len(found) == 5
        assert found[-1][0] == rows[-1][0], "the tail is the newest capture"

    def test_a_broken_index_response_is_survivable(self):
        transport = FakeTransport([make_response(body="not json at all")])
        assert ArchiveTier(transport).captures(URL) == []

    def test_a_malformed_timestamp_is_treated_as_infinitely_old(self):
        index = self._index([["not-a-date", URL]])
        transport = FakeTransport(
            handler=lambda method, url, kwargs: make_response(body=index, url=url)
        )
        with pytest.raises(TierUnavailable):
            ArchiveTier(transport, max_age=60).send(call_for())


class TestManagedTier:
    def test_the_provider_is_called_and_its_response_returned(self):
        seen: Dict[str, Any] = {}

        def provider(method: str, url: str, **options: Any) -> requests.Response:
            seen.update({"method": method, "url": url, "options": options})
            return make_response(body="from the provider", url=url)

        response = ManagedTier(provider).send(call_for(timeout=30))
        assert "from the provider" in response.text
        assert seen["method"] == "GET"
        assert seen["options"]["timeout"] == 30

    def test_the_identity_is_not_imposed_on_a_provider_by_default(self):
        # A provider that manages its own fingerprint does not want a User-Agent
        # forced on it, and forcing one contradicts the profile it matched.
        seen: Dict[str, Any] = {}

        def provider(method: str, url: str, **options: Any) -> requests.Response:
            seen.update(options)
            return make_response()

        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140.0.0.0")
        ManagedTier(provider).send(call_for(identity=identity))
        assert "user-agent" not in (seen.get("headers") or {})

    def test_the_identity_can_be_forwarded_when_asked_for(self):
        seen: Dict[str, Any] = {}

        def provider(method: str, url: str, **options: Any) -> requests.Response:
            seen.update(options)
            return make_response()

        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140.0.0.0")
        ManagedTier(provider, pass_identity=True).send(call_for(identity=identity))
        assert "user-agent" in seen["headers"]

    def test_a_provider_returning_the_wrong_type_says_so_clearly(self):
        with pytest.raises(TierUnavailable, match="not a requests.Response"):
            ManagedTier(lambda method, url, **options: "oops").send(call_for())  # type: ignore[arg-type,return-value]

    def test_the_name_appears_in_failures_so_the_rung_is_identifiable(self):
        tier = ManagedTier(lambda method, url, **options: "oops", name="scrapfly")  # type: ignore[arg-type,return-value]
        with pytest.raises(TierUnavailable, match="scrapfly"):
            tier.send(call_for())


class TestHttpProvider:
    def test_the_target_url_is_passed_as_a_parameter(self):
        transport = FakeTransport([make_response(body="proxied")])
        provider = http_provider("https://api.provider.test/v1", token="k3y", transport=transport)
        assert "proxied" in provider("GET", URL).text
        params = transport.calls[0][2]["params"]
        assert params["url"] == URL
        assert params["key"] == "k3y"

    def test_only_get_is_forwarded(self):
        # A service that tunnels other methods does so in its own format, and
        # guessing that format is the failure this module refuses to build in.
        provider = http_provider("https://api.provider.test/v1", transport=FakeTransport())
        with pytest.raises(TierUnavailable, match="only forwards GET"):
            provider("POST", URL)

    def test_extra_parameters_are_merged(self):
        transport = FakeTransport([make_response()])
        provider = http_provider(
            "https://api.provider.test/v1",
            extra={"render_js": "true"},
            transport=transport,
        )
        provider("GET", URL)
        assert transport.calls[0][2]["params"]["render_js"] == "true"

    def test_without_a_transport_it_falls_back_to_plain_requests(self, monkeypatch):
        # A managed provider terminates TLS itself, so it is the one place where the
        # impersonation transport buys nothing and an ordinary client is correct.
        seen: Dict[str, Any] = {}

        def fake_get(endpoint: str, **kwargs: Any) -> requests.Response:
            seen.update({"endpoint": endpoint, **kwargs})
            return make_response(body="plain")

        monkeypatch.setattr(requests, "get", fake_get)
        provider = http_provider("https://api.provider.test/v1")
        assert "plain" in provider("GET", URL).text
        assert seen["endpoint"] == "https://api.provider.test/v1"
        assert seen["params"]["url"] == URL
        assert seen["timeout"] == 90.0


class TestTheTierContract:
    def test_the_base_tier_is_abstract_in_practice(self):
        with pytest.raises(NotImplementedError):
            Tier().send(call_for())

    def test_closing_a_tier_that_holds_nothing_is_fine(self):
        Tier().close()

    def test_the_default_stream_buffers_through_send(self):
        """Every tier supports downloads whether or not its client can stream.

        A tier that cannot stream is still correct here, just memory-hungry on a large
        file — which is a better failure than `get_file` refusing to work on a rung.
        """

        class Buffering(Tier):
            def send(self, call: Call) -> requests.Response:
                return make_response(body="payload")

        with Buffering().stream(call_for()) as (response, chunks):
            assert response.status_code == 200
            assert b"".join(chunks) == b"payload"

    def test_a_none_header_removes_the_identitys_contribution(self):
        # The escape hatch for a caller who needs a header the identity would
        # otherwise supply to be absent entirely, rather than sent as "None".
        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140.0.0.0")
        headers: Dict[str, Any] = {"User-Agent": None}
        call = call_for(identity=identity, headers=headers)
        assert "user-agent" not in call.merged_headers()

    def test_a_call_without_proxies_is_not_through_a_proxy(self):
        assert not call_for().through_proxy
        assert call_for(proxies={"https": "http://p.test:1"}).through_proxy

    def test_no_clearance_means_no_cookies(self):
        assert call_for().cookie_header() == {}


def stub_solver(
    cookies: Dict[str, str],
    *,
    user_agent: str = "Mozilla/5.0 Chrome/141.0.0.0",
    seen: Optional[List[Dict[str, Any]]] = None,
) -> CallableSolver:
    """A solver that returns *cookies* without launching anything."""

    def solve(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
        if seen is not None:
            seen.append(
                {"url": url, "proxy": proxy, "profile_dir": profile_dir, "timeout": timeout}
            )
        return SolveResult(cookies=cookies, user_agent=user_agent)

    return CallableSolver(solve, name="stub")


def clearance_tier(solver: CallableSolver, transport: FakeTransport, **kwargs: Any):
    return ClearanceTier(solver, DirectTier(transport), **kwargs)


class TestHowLongASolveGets:
    """A visible window gets a person's patience; an unattended one does not."""

    def test_an_unattended_solve_gets_the_ordinary_budget(self):
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "x"}, seen=seen),
            FakeTransport([make_response()]),
            solve_timeout=90.0,
            interactive_solve_timeout=300.0,
        )
        tier.send(call_for())
        assert seen[0]["timeout"] == 90.0

    def test_a_solver_a_person_can_reach_gets_the_longer_one(self):
        # The solve loop detects success by polling and does not care who cleared the
        # page, so a human needs no protocol of their own — only enough time.
        solver = stub_solver({"cf_clearance": "x"}, seen=(seen := []))
        solver.interactive = True
        tier = clearance_tier(
            solver,
            FakeTransport([make_response()]),
            solve_timeout=90.0,
            interactive_solve_timeout=300.0,
        )
        tier.send(call_for())
        assert seen[0]["timeout"] == 300.0

    def test_the_window_says_why_it_opened(self, caplog: pytest.LogCaptureFixture):
        # A browser appearing with nothing said about it reads as the app misbehaving,
        # and a person who does not know to click will not.
        solver = stub_solver({"cf_clearance": "x"})
        solver.interactive = True
        tier = clearance_tier(solver, FakeTransport([make_response()]))
        with caplog.at_level(logging.INFO):
            tier.send(call_for())
        assert any("browser window has opened" in r.getMessage() for r in caplog.records), (
            caplog.text
        )


class TestClearanceTier:
    def test_a_solver_that_earns_nothing_is_unavailable_not_blocked(self):
        """Invariant 9: only a real detection event may be attributed to a layer.

        A solver that finished without a clearance cookie says nothing about the site.
        Reporting it as `Blocked` would rotate an innocent address, report it to the
        pool as burnt, and persist the attribution to the origin's profile.
        """
        tier = clearance_tier(stub_solver({"sid": "x"}), FakeTransport([make_response()]))
        with pytest.raises(TierUnavailable, match="without a clearance cookie"):
            tier.send(call_for())

    def test_the_request_goes_out_under_the_identity_the_browser_earned(self):
        # The browser is the source of truth once it has solved. Sending the cookie
        # under the pre-solve identity is the exact mismatch this tier exists to stop.
        transport = FakeTransport([make_response()])
        tier = clearance_tier(stub_solver({"cf_clearance": "abc"}), transport)
        call = call_for()

        tier.send(call)

        assert call.identity.user_agent == "Mozilla/5.0 Chrome/141.0.0.0"
        assert call.clearance is not None
        assert call.clearance.usable_by(call.identity)
        assert transport.headers_of(0)["user-agent"] == "Mozilla/5.0 Chrome/141.0.0.0"

    def test_the_browser_solves_on_the_address_the_requests_will_use(self):
        # Solving on one exit and fetching from another produces a clearance that is
        # dead on arrival, which reads as "the solver does not work" and leads to
        # re-solving forever.
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen), FakeTransport([make_response()])
        )

        tier.send(call_for(proxies={"https": "http://user:pw@exit.test:8000"}))

        assert seen[0]["proxy"] == "http://user:pw@exit.test:8000"

    def test_a_second_call_to_the_same_origin_does_not_re_launch_a_browser(self):
        # A solve is the most expensive thing this library does; another thread having
        # already paid for this origin is worth the branch.
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen), FakeTransport([make_response()])
        )

        tier.send(call_for())
        tier.send(call_for("https://example.com/novel/chapter-2"))

        assert len(seen) == 1

    def test_a_clearance_earned_under_another_identity_is_re_solved(self):
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen), FakeTransport([make_response()])
        )
        stale = Clearance(
            origin="https://example.com",
            cookies={"cf_clearance": "old"},
            identity_token=Identity(exit_id="a-retired-exit").token(),
        )

        call = call_for(clearance=stale)
        tier.send(call)

        assert len(seen) == 1
        assert call.clearance is not None and call.clearance.cookies == {"cf_clearance": "abc"}

    def test_a_usable_clearance_is_left_alone(self):
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen), FakeTransport([make_response()])
        )
        identity = Identity(exit_id="e1")
        fresh = Clearance(
            origin="https://example.com",
            cookies={"cf_clearance": "held"},
            identity_token=identity.token(),
            expires_at=time.time() + 600,
        )

        tier.send(call_for(identity=identity, clearance=fresh))

        assert seen == [], "a browser launched for a clearance already in hand"

    def test_streaming_goes_through_the_same_solve(self):
        # `get_file` on a challenged site takes this path, and a stream that skipped
        # the solve would download the interstitial.
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen),
            FakeTransport([make_response(body="the file")]),
        )

        with tier.stream(call_for()) as (response, chunks):
            assert b"".join(chunks) == b"the file"
        assert len(seen) == 1

    def test_a_new_clearance_is_handed_to_the_store(self):
        # Repeating the solve every run is the difference between one browser launch
        # and one per session.
        stored: Dict[str, Clearance] = {}
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}),
            FakeTransport([make_response()]),
            store=lambda origin, clearance: stored.__setitem__(origin, clearance),
        )

        tier.send(call_for())

        assert stored["https://example.com/"].cookies == {"cf_clearance": "abc"}

    def test_the_profile_directory_follows_the_address(self, tmp_path):
        seen: List[Dict[str, Any]] = []
        tier = clearance_tier(
            stub_solver({"cf_clearance": "abc"}, seen=seen),
            FakeTransport([make_response()]),
            profile_root=tmp_path,
        )

        tier.send(call_for(identity=Identity(exit_id="pool#s-aaa")))

        assert seen[0]["profile_dir"] is not None
        assert seen[0]["profile_dir"].parent == tmp_path

    def test_a_held_clearance_that_has_since_expired_is_re_earned(self):
        # The per-origin cache is there to stop a second thread paying for a solve that
        # already happened, not to serve a cookie the site will now reject.
        seen: List[Dict[str, Any]] = []

        def solve(url: str, *, proxy=None, profile_dir=None, timeout=60.0) -> SolveResult:
            seen.append({"url": url})
            return SolveResult(cookies={"cf_clearance": "abc"}, user_agent="UA", expires_at=1.0)

        tier = clearance_tier(CallableSolver(solve, name="stub"), FakeTransport([make_response()]))

        tier.send(call_for())
        tier.send(call_for())

        assert len(seen) == 2

    def test_closing_the_tier_closes_the_solver(self):
        closed: List[bool] = []
        solver = stub_solver({"cf_clearance": "abc"})
        solver.close = lambda: closed.append(True)  # type: ignore[method-assign]

        clearance_tier(solver, FakeTransport()).close()

        assert closed == [True]


class TestRegisteringATier:
    """The seam two documents promised. It used to be "edit `_build_tiers`".

    Which is not extensibility — it is an instruction to patch a private method of an
    installed library, and it breaks on the next refactor with no deprecation to notice.
    """

    class Cache(Tier):
        name = "cache"
        cost = 5
        reach = frozenset({Layer.IP_REPUTATION})

        def __init__(self) -> None:
            self.calls: List[str] = []

        def send(self, call: Call) -> requests.Response:
            self.calls.append(call.url)
            return make_response(body="<h1>from the cache</h1>", url=call.url)

    def test_a_custom_tier_joins_the_ladder_at_its_own_cost(self, make_config):
        cache = self.Cache()
        config = make_config(tiers=[cache])
        with Scraper(config=config) as scraper:
            rungs = [cap.name for cap in scraper.planner.ladder()]
        assert rungs == ["cache", "direct"], "cost decides the order, not registration"

    def test_it_is_chosen_when_it_is_the_cheapest_that_covers_the_layer(self, make_config):
        cache = self.Cache()
        transport = FakeTransport()
        config = make_config(transport=transport, tiers=[cache])
        with Scraper(config=config) as scraper:
            # A remembered reputation block: the cheapest rung claiming layer 1 wins.
            scraper.memory.record_failure(URL, Layer.IP_REPUTATION)
            response = scraper.get(URL)
        assert "from the cache" in response.text
        assert cache.calls == [URL]
        assert transport.calls == [], "the custom rung served it, not the direct one"

    def test_a_name_that_is_already_taken_is_refused(self, make_config):
        class Impostor(Tier):
            name = "direct"

            def send(self, call: Call) -> requests.Response:
                raise AssertionError("never reached")

        with pytest.raises(ConfigError, match="already built"):
            Scraper(config=make_config(tiers=[Impostor()]))

    def test_a_tier_may_not_claim_a_layer_that_reads_a_secret(self):
        # Refused rather than filtered: dropping the claim quietly would leave the
        # author believing the planner had honoured it, and layers 18 and 19 are held
        # or they are not — a rung offering one would be offered for something no rung
        # can do.
        class Overreaching(Tier):
            name = "wishful"
            reach = frozenset({Layer.WEB_BOT_AUTH, Layer.ACCESS})

            def send(self, call: Call) -> requests.Response:
                raise AssertionError("never reached")

        with pytest.raises(ConfigError, match="read a secret"):
            Overreaching().capability()

    def test_reach_is_closed_over_the_transport_group(self):
        # No technique satisfies one of layers 2-5 without the others, so a tier that
        # names one has named all four whether it knows it or not.
        class Impersonating(Tier):
            name = "impersonating"
            reach = frozenset({Layer.TLS_FINGERPRINT})

            def send(self, call: Call) -> requests.Response:
                raise AssertionError("never reached")

        reach = Impersonating().capability().reach
        assert Layer.HEADER_ORDER in reach
        assert Layer.HTTP_FRAMES in reach

    def test_a_custom_tier_is_closed_with_the_others(self, make_config):
        closed = {"yes": False}

        class Closing(Tier):
            name = "closing"

            def send(self, call: Call) -> requests.Response:
                raise AssertionError("never reached")

            def close(self) -> None:
                closed["yes"] = True

        Scraper(config=make_config(tiers=[Closing()])).close()
        assert closed["yes"]
