"""Tests for ClearanceStore: in-memory and disk persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path

from scraper.challenges.clearance import ClearanceResult
from scraper.challenges.clearance_store import ClearanceStore


def _result(
    expires: float = 0.0,
    proxy_key: str = "direct",
    cf_bm_expires: float = 0.0,
) -> ClearanceResult:
    return ClearanceResult(
        cookies={"cf_clearance": "TOK"},
        user_agent="UA/1.0",
        expires=expires,
        cf_bm_expires=cf_bm_expires,
        proxy_key=proxy_key,
    )


# --- in-memory store -------------------------------------------------------


def test_save_and_get_returns_result():
    store = ClearanceStore()
    r = _result()
    store.save("example.com", r)
    assert store.get("example.com", "direct") is r


def test_get_miss_returns_none():
    store = ClearanceStore()
    assert store.get("example.com", "direct") is None


def test_get_expired_evicts_and_returns_none():
    store = ClearanceStore()
    r = _result(expires=time.time() - 1)
    store.save("example.com", r)
    assert store.get("example.com", "direct") is None


def test_get_with_refresh_buffer_treats_near_expiry_as_expired():
    store = ClearanceStore()
    r = _result(expires=time.time() + 100)
    store.save("example.com", r)
    # 200s buffer pushes expiry into the past
    assert store.get("example.com", "direct", refresh_buffer=200) is None


def test_get_not_expired_returns_result():
    store = ClearanceStore()
    r = _result(expires=time.time() + 3600)
    store.save("example.com", r)
    assert store.get("example.com", "direct") is r


def test_get_zero_expires_never_evicted():
    store = ClearanceStore()
    r = _result(expires=0.0)
    store.save("example.com", r)
    assert store.get("example.com", "direct") is r


def test_needs_refresh_true_when_absent():
    store = ClearanceStore()
    assert store.needs_refresh("example.com", "direct") is True


def test_needs_refresh_false_when_present():
    store = ClearanceStore()
    r = _result(expires=time.time() + 3600)
    store.save("example.com", r)
    assert store.needs_refresh("example.com", "direct") is False


def test_needs_refresh_true_when_near_expiry():
    store = ClearanceStore()
    r = _result(expires=time.time() + 100)
    store.save("example.com", r)
    assert store.needs_refresh("example.com", "direct", refresh_buffer=200) is True


def test_invalidate_removes_from_memory():
    store = ClearanceStore()
    store.save("example.com", _result())
    store.invalidate("example.com", "direct")
    assert store.get("example.com", "direct") is None


def test_invalidate_nonexistent_is_noop():
    store = ClearanceStore()
    store.invalidate("example.com", "direct")  # should not raise


def test_different_proxy_keys_stored_separately():
    store = ClearanceStore()
    r1 = _result(proxy_key="direct")
    r2 = _result(proxy_key="socks5://p:9150")
    store.save("example.com", r1)
    store.save("example.com", r2)
    assert store.get("example.com", "direct") is r1
    assert store.get("example.com", "socks5://p:9150") is r2


def test_different_domains_stored_separately():
    store = ClearanceStore()
    r1 = _result()
    r2 = _result()
    store.save("example.com", r1)
    store.save("other.com", r2)
    assert store.get("example.com", "direct") is r1
    assert store.get("other.com", "direct") is r2


# --- disk persistence -------------------------------------------------------


def test_disk_save_creates_json_file(tmp_path):
    store = ClearanceStore(cache_dir=tmp_path)
    store.save("example.com", _result(expires=time.time() + 3600))
    files = list(tmp_path.glob("clearance_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["domain"] == "example.com"
    assert data["cookies"]["cf_clearance"] == "TOK"
    assert data["user_agent"] == "UA/1.0"
    assert data["proxy_key"] == "direct"


def test_disk_load_restores_store(tmp_path):
    r = _result(expires=time.time() + 3600, cf_bm_expires=time.time() + 1800)
    store1 = ClearanceStore(cache_dir=tmp_path)
    store1.save("example.com", r)

    store2 = ClearanceStore(cache_dir=tmp_path)
    loaded = store2.get("example.com", "direct")
    assert loaded is not None
    assert loaded.cookies["cf_clearance"] == "TOK"
    assert loaded.user_agent == "UA/1.0"


def test_disk_load_skips_expired_and_unlinks_file(tmp_path):
    r = _result(expires=time.time() - 1)
    store1 = ClearanceStore(cache_dir=tmp_path)
    store1.save("example.com", r)

    store2 = ClearanceStore(cache_dir=tmp_path)
    assert store2.get("example.com", "direct") is None
    assert list(tmp_path.glob("clearance_*.json")) == []


def test_disk_load_skips_nonexistent_dir(tmp_path):
    missing = tmp_path / "does_not_exist"
    store = ClearanceStore(cache_dir=missing)
    assert store.get("example.com", "direct") is None


def test_disk_load_skips_corrupt_json_file(tmp_path):
    (tmp_path / "clearance_deadbeef1234.json").write_text("not json", encoding="utf-8")
    store = ClearanceStore(cache_dir=tmp_path)
    assert store.get("example.com", "direct") is None


def test_disk_load_skips_file_missing_required_key(tmp_path):
    bad = {"domain": "example.com"}  # missing "cookies"
    (tmp_path / "clearance_deadbeef1234.json").write_text(json.dumps(bad), encoding="utf-8")
    store = ClearanceStore(cache_dir=tmp_path)
    assert store.get("example.com", "direct") is None


def test_disk_evict_deletes_file(tmp_path):
    store = ClearanceStore(cache_dir=tmp_path)
    store.save("example.com", _result(expires=time.time() + 3600))
    assert len(list(tmp_path.glob("clearance_*.json"))) == 1

    store.invalidate("example.com", "direct")
    assert list(tmp_path.glob("clearance_*.json")) == []


def test_disk_evict_missing_file_is_silently_ignored(tmp_path):
    store = ClearanceStore(cache_dir=tmp_path)
    store.save("example.com", _result(expires=time.time() + 3600))
    for f in tmp_path.glob("clearance_*.json"):
        f.unlink()
    store.invalidate("example.com", "direct")  # should not raise


def test_disk_evict_oserror_is_swallowed(tmp_path, monkeypatch):
    store = ClearanceStore(cache_dir=tmp_path)
    store.save("example.com", _result(expires=time.time() + 3600))

    def _boom(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom)
    store.invalidate("example.com", "direct")  # should not raise


def test_disk_write_oserror_is_swallowed(tmp_path, monkeypatch):
    store = ClearanceStore(cache_dir=tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    store.save("example.com", _result(expires=time.time() + 3600))  # should not raise


def test_no_disk_init_does_not_load_from_disk():
    store = ClearanceStore()  # no cache_dir
    assert store._cache_dir is None
    assert store.get("example.com", "direct") is None
