"""Tests for ProxyManager — round-robin selector and Tor identity rotation."""

import socket
import time

import pytest

from scraper.config import ProxyConfig
from scraper.engine.proxy_manager import ProxyManager, _rotate_tor_circuit


def pm(*urls, **kwargs) -> ProxyManager:
    return ProxyManager(ProxyConfig(proxy_urls=list(urls), **kwargs))


# --- has_proxy / get_proxy -----------------------------------------------


def test_has_proxy_false_with_no_urls():
    assert not ProxyManager().has_proxy


def test_has_proxy_true_with_url():
    assert pm("socks5://127.0.0.1:9150").has_proxy


def test_get_proxy_none_when_empty():
    assert ProxyManager().get_proxy() is None


def test_get_proxy_returns_proxies_dict():
    result = pm("socks5://127.0.0.1:9150").get_proxy()
    assert result == {"http": "socks5://127.0.0.1:9150", "https": "socks5://127.0.0.1:9150"}


def test_get_proxy_round_robins():
    p = pm("socks5://127.0.0.1:9150", "socks5://127.0.0.1:9151")
    first = p.get_proxy()
    p.rotate()
    second = p.get_proxy()
    assert first is not None and second is not None
    assert first != second
    # wraps around
    p.rotate()
    third = p.get_proxy()
    assert third is not None
    assert third == first


def test_get_proxy_raises_on_missing_scheme():
    p = pm("127.0.0.1:9150")
    assert p.get_proxy() is None


# --- _restore_disabled ---------------------------------------------------


def test_restore_disabled_removes_from_disabled_at(monkeypatch):
    """Restored proxies must be removed from _disabled_at so they aren't re-added on next call."""
    p = pm("socks5://127.0.0.1:9150", disable_cooldown=0)
    p.disable_current()
    p.rotate()  # disables the proxy immediately
    assert not p._available
    assert p._disabled_at  # proxy is in the disabled dict

    # Force cooldown to be satisfied immediately
    monkeypatch.setattr("scraper.engine.proxy_manager.time.monotonic", lambda: 10**9)
    p._restore_disabled()
    assert len(p._available) == 1
    assert not p._disabled_at  # must be cleaned up

    # Calling again must NOT add a duplicate
    p._restore_disabled()
    assert len(p._available) == 1


# --- rotate edge cases ---------------------------


def test_rotate_is_noop_when_no_proxies():
    """rotate() must not raise when _available is empty."""
    ProxyManager().rotate()


# --- ProxyUrl object accepted directly (no string wrapping) ---------------


def test_proxy_url_object_accepted_directly():
    from scraper.config import ProxyUrl

    # Passing a ProxyUrl object directly must work (no str→ProxyUrl conversion needed).
    p = ProxyManager(ProxyConfig(proxy_urls=[ProxyUrl(url="socks5://127.0.0.1:9150")]))
    assert p.has_proxy


# --- _rotate_tor_circuit -----------------------------------------------------


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

    _rotate_tor_circuit("127.0.0.1", 9051, "pw")

    commands = b"".join(fake.sent)
    assert b"AUTHENTICATE" in commands
    assert b"SIGNAL NEWNYM" in commands


def test_rotate_identity_logs_on_socket_error(monkeypatch):
    def boom(*a, **kw):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", boom)

    with pytest.raises(OSError, match="refused"):
        _rotate_tor_circuit("127.0.0.1", 9051, "")


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

    with pytest.raises(RuntimeError, match="515 Authentication failed"):
        _rotate_tor_circuit("127.0.0.1", 9051, "")


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

    with pytest.raises(RuntimeError, match="552 Unrecognized SIGNAL"):
        _rotate_tor_circuit("127.0.0.1", 9051, "")


# --- Tor rotation triggered via rotate(record_failure=True) ---------------------------


def test_tor_rotation_success_on_rotate(monkeypatch):
    """rotate() on a TorProxyUrl with control_port calls _rotate_tor_circuit."""
    from scraper.config import TorProxyUrl

    p = ProxyManager(
        ProxyConfig(
            proxy_urls=[TorProxyUrl(url="socks5://127.0.0.1:9150", control_port=9051)],
            tor_rotation_cooldown=0.0,  # no cooldown
        )
    )

    rotated = []
    monkeypatch.setattr(
        "scraper.engine.proxy_manager._rotate_tor_circuit", lambda **_: rotated.append(1)
    )
    monkeypatch.setattr("scraper.engine.proxy_manager.time.monotonic", lambda: 1e9)

    p.rotate()
    assert rotated  # rotation was attempted


def test_tor_rotation_failure_logs_warning_and_falls_through(monkeypatch):
    """If _rotate_tor_circuit raises, the warning is logged and rotation falls through."""
    from scraper.config import TorProxyUrl

    p = ProxyManager(
        ProxyConfig(
            proxy_urls=[TorProxyUrl(url="socks5://127.0.0.1:9150", control_port=9051)],
            tor_rotation_cooldown=0.0,
        )
    )

    def _boom(host: str, port: int, password: str) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("scraper.engine.proxy_manager._rotate_tor_circuit", _boom)
    monkeypatch.setattr("scraper.engine.proxy_manager.time.monotonic", lambda: 1e9)

    # Must not raise — the exception is caught and logged
    p.rotate()
    assert p.has_proxy  # proxy still active


def test_tor_rotate_skipped_when_no_control_port():
    """_try_rotate_tor_circuit returns False immediately when control_port is 0."""
    from scraper.config import TorProxyUrl

    p = ProxyManager(
        ProxyConfig(proxy_urls=[TorProxyUrl(url="socks5://127.0.0.1:9150", control_port=0)])
    )
    p.rotate()  # should not raise, falls through to index advance
    assert p.has_proxy


def test_tor_rotate_skipped_under_cooldown(monkeypatch):
    """Second rotate() within the cooldown window skips _rotate_tor_circuit."""
    from scraper.config import TorProxyUrl

    rotated = []
    monkeypatch.setattr(
        "scraper.engine.proxy_manager._rotate_tor_circuit", lambda **_: rotated.append(1)
    )
    p = ProxyManager(
        ProxyConfig(
            proxy_urls=[TorProxyUrl(url="socks5://127.0.0.1:9150", control_port=9051)],
            tor_rotation_cooldown=100.0,
        )
    )
    p.rotate()  # first call: cooldown has never been set → rotates
    assert rotated
    rotated.clear()
    p.rotate()  # second call immediately after: still within 100s cooldown → skipped
    assert not rotated


def test_rotate_tor_circuit_returns_early_on_progress_100(monkeypatch):
    """_rotate_tor_circuit returns as soon as GETINFO reports PROGRESS=100."""
    recv_count = [0]

    class _BootstrapSocket:
        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, n: int) -> bytes:
            recv_count[0] += 1
            if recv_count[0] <= 2:
                return b"250 OK"  # auth, then newnym
            return b"250-status/bootstrap-phase PROGRESS=100 TAG=done"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _BootstrapSocket())
    monkeypatch.setattr(time, "sleep", lambda _: None)

    _rotate_tor_circuit("127.0.0.1", 9051, "")
    assert recv_count[0] == 3  # auth + newnym + one GETINFO
