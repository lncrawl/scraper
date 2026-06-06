"""Tests for the UA dataset cache: fast path, ETag/304, download, and error fallback.

The conftest autouse fixture patches ``cache.load_ua_data`` to return None for every
other test. We capture the *real* function at import time (before any monkeypatching)
and call it directly so these tests exercise the actual implementation.
"""

from __future__ import annotations

import gzip
import json
import sys
import types

import responses as rsp

from scraper.engine.user_agent import cache as cache_mod

# Capture real function before conftest patches the module attribute.
from scraper.engine.user_agent.cache import load_ua_data as _real_load

_FAKE_UA = [{"userAgent": "Mozilla/5.0 Chrome/130.0.0.0", "weight": 1.0}]


def _write_gz(path, data: list) -> None:
    path.write_bytes(gzip.compress(json.dumps(data).encode()))


def _write_gz_bytes(data: list) -> bytes:
    return gzip.compress(json.dumps(data).encode())


# --- is_brotli_available: brotlicffi fallback and neither-installed path ---


def test_is_brotli_available_via_brotlicffi(monkeypatch):
    from scraper.engine.user_agent.cache import is_brotli_available

    is_brotli_available.cache_clear()
    monkeypatch.setitem(sys.modules, "brotli", None)  # None → ImportError on import
    monkeypatch.setitem(sys.modules, "brotlicffi", types.ModuleType("brotlicffi"))
    try:
        assert is_brotli_available() is True
    finally:
        is_brotli_available.cache_clear()


def test_is_brotli_available_neither_installed(monkeypatch):
    from scraper.engine.user_agent.cache import is_brotli_available

    is_brotli_available.cache_clear()
    monkeypatch.setitem(sys.modules, "brotli", None)
    monkeypatch.setitem(sys.modules, "brotlicffi", None)
    try:
        assert is_brotli_available() is False
    finally:
        is_brotli_available.cache_clear()


# --- fast path: fresh cache, no network call needed -----------------------


@rsp.activate
def test_load_ua_data_fast_path_skips_network(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json.gz"
    _write_gz(cache_file, _FAKE_UA)
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)

    result = _real_load()

    assert result is not None and result[0]["userAgent"] == _FAKE_UA[0]["userAgent"]
    assert len(rsp.calls) == 0, "no HTTP call should be made for a fresh cache"


# --- 304 not-modified: server confirms data unchanged --------------------


def test_load_ua_data_304_touches_cache_and_returns_data(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json.gz"
    etag_file = tmp_path / "cache.etag"
    _write_gz(cache_file, _FAKE_UA)
    # Force stale: set TTL to -1 so the condition `age < TTL` is never True.
    monkeypatch.setattr(cache_mod, "_FAST_TTL", -1)
    etag_file.write_text('"v1"')

    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(cache_mod, "_ETAG_PATH", etag_file)

    with rsp.RequestsMock() as rsps:
        rsps.add(rsp.GET, cache_mod._CACHE_URL, status=304)
        result = _real_load()
        assert rsps.calls[0].request.headers.get("If-None-Match") == '"v1"'

    assert result is not None and result[0]["userAgent"] == _FAKE_UA[0]["userAgent"]


# --- fresh download: cache absent + 200 response with new data -----------


def test_load_ua_data_downloads_and_caches_new_data(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json.gz"
    etag_file = tmp_path / "cache.etag"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(cache_mod, "_ETAG_PATH", etag_file)

    with rsp.RequestsMock() as rsps:
        rsps.add(
            rsp.GET,
            cache_mod._CACHE_URL,
            body=gzip.compress(json.dumps(_FAKE_UA).encode()),
            status=200,
            headers={"ETag": '"newetag"'},
        )
        result = _real_load()

    assert result is not None and result[0]["userAgent"] == _FAKE_UA[0]["userAgent"]
    assert cache_file.exists(), "new cache file should be written"
    assert etag_file.read_text() == '"newetag"'


# --- network error: returns stale cache as fallback ----------------------


def test_load_ua_data_falls_back_to_stale_on_network_error(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json.gz"
    _write_gz(cache_file, _FAKE_UA)
    monkeypatch.setattr(cache_mod, "_FAST_TTL", -1)
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(cache_mod, "_ETAG_PATH", tmp_path / "no_etag.txt")

    with rsp.RequestsMock() as rsps:
        rsps.add(rsp.GET, cache_mod._CACHE_URL, body=Exception("timeout"))
        result = _real_load()

    assert result is not None and result[0]["userAgent"] == _FAKE_UA[0]["userAgent"]


# --- 200 response with no ETag header — walrus branch skipped (line 70→72) --


def test_load_ua_data_200_no_etag_skips_etag_write(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json.gz"
    etag_file = tmp_path / "cache.etag"
    monkeypatch.setattr(cache_mod, "_FAST_TTL", -1)
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(cache_mod, "_ETAG_PATH", etag_file)

    with rsp.RequestsMock() as rsps:
        rsps.add(
            rsp.GET,
            cache_mod._CACHE_URL,
            body=_write_gz_bytes(_FAKE_UA),
            status=200,
            # No ETag header → `if etag := resp.headers.get("ETag"):` is falsy → file not written
        )
        result = _real_load()

    assert result is not None and result[0]["userAgent"] == _FAKE_UA[0]["userAgent"]
    assert not etag_file.exists()
