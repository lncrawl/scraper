"""Individual tiers: the archive, the baseline, and delegation."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict

import pytest
import requests

from scraper.exceptions import TierUnavailable
from scraper.identity import Identity
from scraper.tiers import ArchiveTier, Call, DirectTier, ManagedTier, http_provider
from scraper.tiers.archive import SOURCE_HEADER

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
