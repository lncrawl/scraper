"""Tests for Scraper download helpers: get_file, get_image."""

import io

import pytest
import responses

from scraper import Scraper

BASE = "https://example.com"


def make(fast_config, **kwargs) -> Scraper:
    return Scraper(origin=BASE, config=fast_config, **kwargs)


# --- image downloads ------------------------------------------------------


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
    img = s.get_image(data_uri)
    assert img.size == (4, 4)


def test_get_image_retries_on_unidentified(fast_config):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (5, 5), "green").save(buf, format="PNG")
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/img", body=b"not-an-image")
        rsps.add(rsps.GET, f"{BASE}/img", body=buf.getvalue(), content_type="image/png")
        s = make(fast_config)
        img = s.get_image(f"{BASE}/img")
        assert img.size == (5, 5)
        assert "Accept" not in rsps.calls[1].request.headers


def test_get_image_invalid_url_raises(fast_config):
    s = make(fast_config)
    with pytest.raises(ValueError, match="Invalid URL"):
        s.get_image("not-a-url")


def test_get_image_svg_data_uri_raises(fast_config, monkeypatch):
    with pytest.raises(NotImplementedError, match="SVG"):
        make(fast_config).get_image("data:image/svg+xml,<svg/>")


# --- file downloads -------------------------------------------------------


def test_get_file_writes_to_disk(fast_config, tmp_path):
    payload = b"binary-content" * 1000
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/file.bin", body=payload)
        s = make(fast_config)
        out = tmp_path / "file.bin"
        s.get_file(f"{BASE}/file.bin", output_file=out)
        assert out.read_bytes() == payload


def test_get_file_accepts_str_path(fast_config, tmp_path):
    out = tmp_path / "via_str.bin"
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/f.bin", body=b"abc")
        s = make(fast_config)
        s.get_file(f"{BASE}/f.bin", output_file=str(out))
        assert out.read_bytes() == b"abc"


def test_get_file_aborts(fast_config, tmp_path):
    from scraper import AbortedException

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(rsps.GET, f"{BASE}/file.bin", body=b"x" * 100000)
        s = make(fast_config)
        s.abort()
        with pytest.raises(AbortedException):
            s.get_file(f"{BASE}/file.bin", output_file=tmp_path / "f.bin")


def test_get_file_aborts_mid_stream(fast_config, tmp_path, monkeypatch):
    from scraper import AbortedException

    s = make(fast_config)

    class _StreamResp:
        def iter_content(self, chunk_size):
            yield b"first"
            s.abort()
            yield b"second"

        def close(self):
            pass

    monkeypatch.setattr(s, "get", lambda *a, **k: _StreamResp())
    with pytest.raises(AbortedException):
        s.get_file(f"{BASE}/stream", output_file=tmp_path / "partial.bin")
