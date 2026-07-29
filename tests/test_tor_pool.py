"""tor-pool integration: session pinning, rotation and failure reporting."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scraper.config import ProxyConfig, ProxyUrl, TorPoolProxyUrl, TorProxyUrl
from scraper.engine.proxy_manager import ProxyManager, _with_credentials

# TOKEN is the proxy credential tor-pool 0.2 and later require.
TOKEN = "tp_7Kq2mXvR8nB4jL6wYtZaPc"


class _PoolHandler(BaseHTTPRequestHandler):
    """Records what the manager sent, and replies like a real pool."""

    calls: list[tuple[str, dict]] = []
    auth: list[str | None] = []
    status = 200

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        type(self).calls.append((self.path, body))
        type(self).auth.append(self.headers.get("authorization"))

        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        payload = json.dumps({"instance": 2}).encode()
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches the base signature
        """Silence the default stderr logging."""


@pytest.fixture
def pool_api():
    """A stand-in tor-pool API on a real socket."""
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


def make_manager(api_url: str, **kwargs) -> ProxyManager:
    kwargs.setdefault("token", TOKEN)
    entry = TorPoolProxyUrl(url="socks5h://127.0.0.1:9250", api_url=api_url, **kwargs)
    return ProxyManager(ProxyConfig(proxy_urls=[entry]))


def test_session_key_is_injected_as_socks_username(pool_api):
    api_url, _ = pool_api
    manager = make_manager(api_url, session="my-session")

    proxies = manager.get_proxy()
    assert proxies is not None
    assert proxies["https"] == f"socks5h://my-session:{TOKEN}@127.0.0.1:9250"
    # Both schemes must route through the same session, or http and https
    # requests would land on different exits.
    assert proxies["http"] == proxies["https"]


def test_generated_session_key_is_stable(pool_api):
    api_url, _ = pool_api
    manager = make_manager(api_url)

    first = manager.get_proxy()
    second = manager.get_proxy()
    assert first == second, "the key must not change between requests, or stickiness is lost"
    assert first is not None
    assert "@127.0.0.1:9250" in first["https"]


def test_separate_managers_get_separate_sessions(pool_api):
    # Two Scrapers in one process should not share an exit IP by accident.
    api_url, _ = pool_api
    assert make_manager(api_url).get_proxy() != make_manager(api_url).get_proxy()


def test_rotate_calls_the_pool_and_keeps_the_url(pool_api):
    api_url, handler = pool_api
    manager = make_manager(api_url, session="abc")

    before = manager.get_proxy()
    manager.rotate()
    after = manager.get_proxy()

    assert [path for path, _ in handler.calls] == ["/api/sessions/abc/rotate"]
    # The endpoint is unchanged; only the instance behind it moved.
    assert before == after


def test_rotate_does_not_deadlock(pool_api):
    # rotate() holds the manager's lock while resolving the session key, and
    # threading.Lock is not reentrant.
    api_url, _ = pool_api
    manager = make_manager(api_url)

    done = threading.Event()

    def go() -> None:
        manager.rotate()
        done.set()

    threading.Thread(target=go, daemon=True).start()
    assert done.wait(timeout=5), "rotate() deadlocked"


def test_report_failure_posts_the_reason_and_its_kind(pool_api):
    api_url, handler = pool_api
    manager = make_manager(api_url, session="abc")

    manager.report_failure("http_403")

    assert handler.calls == [
        ("/api/sessions/abc/failure", {"reason": "http_403", "kind": "blocked"})
    ]


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        # Everything the engine reports on its own must carry a kind: without one
        # the pool weighs a captcha exactly like an unexplained failure.
        ("transport", "transport"),
        ("http_403", "blocked"),
        ("challenge", "captcha"),
        ("rate_limited", "rate_limited"),
        # The pool's own vocabulary, passed straight through by a caller.
        ("captcha", "captcha"),
        ("blocked", "blocked"),
        ("other", "other"),
        # Case and padding are the caller's, not a different reason.
        ("  Challenge ", "captcha"),
    ],
)
def test_known_reasons_carry_their_kind(pool_api, reason: str, kind: str):
    api_url, handler = pool_api
    manager = make_manager(api_url, session="abc")

    manager.report_failure(reason)

    assert handler.calls[0][1]["kind"] == kind, f"{reason!r} should be typed as {kind!r}"
    # The reason goes over untouched: it is the pool's audit-log text.
    assert handler.calls[0][1]["reason"] == reason


def test_an_unknown_reason_is_still_reported(pool_api):
    # No kind rather than a guessed one — the pool counts it as "other", which is
    # what an unclassifiable failure is. Refusing to send it would throw away the
    # only signal that sees inside an HTTPS tunnel.
    api_url, handler = pool_api
    manager = make_manager(api_url, session="abc")

    manager.report_failure("the page looked wrong")

    assert handler.calls == [("/api/sessions/abc/failure", {"reason": "the page looked wrong"})]


def test_report_failure_can_be_disabled(pool_api):
    api_url, handler = pool_api
    manager = make_manager(api_url, session="abc", report_failures=False)

    manager.report_failure("http_403")

    assert handler.calls == []


def test_report_failure_survives_an_unreachable_pool():
    # A pool that is down must never break the scrape.
    manager = make_manager("http://127.0.0.1:1")
    manager.report_failure("transport")  # must not raise


def test_rotate_survives_an_unreachable_pool():
    manager = make_manager("http://127.0.0.1:1")
    manager.rotate()  # must not raise


def test_pool_error_response_does_not_rotate_silently(pool_api):
    api_url, handler = pool_api
    handler.status = 503

    manager = make_manager(api_url, session="abc")
    manager.rotate()  # a 503 means no instance was available; nothing to assert
    # beyond not raising, but the manager must stay usable.
    assert manager.get_proxy() is not None


def test_non_pool_entries_are_untouched():
    # The existing Tor and plain-proxy paths must not gain credentials.
    plain = ProxyUrl(url="socks5h://127.0.0.1:9050")
    manager = ProxyManager(ProxyConfig(proxy_urls=[plain]))
    proxies = manager.get_proxy()
    assert proxies is not None
    assert proxies["https"] == "socks5h://127.0.0.1:9050"

    manager.report_failure("http_403")  # a no-op for non-pool entries


def test_tor_proxy_entries_still_work():
    entry = TorProxyUrl(url="socks5h://127.0.0.1:9050", control_port=0)
    manager = ProxyManager(ProxyConfig(proxy_urls=[entry]))
    proxies = manager.get_proxy()
    assert proxies is not None
    assert proxies["https"] == "socks5h://127.0.0.1:9050"


@pytest.mark.parametrize(
    ("url", "username", "token", "expected"),
    [
        ("socks5h://host:9250", "abc", "tp_tok", "socks5h://abc:tp_tok@host:9250"),
        # Explicit credentials win: the operator named their own session, and
        # owns the password that goes with it.
        ("socks5h://mine:pw@host:9250", "abc", "tp_tok", "socks5h://mine:pw@host:9250"),
        # A key needing escaping must not corrupt the authority.
        ("socks5h://host:9250", "a@b/c", "tp_tok", "socks5h://a%40b%2Fc:tp_tok@host:9250"),
        # IPv6 literals must stay bracketed.
        ("socks5h://[::1]:9250", "abc", "tp_tok", "socks5h://abc:tp_tok@[::1]:9250"),
        # No port is still valid.
        ("socks5h://host", "abc", "tp_tok", "socks5h://abc:tp_tok@host"),
        ("socks5h://host:9250", "", "tp_tok", "socks5h://host:9250"),
        # No token: the username still goes on, which is all tor-pool 0.1.x
        # needed. 0.2 and later refuse the handshake.
        ("socks5h://host:9250", "abc", "", "socks5h://abc@host:9250"),
    ],
)
def test_with_credentials(url: str, username: str, token: str, expected: str):
    assert _with_credentials(url, username, token) == expected


def test_pool_calls_carry_the_token(pool_api):
    """Without this the pool 401s and the failure is swallowed as a warning.

    The proxy half of a missing token fails loudly on every request; this half
    fails silently, so the pool stops hearing about captchas and soft blocks
    while both sides look healthy.
    """
    api_url, handler = pool_api
    manager = make_manager(api_url, session="my-session")

    manager.rotate()
    manager.report_failure("http_403")

    assert handler.calls, "no request reached the pool"
    assert handler.auth == [f"Bearer {TOKEN}"] * len(handler.calls)


def test_pool_calls_omit_the_header_without_a_token(pool_api):
    api_url, handler = pool_api
    manager = make_manager(api_url, session="my-session", token="")

    manager.rotate()

    assert handler.auth == [None]


def scrape_status(fast_config, api_url: str, status: int, body: str = "") -> list[str]:
    """Drive a real Scraper through one response and return the reasons reported."""
    import requests
    import responses

    from scraper import Scraper

    fast_config.proxy = ProxyConfig(
        proxy_urls=[
            TorPoolProxyUrl(
                url="socks5h://127.0.0.1:9250",
                api_url=api_url,
                session="my-session",
                token=TOKEN,
            )
        ]
    )
    url = "https://example.com/probe"
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, url, status=status, body=body)
        scraper = Scraper(origin="https://example.com", config=fast_config)
        # The session raises on any error status, which is not what is under test.
        with pytest.raises(requests.HTTPError):
            scraper.get(url)
    return [
        str(body.get("reason", ""))
        for path, body in _PoolHandler.calls
        if path.endswith("/failure")
    ]


def test_a_throttle_is_reported_as_a_rate_limit(pool_api, fast_config):
    """The reason is what the pool weighs the report by.

    Sent as a generic failure a 429 spends a working exit and the next one is
    throttled just the same, so this has to stay distinct from the 403 path.
    """
    api_url, _ = pool_api
    assert scrape_status(fast_config, api_url, 429, "slow down") == ["rate_limited"]


def test_a_block_is_still_reported_as_a_block(pool_api, fast_config):
    # The two must not collapse into one another: a 403 says the exit is
    # unwelcome, a 429 says it is working and busy.
    api_url, _ = pool_api
    assert scrape_status(fast_config, api_url, 403, "nope") == ["http_403"]


def test_an_ordinary_error_is_not_reported(pool_api, fast_config):
    # Only the statuses that say something about the exit. A 404 is the site's
    # answer about a path and has nothing to do with which IP asked.
    api_url, _ = pool_api
    assert scrape_status(fast_config, api_url, 404) == []
