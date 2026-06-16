"""curl_cffi-backed transport that impersonates a real browser's network fingerprint.

The default urllib3 transport has a fixed OpenSSL TLS ClientHello (JA3/JA4) and
only speaks HTTP/1.1, both of which Cloudflare fingerprints. This transport
routes requests through curl_cffi (curl-impersonate), which reproduces a real
Chrome/Firefox/Safari TLS *and* HTTP/2 fingerprint. It is opt-in via
``ScraperConfig.impersonate`` and requires the ``impersonate`` extra.
"""

from __future__ import annotations

import threading
from typing import Optional

import requests
from requests.structures import CaseInsensitiveDict

# Request kwargs we forward to curl_cffi. Anything else (hooks, requests-only
# internals) is dropped so the underlying call never sees an unknown argument.
_PASSTHROUGH = (
    "headers",
    "data",
    "json",
    "params",
    "files",
    "auth",
    "cookies",
    "timeout",
    "allow_redirects",
    "proxies",
    "verify",
    "cert",
)


class ImpersonateTransport:
    """Thin adapter exposing a ``requests``-compatible request over curl_cffi."""

    def __init__(self, target: str, verify_ssl: bool = True) -> None:
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError as exc:  # pragma: no cover - exercised via the extra
            raise ImportError(
                "ScraperConfig.impersonate requires the 'impersonate' extra: "
                "pip install lncrawl-scraper[impersonate]"
            ) from exc

        self.target = target
        self._verify = verify_ssl
        self._lock = threading.Lock()
        # target may be any curl-impersonate label (e.g. "chrome124"); curl_cffi
        # types it as a Literal, so the dynamic str is intentionally allowed.
        self._session = cffi_requests.Session(impersonate=target)  # pyright: ignore[reportArgumentType]

    @property
    def cookies(self):
        """The curl_cffi cookie jar (authoritative store when impersonating)."""
        return self._session.cookies

    def set_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookie(self, name: str, domain: str = "") -> None:
        try:
            self._session.cookies.delete(name, domain=domain or None)
        except Exception:
            pass

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        call = {k: kwargs[k] for k in _PASSTHROUGH if k in kwargs}
        call.setdefault("verify", self._verify)
        call.setdefault("allow_redirects", True)
        headers = kwargs.get("headers") or {}
        # curl_cffi is not thread-safe per Session; serialize access.
        with self._lock:
            resp = self._session.request(method, url, **call)  # pyright: ignore[reportArgumentType]
        return self._adapt(method, url, headers, resp)

    @staticmethod
    def _adapt(method: str, url: str, req_headers, resp) -> requests.Response:
        """Convert a curl_cffi response into a real ``requests.Response``."""
        out = requests.Response()
        out.status_code = resp.status_code
        out._content = resp.content
        out.url = str(resp.url)
        out.reason = getattr(resp, "reason", "") or ""
        out.encoding = getattr(resp, "encoding", None)
        out.headers = CaseInsensitiveDict(dict(resp.headers))

        # Preserve a minimal request record — some challenge handlers read
        # response.request.method when following challenge redirects.
        prepared = requests.PreparedRequest()
        prepared.method = method.upper()
        prepared.url = out.url
        prepared.headers = CaseInsensitiveDict(dict(req_headers or {}))
        out.request = prepared

        for cookie in resp.cookies.jar:
            out.cookies.set_cookie(cookie)
        return out


def build_transport(target: Optional[str], verify_ssl: bool) -> Optional[ImpersonateTransport]:
    """Return a transport for ``target`` (e.g. ``"chrome"``), or None when disabled."""
    if not target:
        return None
    return ImpersonateTransport(target, verify_ssl=verify_ssl)
