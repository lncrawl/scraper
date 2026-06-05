from __future__ import annotations

import logging
import socket
import threading
import time

from ..config import ProxyConfig, ProxyUrl, TorProxyUrl

logger = logging.getLogger(__name__)


class ProxyManager:
    """Round-robin proxy selector for SOCKS/HTTP proxies.

    Intended for use with containers like `peterdavehello/tor-socks-proxy` (`socks5://127.0.0.1:9150`).
    Call `rotate_identity()` to cycle proxy URL or request a new Tor exit circuit via the
    control port (SIGNAL NEWNYM); it is called automatically on proxy errors.
    """

    def __init__(self, config: ProxyConfig | None = None) -> None:
        self.config = config or ProxyConfig()

        self._index = 0
        self._last_rotate = 0.0
        self._lock = threading.Lock()
        self._available: list[ProxyUrl | TorProxyUrl] = []
        self._fail_count: dict[ProxyUrl | TorProxyUrl, int] = {}
        self._failed_at: dict[ProxyUrl | TorProxyUrl, float] = {}

        for p in self.config.proxy_urls:
            if isinstance(p, str):
                p = ProxyUrl(url=p)
            if "://" not in p.url:
                continue
            self._available.append(p)
        logger.debug(f"{len(self._available)} proxy URL(s) configured.")

    @property
    def has_proxy(self) -> bool:
        self._restore_disabled()
        return len(self._available) > 0

    def get_proxy(self) -> dict[str, str] | None:
        if not self.has_proxy:
            return None
        with self._lock:
            current = self._available[self._index]
        config = {"http": current.url}
        if not current.http_only:
            config["https"] = current.url
        return config

    def _restore_disabled(self) -> None:
        if not self._failed_at:
            return
        with self._lock:
            to_restore = [
                url
                for url, fail_time in self._failed_at.items()
                if time.monotonic() - fail_time >= self.config.disable_cooldown
            ]
            for url in to_restore:
                del self._failed_at[url]
                self._available.append(url)
                logger.debug(f"Restored proxy: {url!r}")

    def report_success(self) -> None:
        if not self._available:
            return
        with self._lock:
            current = self._available[self._index]
            # clear failures on success
            self._failed_at.pop(current, None)
            self._fail_count[current] = 0

    def report_failure(self) -> None:
        if not self._available:
            return
        with self._lock:
            current = self._available[self._index]
            fail_count = self._fail_count.get(current, 0)
            last_fail = self._failed_at.get(current, 0)
            self._failed_at[current] = time.monotonic()
            self._fail_count[current] = fail_count + 1

            # disable current one if failure exceeds tolerance
            if fail_count >= self.config.failure_tolerance:
                self._available.pop(self._index)
                if self._index >= len(self._available):
                    self._index = 0
                return  # disabled currnt proxy

            # rotate tor identity on failure with a cooldown
            if (
                isinstance(current, TorProxyUrl)
                and current.control_port
                and time.monotonic() - last_fail >= self.config.tor_rotation_cooldown
            ):
                try:
                    self.rotate_tor_identity(
                        current.control_host,
                        current.control_port,
                        current.control_password,
                    )
                    return  # rotation success, reuse current proxy
                except Exception as e:
                    logger.warning(f"Tor identity rotation failed: {e}")

            # move to next available proxy if rotation is unavailable or failed
            self._index = (self._index + 1) % len(self._available)

    @staticmethod
    def rotate_tor_identity(host: str, port: int, password: str) -> None:
        """Request a new Tor exit circuit via the control port (SIGNAL NEWNYM).

        Blocks ~5s while Tor builds the new circuit. Debounced to at most once every
        `_ROTATE_COOLDOWN` seconds across threads. No-op when `control_port` is 0.
        """
        with socket.create_connection((host, port), timeout=10) as s:
            s.sendall(f'AUTHENTICATE "{password}"\r\n'.encode())
            resp = s.recv(128)
            if not resp.startswith(b"250"):
                raise RuntimeError(f"Tor control auth failed: {resp.decode()!r}")
            s.sendall(b"SIGNAL NEWNYM\r\n")
            resp = s.recv(128)
            if not resp.startswith(b"250"):
                raise RuntimeError(f"NEWNYM rejected: {resp.decode()!r}")

        logger.debug("Tor identity rotated — waiting 5s for new circuit.")
        for _ in range(50):
            time.sleep(0.1)  # short interval to avoid blocking IO
