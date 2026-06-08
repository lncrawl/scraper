"""Tests for the curl_cffi transport: send, cookie management, response adaptation,
and _match_impersonate."""

import asyncio
import re
from http.cookiejar import Cookie

import httpx
import pytest

from scraper import ImpersonateConfig
from scraper.engine.state import RequestState

from .conftest import make_fast_config

BASE = "https://example.com"
_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"


# --- response adaptation --------------------------------------------------


def test_adapt_curl_response_builds_httpx_response():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import adapt_curl_response

    class _FakeCookies:
        jar: list = []

    class _FakeResp:
        status_code = 200
        content = b"<h1>hi</h1>"
        url = "https://example.com/"
        reason = "OK"
        encoding = "utf-8"
        headers = {"Content-Type": "text/html"}
        cookies = _FakeCookies()

    out = adapt_curl_response("get", "https://example.com/", {"A": "b"}, _FakeResp())
    assert out.status_code == 200
    assert out.content == b"<h1>hi</h1>"
    assert out.headers["Content-Type"] == "text/html"
    assert out.request.method == "GET"
    assert str(out.request.url) == "https://example.com/"


def _make_cookie(name: str, value: str, domain: str) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=False,
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )


def test_adapt_curl_response_copies_cookies():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import adapt_curl_response

    class _FakeCookies:
        jar = [_make_cookie("session", "xyz", "example.com")]

    class _FakeResp:
        status_code = 200
        content = b""
        url = "https://example.com/"
        reason = "OK"
        encoding = "utf-8"
        headers = {}
        cookies = _FakeCookies()

    out = adapt_curl_response("GET", "https://example.com/", {}, _FakeResp())
    assert out.cookies.get("session") == "xyz"


# --- curl transport helpers -----------------------------------------------


class _FakeCurlCookies:
    def __init__(self):
        self._store = {}
        self.jar = []

    def set(self, name, value, domain="", path="/"):
        self._store[name] = value

    def get(self, name, default=None):
        return self._store.get(name, default)

    def delete(self, name, domain=None):
        self._store.pop(name, None)

    def clear(self):
        self._store.clear()


class _FakeCurlResp:
    def __init__(self, url):
        self.status_code = 200
        self.content = b"hi"
        self.url = url
        self.reason = "OK"
        self.encoding = "utf-8"
        self.headers = {"Content-Type": "text/html"}
        self.cookies = _FakeCurlCookies()


def _curl_transport():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))
    calls = []

    class _FakeSession:
        cookies = _FakeCurlCookies()

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _FakeCurlResp(url)

        def close(self):
            pass

    t._session = _FakeSession()  # type: ignore[assignment]
    return t, calls


def _run_send(t, ctx):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(t.send(ctx))
    finally:
        loop.close()


# --- send -----------------------------------------------------------------


def test_curl_send_default_headers_forwards_only_request_headers():
    t, calls = _curl_transport()
    t.bind_headers({"User-Agent": "synthetic"})
    resp = _run_send(t, RequestState("GET", f"{BASE}/x", kwargs={"headers": {"Origin": BASE}}))
    assert resp.status_code == 200
    sent_headers = calls[0][2]["headers"]
    assert "User-Agent" not in sent_headers
    assert sent_headers["Origin"] == BASE


def test_curl_send_without_default_headers_merges_session():
    t, calls = _curl_transport()
    t.bind_headers({"User-Agent": "synthetic"})
    resp = _run_send(
        t, RequestState("GET", f"{BASE}/x", kwargs={"default_headers": False, "headers": {}})
    )
    assert resp.status_code == 200
    assert calls[0][2]["headers"]["User-Agent"] == "synthetic"


def test_curl_forced_user_agent_overrides_impersonation_default():
    t, calls = _curl_transport()
    t.force_user_agent("Browser/123.0")
    _run_send(t, RequestState("GET", f"{BASE}/x", kwargs={"headers": {"Origin": BASE}}))
    assert calls[0][2]["headers"]["User-Agent"] == "Browser/123.0"


