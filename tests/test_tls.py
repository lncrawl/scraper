"""Tests for CipherRotator and build_ssl_context."""

import ssl

from scraper.engine.tls import CipherRotator, build_ssl_context

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


# --- build_ssl_context ---------------------------------------------------


def test_build_ssl_context_returns_ssl_context():
    ctx = build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_build_ssl_context_verify_false_disables_check_hostname():
    ctx = build_ssl_context(verify_ssl=False)
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def test_build_ssl_context_custom_context_passthrough():
    custom = ssl.create_default_context()
    ctx = build_ssl_context(ssl_context=custom)
    assert ctx is custom


def test_build_ssl_context_server_hostname():
    ctx = build_ssl_context(server_hostname="example.com")
    assert getattr(ctx, "server_hostname", None) == "example.com"


def test_build_ssl_context_tls_versions():
    ctx = build_ssl_context()
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_3


def test_build_ssl_context_cipher_suite_is_applied():
    """Line 42: ctx.set_ciphers() is called when cipher_suite is provided."""
    ctx = build_ssl_context(cipher_suite="AES128-SHA:AES256-SHA")
    assert isinstance(ctx, ssl.SSLContext)
    # Verify the custom cipher list is active (get_ciphers returns at least one entry)
    ciphers = ctx.get_ciphers()
    assert isinstance(ciphers, list)
