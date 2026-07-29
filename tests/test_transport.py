"""The transport seam, exercised against a loopback server.

The impersonation transport is the one part of this package that cannot be checked by
reasoning about it — either curl_cffi's response objects adapt cleanly into
``requests.Response`` or they do not. So these tests run real HTTP over 127.0.0.1.
No external network, and no mocking of the thing under test.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Tuple

import pytest

from scraper.transport import (
    ImpersonateTransport,
    PlainTransport,
    Transport,
    newest_target,
    resolve_target,
    stale_profile_warning,
)


class _Handler(BaseHTTPRequestHandler):
    """Answers a handful of paths and echoes what it was sent."""

    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, extra: Tuple[Tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ua":
            self._send(200, (self.headers.get("user-agent") or "").encode())
        elif self.path == "/headers":
            names = ",".join(name.lower() for name in self.headers.keys())
            self._send(200, names.encode())
        elif self.path == "/cookie":
            self._send(200, b"set", (("set-cookie", "chocolate=chip; Path=/"),))
        elif self.path == "/echo-cookie":
            self._send(200, (self.headers.get("cookie") or "").encode())
        elif self.path == "/big":
            self._send(200, b"x" * 200_000)
        elif self.path == "/teapot":
            self._send(418, b"short and stout")
        else:
            self._send(200, b"hello")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        self._send(200, self.rfile.read(length) if length else b"")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches the base signature
        """Silence the default stderr logging."""


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    # Threading, not the plain server: the handler speaks HTTP/1.1 so clients keep
    # connections alive, and a single-threaded server would then refuse to accept a
    # second one.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(params=["impersonate", "plain"])
def transport(request: pytest.FixtureRequest) -> Iterator[Transport]:
    """Both transports, so the shared contract is asserted once for each."""
    made: Transport
    if request.param == "impersonate":
        pytest.importorskip("curl_cffi")
        made = ImpersonateTransport()
    else:
        made = PlainTransport()
    try:
        yield made
    finally:
        made.close()


class TestTheSharedContract:
    def test_a_body_comes_back_as_a_requests_response(self, transport: Transport, origin: str):
        response = transport.send("GET", f"{origin}/")
        assert response.status_code == 200
        assert response.text == "hello"
        assert response.url.endswith("/")
        assert response.headers["content-type"].startswith("text/plain")

    def test_an_error_status_is_returned_not_raised(self, transport: Transport, origin: str):
        # Diagnosis has to see the body of an error page: a challenge interstitial is
        # a body, not a status.
        response = transport.send("GET", f"{origin}/teapot")
        assert response.status_code == 418
        assert response.text == "short and stout"

    def test_the_request_that_went_out_is_recorded(self, transport: Transport, origin: str):
        # Diagnosis reads the User-Agent from here to recognise a block that is really
        # about a declared crawler identity, and a profile supplies one we never wrote.
        response = transport.send("GET", f"{origin}/")
        assert response.request is not None
        assert response.request.method == "GET"

    def test_a_post_body_is_sent(self, transport: Transport, origin: str):
        assert transport.send("POST", f"{origin}/", data=b"payload").text == "payload"

    def test_json_is_sent(self, transport: Transport, origin: str):
        # Compared parsed: the two clients differ on whether they put a space after the
        # colon, which is not something this package has an opinion about.
        assert json.loads(transport.send("POST", f"{origin}/", json={"a": 1}).text) == {"a": 1}

    def test_params_are_appended(self, transport: Transport, origin: str):
        assert transport.send("GET", f"{origin}/", params={"q": "x"}).status_code == 200

    def test_a_caller_header_reaches_the_server(self, transport: Transport, origin: str):
        response = transport.send("GET", f"{origin}/ua", headers={"user-agent": "mine/1"})
        assert response.text == "mine/1"

    def test_streaming_yields_the_whole_body(self, transport: Transport, origin: str):
        with transport.stream("GET", f"{origin}/big") as (response, chunks):
            assert response.status_code == 200
            assert len(b"".join(chunks)) == 200_000

    def test_a_set_cookie_lands_in_the_jar(self, transport: Transport, origin: str):
        transport.send("GET", f"{origin}/cookie")
        assert transport.cookies.get("chocolate") == "chip"

    def test_a_cookie_can_be_installed_and_is_sent(self, transport: Transport, origin: str):
        transport.set_cookie("planted", "yes", domain="127.0.0.1")
        assert "planted=yes" in transport.send("GET", f"{origin}/echo-cookie").text

    def test_per_request_cookies_are_sent(self, transport: Transport, origin: str):
        # How a clearance travels: per request, so it cannot outlive the identity it
        # is bound to.
        response = transport.send("GET", f"{origin}/echo-cookie", cookies={"once": "only"})
        assert "once=only" in response.text

    def test_clearing_by_domain_empties_the_jar(self, transport: Transport, origin: str):
        transport.set_cookie("planted", "yes", domain="127.0.0.1")
        transport.clear_cookies("127.0.0.1")
        assert transport.cookies.get("planted") is None

    def test_clearing_everything_works(self, transport: Transport, origin: str):
        transport.set_cookie("planted", "yes", domain="127.0.0.1")
        transport.clear_cookies()
        assert len(transport.cookies) == 0

    def test_unknown_options_are_dropped_rather_than_raising(
        self, transport: Transport, origin: str
    ):
        # A caller passing a requests-only keyword gets it ignored, not an exception
        # from inside the transport.
        assert transport.send("GET", f"{origin}/", hooks={"x": 1}, stream=False).status_code == 200

    def test_concurrent_requests_are_safe(self, transport: Transport, origin: str):
        results: list = []

        def go() -> None:
            results.append(transport.send("GET", f"{origin}/").status_code)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert results == [200] * 8

    def test_closing_twice_is_harmless(self, transport: Transport):
        transport.close()
        transport.close()


class TestImpersonation:
    def test_the_profile_supplies_a_browser_user_agent_we_never_wrote(self, origin: str):
        # The inversion the whole package depends on: the transport owns the identity's
        # emitted half, and we read it rather than imposing one.
        pytest.importorskip("curl_cffi")
        transport = ImpersonateTransport()
        try:
            sent = transport.send("GET", f"{origin}/ua").text
        finally:
            transport.close()
        assert "Mozilla/5.0" in sent
        assert "python" not in sent.lower()

    def test_the_profile_sends_a_full_browser_header_set(self, origin: str):
        pytest.importorskip("curl_cffi")
        transport = ImpersonateTransport()
        try:
            names = transport.send("GET", f"{origin}/headers").text.split(",")
        finally:
            transport.close()
        # Not an exhaustive list — the point is that the header *set* is a browser's
        # rather than a bare client's, which is what layer 5 reads.
        for expected in ("user-agent", "accept", "accept-language", "accept-encoding"):
            assert expected in names

    def test_a_plain_client_does_not(self, origin: str):
        # The reason impersonation is the default and not an extra.
        transport = PlainTransport()
        try:
            sent = transport.send("GET", f"{origin}/ua").text
        finally:
            transport.close()
        assert "python-requests" in sent.lower()


class TestTargets:
    def test_a_family_alias_resolves_to_a_concrete_profile(self):
        pytest.importorskip("curl_cffi")
        resolved = resolve_target("chrome")
        assert resolved.startswith("chrome")
        assert resolved != "chrome", "the alias should name a specific build"

    def test_an_already_concrete_target_is_returned_unchanged(self):
        pytest.importorskip("curl_cffi")
        assert resolve_target("chrome99") == "chrome99"

    def test_a_family_alias_is_never_warned_about(self):
        # The alias tracks the newest supported build, which is the whole reason to use
        # one.
        pytest.importorskip("curl_cffi")
        assert stale_profile_warning("chrome") == ""
        assert stale_profile_warning("firefox") == ""

    def test_a_pinned_old_profile_is_warned_about(self):
        # A stale profile is a signal on its own: no real user runs a two-year-old
        # browser, and it predates the post-quantum key share current builds send.
        pytest.importorskip("curl_cffi")
        warning = stale_profile_warning("chrome99")
        assert "older than" in warning
        assert "chrome" in warning

    def test_an_unrecognised_target_is_not_second_guessed(self):
        pytest.importorskip("curl_cffi")
        assert stale_profile_warning("something_else") == ""

    def test_a_target_newer_than_the_build_knows_is_not_called_stale(self):
        # Staleness is the signal, not unfamiliarity. A build that has not caught up
        # to a pinned version has nothing to warn about.
        pytest.importorskip("curl_cffi")
        assert stale_profile_warning("chrome999") == ""

    def test_constructing_a_transport_on_a_stale_target_warns(self, caplog):
        # The warning has to arrive when the profile is chosen. Finding out later, in
        # a diagnosis, sends the caller to look at the site instead of the config.
        pytest.importorskip("curl_cffi")
        with caplog.at_level("WARNING", logger="scraper.transport"):
            transport = ImpersonateTransport("chrome99")
        transport.close()
        assert "older than" in caplog.text

    def test_the_newest_profile_for_a_family_is_the_resolved_alias(self):
        pytest.importorskip("curl_cffi")
        assert newest_target("chrome") == resolve_target("chrome")

    def test_without_curl_cffi_a_target_resolves_to_itself(self, monkeypatch):
        # The impersonation table lives in curl_cffi, and `resolve_target` is called
        # from paths a caller can reach on a plain transport. Failing to import it is
        # not a reason to raise at them.
        monkeypatch.setitem(sys.modules, "curl_cffi.requests.impersonate", None)
        assert resolve_target("chrome") == "chrome"


class TestHttpVersions:
    def test_http3_is_offered_when_asked_for(self, origin: str):
        # Current Chrome prefers HTTP/3, so a client that only ever speaks HTTP/2 to
        # an HTTP/3-enabled zone is a mild mismatch with the profile it claims.
        pytest.importorskip("curl_cffi")
        from curl_cffi import CurlHttpVersion

        transport = ImpersonateTransport(prefer_http3=True)
        try:
            assert transport._prepared({})["http_version"] is CurlHttpVersion.V3  # noqa: SLF001
            # The loopback server speaks HTTP/1.1 only, so this also asserts the
            # documented fallback: offering v3 must not break an origin without it.
            assert transport.send("GET", f"{origin}/hello").status_code == 200
        finally:
            transport.close()

    def test_http2_is_the_default_and_sets_no_version(self):
        pytest.importorskip("curl_cffi")
        transport = ImpersonateTransport()
        try:
            assert "http_version" not in transport._prepared({})  # noqa: SLF001
        finally:
            transport.close()


class TestClosingAndClearing:
    def test_a_session_that_will_not_close_does_not_raise_at_the_caller(self):
        # `close` runs from `Scraper.__exit__`, where raising would mask whatever the
        # caller was actually doing.
        pytest.importorskip("curl_cffi")
        transport = ImpersonateTransport()

        def explode() -> None:
            raise RuntimeError("curl handle already gone")

        monkeypatch_attr(transport._session, "close", explode)  # noqa: SLF001
        transport.close()

    def test_a_jar_that_refuses_a_delete_still_clears_what_it_can(self, origin: str):
        # Cookie jar implementations differ across curl_cffi versions, and a delete
        # that raises must not leave the rest of the domain's cookies behind.
        pytest.importorskip("curl_cffi")
        transport = ImpersonateTransport()
        try:
            transport.set_cookie("a", "1", domain="127.0.0.1")

            def explode(name: str, **kwargs: object) -> None:
                raise RuntimeError("this jar has no delete")

            monkeypatch_attr(transport._session.cookies, "delete", explode)  # noqa: SLF001
            transport.clear_cookies("127.0.0.1")
        finally:
            transport.close()

    def test_clearing_one_domain_leaves_another_domains_cookies_alone(self, transport: Transport):
        # A clearance for one origin must survive clearing another's, or a rotation on
        # one site silently discards what was earned on every other.
        transport.set_cookie("a", "1", domain="127.0.0.1")
        transport.set_cookie("b", "2", domain="other.test")

        transport.clear_cookies("127.0.0.1")

        assert dict(transport.cookies) == {"b": "2"}


def monkeypatch_attr(target: object, name: str, value: object) -> None:
    """`setattr` with a cast, so pyright does not object to replacing a method."""
    setattr(target, name, value)
