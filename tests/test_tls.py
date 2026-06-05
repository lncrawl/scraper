"""Tests for CipherRotator and CipherSuiteAdapter."""

import ssl

from requests.adapters import HTTPAdapter

from scraper.engine.tls import CipherRotator, CipherSuiteAdapter

# --- CipherRotator -------------------------------------------------------


def test_suite_for_empty_pool_returns_none():
    assert CipherRotator([]).suite_for(1) is None


def test_suite_for_single_cipher_returns_none():
    assert CipherRotator(["AES128-SHA"]).suite_for(1) is None


def test_suite_for_two_ciphers_returns_string():
    c = CipherRotator(["A", "B"])
    suite = c.suite_for(1)
    assert suite is not None
    assert ":" in suite or suite in ("A", "B")


def test_suite_for_large_pool_varies():
    pool = [f"CIPHER-{i}" for i in range(20)]
    c = CipherRotator(pool)
    suites = {c.suite_for(i) for i in range(30)}
    assert len(suites) > 1  # rotating window produces different orderings


def test_suite_for_returns_subset_of_pool():
    pool = [f"C{i}" for i in range(10)]
    c = CipherRotator(pool)
    suite = c.suite_for(3)
    assert suite is not None
    for cipher in suite.split(":"):
        assert cipher in pool


# --- CipherSuiteAdapter --------------------------------------------------


def test_source_address_string_converted_to_tuple():
    a = CipherSuiteAdapter(source_address="127.0.0.1")
    assert a.source_address == ("127.0.0.1", 0)


def test_source_address_tuple_preserved():
    a = CipherSuiteAdapter(source_address=("192.168.1.1", 8080))
    assert a.source_address == ("192.168.1.1", 8080)


def test_source_address_none_preserved():
    a = CipherSuiteAdapter()
    assert a.source_address is None


def test_custom_ssl_context_not_overridden():
    ctx = ssl.create_default_context()
    a = CipherSuiteAdapter(ssl_context=ctx)
    assert a.ssl_context is ctx


def test_server_hostname_set_on_ssl_context():
    a = CipherSuiteAdapter(server_hostname="example.com")
    assert getattr(a.ssl_context, "server_hostname", None) == "example.com"


def test_verify_ssl_false_disables_check_hostname():
    a = CipherSuiteAdapter(verify_ssl=False)
    assert a.ssl_context.check_hostname is False
    assert a.ssl_context.verify_mode == ssl.CERT_NONE


def test_cipher_suite_none_skips_set_ciphers():
    # Should not raise even with no cipher_suite provided
    a = CipherSuiteAdapter(cipher_suite=None)
    assert a.ssl_context is not None


def test_wrap_socket_with_server_hostname(monkeypatch):
    a = CipherSuiteAdapter(server_hostname="example.com")
    captured = {}

    def fake_wrap(*args, **kwargs):
        captured.update(kwargs)
        return object()

    a.ssl_context.orig_wrap_socket = fake_wrap  # type: ignore[attr-defined]
    a._wrap_socket()
    assert captured.get("server_hostname") == "example.com"


def test_wrap_socket_without_server_hostname(monkeypatch):
    a = CipherSuiteAdapter(verify_ssl=True)

    def fake_wrap(*args, **kwargs):
        return object()

    a.ssl_context.orig_wrap_socket = fake_wrap  # type: ignore[attr-defined]
    a._wrap_socket()
    assert a.ssl_context.check_hostname is True


def test_proxy_manager_for_injects_ssl_context(monkeypatch):
    captured = {}

    def fake_super(self_, proxy, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(HTTPAdapter, "proxy_manager_for", fake_super)
    a = CipherSuiteAdapter()
    a.proxy_manager_for("http://proxy.example.com")
    assert captured.get("ssl_context") is a.ssl_context
    assert "source_address" in captured
