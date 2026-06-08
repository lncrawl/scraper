"""The :class:`Transport` interface the engine drives."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, MutableMapping, Optional

import httpx

if TYPE_CHECKING:
    from ...engine.state import RequestState


class Transport(ABC):
    """HTTP transport the engine talks to instead of a library directly.

    Two implementations exist:
    :class:`~scraper.engine.transport.httpx_transport.HttpxTransport` (the
    fallback path, full http2 support) and
    :class:`~scraper.engine.transport.curl.CurlCffiTransport` (the primary,
    full-browser-fingerprint path via curl_cffi). Each owns an *authoritative*
    cookie jar; the engine keeps a canonical :class:`httpx.Cookies` and refreshes
    it from the transport after every send via :meth:`export_into`.
    """

    _session_headers: MutableMapping[str, str]
    _forced_user_agent: Optional[str]

    def __init__(self) -> None:
        self._session_headers = {}
        self._forced_user_agent = None

    def bind_headers(self, headers: MutableMapping[str, str]) -> None:
        """Hand the engine's live session-header mapping to the transport."""
        self._session_headers = headers

    def force_user_agent(self, user_agent: Optional[str]) -> None:
        """Pin an exact User-Agent that overrides impersonation defaults every send."""
        self._forced_user_agent = user_agent

    @abstractmethod
    async def send(self, ctx: "RequestState") -> httpx.Response:
        """Issue the raw HTTP request described by *ctx* and return the response."""
        raise NotImplementedError

    @abstractmethod
    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on the transport's authoritative jar."""
        raise NotImplementedError

    @abstractmethod
    def clear_cookie(self, name: str, domain: str = "") -> None:
        """Remove a cookie (by name) from the transport's authoritative jar."""
        raise NotImplementedError

    @abstractmethod
    def clear_all_cookies(self) -> None:
        """Clear the transport's authoritative cookie jar."""
        raise NotImplementedError

    @abstractmethod
    def export_into(self, jar: httpx.Cookies) -> None:
        """Mirror the transport's authoritative jar into *jar*."""
        raise NotImplementedError

    def rotate_ciphers(self) -> None:
        """Rotate the TLS cipher suite (httpx fallback transport only); no-op by default."""

    def reset_session(self) -> None:
        """Drop all pooled connections (e.g. after Tor NEWNYM). No-op by default."""

    def close(self) -> None:
        """Release any synchronous transport resources."""

    async def aclose(self) -> None:
        """Release any async transport resources (httpx client, connections)."""