def test_curl_force_user_agent_aligns_impersonate_per_request():
    t, calls = _curl_transport()
    t.force_user_agent(_CHROME_UA)
    _run_send(t, RequestState("GET", f"{BASE}/x", kwargs={"headers": {}}))
    assert str(calls[0][2]["impersonate"]).startswith("chrome")
    assert calls[0][2]["headers"]["User-Agent"] == _CHROME_UA


# --- cookie management ---------------------------------------------------


def test_curl_cookie_roundtrip_and_export():
    t, _ = _curl_transport()
    t.put_cookie("sid", "abc")
    assert t._session.cookies.get("sid") == "abc"
    t.clear_cookie("sid")
    assert t._session.cookies.get("sid") is None
    t.clear_all_cookies()
    t.close()


def test_curl_clear_cookie_swallows_exception(monkeypatch):
    t, _ = _curl_transport()

    def _boom(name, domain=None):
        raise RuntimeError("cookie store exploded")

    monkeypatch.setattr(t._session.cookies, "delete", _boom)
    t.clear_cookie("missing")


def test_curl_close_swallows_exception(monkeypatch):
    t, _ = _curl_transport()

    def _boom():
        raise RuntimeError("session exploded")

    monkeypatch.setattr(t._session, "close", _boom)
    t.close()


def test_curl_export_into_copies_cookies():
    t, _ = _curl_transport()
    t._session.cookies.jar = [_make_cookie("tok", "abc", "example.com")]  # type: ignore[attr-defined]

    jar = httpx.Cookies()
    t.export_into(jar)

    assert jar.get("tok") == "abc"


# --- _match_impersonate ---------------------------------------------------


def test_curl_match_impersonate_picks_closest():
    pytest.importorskip("curl_cffi")
    from curl_cffi.requests.impersonate import BrowserType

    from scraper.engine.transport.curl import CurlCffiTransport

    chrome_vers = sorted(
        int(m.group(1)) for bt in BrowserType if (m := re.fullmatch(r"chrome(\d+)", str(bt.value)))
    )

    target = CurlCffiTransport._match_impersonate(_CHROME_UA)
    assert target and target.startswith("chrome")
    assert int(target.removeprefix("chrome")) <= 131
    newest = CurlCffiTransport._match_impersonate("Chrome/999.0.0.0")
    assert int(str(newest).removeprefix("chrome")) == max(chrome_vers)
    assert CurlCffiTransport._match_impersonate("totally unknown agent") is None


def test_match_impersonate_edge_ua():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    result = CurlCffiTransport._match_impersonate(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
    )
    assert result is None or result.startswith("edge") or result.startswith("chrome")


def test_match_impersonate_firefox_ua():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    result = CurlCffiTransport._match_impersonate(
        "Mozilla/5.0 (Windows NT 10.0; rv:120.0) Gecko/20100101 Firefox/120.0"
    )
    assert result is None or isinstance(result, str)


def test_match_impersonate_no_version_digits_returns_none():
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    assert CurlCffiTransport._match_impersonate("Mozilla/5.0 Edg/") is None


# --- streaming response (_CurlStream + adapt_curl_response_streaming) ------


def test_curl_stream_iter_yields_chunks_and_releases_lock():
    pytest.importorskip("curl_cffi")
    import threading

    from scraper.engine.transport.curl import _CurlStream

    class _FakeResp:
        def iter_content(self):
            yield b"chunk1"
            yield b"chunk2"

        def close(self):
            pass

    lock = threading.Lock()
    lock.acquire()
    stream = _CurlStream(_FakeResp(), lock)
    chunks = list(stream)
    assert chunks == [b"chunk1", b"chunk2"]
    assert lock.acquire(blocking=False)
    lock.release()


def test_curl_stream_close_releases_lock():
    pytest.importorskip("curl_cffi")
    import threading

    from scraper.engine.transport.curl import _CurlStream

    class _FakeResp:
        def iter_content(self):
            yield b"data"

        def close(self):
            pass

    lock = threading.Lock()
    lock.acquire()
    stream = _CurlStream(_FakeResp(), lock)
    stream.close()
    assert lock.acquire(blocking=False)
    lock.release()


