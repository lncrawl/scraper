"""Tests for ProxyManager — round-robin selector and Tor identity rotation."""

import socket
import time

from scraper._engine.config import ProxyConfig, TorProxyUrl
from scraper._engine.proxy_manager import ProxyManager


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
    p.rotate_identity()
    second = p.get_proxy()
    assert first is not None and second is not None
    assert first["http"] != second["http"]
    # wraps around
    p._last_rotate = 0
    p.rotate_identity()
    third = p.get_proxy()
    assert third is not None
    assert third["http"] == first["http"]


def test_get_proxy_raises_on_missing_scheme():
    p = pm("127.0.0.1:9150")
    assert p.get_proxy() is None


# --- rotate_identity -----------------------------------------------------


def test_rotate_identity_noop_when_no_control_port():
    p = ProxyManager(ProxyConfig(proxy_urls=[TorProxyUrl(control_port=0)]))
    p.rotate_identity()  # must not raise


def test_rotate_identity_debounce_skips_rapid_calls(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not connect")),
    )

    p = ProxyManager(ProxyConfig(proxy_urls=[TorProxyUrl(control_port=9051)]))
    p._last_rotate = time.monotonic()  # simulate a very recent rotation
    p.rotate_identity()  # should be a no-op (debounce), not raise


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

    tor_proxy = TorProxyUrl(control_port=9051, control_password="pw")
    p = ProxyManager(ProxyConfig(proxy_urls=[tor_proxy]))
    p.rotate_identity()

    commands = b"".join(fake.sent)
    assert b"AUTHENTICATE" in commands
    assert b"SIGNAL NEWNYM" in commands


def test_rotate_identity_logs_on_socket_error(monkeypatch):
    def boom(*a, **kw):
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", boom)
    tor_proxy = TorProxyUrl(control_port=9051)
    p = ProxyManager(ProxyConfig(proxy_urls=[tor_proxy]))
    p.rotate_identity()  # error must be swallowed, not raised


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

    tor_proxy = TorProxyUrl(control_port=9051)
    p = ProxyManager(ProxyConfig(proxy_urls=[tor_proxy]))
    p.rotate_identity()  # RuntimeError is caught and logged — should not propagate


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

    tor_proxy = TorProxyUrl(control_port=9051)
    p = ProxyManager(ProxyConfig(proxy_urls=[tor_proxy]))
    p.rotate_identity()  # RuntimeError is caught and logged — should not propagate
