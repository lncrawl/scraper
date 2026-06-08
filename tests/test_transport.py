"""Tests for transport selection/fallback and HttpxTransport behaviour."""

import httpx
import pytest
import respx

from scraper import BrowserConfig, ImpersonateConfig
from scraper.engine.state import RequestState
from scraper.engine.transport import build_transport
from scraper.engine.transport.httpx import HttpxTransport

from .conftest import make_fast_config

BASE = "https://example.com"


# --- selection / fallback -------------------------------------------------


def test_build_transport_no_target_returns_httpx():
    cfg = make_fast_config()  # impersonate target is None
    assert isinstance(build_transport(cfg), HttpxTransport)


def test_build_transport_with_target_returns_curl():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome"))
    assert isinstance(build_transport(cfg), CurlCffiTransport)


def test_build_transport_falls_back_on_curl_init_exception(monkeypatch):
    pytest.importorskip("curl_cffi")
    import scraper.engine.transport.curl as _curl_mod

    class _BrokenTransport:
        def __init__(self, config):
            raise RuntimeError("simulated curl init failure")

    monkeypatch.setattr(_curl_mod, "CurlCffiTransport", _BrokenTransport)
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome"))
    assert isinstance(build_transport(cfg), HttpxTransport)


# --- HttpxTransport behaviour --------------------------------------------


@respx.mock
def test_httpx_transport_send_and_cookie_export():
    import asyncio

    cfg = make_fast_config()
    t = HttpxTransport(cfg)
    respx.get(f"{BASE}/x").mock(
        return_value=httpx.Response(200, headers={"set-cookie": "sid=abc; Path=/"})
    )
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(t.send(ctx))
        assert resp.status_code == 200
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()

    jar = httpx.Cookies()
    t.export_into(jar)
    assert jar.get("sid") == "abc"


def test_httpx_transport_put_and_clear_cookie():
    import asyncio

    t = HttpxTransport(make_fast_config())
    t.put_cookie("a", "1", domain="example.com")
    jar = httpx.Cookies()
    t.export_into(jar)
    assert jar.get("a") == "1"

    t.clear_cookie("a")
    jar2 = httpx.Cookies()
    t.export_into(jar2)
    assert jar2.get("a") is None

    loop = asyncio.new_event_loop()
    loop.run_until_complete(t.aclose())
    loop.close()


def test_httpx_transport_clear_cookie_missing_is_ignored():
    t = HttpxTransport(make_fast_config())
    t.put_cookie("present", "v", domain="example.com")
    t.clear_cookie("absent", domain="example.com")
    t.clear_cookie("present")


def test_httpx_transport_cipher_rotation_evicts_pool():
    t = HttpxTransport(make_fast_config(browser=BrowserConfig(browser="chrome")))
    before = t._cipher_suite
    for _ in range(5):
        t.rotate_ciphers()
    assert t._cipher_suite != before


def test_httpx_transport_bind_headers_shares_mapping():
    t = HttpxTransport(make_fast_config())
    headers = {"User-Agent": "UA/1"}
    t.bind_headers(headers)
    headers["User-Agent"] = "UA/2"
    assert t._session_headers["User-Agent"] == "UA/2"


# --- _build_ssl_ctx (lines 84-91) -----------------------------------------


@respx.mock
def test_httpx_transport_build_ssl_ctx_with_server_hostname():
    """_build_ssl_ctx is invoked when server_hostname is set."""
    import asyncio

    cfg = make_fast_config(server_hostname="example.com")
    t = HttpxTransport(cfg)
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(t.send(ctx))
        assert resp.status_code == 200
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()


# --- proxy client pool (line 106) -----------------------------------------


@respx.mock
def test_httpx_transport_send_with_proxy_creates_proxy_client():
    """When proxy_url is provided, a client with proxy= is added to the pool."""
    import asyncio

    t = HttpxTransport(make_fast_config())
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    ctx = RequestState("GET", f"{BASE}/x", kwargs={"proxy": "http://proxy.example.com:8080"})

    loop = asyncio.new_event_loop()
    try:
        resp = loop.run_until_complete(t.send(ctx))
        assert resp.status_code == 200
        # pool now holds an entry keyed by the proxy URL
        assert any(k[0] is not None for k in t._pool)
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()


# --- forced User-Agent (line 164) -----------------------------------------


@respx.mock
def test_httpx_transport_forced_ua_overrides_session_header():
    """force_user_agent() injects UA on every request."""
    import asyncio

    t = HttpxTransport(make_fast_config())
    t.bind_headers({"User-Agent": "DefaultUA/1.0"})
    t.force_user_agent("ForcedUA/2.0")

    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(t.send(ctx))
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()

    assert route.calls[0].request.headers["user-agent"] == "ForcedUA/2.0"


