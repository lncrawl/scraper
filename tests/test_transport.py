"""Tests for transport selection/fallback and UrllibTransport behaviour."""

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
    t = DirectUrllibTransport(make_fast_config())
    t.put_cookie("present", "v", domain="example.com")
    t.clear_cookie("absent", domain="example.com")
    t.clear_cookie("present")


def test_urllib_transport_close():
    DirectUrllibTransport(make_fast_config()).close()


def test_urllib_transport_cipher_rotation_remounts():
    t = DirectUrllibTransport(make_fast_config(browser=BrowserConfig(browser="chrome")))
    before = t._cipher_suite
    for _ in range(5):
        t.rotate_ciphers()
    assert t._cipher_suite != before


def test_urllib_transport_bind_headers_shares_mapping():
    from requests.structures import CaseInsensitiveDict

    t = DirectUrllibTransport(make_fast_config())
    headers = CaseInsensitiveDict({"User-Agent": "UA/1"})
    t.bind_headers(headers)
    headers["User-Agent"] = "UA/2"
    assert t._session.headers["User-Agent"] == "UA/2"
