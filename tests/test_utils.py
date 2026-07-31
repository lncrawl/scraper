"""Tests for internal utilities (URL helpers, atomic writes, exceptions)."""

import os
import threading

import pytest

from scraper import Aborted, Blocked, Exhausted, Impassable, ScraperError
from scraper.layers import Layer
from scraper.utils.file_tools import atomic_write
from scraper.utils.signals import AnySignal, combine
from scraper.utils.url_tools import extract_base, extract_host, validate_url

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


def test_validate_url_is_stricter_than_the_other_two():
    # The three do not agree, and the difference is the point. extract_base and
    # extract_host repair what a person typed; validate_url answers whether the string
    # as given names a scheme this library speaks. I described this backwards in a
    # docstring once, which is why it is pinned here rather than only written down.
    assert validate_url("https://example.com/a")
    assert not validate_url("example.com/a")
    assert extract_base("example.com/a") == "http://example.com/"
    assert not validate_url("ftp://example.com/a"), "a scheme we do not speak"
    assert not validate_url("")


def test_validate_url_accepts_a_caller_widening_the_schemes():
    assert validate_url("ftp://example.com/a", ["ftp"])
    assert validate_url("ftp://example.com/a", ("http", "https", "ftp"))


def test_extract_host_empty_returns_empty():
    assert extract_host("") == ""
    assert extract_host("/just/a/path") == ""


def test_extract_host_idna_fallback_on_invalid_label():
    # A label that is valid unicode but fails IDNA encoding (e.g. too long or
    # contains characters that break the IDNA codec) should still return the
    # normalised host rather than raising.
    long_label = "a" * 64  # IDNA labels must be ≤63 chars → UnicodeError
    host = extract_host(f"https://{long_label}.com/")
    assert long_label in host


def test_extract_host_survives_a_colon_that_is_not_a_port():
    """A share button is enough to take out a page's whole link frontier.

    `urlparse(...).port` raises rather than returning None when the netloc's ":" is
    followed by something that is not a number, and `safe_links` calls this on every
    resolved href. One `whatsapp:send?text=…` anchor used to abort extraction for the
    entire page.
    """
    assert extract_host("whatsapp:send?text=hi") == "whatsapp"
    assert extract_host("data:text/html,x") == "data"
    # A real host with an unparseable port keeps the host; only the port is dropped.
    assert extract_host("https://example.com:notaport/x") == "example.com"


def test_an_unusual_scheme_does_not_cost_the_pages_real_links():
    from scraper.links import safe_links

    html = '<a href="/chapters/1">Ch 1</a><a href="whatsapp:send?text=hi">Share</a>'
    found = safe_links(html, "https://example.com/novel/")
    assert [link.url for link in found] == ["https://example.com/chapters/1"]


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


# --- exception hierarchy --------------------------------------------------


def test_everything_shares_one_root():
    for kind in (Aborted, Blocked, Exhausted, Impassable):
        assert issubclass(kind, ScraperError)


def test_a_block_names_the_layer_and_the_url():
    error = Blocked(Layer.BEHAVIOURAL, "rate limited", "https://example.com/x")
    assert error.layer is Layer.BEHAVIOURAL
    assert "L8" in str(error)
    assert "https://example.com/x" in str(error)
    assert error.layer_info is not None
    assert error.layer_info.trait.value == "possess"


def test_a_block_with_nothing_to_attribute_says_so():
    """Found live: a rejected proxy credential used to be reported as layer 15.

    "L15 Operator edge code" is indistinguishable from a Cloudflare Worker refusing
    the request, so the message pointed at the site when the fault was a token.
    """
    error = Blocked(None, "the proxy rejected the credential", "https://example.com/x")
    assert error.layer is None
    assert error.layer_info is None
    assert "no detection layer" in str(error)
    assert "L15" not in str(error)


def test_an_impassable_failure_always_carries_the_legitimate_route():
    # The only actionable half of the message. A diagnosis that supplied its own
    # detail must not suppress it.
    error = Impassable(Layer.ACCESS, "authentication required (HTTP 401)")
    assert "account" in str(error)
    assert "401" in str(error)


# --- signals --------------------------------------------------------------


def test_combining_one_signal_hands_it_back_unchanged():
    # The common case is no per-request signal, and it should cost nothing.
    event = threading.Event()
    assert combine(event) is event
    assert combine(None, event) is event


def test_a_combination_is_set_when_either_half_is():
    shared, mine = threading.Event(), threading.Event()
    both = combine(shared, mine)
    assert not both.is_set()
    mine.set()
    assert both.is_set()
    mine.clear()
    shared.set()
    assert both.is_set()


def test_a_combination_of_nothing_is_never_set():
    assert not AnySignal().is_set()
