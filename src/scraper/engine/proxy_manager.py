from __future__ import annotations

import logging
import socket
import threading
import time

from ..config import ProxyConfig, ProxyUrl, TorProxyUrl

logger = logging.getLogger(__name__)


class ProxyManager:
    """Round-robin proxy selector for SOCKS/HTTP proxies.

    Tor proxies (:class:`~scraper.config.TorProxyUrl`) get a new exit circuit
    (SIGNAL NEWNYM) on rotate/failure; plain proxies advance round-robin.
    Exhausted proxies are temporarily disabled and re-enabled after
    ``disable_cooldown`` seconds.
    """

    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()

        self._index = 0
        self._lock = threading.Lock()
        self._available: list[ProxyUrl | TorProxyUrl] = []
        self._tor_rotated_at: dict[TorProxyUrl, float] = {}
        self._disabled_at: dict[ProxyUrl | TorProxyUrl, float] = {}

        for p in self.config.proxy_urls:
            if isinstance(p, str):
                p = ProxyUrl(url=p)
            if "://" not in p.url:
                logger.warning(f"Ignoring proxy with no scheme: {p.url}")
                continue
            self._available.append(p)
        logger.debug(f"{len(self._available)} proxy URL(s) configured.")

    @property
    def has_proxy(self) -> bool:
        self._restore_disabled()
        return bool(self._available)

    def get_proxy(self) -> str | None:
        """Return the current proxy URL string, or ``None`` when no proxy is active."""
        if not self.has_proxy:
            return None
        with self._lock:
            current = self._available[self._index]
        return current.url

    def rotate(self, *, disable: bool = False) -> None:
        """Advance to the next proxy identity.

        Pass ``disable=True``to temporarily disable the proxy.

        For a :class:`~scraper.config.TorProxyUrl` with a ``control_port``, sends
        SIGNAL NEWNYM to obtain a new exit circuit; falls back to advancing the
        round-robin index if NEWNYM fails or isn't applicable.
        """
        if not self._available:
            return
        with self._lock:
            current = self._available[self._index]
            if disable:
                now = time.monotonic()
                self._disabled_at[current] = now
                self._available.pop(self._index)
                if self._index >= len(self._available):
                    self._index = 0
                logger.debug(f"Disabled: {current.url!r}")
                return  # removed current and moved to next proxy

            if isinstance(current, TorProxyUrl) and self._try_rotate_tor_circuit(current):
                return  # keep using current proxy on rotation success

            # move to next proxy
            self._index = (self._index + 1) % len(self._available)

    def _restore_disabled(self) -> None:
        if not self._disabled_at:
            return
        with self._lock:
            now = time.monotonic()
            to_restore = [
                url
                for url, fail_time in self._disabled_at.items()
                if now - fail_time >= self.config.disable_cooldown
            ]
            for url in to_restore:
                del self._disabled_at[url]
                self._available.append(url)
                logger.debug(f"Restored: {url!r}")

    def _try_rotate_tor_circuit(self, entry: TorProxyUrl) -> bool:
        """Send SIGNAL NEWNYM to the Tor control port. Returns True on success."""
        if not entry.control_port:
            return False  # no control port

        now = time.monotonic()
        last_rotate = self._tor_rotated_at.get(entry, 0)
        if now - last_rotate < self.config.tor_rotation_cooldown:
            return False  # still under cooldown
        self._tor_rotated_at[entry] = time.monotonic()

        try:
            _rotate_tor_circuit(
                host=entry.control_host,
                port=entry.control_port,
                password=entry.control_password,
            )
            return True  # rotation successfull
        except Exception:
            logger.warning("Tor circuit rotation failed", exc_info=True)
            return False


def _rotate_tor_circuit(host: str, port: int, password: str) -> None:
    """Authenticate to the Tor control port and request a new exit circuit."""
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(f'AUTHENTICATE "{password}"\r\n'.encode())
        resp = s.recv(128).decode().strip()
        if resp != "250 OK":
            raise RuntimeError(resp)

        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(128).decode().strip()
        if resp != "250 OK":
            raise RuntimeError(resp)

        for _ in range(10):
            s.sendall(b"GETINFO status/bootstrap-phase\r\n")
            resp = s.recv(256).decode().strip()
            if "250" in resp and "PROGRESS=100" in resp:
                return
            time.sleep(0.5)
