"""Tests for the curl_cffi transport: send, cookie management, response adaptation,
and _match_impersonate."""

import re
from http.cookiejar import Cookie

import pytest
from requests.cookies import RequestsCookieJar

from scraper import ImpersonateConfig
from scraper.engine.context import RequestContext

from .conftest import make_fast_config

BASE = "https://example.com"
_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36"


# --- response adaptation --------------------------------------------------


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


# --- send -----------------------------------------------------------------


def test_curl_send_default_headers_forwards_only_request_headers():
    from requests.structures import CaseInsensitiveDict

    t, calls = _curl_transport()
    t.bind_headers(CaseInsensitiveDict({"User-Agent": "synthetic"}))
    resp = t.send(RequestContext("GET", f"{BASE}/x", kwargs={"headers": {"Origin": BASE}}))
    assert resp.status_code == 200
    sent_headers = calls[0][2]["headers"]
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


def test_curl_forced_user_agent_overrides_impersonation_default():
    t, calls = _curl_transport()
    t.force_user_agent("Browser/123.0")
    t.send(RequestContext("GET", f"{BASE}/x", kwargs={"headers": {"Origin": BASE}}))
    assert calls[0][2]["headers"]["User-Agent"] == "Browser/123.0"


def test_curl_force_user_agent_aligns_impersonate_per_request():
    t, calls = _curl_transport()
    t.force_user_agent(_CHROME_UA)
    t.send(RequestContext("GET", f"{BASE}/x", kwargs={"headers": {}}))
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

    jar = RequestsCookieJar()
    jar.set("old", "value", domain="example.com")
    t.export_into(jar)

    assert jar.get("tok") == "abc"
    assert jar.get("old") is None


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


def test_match_impersonate_no_candidates_returns_none(monkeypatch):
    pytest.importorskip("curl_cffi")
    import curl_cffi.requests.impersonate as _imp

    from scraper.engine.transport.curl import CurlCffiTransport

    monkeypatch.setattr(_imp, "BrowserType", [])
    assert CurlCffiTransport._match_impersonate("Chrome/131.0.0.0") is None
