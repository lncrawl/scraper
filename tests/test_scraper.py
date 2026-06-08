"""Tests for the Scraper session: soup, JSON, forms, headers, status, and verbs."""

import httpx
import pytest
import respx

from scraper import PageSoup, Scraper

BASE = "https://example.com"


def make(fast_config, **kwargs) -> Scraper:
    return Scraper(origin=BASE, config=fast_config, **kwargs)


# --- HTML / soup ----------------------------------------------------------


@respx.mock
def test_get_soup_parses_html(fast_config):
    respx.get(f"{BASE}/page").mock(return_value=httpx.Response(200, html="<h1>Title</h1>"))
    s = make(fast_config)
    soup = s.get_soup(f"{BASE}/page")
    assert isinstance(soup, PageSoup)
    assert soup.select_one("h1").text == "Title"


@respx.mock
def test_post_soup_parses_html(fast_config):
    respx.post(f"{BASE}/search").mock(
        return_value=httpx.Response(200, html="<div class='r'>hit</div>")
    )
    s = make(fast_config)
    soup = s.post_soup(f"{BASE}/search", data={"q": "x"})
    assert soup.select_one(".r").text == "hit"


# --- JSON -----------------------------------------------------------------


@respx.mock
def test_get_json(fast_config):
    respx.get(f"{BASE}/api").mock(
        return_value=httpx.Response(200, json={"ok": True, "items": [1, 2]})
    )
    s = make(fast_config)
    data = s.get_json(f"{BASE}/api")
    assert data == {"ok": True, "items": [1, 2]}


@respx.mock
def test_post_json_sends_and_parses(fast_config):
    route = respx.post(f"{BASE}/api").mock(return_value=httpx.Response(200, json={"created": 1}))
    s = make(fast_config)
    out = s.post_json(f"{BASE}/api", json={"title": "x"})
    assert out == {"created": 1}
    assert "application/json" in route.calls[0].request.headers["content-type"]


# --- forms ----------------------------------------------------------------


@respx.mock
def test_submit_form_urlencoded(fast_config):
    route = respx.post(f"{BASE}/login").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    s.submit_form(f"{BASE}/login", data={"u": "a", "p": "b"})
    req = route.calls[0].request
    assert "application/x-www-form-urlencoded" in req.headers["content-type"]
    body = req.content.decode()
    assert "u=a" in body and "p=b" in body


@respx.mock
def test_submit_form_multipart(fast_config):
    route = respx.post(f"{BASE}/up").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    s.submit_form(f"{BASE}/up", data={"f": "v"}, multipart=True)
    assert "multipart/form-data" in route.calls[0].request.headers["content-type"]


# --- header / referer injection ------------------------------------------


@respx.mock
def test_origin_and_referer_injected(fast_config):
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    s.get(f"{BASE}/x")
    headers = route.calls[0].request.headers
    assert headers["origin"] == BASE
    assert str(headers["referer"]).startswith(BASE)


@respx.mock
def test_set_header_and_cookie_are_sent(fast_config):
    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    s.set_header("X-Token", "secret")
    s.set_cookie("sid", "abc")
    s.get(f"{BASE}/x")
    headers = route.calls[0].request.headers
    assert headers["x-token"] == "secret"
    assert "sid=abc" in str(headers.get("cookie", ""))


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


@respx.mock
def test_request_without_origin_omits_origin_referer_headers():
    from .conftest import make_fast_config

    route = respx.get(f"{BASE}/x").mock(return_value=httpx.Response(200))
    s = Scraper(config=make_fast_config())
    s.get(f"{BASE}/x")
    hdrs = route.calls[0].request.headers
    assert "origin" not in hdrs
    assert "referer" not in hdrs


# --- rotate_proxy and context manager ------------------------------------


def test_rotate_proxy_does_not_raise(fast_config):
    s = make(fast_config)
    s.rotate_proxy()


def test_context_manager_returns_scraper_and_closes(fast_config):
    with make(fast_config) as s:
        assert isinstance(s, Scraper)


# --- status handling ------------------------------------------------------


@respx.mock
def test_raise_for_status_on_error(fast_config):
    respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))
    s = make(fast_config)
    with pytest.raises(httpx.HTTPStatusError):
        s.get(f"{BASE}/missing")


@respx.mock
def test_ping_uses_head(fast_config):
    route = respx.head(f"{BASE}/x").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    resp = s.ping(f"{BASE}/x")
    assert resp.status_code == 200
    assert route.calls[0].request.method == "HEAD"


# --- HTTP verb methods ----------------------------------------------------


@respx.mock
def test_http_verb_methods(fast_config):
    respx.options(f"{BASE}/x").mock(return_value=httpx.Response(200))
    respx.head(f"{BASE}/x").mock(return_value=httpx.Response(200))
    respx.put(f"{BASE}/x").mock(return_value=httpx.Response(200))
    respx.patch(f"{BASE}/x").mock(return_value=httpx.Response(200))
    respx.delete(f"{BASE}/x").mock(return_value=httpx.Response(200))
    s = make(fast_config)
    s.options(f"{BASE}/x")
    s.head(f"{BASE}/x")
    s.put(f"{BASE}/x")
    s.patch(f"{BASE}/x")
    s.delete(f"{BASE}/x")


# --- make_soup / delegated properties / close ----------------------------


def test_make_soup_from_various(fast_config):
    s = make(fast_config)
    assert s.make_soup("<p>a</p>").select_one("p").text == "a"
    assert s.make_soup(b"<p>b</p>").select_one("p").text == "b"


def test_config_and_proxy_manager_properties(fast_config):
    s = make(fast_config)
    assert s.config is s.engine.config
    assert s.proxy_manager is s.engine.proxy_manager


def test_close_does_not_raise(fast_config):
    make(fast_config).close()
