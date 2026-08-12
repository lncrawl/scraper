"""Addresses: honest kinds, sticky leases, and the pool contract."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional, Tuple

import pytest

from scraper.exits import (
    ExitKind,
    ExitPool,
    ExitSpec,
    TorPoolSpec,
    failure_kind,
    with_credentials,
)
from scraper.layers import Layer

TOKEN = "tp_7Kq2mXvR8nB4jL6wYtZaPc"


class TestKinds:
    def test_published_ranges_do_not_clear_the_reputation_layer(self):
        # Not pessimism — a claim this library relies on. It is what stops the
        # planner from rotating between blocklisted addresses forever.
        assert ExitKind.TOR.reach == frozenset()
        assert ExitKind.DATACENTER.reach == frozenset()

    def test_real_subscriber_addresses_do(self):
        for kind in (ExitKind.MOBILE, ExitKind.RESIDENTIAL, ExitKind.ISP, ExitKind.DIRECT):
            assert Layer.IP_REPUTATION in kind.reach

    def test_kinds_are_ranked_worst_to_best(self):
        assert ExitKind.MOBILE.rank > ExitKind.RESIDENTIAL.rank > ExitKind.DATACENTER.rank
        assert ExitKind.DATACENTER.rank > ExitKind.TOR.rank

    def test_a_proxy_url_needs_a_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            ExitSpec(url="127.0.0.1:9050")


class TestLeases:
    def test_an_origin_keeps_one_address(self):
        # Stickiness is the point: a clearance and the accumulated history are both
        # bound to the address, so moving it invalidates both.
        pool = ExitPool([ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL)])
        assert pool.lease("example.com").exit_id == pool.lease("example.com").exit_id

    def test_different_origins_get_their_own_leases(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL, label="a"),
                ExitSpec(url="http://b.test:1", kind=ExitKind.RESIDENTIAL, label="b"),
            ]
        )
        assert pool.lease("one.test").exit_id != pool.lease("two.test").exit_id

    def test_the_best_available_kind_is_preferred(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://dc.test:1", kind=ExitKind.DATACENTER),
                ExitSpec(url="http://res.test:1", kind=ExitKind.RESIDENTIAL),
            ]
        )
        assert pool.lease("example.com").kind is ExitKind.RESIDENTIAL
        assert pool.best_kind is ExitKind.RESIDENTIAL

    def test_the_pools_reach_is_the_best_kinds_reach(self):
        pool = ExitPool([ExitSpec(url="socks5h://t.test:9050", kind=ExitKind.TOR)])
        assert Layer.IP_REPUTATION not in pool.reach()

    def test_no_exits_means_a_direct_connection(self):
        pool = ExitPool([])
        assert not pool.configured
        assert pool.lease("example.com").proxies is None
        assert pool.lease("example.com").exit_id == "direct#example.com"

    def test_an_unconfigured_pool_reports_no_reach(self):
        # `best_kind` falls back to DIRECT, whose reach covers layer 1 — true when
        # there is a proxy to move off, meaningless when there is nothing configured.
        # Reported anyway it told the planner a remedy existed that it could not run.
        assert ExitPool([]).reach() == frozenset()

    def test_direct_gates_are_per_origin(self):
        # One shared "direct" id gated the whole process at max_sessions_per_exit
        # whenever no proxy was configured, which is what forced consumers to build
        # one pool per domain.
        pool = ExitPool([])
        assert pool.slot(pool.lease("a.test")) is not pool.slot(pool.lease("b.test"))

    def test_a_kind_without_a_proxy_url_is_refused(self):
        # Every packet would leave from the local address while the pool reported
        # residential reach and explain() printed the kind.
        with pytest.raises(ValueError, match="needs a proxy URL"):
            ExitSpec(kind=ExitKind.RESIDENTIAL)
        # DIRECT with no URL is the honest one, and what a fallback entry looks like.
        assert ExitSpec(kind=ExitKind.DIRECT, label="direct").name == "direct"

    def test_a_rotated_address_does_not_leave_its_gate_behind(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL, label="a"),
                ExitSpec(url="http://b.test:1", kind=ExitKind.RESIDENTIAL, label="b"),
            ]
        )
        pool.slot(pool.lease("example.com"))
        for _ in range(5):
            pool.slot(pool.rotate("example.com", Layer.IP_REPUTATION))
        assert len(pool._slots) == 1, "each rotation minted a gate and kept it forever"

    def test_both_schemes_route_through_one_entry(self):
        # Otherwise http and https requests to one origin leave from different
        # exits and everything bound to the address comes apart.
        pool = ExitPool([ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL)])
        proxies = pool.lease("example.com").proxies
        assert proxies is not None
        assert proxies["http"] == proxies["https"]

    def test_a_rotation_produces_a_different_address_identifier(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL, label="a"),
                ExitSpec(url="http://b.test:1", kind=ExitKind.RESIDENTIAL, label="b"),
            ]
        )
        before = pool.lease("example.com")
        after = pool.rotate("example.com", Layer.IP_REPUTATION)
        assert after.exit_id != before.exit_id

    def test_a_single_exit_is_still_re_leased_rather_than_lost(self):
        pool = ExitPool([ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL)])
        pool.lease("example.com")
        assert pool.rotate("example.com", Layer.IP_REPUTATION).spec.url == "http://a.test:1"

    def test_releasing_forgets_without_blaming(self):
        pool = ExitPool([ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL)])
        first = pool.lease("example.com")
        pool.release("example.com")
        assert pool.lease("example.com").exit_id != first.exit_id

    def test_concurrency_per_address_is_clamped_low(self):
        # Concurrent sessions per address is itself a behavioural signal, so the
        # configured value is clamped rather than trusted.
        pool = ExitPool([ExitSpec(url="http://a.test:1")], max_sessions_per_exit=99)
        gate = pool.slot(pool.lease("example.com"))
        acquired = sum(1 for _ in range(10) if gate.acquire(blocking=False))
        assert acquired <= 3

    def test_the_same_address_shares_one_gate(self):
        pool = ExitPool([ExitSpec(url="http://a.test:1")])
        lease = pool.lease("example.com")
        assert pool.slot(lease) is pool.slot(lease)


class TestRetiring:
    def test_when_every_exit_is_retired_the_oldest_comes_back(self):
        # Falling back to a direct connection would silently drop the whole
        # reputation strategy the caller configured these addresses for.
        pool = ExitPool(
            [ExitSpec(url="http://a.test:1"), ExitSpec(url="http://b.test:1")],
            retire_for=600.0,
        )
        pool.lease("example.com")
        pool.rotate("example.com", Layer.IP_REPUTATION)

        recycled = pool.rotate("example.com", Layer.IP_REPUTATION)

        assert recycled.spec.url == "http://a.test:1", "the longest-rested exit is next"

    def test_rotating_an_origin_that_was_never_leased_just_leases_one(self):
        # Nothing to report and nothing to retire: an origin with no history has no
        # address to blame, and blaming one anyway would retire it on no evidence.
        pool = ExitPool([ExitSpec(url="http://a.test:1"), ExitSpec(url="http://b.test:1")])
        assert pool.rotate("example.com", Layer.IP_REPUTATION).spec.url == "http://a.test:1"

    def test_a_retirement_expires(self):
        # A block is evidence about a moment, not a life sentence: exits recover, and
        # a pool that never un-retires anything shrinks to nothing over a long run.
        pool = ExitPool(
            [ExitSpec(url="http://a.test:1"), ExitSpec(url="http://b.test:1")],
            retire_for=0.0,
        )
        pool.lease("example.com")
        pool.rotate("example.com", Layer.IP_REPUTATION)

        assert pool.rotate("example.com", Layer.IP_REPUTATION).spec.url == "http://a.test:1"


class TestStatus:
    def test_it_reports_what_is_retired_and_when_it_returns(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://a.test:1", kind=ExitKind.RESIDENTIAL),
                ExitSpec(url="http://b.test:1", kind=ExitKind.DATACENTER),
            ],
            retire_for=600.0,
        )
        pool.lease("example.com")
        pool.rotate("example.com", Layer.IP_REPUTATION)

        rows = {row.name: row for row in pool.status()}
        assert rows["a.test:1"].retired
        assert 0 < rows["a.test:1"].returns_in <= 600.0
        assert rows["a.test:1"].origins == 0
        assert not rows["b.test:1"].retired
        assert rows["b.test:1"].returns_in == 0.0
        assert rows["b.test:1"].origins == 1

    def test_it_lists_the_best_kind_first(self):
        pool = ExitPool(
            [
                ExitSpec(url="http://dc.test:1", kind=ExitKind.DATACENTER),
                ExitSpec(url="http://mob.test:1", kind=ExitKind.MOBILE),
            ]
        )
        assert [row.kind for row in pool.status()] == [ExitKind.MOBILE, ExitKind.DATACENTER]

    def test_a_credential_never_reaches_the_status_view(self):
        # Written for a status page, and a proxy URL carries its password.
        pool = ExitPool([ExitSpec(url="http://user:hunter2@a.test:1", kind=ExitKind.ISP)])
        pool.lease("example.com")
        assert "hunter2" not in json.dumps([row.name for row in pool.status()])

    def test_nothing_configured_is_an_empty_view(self):
        assert ExitPool().status() == []


class TestFailureKinds:
    @pytest.mark.parametrize(
        ("layer", "expected"),
        [
            (Layer.IP_REPUTATION, "blocked"),
            (Layer.SUPER_BOT_FIGHT, "blocked"),
            (Layer.MANAGED_CHALLENGE, "captcha"),
            (Layer.TURNSTILE, "captcha"),
            (Layer.CDP, "captcha"),
            # A throttle says the exit works and is busy. Reported as a block it
            # retires a working exit and the replacement is throttled the same.
            (Layer.BEHAVIOURAL, "rate_limited"),
            (Layer.WORKERS, "other"),
            (None, "transport"),
        ],
    )
    def test_a_layer_translates_to_the_pools_vocabulary(self, layer, expected: str):
        assert failure_kind(layer) == expected


class TestCredentials:
    @pytest.mark.parametrize(
        ("url", "username", "password", "expected"),
        [
            ("socks5h://host:9250", "abc", "tp_tok", "socks5h://abc:tp_tok@host:9250"),
            # Explicit credentials win: the operator named their own session and
            # owns the password that goes with it.
            ("socks5h://mine:pw@host:9250", "abc", "tp_tok", "socks5h://mine:pw@host:9250"),
            # A key needing escaping must not corrupt the authority.
            ("socks5h://host:9250", "a@b/c", "tp_tok", "socks5h://a%40b%2Fc:tp_tok@host:9250"),
            # IPv6 literals stay bracketed.
            ("socks5h://[::1]:9250", "abc", "tp_tok", "socks5h://abc:tp_tok@[::1]:9250"),
            ("socks5h://host", "abc", "tp_tok", "socks5h://abc:tp_tok@host"),
            ("socks5h://host:9250", "", "tp_tok", "socks5h://host:9250"),
            # An unset token still gets a placeholder password, because a username
            # without one never goes on the wire at all. See below.
            ("socks5h://host:9250", "abc", "", "socks5h://abc:-@host:9250"),
        ],
    )
    def test_userinfo_is_assembled_safely(self, url, username, password, expected):
        assert with_credentials(url, username, password) == expected

    def test_session_key_survives_an_unset_token(self):
        """A tokenless pool must still be told which session is calling.

        RFC 1929 cannot express a username with no password, so a SOCKS5 client
        handed one skips authentication and the key never arrives. The pool then
        keys by client address and pins every session to a single instance — a pool
        of any size serving one exit, with nothing on either side reporting a fault.
        """
        url = with_credentials("socks5h://host:9250", "session-key", "")
        assert urllib.parse.urlsplit(url).username == "session-key"
        assert urllib.parse.urlsplit(url).password


# -- the tor-pool contract ----------------------------------------------------------


class _PoolHandler(BaseHTTPRequestHandler):
    """Records what the pool was told, and answers like a real one."""

    calls: List[Tuple[str, Dict]] = []
    auth: List[str] = []
    status = 200
    session_port: Optional[int] = 19602
    """What `GET /api/sessions/{key}` reports. ``None`` omits the field, as a real
    pool does when `SESSION_PORT_BASE` is unset — absent means "not available"
    rather than "port zero"."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append((f"GET {self.path}", {}))
        type(self).auth.append(self.headers.get("authorization") or "")
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body: Dict[str, object] = {"session": "s", "instance": 2}
        if type(self).session_port is not None:
            body["session_port"] = type(self).session_port
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        type(self).calls.append((self.path, json.loads(raw) if raw else {}))
        type(self).auth.append(self.headers.get("authorization") or "")
        payload = json.dumps({"instance": 2}).encode()
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).calls.append((f"DELETE {self.path}", {}))
        type(self).auth.append(self.headers.get("authorization") or "")
        self.send_response(204)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches the base signature
        """Silence the default stderr logging."""


