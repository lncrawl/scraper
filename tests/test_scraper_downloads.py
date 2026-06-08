"""Tests for Scraper download helpers: get_file, get_image."""

import io

import httpx
import pytest
import respx

from scraper import Scraper

BASE = "https://example.com"


def make(fast_config, **kwargs) -> Scraper:
    return Scraper(origin=BASE, config=fast_config, **kwargs)


# --- image downloads ------------------------------------------------------


@respx.mock
def test_get_image_returns_pil_image(fast_config):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, format="PNG")
    respx.get(f"{BASE}/cover.png").mock(return_value=httpx.Response(200, content=buf.getvalue()))
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


@respx.mock
def test_get_image_retries_on_unidentified(fast_config):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (5, 5), "green").save(buf, format="PNG")
    route = respx.get(f"{BASE}/img")
    route.side_effect = [
        httpx.Response(200, content=b"not-an-image"),
        httpx.Response(200, content=buf.getvalue()),
    ]
    s = make(fast_config)
    img = s.get_image(f"{BASE}/img")
    assert img.size == (5, 5)
    # Two requests were made (original + retry after UnidentifiedImageError)
    assert len(route.calls) == 2


def test_get_image_invalid_url_raises(fast_config):
    s = make(fast_config)
    with pytest.raises(ValueError, match="Invalid URL"):
        s.get_image("not-a-url")


def test_get_image_svg_data_uri_raises(fast_config):
    with pytest.raises(NotImplementedError, match="SVG"):
        make(fast_config).get_image("data:image/svg+xml,<svg/>")


# --- file downloads -------------------------------------------------------


@respx.mock
def test_get_file_writes_to_disk(fast_config, tmp_path):
    payload = b"binary-content" * 1000
    respx.get(f"{BASE}/file.bin").mock(return_value=httpx.Response(200, content=payload))
    s = make(fast_config)
    out = tmp_path / "file.bin"
    s.get_file(f"{BASE}/file.bin", output_file=out)
    assert out.read_bytes() == payload


@respx.mock
def test_get_file_accepts_str_path(fast_config, tmp_path):
    out = tmp_path / "via_str.bin"
    respx.get(f"{BASE}/f.bin").mock(return_value=httpx.Response(200, content=b"abc"))
    s = make(fast_config)
    s.get_file(f"{BASE}/f.bin", output_file=str(out))
    assert out.read_bytes() == b"abc"


@respx.mock
def test_get_file_aborts(fast_config, tmp_path):
    from scraper import AbortedException

    s = make(fast_config)
    s.engine.abort()
    with pytest.raises(AbortedException):
        s.get_file(f"{BASE}/file.bin", output_file=tmp_path / "f.bin")


def test_get_file_aborts_mid_stream(fast_config, tmp_path, monkeypatch):
    from scraper import AbortedException

    s = make(fast_config)

    class _StreamResp:
        def iter_bytes(self, chunk_size):
            yield b"first"
            s.engine.abort()
            yield b"second"

        def close(self):
            pass

    monkeypatch.setattr(s, "get", lambda *a, **k: _StreamResp())
    with pytest.raises(AbortedException):
        s.get_file(f"{BASE}/stream", output_file=tmp_path / "partial.bin")
