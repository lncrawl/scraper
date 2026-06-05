"""Tests for internal utilities (URL helpers, atomic writes, exceptions)."""

import os

import pytest

from scraper import AbortedException, CloudflareException
from scraper.utils import atomic_write, extract_base, extract_host, validate_url

# --- url_tools ------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/path/page?q=1", "https://example.com/"),
        ("http://sub.example.com:8080/x", "http://sub.example.com:8080/"),
        ("1.2.3.4:8080/x", "http://1.2.3.4:8080/"),
    ],
)
def test_extract_base(url, expected):
    assert extract_base(url) == expected


def test_extract_host_strips_www_and_keeps_port():
    assert extract_host("https://www.example.com/x") == "example.com"
    assert extract_host("https://example.com:8443/x") == "example.com:8443"


def test_extract_host_without_scheme_uses_path_fallback():
    # No scheme → urlparse yields no hostname; host/port come from the path.
    assert extract_host("1.2.3.4:8080/x") == "1.2.3.4:8080"  # host:port split
    assert extract_host("example.com") == "example.com"  # bare host, no colon


def test_extract_host_empty_returns_empty():
    assert extract_host("") == ""
    assert extract_host("/just/a/path") == ""


def test_validate_url():
    assert validate_url("https://example.com")
    assert not validate_url("ftp://example.com")
    assert not validate_url("not-a-url")


# --- file_tools.atomic_write ---------------------------------------------


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "sub" / "out.txt"
    with atomic_write(target, mode="w") as f:
        f.write("hello")
    assert target.read_text() == "hello"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.bin"
    with atomic_write(target) as f:
        f.write(b"data")
    siblings = list(tmp_path.iterdir())
    assert siblings == [target]


def test_atomic_write_rolls_back_on_error(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("original")
    with pytest.raises(RuntimeError):
        with atomic_write(target, mode="w") as f:
            f.write("partial")
            raise RuntimeError("boom")
    # original is untouched and no temp files remain
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_swallows_unlink_error(tmp_path, monkeypatch):
    # If cleanup of the temp file fails, the original error still propagates.
    def boom_unlink(_):
        raise OSError("cannot unlink")

    monkeypatch.setattr(os, "unlink", boom_unlink)
    target = tmp_path / "out.txt"
    with pytest.raises(ValueError):
        with atomic_write(target, mode="w") as f:
            f.write("partial")
            raise ValueError("boom")


def test_extract_host_idna_error_is_swallowed():
    # Labels > 63 chars fail IDNA encoding; the UnicodeError is swallowed and
    # the already-normalized (but non-IDNA-encoded) host is returned instead.
    long_label = "a" * 64
    result = extract_host(f"https://{long_label}.example.com/path")
    assert long_label in result


# --- exception hierarchy --------------------------------------------------


def test_aborted_is_a_cloudflare_exception():
    assert issubclass(AbortedException, CloudflareException)
    assert issubclass(CloudflareException, Exception)
