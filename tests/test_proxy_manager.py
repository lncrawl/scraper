"""Tests for ProxyManager — round-robin selector and Tor identity rotation."""

import socket
import time

import pytest

from scraper.config import ProxyConfig
from scraper.engine.proxy_manager import ProxyManager


def pm(*urls, **kwargs) -> ProxyManager:
    return ProxyManager(ProxyConfig(proxy_urls=list(urls), **kwargs))


# --- has_proxy / get_proxy -----------------------------------------------


def test_has_proxy_false_with_no_urls():
    assert not ProxyManager().has_proxy


def test_has_proxy_true_with_url():
    assert pm("socks5://127.0.0.1:9150").has_proxy


def test_get_proxy_none_when_empty():
    assert ProxyManager().get_proxy() is None


def test_get_proxy_returns_both_schemes():
    result = pm("socks5://127.0.0.1:9150").get_proxy()
    assert result == {"http": "socks5://127.0.0.1:9150", "https": "socks5://127.0.0.1:9150"}


def test_get_proxy_round_robins():
    p = pm("socks5://127.0.0.1:9150", "socks5://127.0.0.1:9151")
    first = p.get_proxy()
    p._last_rotate = 0
    p.report_failure()
    second = p.get_proxy()
    assert first is not None and second is not None
    assert first["http"] != second["http"]
    # wraps around
    p._last_rotate = 0
    p.report_failure()
    third = p.get_proxy()
    assert third is not None
    assert third["http"] == first["http"]


def test_get_proxy_raises_on_missing_scheme():
    p = pm("127.0.0.1:9150")
    assert p.get_proxy() is None


# --- _restore_disabled ---------------------------------------------------


def test_restore_disabled_removes_from_failed_at(monkeypatch):
    """Restored proxies must be removed from _failed_at so they aren't re-added on next call."""
    p = pm("socks5://127.0.0.1:9150", failure_tolerance=0, disable_cooldown=0)
    p.report_failure()  # disables the proxy immediately (tolerance=0)
    assert not p._available
    assert p._failed_at  # proxy is in the failed dict

    # Force cooldown to be satisfied immediately
    monkeypatch.setattr("scraper.engine.proxy_manager.time.monotonic", lambda: 10**9)
    p._restore_disabled()
    assert len(p._available) == 1
    assert not p._failed_at  # must be cleaned up

    # Calling again must NOT add a duplicate
    p._restore_disabled()
    assert len(p._available) == 1


# --- ProxyManager.rotate_tor_identity -----------------------------------------------------


def test_rotate_identity_sends_newnym(monkeypatch):
    class _FakeSocket:
        def __init__(self):
            self.sent = []

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, n: int) -> bytes:
            return b"250 OK"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    fake = _FakeSocket()
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: fake)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    ProxyManager.rotate_tor_identity("127.0.0.1", 9051, "pw")

    commands = b"".join(fake.sent)
    assert b"AUTHENTICATE" in commands
    assert b"SIGNAL NEWNYM" in commands


def test_rotate_identity_logs_on_socket_error(monkeypatch):
    def boom(*a, **kw):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", boom)

    with pytest.raises(OSError, match="refused"):
        ProxyManager.rotate_tor_identity("127.0.0.1", 9051, "")


def test_rotate_identity_raises_on_bad_auth_response(monkeypatch):
    class _BadAuthSocket:
        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, n: int) -> bytes:
            return b"515 Authentication failed"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _BadAuthSocket())
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="Tor control auth failed: '515 Authentication failed'"):
        ProxyManager.rotate_tor_identity("127.0.0.1", 9051, "")


def test_rotate_identity_raises_on_bad_newnym_response(monkeypatch):
    recv_count = [0]

    class _BadNewNymSocket:
        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, n: int) -> bytes:
            recv_count[0] += 1
            if recv_count[0] == 1:
                return b"250 OK"  # auth ok
            return b"552 Unrecognized SIGNAL"  # newnym rejected

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _BadNewNymSocket())
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="NEWNYM rejected: '552 Unrecognized SIGNAL'"):
        ProxyManager.rotate_tor_identity("127.0.0.1", 9051, "")
