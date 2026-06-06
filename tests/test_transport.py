"""Tests for the transport layer: selection, fallback, cookies, and adaptation."""

from http.cookiejar import Cookie

import responses
from requests.cookies import RequestsCookieJar

from scraper import BrowserConfig, ImpersonateConfig
from scraper.engine.context import RequestContext
from scraper.engine.transport import build_transport
from scraper.engine.transport.urllib import UrllibTransport as DirectUrllibTransport

from .conftest import make_fast_config

BASE = "https://example.com"


# --- selection / fallback -------------------------------------------------


def test_build_transport_no_target_returns_urllib():
    cfg = make_fast_config()  # impersonate target is None
    assert isinstance(build_transport(cfg), DirectUrllibTransport)


def test_build_transport_with_target_returns_curl():
    curl = __import__("pytest").importorskip("curl_cffi")  # noqa: F841
    from scraper.engine.transport.curl import CurlCffiTransport

    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome"))
    assert isinstance(build_transport(cfg), CurlCffiTransport)


def test_build_transport_falls_back_when_curl_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("simulated missing curl_cffi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome"))
    assert isinstance(build_transport(cfg), DirectUrllibTransport)


# --- UrllibTransport behaviour --------------------------------------------


def test_urllib_transport_send_and_cookie_export():
    cfg = make_fast_config()
    t = DirectUrllibTransport(cfg)
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok", headers={"Set-Cookie": "sid=abc; Path=/"})
        resp = t.send(RequestContext("GET", f"{BASE}/x"))
        assert resp.status_code == 200

    jar = RequestsCookieJar()
    t.export_into(jar)
    assert jar.get("sid") == "abc"


def test_urllib_transport_put_and_clear_cookie():
    t = DirectUrllibTransport(make_fast_config())
    t.put_cookie("a", "1", domain="example.com")
    jar = RequestsCookieJar()
    t.export_into(jar)
    assert jar.get("a") == "1"

    t.clear_cookie("a")
    jar2 = RequestsCookieJar()
    t.export_into(jar2)
    assert jar2.get("a") is None


def test_urllib_transport_clear_cookie_missing_is_ignored():
    """clear_cookie must not raise when the named cookie does not exist."""
    t = DirectUrllibTransport(make_fast_config())
    t.put_cookie("present", "v", domain="example.com")
    t.clear_cookie("absent", domain="example.com")  # cookie never set → KeyError suppressed
    t.clear_cookie("present")  # removes it


def test_urllib_transport_close():
    DirectUrllibTransport(make_fast_config()).close()


def test_urllib_transport_cipher_rotation_remounts():
    # Use the real chrome pool (>window size) so rotation picks a different window.
    t = DirectUrllibTransport(make_fast_config(browser=BrowserConfig(browser="chrome")))
    before = t._cipher_suite
    for _ in range(5):
        t.rotate_ciphers()
    assert t._cipher_suite != before  # rotated to a different window


def test_urllib_transport_bind_headers_shares_mapping():
    from requests.structures import CaseInsensitiveDict

    t = DirectUrllibTransport(make_fast_config())
    headers = CaseInsensitiveDict({"User-Agent": "UA/1"})
    t.bind_headers(headers)
    headers["User-Agent"] = "UA/2"  # later mutation must be visible to the session
    assert t._session.headers["User-Agent"] == "UA/2"


# --- curl_cffi response adaptation ----------------------------------------


def test_adapt_curl_response_builds_requests_response():
    __import__("pytest").importorskip("curl_cffi")
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
    assert out.request.url == "https://example.com/"


# --- curl_cffi transport send (fake curl session, no network) -------------


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
    __import__("pytest").importorskip("curl_cffi")
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


def test_curl_send_default_headers_forwards_only_request_headers():
    from requests.structures import CaseInsensitiveDict

    t, calls = _curl_transport()
    t.bind_headers(CaseInsensitiveDict({"User-Agent": "synthetic"}))
    resp = t.send(RequestContext("GET", f"{BASE}/x", kwargs={"headers": {"Origin": BASE}}))
    assert resp.status_code == 200
    sent_headers = calls[0][2]["headers"]
    # default_headers True → synthetic session UA is NOT merged in
    assert "User-Agent" not in sent_headers
    assert sent_headers["Origin"] == BASE


def test_curl_send_without_default_headers_merges_session():
    from requests.structures import CaseInsensitiveDict

    t, calls = _curl_transport()
    t.bind_headers(CaseInsensitiveDict({"User-Agent": "synthetic"}))
    resp = t.send(
        RequestContext("GET", f"{BASE}/x", kwargs={"default_headers": False, "headers": {}})
    )
    assert resp.status_code == 200
    assert calls[0][2]["headers"]["User-Agent"] == "synthetic"


def test_curl_cookie_roundtrip_and_export():
    t, _ = _curl_transport()
    t.put_cookie("sid", "abc")
    assert t._session.cookies.get("sid") == "abc"
    t.clear_cookie("sid")
    assert t._session.cookies.get("sid") is None
    t.clear_all_cookies()  # no error on empty
    t.close()


def test_curl_clear_cookie_swallows_exception(monkeypatch):
    """clear_cookie must not raise when the underlying delete() throws."""
    t, _ = _curl_transport()

    def _boom(name, domain=None):
        raise RuntimeError("cookie store exploded")

    monkeypatch.setattr(t._session.cookies, "delete", _boom)
    t.clear_cookie("missing")  # must not raise


def test_curl_close_swallows_exception(monkeypatch):
    """close() must not raise when the underlying session.close() throws."""
    t, _ = _curl_transport()

    def _boom():
        raise RuntimeError("session exploded")

    monkeypatch.setattr(t._session, "close", _boom)
    t.close()  # must not raise


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
    """adapt_curl_response must copy cookies from the curl response into the result."""
    __import__("pytest").importorskip("curl_cffi")
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


def test_curl_export_into_copies_cookies():
    """export_into must populate the target jar with the session's cookies."""
    from requests.cookies import RequestsCookieJar

    t, _ = _curl_transport()

    t._session.cookies.jar = [_make_cookie("tok", "abc", "example.com")]  # type: ignore[attr-defined]

    jar = RequestsCookieJar()
    jar.set("old", "value", domain="example.com")
    t.export_into(jar)

    assert jar.get("tok") == "abc"
    assert jar.get("old") is None  # jar.clear() was called first
