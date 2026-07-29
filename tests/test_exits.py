"""Addresses: honest kinds, sticky leases, and the pool contract."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Tuple

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
        assert pool.lease("example.com").exit_id == "direct"

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
            ("socks5h://host:9250", "abc", "", "socks5h://abc@host:9250"),
        ],
    )
    def test_userinfo_is_assembled_safely(self, url, username, password, expected):
        assert with_credentials(url, username, password) == expected


# -- the tor-pool contract ----------------------------------------------------------


class _PoolHandler(BaseHTTPRequestHandler):
    """Records what the pool was told, and answers like a real one."""

    calls: List[Tuple[str, Dict]] = []
    auth: List[str] = []
    status = 200

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

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches the base signature
        """Silence the default stderr logging."""


@pytest.fixture
def pool_api():
    _PoolHandler.calls = []
    _PoolHandler.auth = []
    _PoolHandler.status = 200
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

    def test_a_pool_that_is_down_never_breaks_the_scrape(self):
        pool = ExitPool([TorPoolSpec(api_url="http://127.0.0.1:1", token=TOKEN)])
        lease = pool.lease("example.com")
        pool.report(lease, Layer.IP_REPUTATION)  # must not raise
        assert pool.rotate("example.com", Layer.IP_REPUTATION) is not None

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
