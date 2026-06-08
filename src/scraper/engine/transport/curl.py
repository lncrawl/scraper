"""CurlCffiTransport — the primary transport backed by curl_cffi.

The httpx transport has a standard OpenSSL TLS ClientHello (JA3/JA4) which
Cloudflare fingerprints. curl_cffi (curl-impersonate) reproduces a real
Chrome/Firefox/Safari TLS *and* HTTP/2 fingerprint, so it is the default
transport whenever an impersonation target is configured and curl_cffi is
installed.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import inspect
import re
import threading
from typing import Any, Iterator, Optional

import httpx
from curl_cffi.requests import Response, Session
from curl_cffi.requests.impersonate import BrowserType
from curl_cffi.requests.session import BaseSession as _BaseSession
from httpx import Cookies, SyncByteStream

from ...config import ScraperConfig
from ...exceptions import ProxyTransportError, SSLTransportError
from ..state import RequestState
from .base import Transport

# curl_cffi 0.13.x (the last release supporting Python 3.9) does not have the
# 'perk' fingerprint parameter. Detect once at import time so we never pass an
# unknown keyword argument to BaseSession.__init__ on older installs.
_CURL_HAS_PERK: bool = "perk" in inspect.signature(_BaseSession.__init__).parameters
del _BaseSession

# kwargs forwarded to curl_cffi — standard requests ones plus curl_cffi-specific
# fingerprint and transport overrides. Anything else is dropped so the underlying
# call never sees an unknown argument.
_PASSTHROUGH = (
    # standard requests-style kwargs curl_cffi also accepts
    "data",
    "json",
    "params",
    "files",
    "auth",
    "cookies",
    "timeout",
    "verify",
    "cert",
    "stream",
    "proxies",
    # curl_cffi-specific per-request overrides
    "ja3",
    "akamai",
    "perk",
    "extra_fp",
    "http_version",
    "interface",
    "max_recv_speed",
    "discard_cookies",
    "content_callback",
    "default_headers",
)


_STRIP_HEADERS = frozenset({"content-encoding", "transfer-encoding"})


def _resp_headers(resp: object) -> list:
    """Extract response headers, stripping auto-decoded content/transfer encodings."""
    return [
        (k, v) for k, v in getattr(resp, "headers", {}).items() if k.lower() not in _STRIP_HEADERS
    ]


def _attach_cookies(response: httpx.Response, resp: object) -> None:
    """Copy cookies from a curl_cffi response object into an httpx.Response."""
    jar = http.cookiejar.CookieJar()
    for cookie in getattr(getattr(resp, "cookies", None), "jar", []):
        jar.set_cookie(cookie)
    for cookie in jar:
        response.cookies.jar.set_cookie(cookie)


def adapt_curl_response(method: str, url: str, req_headers: dict, resp: object) -> httpx.Response:
    """Convert a buffered curl_cffi response into an :class:`httpx.Response`."""
    req = httpx.Request(method.upper(), url, headers=req_headers)
    response = httpx.Response(
        status_code=getattr(resp, "status_code", 200),
        headers=_resp_headers(resp),
        content=getattr(resp, "content", b""),
        request=req,
    )
    _attach_cookies(response, resp)
    return response


class _CurlStream(SyncByteStream):
    """Synchronous byte stream backed by a streaming curl_cffi response.

    Holds ``lock`` for the lifetime of the stream so that the underlying
    curl_cffi session cannot be used concurrently while body bytes are in
    flight.  The lock is released exactly once — either after the iterator is
    exhausted or when :meth:`close` is called, whichever comes first.
    """

    def __init__(self, resp: Any, lock: threading.Lock) -> None:
        self._resp = resp
        self._lock = lock
        self._released = False

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._resp.iter_content()
        finally:
            self._release()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass
        finally:
            self._release()


def adapt_curl_response_streaming(
    method: str, url: str, req_headers: dict, resp: object, lock: threading.Lock
) -> httpx.Response:
    """Build a streaming :class:`httpx.Response` backed by *resp*.

    The curl_cffi session ``lock`` is held until the response body is fully
    consumed or closed, preventing concurrent use of the same session handle.
    """
    req = httpx.Request(method.upper(), url, headers=req_headers)
    response = httpx.Response(
        status_code=getattr(resp, "status_code", 200),
        headers=_resp_headers(resp),
        stream=_CurlStream(resp, lock),
        request=req,
    )
    # Cookies from Set-Cookie headers are available immediately (before body).
    _attach_cookies(response, resp)
    return response


class CurlCffiTransport(Transport):
    """A :class:`Transport` backed directly by ``curl_cffi.requests.Session``."""

    def __init__(self, config: ScraperConfig) -> None:
        super().__init__()
        self._config = config
        self._lock = threading.Lock()
        self._forced_impersonate: Optional[str] = None
        self._session: Session[Response] = self._new_session()

    def _new_session(self) -> Session[Response]:
        cfg = self._config.impersonate
        session = Session(
            impersonate=cfg.target,
            http_version=cfg.http_version,
            ja3=cfg.ja3,
            akamai=cfg.akamai,
            extra_fp=cfg.extra_fp,
            default_headers=cfg.default_headers,
            trust_env=cfg.trust_env,
            curl_options=cfg.curl_options or {},
            verify=self._config.verify_ssl,
        )
        if _CURL_HAS_PERK:
            setattr(session, "perk", cfg.perk)
        return session

    # -- Fingerprint ----------------------------------------------------------

    def force_user_agent(self, user_agent: Optional[str]) -> None:
        super().force_user_agent(user_agent)
        self._forced_impersonate = self._match_impersonate(user_agent) if user_agent else None

    @staticmethod
    def _match_impersonate(user_agent: str) -> Optional[str]:
        ua = user_agent or ""
        if "Edg/" in ua:
            family, match = "edge", re.search(r"Edg/(\d+)", ua)
        elif "Firefox/" in ua:
            family, match = "firefox", re.search(r"Firefox/(\d+)", ua)
        elif "Chrome/" in ua:
            family, match = "chrome", re.search(r"Chrome/(\d+)", ua)
        else:
            return None
        if not match:
            return None
        want = int(match.group(1))

        candidates = sorted(
            (int(m.group(1)), bt.value)
            for bt in BrowserType
            if (m := re.fullmatch(rf"{family}(\d+)", str(bt.value)))
        )
        if not candidates:
            return None
        best = next((label for ver, label in reversed(candidates) if ver <= want), None)
        return best or candidates[0][1]

    # -- Cookies --------------------------------------------------------------

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookie(self, name: str, domain: str = "") -> None:
        try:
            self._session.cookies.delete(name, domain=domain or None)
        except Exception:
            pass

    def clear_all_cookies(self) -> None:
        self._session.cookies.clear()

    def export_into(self, jar: Cookies) -> None:
        jar.jar.clear()
        for cookie in self._session.cookies.jar:
            jar.jar.set_cookie(cookie)

    # -- Send -----------------------------------------------------------------

    async def send(self, ctx: RequestState) -> httpx.Response:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, ctx)

    def _send_sync(self, ctx: RequestState) -> httpx.Response:
        kwargs: dict = dict(ctx.kwargs)
        stream_mode = bool(kwargs.pop("stream", False))

        use_default = kwargs.get("default_headers", self._config.impersonate.default_headers)
        if use_default:
            headers: dict = dict(kwargs.get("headers") or {})
        else:
            headers = dict(self._session_headers)
            headers.update(kwargs.get("headers") or {})
        if self._forced_user_agent:
            headers["User-Agent"] = self._forced_user_agent
        kwargs["headers"] = headers

        # Convert unified "proxy" string → requests-style dict for curl_cffi
        proxy_url: Optional[str] = kwargs.pop("proxy", None)
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}

        call = {k: kwargs[k] for k in _PASSTHROUGH if k in kwargs}
        call["headers"] = headers
        call.setdefault("verify", self._config.verify_ssl)
        call.setdefault("allow_redirects", True)
        if self._forced_impersonate:
            call["impersonate"] = self._forced_impersonate

        if stream_mode:
            # Acquire the lock before starting the request and keep it held
            # for the lifetime of the stream (_CurlStream.close releases it).
            # This prevents the session from being used for concurrent sends
            # while the body is still being consumed.
            self._lock.acquire()
            try:
                resp = self._session.request(ctx.method, ctx.url, stream=True, **call)  # pyright: ignore[reportArgumentType]
            except Exception as exc:
                self._lock.release()
                exc_str = str(exc).lower()
                if "proxy" in exc_str or "407" in exc_str:
                    raise ProxyTransportError(str(exc)) from exc
                if "ssl" in exc_str or "certificate" in exc_str:
                    raise SSLTransportError(str(exc)) from exc
                raise
            return adapt_curl_response_streaming(ctx.method, ctx.url, headers, resp, self._lock)

        try:
            with self._lock:
                resp = self._session.request(ctx.method, ctx.url, **call)  # pyright: ignore[reportArgumentType]
        except Exception as exc:
            exc_str = str(exc).lower()
            if "proxy" in exc_str or "407" in exc_str:
                raise ProxyTransportError(str(exc)) from exc
            if "ssl" in exc_str or "certificate" in exc_str:
                raise SSLTransportError(str(exc)) from exc
            raise

        return adapt_curl_response(ctx.method, ctx.url, headers, resp)

    # -- Lifecycle ------------------------------------------------------------

    def reset_session(self) -> None:
        """Close the current curl_cffi session and open a fresh one.

        Called after Tor NEWNYM so that subsequent requests open new TCP
        connections through the new circuit rather than reusing pooled ones.
        """
        with self._lock:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = self._new_session()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        self.close()
