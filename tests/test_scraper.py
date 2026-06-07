"""Tests for the Scraper session: soup, JSON, forms, headers, status, and verbs."""

import pytest
import requests
import responses

from scraper import PageSoup, Scraper

BASE = "https://example.com"


def make(fast_config, **kwargs) -> Scraper:
    return Scraper(origin=BASE, config=fast_config, **kwargs)


# --- HTML / soup ----------------------------------------------------------


def test_get_soup_parses_html(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/page", body="<h1>Title</h1>", content_type="text/html")
        s = make(fast_config)
        soup = s.get_soup(f"{BASE}/page")
        assert isinstance(soup, PageSoup)
        assert soup.select_one("h1").text == "Title"


def test_post_soup_parses_html(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.POST, f"{BASE}/search", body="<div class='r'>hit</div>")
        s = make(fast_config)
        soup = s.post_soup(f"{BASE}/search", data={"q": "x"})
        assert soup.select_one(".r").text == "hit"


# --- JSON -----------------------------------------------------------------


def test_get_json(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/api", json={"ok": True, "items": [1, 2]})
        s = make(fast_config)
        data = s.get_json(f"{BASE}/api")
        assert data == {"ok": True, "items": [1, 2]}


def test_post_json_sends_and_parses(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.POST, f"{BASE}/api", json={"created": 1})
        s = make(fast_config)
        out = s.post_json(f"{BASE}/api", json={"title": "x"})
        assert out == {"created": 1}
        assert rsps.calls[0].request.headers["Content-Type"] == "application/json"


# --- forms ----------------------------------------------------------------


def test_submit_form_urlencoded(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.POST, f"{BASE}/login", body="ok")
        s = make(fast_config)
        s.submit_form(f"{BASE}/login", data={"u": "a", "p": "b"})
        req = rsps.calls[0].request
        assert "application/x-www-form-urlencoded" in str(req.headers["Content-Type"])
        body = str(req.body)
        assert "u=a" in body and "p=b" in body


def test_submit_form_multipart(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.POST, f"{BASE}/up", body="ok")
        s = make(fast_config)
        s.submit_form(f"{BASE}/up", data={"f": "v"}, multipart=True)
        assert "multipart/form-data" in str(rsps.calls[0].request.headers["Content-Type"])


# --- header / referer injection ------------------------------------------


def test_origin_and_referer_injected(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = make(fast_config)
        s.get(f"{BASE}/x")
        headers = rsps.calls[0].request.headers
        assert headers["Origin"] == BASE
        assert str(headers["Referer"]).startswith(BASE)


def test_set_header_and_cookie_are_sent(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = make(fast_config)
        s.set_header("X-Token", "secret")
        s.set_cookie("sid", "abc")
        s.get(f"{BASE}/x")
        headers = rsps.calls[0].request.headers
        assert headers["X-Token"] == "secret"
        assert "sid=abc" in str(headers["Cookie"])


def test_set_header_decodes_bytes(fast_config):
    s = make(fast_config)
    s.set_header("X-Token", b"bytes-value")
    assert s.headers["X-Token"] == "bytes-value"


def test_reset_clears_state(fast_config):
    s = make(fast_config)
    s.set_cookie("sid", "abc")
    s.set_header("X-Token", "secret")
    s.reset()
    assert len(list(s.cookies)) == 0
    assert "X-Token" not in s.headers


def test_request_without_origin_omits_origin_referer_headers():
    from .conftest import make_fast_config

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(config=make_fast_config())
        s.get(f"{BASE}/x")
        hdrs = rsps.calls[0].request.headers
        assert "Origin" not in hdrs
        assert "Referer" not in hdrs


# --- status handling ------------------------------------------------------


def test_raise_for_status_on_error(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/missing", status=404)
        s = make(fast_config)
        with pytest.raises(requests.HTTPError):
            s.get(f"{BASE}/missing")


def test_ping_uses_head(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.HEAD, f"{BASE}/x", status=200)
        s = make(fast_config)
        resp = s.ping(f"{BASE}/x")
        assert resp.status_code == 200
        assert rsps.calls[0].request.method == "HEAD"


# --- HTTP verb methods ----------------------------------------------------


def test_http_verb_methods(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.OPTIONS, f"{BASE}/x", body=b"ok")
        rsps.add(rsps.HEAD, f"{BASE}/x", body=b"")
        rsps.add(rsps.PUT, f"{BASE}/x", body=b"ok")
        rsps.add(rsps.PATCH, f"{BASE}/x", body=b"ok")
        rsps.add(rsps.DELETE, f"{BASE}/x", body=b"ok")
        s = make(fast_config)
        s.options(f"{BASE}/x")
        s.head(f"{BASE}/x")
        s.put(f"{BASE}/x")
        s.patch(f"{BASE}/x")
        s.delete(f"{BASE}/x")
        assert len(rsps.calls) == 5


# --- make_soup / delegated properties / close ----------------------------


def test_make_soup_from_various(fast_config):
    s = make(fast_config)
    assert s.make_soup("<p>a</p>").select_one("p").text == "a"
    assert s.make_soup(b"<p>b</p>").select_one("p").text == "b"


def test_config_and_proxy_manager_properties(fast_config):
    s = make(fast_config)
    assert s.config is s.engine.config
    assert s.proxy_manager is s.engine.proxy_manager


def test_signal_setter(fast_config):
    from scraper.utils import EventLock

    s = make(fast_config)
    new_signal = EventLock()
    s.signal = new_signal
    assert s.engine.signal is new_signal


def test_close_does_not_raise(fast_config):
    make(fast_config).close()