@pytest.fixture
def pool_api():
    _PoolHandler.calls = []
    _PoolHandler.auth = []
    _PoolHandler.status = 200
    _PoolHandler.session_port = 19602
    server = HTTPServer(("127.0.0.1", 0), _PoolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _PoolHandler
    finally:
        server.shutdown()
        server.server_close()


def tor_pool(api_url: str, **kwargs) -> ExitPool:
    return ExitPool([TorPoolSpec(api_url=api_url, token=TOKEN, **kwargs)])


class TestReleasingASession:
    """Found live: nothing ever told the pool a session was finished.

    Each lease mints a fresh key, and an unreleased session holds its slot until the
    pool's SESSION_TTL — so a process building several scrapers in a row walked the
    pool out of capacity. The symptom is the misleading part: the next lease cannot
    connect, a transport failure through a proxy is evidence about the exit, and the
    model then reports a reputation block on a destination that never saw the request.
    """

    def test_releasing_tells_the_pool(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        lease = pool.lease("example.com")
        pool.release("example.com")
        assert (f"DELETE /api/sessions/{lease.session_key}", {}) in handler.calls

    def test_release_all_covers_every_origin_held(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        keys = [pool.lease(f"site{n}.test").session_key for n in range(3)]
        pool.release_all()
        dropped = {path for path, _ in handler.calls if path.startswith("DELETE")}
        assert dropped == {f"DELETE /api/sessions/{key}" for key in keys}

    def test_a_pool_that_is_down_does_not_break_closing(self, pool_api):
        # Best-effort, like reporting a failure: releasing runs from close(), and an
        # unreachable pool must not turn a finished scrape into an exception.
        pool = ExitPool([TorPoolSpec(api_url="http://127.0.0.1:1", token=TOKEN)])
        pool.lease("example.com")
        pool.release_all()

    def test_a_plain_proxy_has_nothing_to_release(self, pool_api):
        api_url, handler = pool_api
        pool = ExitPool([ExitSpec(url="http://user:pw@proxy.test:8000", kind=ExitKind.DATACENTER)])
        pool.lease("example.com")
        pool.release_all()
        assert not [path for path, _ in handler.calls if path.startswith("DELETE")]


class TestTorPool:
    def test_the_session_key_becomes_the_socks_username(self, pool_api):
        api_url, _ = pool_api
        lease = tor_pool(api_url).lease("example.com")
        proxies = lease.proxies
        assert proxies is not None
        assert proxies["https"] == f"socks5h://{lease.session_key}:{TOKEN}@127.0.0.1:9250"

    def test_a_pool_is_reported_as_tor_because_that_is_what_it_is(self, pool_api):
        # Exit lists are published. Claiming otherwise would only hide the reason a
        # scored site keeps refusing.
        api_url, _ = pool_api
        assert tor_pool(api_url).best_kind is ExitKind.TOR

    def test_rotating_asks_the_pool_and_keeps_the_endpoint(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        before = pool.lease("example.com")
        after = pool.rotate("example.com", Layer.IP_REPUTATION)
        paths = [path for path, _ in handler.calls]
        assert any(path.endswith("/failure") for path in paths)
        assert any(path.endswith("/rotate") for path in paths)
        assert after.spec.url == before.spec.url
        # The URL is unchanged but the instance behind it moved, so anything bound
        # to the address has to see a new identifier.
        assert after.exit_id != before.exit_id

    def test_the_failure_goes_out_before_the_rotation(self, pool_api):
        # Otherwise the next consumer leases the exit that just failed.
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        pool.lease("example.com")
        pool.rotate("example.com", Layer.MANAGED_CHALLENGE)
        paths = [path for path, _ in handler.calls]
        assert paths[0].endswith("/failure")

    def test_a_report_carries_the_kind_the_pool_weighs_it_by(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        pool.report(pool.lease("example.com"), Layer.MANAGED_CHALLENGE)
        assert handler.calls[0][1]["kind"] == "captcha"
        assert "L9" in handler.calls[0][1]["reason"]

    def test_every_call_carries_the_token(self, pool_api):
        # The proxy half of a missing token fails loudly on every request; this half
        # fails silently, and then the pool stops hearing about soft blocks.
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        pool.report(pool.lease("example.com"), Layer.IP_REPUTATION)
        assert handler.auth == [f"Bearer {TOKEN}"]

    def test_no_token_means_no_header(self, pool_api):
        api_url, handler = pool_api
        pool = ExitPool([TorPoolSpec(api_url=api_url, token="")])
        pool.report(pool.lease("example.com"), Layer.IP_REPUTATION)
        assert handler.auth == [""]

    def test_reporting_can_be_switched_off(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url, report_failures=False)
        pool.report(pool.lease("example.com"), Layer.IP_REPUTATION)
        assert handler.calls == []


class TestAnAddressABrowserCanUse:
    """Chrome rejects `--proxy-server` outright when the URL carries userinfo, and a
    pool lease's URL always does — the session key travels as the SOCKS5 username.

    Dropping the credential is not the fix: the pool then keys by client address, so
    the browser leaves by a different instance than the requests replaying its
    clearance, and a clearance from an address that did not earn it reads as the site
    refusing us. tor-pool's answer is one credential-free listener per instance, whose
    port it reports per session.
    """

    def test_the_pools_credential_free_port_is_asked_for_not_assembled(self, pool_api):
        api_url, handler = pool_api
        pool = tor_pool(api_url)
        lease = pool.lease("example.com")
        assert pool.browser_proxy(lease) == "socks5h://127.0.0.1:19602"
        # Asked per session, because which instance a session sits on is the pool's
        # to know: SESSION_PORT_BASE is not visible from here, and draining moves a
        # session without telling us.
        assert any(
            path.startswith(f"GET /api/sessions/{lease.session_key}") for path, _ in handler.calls
        )

    def test_it_carries_no_credential(self, pool_api):
        api_url, _ = pool_api
        pool = tor_pool(api_url)
        lease = pool.lease("example.com")
        address = pool.browser_proxy(lease)
        assert address is not None
        parsed = urllib.parse.urlsplit(address)
        assert not parsed.username and not parsed.password
        # And the credentialed URL the requests half uses is unchanged.
        assert urllib.parse.urlsplit((lease.proxies or {})["https"]).username

    def test_a_pool_without_the_listeners_offers_nothing(self, pool_api):
        # SESSION_PORT_BASE unset. None means "skip the browser", and the tier turns
        # that into TierUnavailable — launching anyway would earn a clearance on an
        # address the requests cannot replay it from.
        api_url, handler = pool_api
        handler.session_port = None
        pool = tor_pool(api_url)
        assert pool.browser_proxy(pool.lease("example.com")) is None

    def test_a_session_the_pool_has_not_pinned_yet_offers_nothing(self, pool_api):
        # A 404: nothing has been fetched through this session, so there is no
        # instance to name a port on.
        api_url, handler = pool_api
        handler.status = 404
        pool = tor_pool(api_url)
        assert pool.browser_proxy(pool.lease("example.com")) is None

    def test_a_pool_that_is_down_offers_nothing(self):
        pool = ExitPool([TorPoolSpec(api_url="http://127.0.0.1:1", token=TOKEN)])
        assert pool.browser_proxy(pool.lease("example.com")) is None

    def test_the_listener_is_socks_whatever_the_endpoint_was(self, pool_api):
        # An operator who pointed the exit at the pool's HTTP proxy still gets a
        # socks5h:// URL, because the per-instance listeners only speak SOCKS.
        api_url, _ = pool_api
        pool = ExitPool([TorPoolSpec(url="http://127.0.0.1:9251", api_url=api_url, token=TOKEN)])
        assert pool.browser_proxy(pool.lease("example.com")) == "socks5h://127.0.0.1:19602"

    def test_a_direct_exit_needs_no_proxy_at_all(self):
        pool = ExitPool()
        assert pool.browser_proxy(pool.lease("example.com")) == ""

    def test_a_plain_proxy_is_handed_over_as_configured(self):
        # Nothing to look up: it has no session, and if it needs a credential the
        # browser layer refuses it with a message naming that.
        pool = ExitPool([ExitSpec(url="socks5h://p.test:1080", kind=ExitKind.DATACENTER)])
        assert pool.browser_proxy(pool.lease("example.com")) == "socks5h://p.test:1080"

    def test_a_pool_that_is_down_never_breaks_the_scrape(self):
        pool = ExitPool([TorPoolSpec(api_url="http://127.0.0.1:1", token=TOKEN)])
        lease = pool.lease("example.com")
        pool.report(lease, Layer.IP_REPUTATION)  # must not raise
        assert pool.rotate("example.com", Layer.IP_REPUTATION) is not None

    def test_a_rejected_credential_is_logged_at_error_not_swallowed(self, pool_api, caplog):
        """Found live: without a valid token the pool stops hearing about soft blocks.

        Nothing fails — the proxy still forwards traffic — so burnt exits keep taking
        requests and neither side looks broken. The log line is the only symptom, so
        it has to be loud and it has to name the fix.
        """
        api_url, handler = pool_api
        handler.status = 403
        pool = tor_pool(api_url)
        with caplog.at_level("ERROR", logger="scraper.exits"):
            pool.report(pool.lease("example.com"), Layer.IP_REPUTATION)
        assert "token" in caplog.text
        assert "not working" in caplog.text

    def test_an_unknown_session_is_routine_rather_than_alarming(self, pool_api, caplog):
        # Acting on a report the pool unpins the session, so the next report has
        # nothing to attach to and the next request re-pins to a healthy instance.
        api_url, handler = pool_api
        handler.status = 404
        pool = tor_pool(api_url)
        with caplog.at_level("WARNING", logger="scraper.exits"):
            pool.report(pool.lease("example.com"), Layer.IP_REPUTATION)
        assert caplog.text == ""

    def test_an_error_response_leaves_the_pool_usable(self, pool_api):
        api_url, handler = pool_api
        handler.status = 503
        pool = tor_pool(api_url)
        pool.lease("example.com")
        assert pool.rotate("example.com", Layer.IP_REPUTATION).proxies is not None

    def test_a_plain_proxy_is_left_alone(self, pool_api):
        api_url, handler = pool_api
        pool = ExitPool([ExitSpec(url="socks5h://127.0.0.1:9050", kind=ExitKind.DATACENTER)])
        lease = pool.lease("example.com")
        proxies = lease.proxies
        assert proxies is not None
        assert proxies["https"] == "socks5h://127.0.0.1:9050"
        pool.report(lease, Layer.IP_REPUTATION)
        assert handler.calls == []