def test_adapt_curl_response_streaming_builds_response():
    pytest.importorskip("curl_cffi")
    import threading

    from scraper.engine.transport.curl import adapt_curl_response_streaming

    class _FakeResp:
        status_code = 200
        url = f"{BASE}/"
        reason = "OK"
        encoding = "utf-8"
        headers = {"Content-Type": "text/html"}
        cookies = _FakeCurlCookies()

        def iter_content(self):
            yield b"streamed"

        def close(self):
            pass

    lock = threading.Lock()
    lock.acquire()
    resp = adapt_curl_response_streaming("get", f"{BASE}/", {}, _FakeResp(), lock)
    assert resp.status_code == 200
    assert resp.read() == b"streamed"


def test_curl_send_streaming_mode():
    pytest.importorskip("curl_cffi")
    t, _ = _curl_transport()

    class _FakeStreamResp:
        status_code = 200
        url = f"{BASE}/x"
        reason = "OK"
        encoding = "utf-8"
        headers = {}
        cookies = _FakeCurlCookies()

        def iter_content(self):
            yield b"streamed_body"

        def close(self):
            pass

    class _FakeStreamSession:
        cookies = _FakeCurlCookies()

        def request(self, method, url, **kwargs):
            return _FakeStreamResp()

        def close(self):
            pass

    t._session = _FakeStreamSession()  # type: ignore[assignment]
    ctx = RequestState("GET", f"{BASE}/x", kwargs={"stream": True})
    resp = _run_send(t, ctx)
    assert resp.read() == b"streamed_body"


def test_curl_reset_session_creates_fresh_session():
    pytest.importorskip("curl_cffi")
    t, _ = _curl_transport()
    old_session = t._session
    t.reset_session()
    assert t._session is not old_session


def test_curl_reset_session_swallows_close_exception():
    pytest.importorskip("curl_cffi")
    t, _ = _curl_transport()

    def _boom():
        raise RuntimeError("session exploded during reset")

    t._session.close = _boom  # type: ignore[method-assign]
    t.reset_session()  # must not raise
    assert t._session is not None


def test_curl_stream_close_swallows_resp_close_exception():
    pytest.importorskip("curl_cffi")
    import threading

    from scraper.engine.transport.curl import _CurlStream

    class _FailCloseResp:
        def iter_content(self):
            yield b"data"

        def close(self):
            raise RuntimeError("resp close exploded")

    lock = threading.Lock()
    lock.acquire()
    stream = _CurlStream(_FailCloseResp(), lock)
    stream.close()  # must not raise despite resp.close() failing
    assert lock.acquire(blocking=False)
    lock.release()


def test_curl_send_streaming_raises_proxy_error_on_failure():
    pytest.importorskip("curl_cffi")
    from scraper.exceptions import ProxyTransportError

    t, _ = _curl_transport()

    class _ErrorSession:
        cookies = _FakeCurlCookies()

        def request(self, method, url, **kwargs):
            raise RuntimeError("proxy connection refused")

        def close(self):
            pass

    t._session = _ErrorSession()  # type: ignore[assignment]
    ctx = RequestState("GET", f"{BASE}/x", kwargs={"stream": True})
    with pytest.raises(ProxyTransportError):
        _run_send(t, ctx)


def test_curl_send_streaming_reraises_generic_exception():
    pytest.importorskip("curl_cffi")
    t, _ = _curl_transport()

    class _ErrorSession:
        cookies = _FakeCurlCookies()

        def request(self, method, url, **kwargs):
            raise RuntimeError("unexpected network failure")

        def close(self):
            pass

    t._session = _ErrorSession()  # type: ignore[assignment]
    ctx = RequestState("GET", f"{BASE}/x", kwargs={"stream": True})
    with pytest.raises(RuntimeError, match="unexpected network failure"):
        _run_send(t, ctx)


def test_curl_send_streaming_raises_ssl_error_on_ssl_failure():
    pytest.importorskip("curl_cffi")
    from scraper.exceptions import SSLTransportError

    t, _ = _curl_transport()

    class _ErrorSession:
        cookies = _FakeCurlCookies()

        def request(self, method, url, **kwargs):
            raise RuntimeError("ssl certificate verify failed")

        def close(self):
            pass

    t._session = _ErrorSession()  # type: ignore[assignment]
    ctx = RequestState("GET", f"{BASE}/x", kwargs={"stream": True})
    with pytest.raises(SSLTransportError):
        _run_send(t, ctx)


