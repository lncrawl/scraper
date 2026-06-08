from __future__ import annotations

import ssl
from typing import Any, Optional


class CipherRotator:
    """Produces rotating TLS cipher-suite strings from a fixed in-memory pool."""

    _WINDOW = 8

    def __init__(self, ciphers: list[str]) -> None:
        self._pool = list(ciphers or [])

    def suite_for(self, rotation: int) -> Optional[str]:
        """Return a cipher-suite string for the given rotation count, or None to skip."""
        size = len(self._pool)
        if size <= 1:
            return None
        window = min(self._WINDOW, size)
        start = (rotation % size) % max(1, size - window + 1)
        return ":".join(self._pool[start : start + window])


def build_ssl_context(
    cipher_suite: Optional[str] = None,
    ecdh_curve: str = "prime256v1",
    server_hostname: Optional[str] = None,
    source_address: Any = None,
    ssl_context: Optional[ssl.SSLContext] = None,
    verify_ssl: bool = True,
) -> ssl.SSLContext:
    """Return an SSLContext with optional cipher-suite and ECDH-curve constraints.

    Used by :class:`~scraper.engine.transport.httpx_transport.HttpxTransport` to
    build a custom httpcore transport with specific TLS settings.
    """
    ctx = ssl_context or ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if server_hostname:
        ctx.server_hostname = server_hostname  # type: ignore[attr-defined]
    if cipher_suite:
        ctx.set_ciphers(cipher_suite)
    ctx.set_ecdh_curve(ecdh_curve)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