# --- exception handling (lines 175-180) -----------------------------------


@respx.mock
def test_httpx_transport_proxy_error_raises_proxy_transport_error():
    """httpx.ProxyError → ProxyTransportError."""
    import asyncio

    from scraper.exceptions import ProxyTransportError

    t = HttpxTransport(make_fast_config())
    respx.get(f"{BASE}/x").mock(side_effect=httpx.ProxyError("proxy refused"))
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ProxyTransportError):
            loop.run_until_complete(t.send(ctx))
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()


@respx.mock
def test_httpx_transport_connect_error_non_ssl_raises_proxy_transport_error():
    """httpx.ConnectError without SSL cause → ProxyTransportError."""
    import asyncio

    from scraper.exceptions import ProxyTransportError

    t = HttpxTransport(make_fast_config())
    respx.get(f"{BASE}/x").mock(side_effect=httpx.ConnectError("connection refused"))
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ProxyTransportError):
            loop.run_until_complete(t.send(ctx))
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()


def test_httpx_transport_connect_error_with_ssl_raises_ssl_transport_error():
    """httpx.ConnectError with ssl.SSLError cause → SSLTransportError."""
    import asyncio
    import ssl

    from scraper.exceptions import SSLTransportError

    t = HttpxTransport(make_fast_config())

    ssl_exc = ssl.SSLError("certificate verify failed")
    connect_exc = httpx.ConnectError("ssl error")
    connect_exc.__cause__ = ssl_exc

    class _MockCookies:
        def __init__(self):
            self.jar = []

        def set_cookie(self, cookie):
            pass

        def clear(self):
            pass

    class _SSLClient:
        def __init__(self):
            self.cookies = _MockCookies()

        async def request(self, *args, **kwargs):
            raise connect_exc

        async def aclose(self):
            pass

    # Bypass the pool so our fake client is used
    t._pool[(None, True)] = _SSLClient()  # type: ignore[assignment]

    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(SSLTransportError):
            loop.run_until_complete(t.send(ctx))
    finally:
        loop.run_until_complete(t.aclose())
        loop.close()


# --- aclose with pool (lines 194-195) -------------------------------------


@respx.mock
def test_httpx_transport_aclose_closes_all_pool_clients():
    """aclose() closes every client in the pool."""
    import asyncio

    t = HttpxTransport(make_fast_config())
    respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    ctx = RequestState("GET", f"{BASE}/x")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(t.send(ctx))
        assert t._pool  # at least one client in pool after a send
        loop.run_until_complete(t.aclose())
        assert not t._pool  # pool cleared after aclose
    finally:
        loop.close()


def test_httpx_transport_aclose_swallows_exception():
    """aclose() swallows exceptions from client.aclose() — lines 194-195."""
    import asyncio

    t = HttpxTransport(make_fast_config())

    # Inject a mock client that raises on aclose
    class _MockCookies:
        def __init__(self):
            self.jar = []

        def set_cookie(self, cookie):
            pass

        def clear(self):
            pass

    class _FailingClient:
        def __init__(self):
            self.cookies = _MockCookies()

        async def aclose(self):
            raise RuntimeError("client close failed")

    t._pool["example.com"] = _FailingClient()  # type: ignore

    loop = asyncio.new_event_loop()
    try:
        # Should not raise; exception is caught and swallowed
        loop.run_until_complete(t.aclose())
        assert not t._pool  # pool is still cleared even if aclose fails
    finally:
        loop.close()


# --- _is_ssl_error (lines 201-208) ----------------------------------------


def test_is_ssl_error_direct_ssl_exception():
    import ssl

    from scraper.engine.transport.httpx import _is_ssl_error

    exc = ssl.SSLError("bad cert")
    assert _is_ssl_error(exc) is True


def test_is_ssl_error_chained_ssl_exception():
    import ssl

    from scraper.engine.transport.httpx import _is_ssl_error

    root = ssl.SSLError("verify failed")
    wrapper = ValueError("wrapped")
    wrapper.__cause__ = root
    assert _is_ssl_error(wrapper) is True


def test_is_ssl_error_no_ssl_exception():
    from scraper.engine.transport.httpx import _is_ssl_error

    assert _is_ssl_error(ValueError("just a value error")) is False


def test_is_ssl_error_context_chain():
    import ssl

    from scraper.engine.transport.httpx import _is_ssl_error

    root = ssl.SSLError("ssl error")
    wrapper = RuntimeError("runtime")
    wrapper.__context__ = root
    assert _is_ssl_error(wrapper) is True
