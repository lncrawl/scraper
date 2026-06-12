"""Tests for CipherSuiteAdapter TLS behaviour."""

import ssl
from unittest.mock import MagicMock, patch

import pytest
from requests import PreparedRequest

from scraper._engine.tls import CipherSuiteAdapter


def _make_request(url: str = "https://example.com/") -> PreparedRequest:
    r = PreparedRequest()
    r.prepare_url(url, {})
    r.prepare_headers({})
    r.prepare_body(None, None)
    r.prepare_method("GET")
    return r


# ---------------------------------------------------------------------------
# Initial context state
# ---------------------------------------------------------------------------


def test_default_adapter_has_cert_verification_enabled():
    adapter = CipherSuiteAdapter()
    assert adapter.ssl_context.check_hostname is True
    assert adapter.ssl_context.verify_mode == ssl.CERT_REQUIRED


def test_verify_ssl_false_disables_verification_at_build_time():
    adapter = CipherSuiteAdapter(verify_ssl=False)
    assert adapter.ssl_context.check_hostname is False
    assert adapter.ssl_context.verify_mode == ssl.CERT_NONE


# ---------------------------------------------------------------------------
# send() with verify=False — the SSL-retry ValueError fix
# ---------------------------------------------------------------------------


def test_send_verify_false_clears_check_hostname_before_super():
    """verify=False must disable check_hostname on the shared context before
    urllib3's connect() tries to set verify_mode=CERT_NONE, or Python raises
    ValueError: Cannot set verify_mode to CERT_NONE when check_hostname is enabled.
    """
    adapter = CipherSuiteAdapter()
    assert adapter.ssl_context.check_hostname is True  # starts enabled

    captured = {}

    def fake_super_send(request, *args, **kwargs):
        captured["check_hostname"] = adapter.ssl_context.check_hostname
        captured["verify_mode"] = adapter.ssl_context.verify_mode
        return MagicMock(status_code=200)

    with patch("requests.adapters.HTTPAdapter.send", fake_super_send):
        adapter.send(_make_request(), verify=False)

    assert captured["check_hostname"] is False
    assert captured["verify_mode"] == ssl.CERT_NONE


def test_send_verify_true_leaves_context_unchanged():
    adapter = CipherSuiteAdapter()

    captured = {}

    def fake_super_send(request, *args, **kwargs):
        captured["check_hostname"] = adapter.ssl_context.check_hostname
        captured["verify_mode"] = adapter.ssl_context.verify_mode
        return MagicMock(status_code=200)

    with patch("requests.adapters.HTTPAdapter.send", fake_super_send):
        adapter.send(_make_request(), verify=True)

    assert captured["check_hostname"] is True
    assert captured["verify_mode"] == ssl.CERT_REQUIRED


def test_send_verify_false_does_not_raise_value_error():
    """Regression: the retry path must not raise ValueError from ssl.verify_mode."""
    adapter = CipherSuiteAdapter()

    with patch("requests.adapters.HTTPAdapter.send", return_value=MagicMock(status_code=200)):
        # Must not raise ValueError
        adapter.send(_make_request(), verify=False)


def test_send_verify_false_no_ssl_context_is_safe():
    adapter = CipherSuiteAdapter()
    adapter.ssl_context = None  # type: ignore[assignment]

    with patch("requests.adapters.HTTPAdapter.send", return_value=MagicMock(status_code=200)):
        adapter.send(_make_request(), verify=False)  # must not raise


# ---------------------------------------------------------------------------
# CipherRotator
# ---------------------------------------------------------------------------


def test_cipher_rotator_single_suite_returns_none():
    from scraper._engine.tls import CipherRotator

    rotator = CipherRotator(["AES256-SHA"])
    assert rotator.suite_for(0) is None
    assert rotator.suite_for(99) is None


def test_cipher_rotator_empty_returns_none():
    from scraper._engine.tls import CipherRotator

    assert CipherRotator([]).suite_for(0) is None


def test_cipher_rotator_rotates_window():
    from scraper._engine.tls import CipherRotator

    ciphers = [f"CIPHER-{i}" for i in range(10)]
    rotator = CipherRotator(ciphers)
    suite0 = rotator.suite_for(0)
    suite1 = rotator.suite_for(5)
    assert suite0 is not None
    assert suite1 is not None
    assert suite0 != suite1


@pytest.mark.parametrize("rotation", [0, 1, 7, 100])
def test_cipher_rotator_always_returns_string(rotation):
    from scraper._engine.tls import CipherRotator

    ciphers = [f"C{i}" for i in range(5)]
    result = CipherRotator(ciphers).suite_for(rotation)
    assert result is None or isinstance(result, str)
