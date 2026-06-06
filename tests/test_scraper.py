"""Tests for the Scraper session, with HTTP mocked via `responses`."""

import io

import pytest
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


def test_reset_clears_state(fast_config):
    s = make(fast_config)
    s.set_cookie("sid", "abc")
    s.set_header("X-Token", "secret")
    s.reset()
    assert len(list(s.cookies)) == 0
    assert "X-Token" not in s.headers


# --- status handling ------------------------------------------------------


def test_raise_for_status_on_error(fast_config):
    import requests

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


# --- downloads ------------------------------------------------------------


def test_get_file_writes_to_disk(fast_config, tmp_path):
    payload = b"binary-content" * 1000
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/file.bin", body=payload)
        s = make(fast_config)
        out = tmp_path / "file.bin"
        s.get_file(f"{BASE}/file.bin", output_file=out)
        assert out.read_bytes() == payload


def test_get_file_aborts(fast_config, tmp_path):
    from scraper import AbortedException

    # The abort signal trips the pre-send check, so the request never fires —
    # disable the "all requests fired" assertion accordingly.
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(rsps.GET, f"{BASE}/file.bin", body=b"x" * 100000)
        s = make(fast_config)
        s.abort()  # signal set before download starts
        with pytest.raises(AbortedException):
            s.get_file(f"{BASE}/file.bin", output_file=tmp_path / "f.bin")


def test_get_image_returns_pil_image(fast_config):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/cover.png", body=buf.getvalue(), content_type="image/png")
        s = make(fast_config)
        img = s.get_image(f"{BASE}/cover.png")
        assert img.size == (8, 8)


def test_get_image_from_data_uri(fast_config):
    import base64

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    s = make(fast_config)
    img = s.get_image(data_uri)  # no network call
    assert img.size == (4, 4)


def test_get_image_retries_on_unidentified(fast_config):
    # First response isn't a valid image → UnidentifiedImageError → retry with
    # an explicit Accept header, second response is a real PNG.
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (5, 5), "green").save(buf, format="PNG")
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/img", body=b"not-an-image")
        rsps.add(rsps.GET, f"{BASE}/img", body=buf.getvalue(), content_type="image/png")
        s = make(fast_config)
        img = s.get_image(f"{BASE}/img")
        assert img.size == (5, 5)
        # the retry sent without explicit image Accept header
        assert "Accept" not in rsps.calls[1].request.headers


def test_get_file_accepts_str_path(fast_config, tmp_path):
    out = tmp_path / "via_str.bin"
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/f.bin", body=b"abc")
        s = make(fast_config)
        s.get_file(f"{BASE}/f.bin", output_file=str(out))  # str, not Path
        assert out.read_bytes() == b"abc"


def test_get_file_aborts_mid_stream(fast_config, tmp_path, monkeypatch):
    from scraper import AbortedException

    s = make(fast_config)

    class _StreamResp:
        def iter_content(self, chunk_size):
            yield b"first"
            s.abort()  # signal set after the first chunk is written
            yield b"second"

        def close(self):
            pass

    # Bypass the network: the throttle pre-send check would otherwise trip first.
    monkeypatch.setattr(s, "get", lambda *a, **k: _StreamResp())
    with pytest.raises(AbortedException):
        s.get_file(f"{BASE}/stream", output_file=tmp_path / "partial.bin")


def test_set_header_decodes_bytes(fast_config):
    s = make(fast_config)
    s.set_header("X-Token", b"bytes-value")
    assert s.headers["X-Token"] == "bytes-value"


# --- make_soup ------------------------------------------------------------


def test_make_soup_from_various(fast_config):
    s = make(fast_config)
    assert s.make_soup("<p>a</p>").select_one("p").text == "a"
    assert s.make_soup(b"<p>b</p>").select_one("p").text == "b"


# --- delegated properties / close -----------------------------------------


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


# --- request: Origin/Referer omitted when no origin configured ------------


def test_request_without_origin_omits_origin_referer_headers():
    from .conftest import make_fast_config

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(config=make_fast_config())  # no origin= kwarg
        s.get(f"{BASE}/x")
        hdrs = rsps.calls[0].request.headers
        assert "Origin" not in hdrs
        assert "Referer" not in hdrs


# --- HTTP verb methods (options / head / put / patch / delete) -----------


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


# --- get_image: invalid URL raises ValueError ----------------------------


def test_get_image_invalid_url_raises(fast_config):
    s = make(fast_config)
    with pytest.raises(ValueError, match="Invalid URL"):
        s.get_image("not-a-url")


# --- get_image: SVG data URI via cairosvg --------------------------------


def test_get_image_svg_data_uri_returns_image(fast_config, monkeypatch):
    cairosvg = pytest.importorskip("cairosvg")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    monkeypatch.setattr(cairosvg, "svg2png", lambda bytestring: png_bytes)

    img = make(fast_config).get_image("data:image/svg+xml,<svg/>")
    assert img is not None


def test_get_image_svg_data_uri_empty_raises(fast_config, monkeypatch):
    cairosvg = pytest.importorskip("cairosvg")
    monkeypatch.setattr(cairosvg, "svg2png", lambda bytestring: None)

    with pytest.raises(RuntimeError, match="SVG"):
        make(fast_config).get_image("data:image/svg+xml,<svg/>")
