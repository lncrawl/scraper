"""HttpxTransport — the httpx/httpcore-based fallback transport.

Replaces the legacy ``UrllibTransport``.  Used when curl_cffi is unavailable or
when ``config.impersonate.target`` is not set.  Backed by ``httpx.AsyncClient``
with full HTTP/2 support; supports per-proxy client pooling so ProxyMiddleware can
rotate proxies without losing the connection pool for other proxies.
"""

from __future__ import annotations

import http.cookiejar
import logging
from typing import Dict, Optional, Tuple

import httpx

from ...config import ScraperConfig
from ...exceptions import ProxyTransportError, SSLTransportError
from ..state import RequestState
from ..tls import CipherRotator, build_ssl_context
from ..user_agent.data import CIPHER_SUITES
from .base import Transport

logger = logging.getLogger(__name__)

# kwargs from RequestState.kwargs forwarded to httpx.AsyncClient.request()
_HTTPX_PASSTHROUGH = frozenset(
    (
        "content",
        "data",
        "files",
        "json",
        "params",
        "auth",
        "timeout",
        "extensions",
    )
)


class HttpxTransport(Transport):
    """A :class:`Transport` backed by ``httpx.AsyncClient``.

    Maintains a pool of clients keyed by ``(proxy_url, verify)`` so that proxy
    changes (injected per-request by :class:`ProxyMiddleware`) are transparent.
    Cookies are tracked in a shared :class:`http.cookiejar.CookieJar`; before each
    send, the shared cookies are synced into the client's cookies, and after each
    response, response cookies are merged back into the shared jar.
    """

    def __init__(self, config: ScraperConfig) -> None:
        super().__init__()
        self._config = config

        pool: list[str] = []
        if config.browser.browser in CIPHER_SUITES:
            pool = CIPHER_SUITES[config.browser.browser]
        self._rotator = CipherRotator(pool)
        self._rotation = 0
        configured = config.cipher_suite
        self._cipher_suite: Optional[str] = (
            ":".join(configured) if isinstance(configured, list) else (configured or None)
        )

        # (proxy_url | None, verify) → httpx.AsyncClient
        self._pool: Dict[Tuple[Optional[str], bool], httpx.AsyncClient] = {}
        # Shared cookie store — injected per-request, response cookies merged in
        self._jar = http.cookiejar.CookieJar()

    # -- TLS ------------------------------------------------------------------

    def rotate_ciphers(self) -> None:
        self._rotation += 1
        suite = self._rotator.suite_for(self._rotation)
        if suite and suite != self._cipher_suite:
            self._cipher_suite = suite
            # Evict existing clients so the next request rebuilds with new ciphers
            self._pool.clear()

    def _build_ssl_ctx(self, verify: bool) -> Optional[httpx.SSLConfig]:  # type: ignore[name-defined]
        """Build an ssl.SSLContext with the current cipher suite (if any)."""
        if self._cipher_suite or self._config.ssl_context or self._config.server_hostname:
            ctx = build_ssl_context(
                cipher_suite=self._cipher_suite,
                ecdh_curve=self._config.ecdh_curve,
                server_hostname=self._config.server_hostname,
                ssl_context=self._config.ssl_context,
                verify_ssl=verify,
            )
            return ctx  # type: ignore[return-value]
        return None

    # -- Client pool ----------------------------------------------------------

    def _get_client(self, proxy_url: Optional[str], verify: bool) -> httpx.AsyncClient:
        key = (proxy_url, verify)
        if key not in self._pool:
            ssl_ctx = self._build_ssl_ctx(verify)
            kwargs: dict = {
                "http2": True,
                "follow_redirects": True,
                "verify": ssl_ctx if ssl_ctx is not None else verify,
            }
            if proxy_url:
                kwargs["proxy"] = proxy_url
            self._pool[key] = httpx.AsyncClient(**kwargs)
        return self._pool[key]

    # -- Cookies --------------------------------------------------------------

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        cookie = http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith(".") if domain else False,
            path=path or "/",
            path_specified=bool(path),
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        )
        self._jar.set_cookie(cookie)

    def clear_cookie(self, name: str, domain: str = "") -> None:
        domains = [domain] if domain else list({c.domain for c in self._jar})
        for dom in domains:
            try:
                self._jar.clear(dom, "/", name)
            except KeyError:
                pass

    def clear_all_cookies(self) -> None:
        self._jar.clear()

    def export_into(self, jar: httpx.Cookies) -> None:
        jar.jar.clear()
        for cookie in self._jar:
            jar.jar.set_cookie(cookie)

    # -- Send -----------------------------------------------------------------

    async def send(self, ctx: RequestState) -> httpx.Response:
        kwargs = dict(ctx.kwargs)
        proxy_url: Optional[str] = kwargs.pop("proxy", None)
        verify: bool = kwargs.pop("verify", self._config.verify_ssl)
        kwargs.pop("stream", None)

        # Translate allow_redirects (requests-style) → follow_redirects (httpx)
        follow_redirects: bool = kwargs.pop("allow_redirects", True)

        # Merge session headers then per-request headers on top
        headers: dict = dict(self._session_headers)
        headers.update(kwargs.pop("headers", {}) or {})
        if self._forced_user_agent:
            headers["User-Agent"] = self._forced_user_agent

        call = {k: kwargs[k] for k in _HTTPX_PASSTHROUGH if k in kwargs}
        call["headers"] = headers
        call["follow_redirects"] = follow_redirects

        client = self._get_client(proxy_url, verify)
        # Sync shared cookies into the client (before request, not per-request)
        client.cookies.jar.clear()
        for cookie in self._jar:
            client.cookies.jar.set_cookie(cookie)

        try:
            response = await client.request(ctx.method, ctx.url, **call)
        except httpx.ProxyError as exc:
            raise ProxyTransportError(str(exc)) from exc
        except httpx.ConnectError as exc:
            if _is_ssl_error(exc):
                raise SSLTransportError(str(exc)) from exc
            raise ProxyTransportError(str(exc)) from exc

        # Merge response cookies back into the shared jar
        for cookie in response.cookies.jar:
            self._jar.set_cookie(cookie)

        return response

    # -- Lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        for client in self._pool.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._pool.clear()


def _is_ssl_error(exc: BaseException) -> bool:
    """Walk the exception chain looking for an ssl.SSLError."""
    import ssl

    node: Optional[BaseException] = exc
    while node is not None:
        if isinstance(node, ssl.SSLError):
            return True
        node = getattr(node, "__cause__", None) or getattr(node, "__context__", None)
    return False
