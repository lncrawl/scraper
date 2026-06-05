"""The :class:`Transport` interface the engine drives."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, MutableMapping

from requests import Response
from requests.cookies import RequestsCookieJar

if TYPE_CHECKING:
    from ...engine.context import RequestContext


class Transport(ABC):
    """HTTP transport the engine talks to instead of requests/curl directly.

    Two implementations exist: :class:`~scraper.engine.transport.urllib.UrllibTransport`
    (the legacy urllib3 path, kept as a fallback) and
    :class:`~scraper.engine.transport.curl.CurlCffiTransport` (the primary,
    full-browser-fingerprint path). Each owns an *authoritative* cookie jar; the
    engine keeps a canonical :class:`RequestsCookieJar` and refreshes it from the
    transport after every send via :meth:`export_into`.
    """

    _session_headers: MutableMapping = {}

    def bind_headers(self, headers: MutableMapping) -> None:
        """Hand the engine's live session-header mapping to the transport.

        Subclasses keep a reference so later mutations (e.g. a refreshed
        User-Agent) are reflected on the next send.
        """
        self._session_headers = headers

    @abstractmethod
    def send(self, ctx: RequestContext) -> Response:
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
    def export_into(self, jar: RequestsCookieJar) -> None:
        """Mirror the transport's authoritative jar into *jar*."""
        raise NotImplementedError

    def rotate_ciphers(self) -> None:
        """Rotate the TLS cipher suite (urllib transport only); no-op by default."""

    def close(self) -> None:
        """Release any underlying transport resources."""
