from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Dict, List

from .config import ProxyConfig, ProxyUrl, TorProxyUrl

logger = logging.getLogger(__name__)

# Minimum seconds between consecutive identity rotations (debounce).
_ROTATE_COOLDOWN = 10.0
_ROTATE_LOCK = threading.Lock()


class ProxyManager:
    """Round-robin proxy selector for SOCKS/HTTP proxies.

    Intended for use with containers like `peterdavehello/tor-socks-proxy` (`socks5://127.0.0.1:9150`).
    Call `rotate_identity()` to cycle proxy URL or request a new Tor exit circuit via the
    control port (SIGNAL NEWNYM); it is called automatically on proxy errors.
    """

    def __init__(self, config: ProxyConfig | None = None) -> None:
        cfg = config or ProxyConfig()

        self._index = 0
        self._last_rotate = 0.0

        self._urls: List[ProxyUrl | TorProxyUrl] = []
        for p in cfg.proxy_urls:
            if isinstance(p, str):
                p = ProxyUrl(url=p)
            if "://" not in p.url:
                continue
            self._urls.append(p)

        logger.debug(f"{len(self._urls)} proxy URL(s) configured.")

    @property
    def has_proxy(self) -> bool:
        return bool(self._urls)

    def get_proxy(self) -> Dict[str, str] | None:
        if not self._urls:
            return None

        with _ROTATE_LOCK:
            current = self._urls[self._index]

        config = {"http": current.url}
        if not current.http_only:
            config["https"] = current.url
        return config

    def rotate_identity(self) -> None:
        if not self._urls:
            return
        with _ROTATE_LOCK:
            now = time.monotonic()
            if now - self._last_rotate < _ROTATE_COOLDOWN:
                return
            self._last_rotate = now

            self._index = (self._index + 1) % len(self._urls)

            current = self._urls[self._index]
            if isinstance(current, TorProxyUrl) and current.control_port:
                self._new_tor_circuit(
                    current.control_host,
                    current.control_port,
                    current.control_password,
                )

    @staticmethod
    def _new_tor_circuit(host: str, port: int, password: str = "") -> None:
        """Request a new Tor exit circuit via the control port (SIGNAL NEWNYM).

        Blocks ~5 s while Tor builds the new circuit. Debounced to at most
        once every `_ROTATE_COOLDOWN` seconds across threads. No-op when
        `tor_control_port` is 0.
        """
        try:
            with socket.create_connection((host, port), timeout=10) as s:
                s.sendall(f'AUTHENTICATE "{password}"\r\n'.encode())
                resp = s.recv(128)
                if not resp.startswith(b"250"):
                    raise RuntimeError(f"Tor control auth failed: {resp.decode()!r}")
                s.sendall(b"SIGNAL NEWNYM\r\n")
                resp = s.recv(128)
                if not resp.startswith(b"250"):
                    raise RuntimeError(f"NEWNYM rejected: {resp.decode()!r}")
            logger.debug("Tor identity rotated — waiting for new circuit.")
            time.sleep(5)
        except Exception as exc:
            logger.warning("Tor identity rotation failed: %s", exc)
