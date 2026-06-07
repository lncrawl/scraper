"""CurlCffiTransport — the primary transport backed by curl_cffi.

The urllib3 transport has a fixed OpenSSL TLS ClientHello (JA3/JA4) and only
speaks HTTP/1.1, both of which Cloudflare fingerprints. curl_cffi
(curl-impersonate) reproduces a real Chrome/Firefox/Safari TLS *and* HTTP/2
fingerprint, so it is the default transport whenever an impersonation target is
configured and curl_cffi is installed.
"""

from __future__ import annotations

import re
import threading
from typing import Optional

import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from ...config import ScraperConfig
from ..context import RequestContext
from .base import Transport

# kwargs forwarded to curl_cffi — standard requests ones plus curl_cffi-specific
# fingerprint and transport overrides. Anything else is dropped so the underlying
# call never sees an unknown argument.
_PASSTHROUGH = (
    # standard requests kwargs
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
    "stream",
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


def adapt_curl_response(method: str, url: str, req_headers, resp) -> requests.Response:
    """Convert a curl_cffi response into a :class:`requests.Response`."""
    out = requests.Response()
    out.status_code = resp.status_code
    out._content = resp.content
    setattr(out, "_content_consumed", True)
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


class CurlCffiTransport(Transport):
    """A :class:`Transport` backed directly by ``curl_cffi.requests.Session``."""

    def __init__(self, config: ScraperConfig) -> None:
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError as exc:  # pragma: no cover - exercised via the extra
            raise ImportError(
                "CurlCffiTransport requires curl_cffi: pip install curl_cffi"
            ) from exc

        self._config = config
        cfg = config.impersonate
        self._lock = threading.Lock()
        self._forced_impersonate: Optional[str] = None
        # target may be any curl-impersonate label (e.g. "chrome124") and several
        # fingerprint options are typed as Literals by curl_cffi; our dynamic
        # str/int/TypedDict values are intentionally allowed, so the call is ignored.
        session_opts = {
            "impersonate": cfg.target,
            "http_version": cfg.http_version,
            "ja3": cfg.ja3,
            "akamai": cfg.akamai,
            "perk": cfg.perk,
            "extra_fp": cfg.extra_fp,
            "default_headers": cfg.default_headers,
            "trust_env": cfg.trust_env,
            "curl_options": cfg.curl_options or {},
            "verify": config.verify_ssl,
        }
        # Drop None-valued options so older curl_cffi versions that don't
        # recognise newer kwargs (e.g. "perk") don't raise TypeError.
        session_opts = {k: v for k, v in session_opts.items() if v is not None}
        self._session = cffi_requests.Session(**session_opts)  # pyright: ignore[reportArgumentType]

    # -- Fingerprint --------------------------------------------------------------

    def force_user_agent(self, user_agent: Optional[str]) -> None:
        """Pin the UA *and* align the impersonation target to it.

        Beyond the base UA pin, this also matches curl_cffi's impersonation to the
        browser family + major version in *user_agent*. cf_clearance is bound to the
        TLS (JA3) fingerprint as well as the UA, so aligning impersonation maximises
        the chance Cloudflare accepts a clearance solved by that browser.
        """
        super().force_user_agent(user_agent)
        self._forced_impersonate = self._match_impersonate(user_agent) if user_agent else None

    @staticmethod
    def _match_impersonate(user_agent: str) -> Optional[str]:
        """Return the curl_cffi impersonate label closest to *user_agent*, or None.

        Picks the highest supported version that does not exceed the browser's major
        version (falling back to the lowest available for older browsers).
        """
        try:
            from curl_cffi.requests.impersonate import BrowserType
        except Exception:  # pragma: no cover - curl_cffi always present in practice
            return None

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

        # Only plain "<family><digits>" labels (skip android/ios/beta variants).
        candidates = sorted(
            (int(m.group(1)), bt.value)
            for bt in BrowserType
            if (m := re.fullmatch(rf"{family}(\d+)", str(bt.value)))
        )
        if not candidates:
            return None
        best = next((label for ver, label in reversed(candidates) if ver <= want), None)
        return best or candidates[0][1]

    # -- Cookies ------------------------------------------------------------------

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookie(self, name: str, domain: str = "") -> None:
        try:
            self._session.cookies.delete(name, domain=domain or None)
        except Exception:
            pass

    def clear_all_cookies(self) -> None:
        self._session.cookies.clear()

    def export_into(self, jar: RequestsCookieJar) -> None:
        jar.clear()
        for cookie in self._session.cookies.jar:
            jar.set_cookie(cookie)

    # -- Send ---------------------------------------------------------------------

    def send(self, ctx: RequestContext) -> requests.Response:
        """Send via curl_cffi and adapt the response.

        When ``default_headers`` is True (the default) curl-impersonate injects the
        real browser User-Agent and Accept headers at the libcurl level. Merging our
        synthetic ``UserAgent`` headers on top would override those and degrade the
        fingerprint, so we forward only the per-request headers (Origin, Referer,
        custom user headers). When False, the caller wants full control, so we merge
        the session headers as a base.
        """
        kwargs: dict = dict(ctx.kwargs)
        use_default = kwargs.get("default_headers", self._config.impersonate.default_headers)
        if use_default:
            headers = dict(kwargs.get("headers") or {})
        else:
            headers = dict(self._session_headers)
            headers.update(kwargs.get("headers") or {})
        # A pinned UA (e.g. from a reused browser cf_clearance) must override the
        # impersonation default, since Cloudflare binds the clearance to that UA.
        if self._forced_user_agent:
            headers["User-Agent"] = self._forced_user_agent
        kwargs["headers"] = headers

        call = {k: kwargs[k] for k in _PASSTHROUGH if k in kwargs}
        call.setdefault("verify", self._config.verify_ssl)
        call.setdefault("allow_redirects", True)
        if self._forced_impersonate:
            call["impersonate"] = self._forced_impersonate

        with self._lock:
            resp = self._session.request(ctx.method, ctx.url, **call)  # pyright: ignore[reportArgumentType]
        return adapt_curl_response(ctx.method, ctx.url, headers, resp)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