def test_match_impersonate_no_candidates_returns_none(monkeypatch):
    pytest.importorskip("curl_cffi")
    import scraper.engine.transport.curl as _curl_mod
    from scraper.engine.transport.curl import CurlCffiTransport

    monkeypatch.setattr(_curl_mod, "BrowserType", [])
    assert CurlCffiTransport._match_impersonate("Chrome/131.0.0.0") is None


# --- proxy URL conversion -------------------------------------------------


def test_curl_send_proxy_kwarg_is_consumed():
    """proxy string → popped from kwargs."""
    t, calls = _curl_transport()
    _run_send(
        t,
        RequestState("GET", f"{BASE}/x", kwargs={"proxy": "socks5://127.0.0.1:9150"}),
    )
    sent_kwargs = calls[0][2]
    # "proxy" is popped; request still completes
    assert "proxy" not in sent_kwargs


# --- exception classification ---------------------------------------------


def test_curl_send_proxy_error_raises_proxy_transport_error():
    """'proxy' in exception message → ProxyTransportError."""
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport
    from scraper.exceptions import ProxyTransportError

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))

    class _BadSession:
        cookies = _FakeCurlCookies()

        def request(self, *args, **kwargs):
            raise Exception("proxy tunnel failed")

        def close(self):
            pass

    t._session = _BadSession()  # type: ignore[assignment]
    with pytest.raises(ProxyTransportError, match="proxy tunnel failed"):
        _run_send(t, RequestState("GET", f"{BASE}/x"))


def test_curl_send_407_error_raises_proxy_transport_error():
    """'407' in exception message → ProxyTransportError."""
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport
    from scraper.exceptions import ProxyTransportError

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))

    class _BadSession:
        cookies = _FakeCurlCookies()

        def request(self, *args, **kwargs):
            raise Exception("HTTP 407 Proxy Authentication Required")

        def close(self):
            pass

    t._session = _BadSession()  # type: ignore[assignment]
    with pytest.raises(ProxyTransportError):
        _run_send(t, RequestState("GET", f"{BASE}/x"))


def test_curl_send_ssl_error_raises_ssl_transport_error():
    """'ssl' in exception message → SSLTransportError."""
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport
    from scraper.exceptions import SSLTransportError

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))

    class _BadSession:
        cookies = _FakeCurlCookies()

        def request(self, *args, **kwargs):
            raise Exception("ssl handshake failed")

        def close(self):
            pass

    t._session = _BadSession()  # type: ignore[assignment]
    with pytest.raises(SSLTransportError, match="ssl handshake failed"):
        _run_send(t, RequestState("GET", f"{BASE}/x"))


def test_curl_send_certificate_error_raises_ssl_transport_error():
    """'certificate' in exception message → SSLTransportError."""
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport
    from scraper.exceptions import SSLTransportError

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))

    class _BadSession:
        cookies = _FakeCurlCookies()

        def request(self, *args, **kwargs):
            raise Exception("certificate verify failed")

        def close(self):
            pass

    t._session = _BadSession()  # type: ignore[assignment]
    with pytest.raises(SSLTransportError):
        _run_send(t, RequestState("GET", f"{BASE}/x"))


def test_curl_send_other_exception_propagates():
    """Unclassified exception is re-raised as-is."""
    pytest.importorskip("curl_cffi")
    from scraper.engine.transport.curl import CurlCffiTransport

    t = CurlCffiTransport(make_fast_config(impersonate=ImpersonateConfig(target="chrome")))

    class _BadSession:
        cookies = _FakeCurlCookies()

        def request(self, *args, **kwargs):
            raise RuntimeError("something unexpected happened")

        def close(self):
            pass

    t._session = _BadSession()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="something unexpected"):
        _run_send(t, RequestState("GET", f"{BASE}/x"))


# --- aclose ---------------------------------------------------------------


def test_curl_aclose_calls_close():
    """aclose() is an async wrapper that delegates to close()."""
    t, _ = _curl_transport()
    closed = []
    t._session.close = lambda: closed.append(True)  # type: ignore[method-assign]

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(t.aclose())
    finally:
        loop.close()

    assert closed
