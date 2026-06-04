"""Tests for internal utilities (URL helpers, atomic writes, exceptions)."""

import pytest

from scraper import AbortedException, CloudflareException
from scraper._utils.file_tools import atomic_write
from scraper._utils.url_tools import extract_base, extract_host, validate_url

# --- url_tools ------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/path/page?q=1", "https://example.com/"),
        ("http://sub.example.com:8080/x", "http://sub.example.com:8080/"),
    ],
)
def test_extract_base(url, expected):
    assert extract_base(url) == expected


def test_extract_host_strips_www_and_keeps_port():
    assert extract_host("https://www.example.com/x") == "example.com"
    assert extract_host("https://example.com:8443/x") == "example.com:8443"


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


# --- exception hierarchy --------------------------------------------------


def test_aborted_is_a_cloudflare_exception():
    assert issubclass(AbortedException, CloudflareException)
    assert issubclass(CloudflareException, Exception)
