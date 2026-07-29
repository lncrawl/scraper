"""Getting bytes off the wire while emitting a browser's network signature.

Layers 2 to 5 — TLS fingerprint, post-quantum key share, HTTP/2 frame order,
header order — read different parts of the request, but they are one barrier: a
client built to reproduce one browser's network stack passes all four together,
and one that is not fails all four together. That is why there is a single
transport here rather than four settings, and why impersonation is the default
rather than an opt-in extra. An ordinary Python HTTP client is identified inside
the first round trip; making that the default and impersonation the special case
gets the common path exactly backwards.

Two things this module deliberately does *not* do, both of which the previous
design did:

**No cipher rotation.** Reordering the cipher list per request does not produce a
browser fingerprint, it produces an unstable one — and an unstable TLS
fingerprint invalidates any clearance bound to it, so the feature actively breaks
the layer above.

**No header randomisation.** Header *order* is read, not just header values. A
profile emits a complete, correctly ordered set; writing over it with a
hand-assembled dictionary is how a client ends up claiming to be Chrome with
Python's header order.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple, Type

import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from .exceptions import MissingDependency

logger = logging.getLogger(__name__)

# Request options forwarded to the underlying client. Anything else is dropped, so
# a caller passing a requests-only keyword gets it ignored rather than raising
# from inside the transport.
_FORWARDED = (
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

_VERSION_IN_TARGET = re.compile(r"(\d+)")

TRANSPORT_ERRORS: Tuple[Type[BaseException], ...] = (requests.RequestException, OSError)
"""Exception types that mean "no response arrived".

``OSError`` is not padding: curl_cffi's own ``RequestException`` derives from it, so
one entry covers both clients and any transport a caller supplies that raises
socket-level errors. Catching the union is what lets the pipeline attribute a dead
connection to a layer instead of letting it escape unhandled.
"""


def resolve_target(target: str) -> str:
    """The concrete impersonation profile *target* selects.

    A family alias such as ``"chrome"`` resolves to whatever the installed build
    considers current, which is the reason to prefer one.
    """
    try:
        from curl_cffi.requests.impersonate import REAL_TARGET_MAP
    except ImportError:
        return target
    return str(REAL_TARGET_MAP.get(target, target))


def newest_target(family: str) -> str:
    """The newest profile the installed build has for *family*."""
    return resolve_target(family)


def stale_profile_warning(target: str) -> str:
    """A warning if *target* pins an older profile than the build offers, else ``""``.

    Pinning is a detection signal in its own right, for two reasons that compound:
    no real user runs a two-year-old browser, and an older profile predates the
    post-quantum key share that current builds all send — so a client claiming to
    be current Chrome without one contradicts its own User-Agent. A family alias
    has neither problem.
    """
    from curl_cffi.requests.impersonate import REAL_TARGET_MAP

    if target in REAL_TARGET_MAP:
        return ""
    family = re.split(r"\d", target, maxsplit=1)[0].rstrip("_")
    current = REAL_TARGET_MAP.get(family)
    if not current:
        return ""
    pinned = _VERSION_IN_TARGET.search(target)
    latest = _VERSION_IN_TARGET.search(current)
    if not pinned or not latest or int(pinned.group(1)) >= int(latest.group(1)):
        return ""
    return (
        f"impersonation target {target!r} is older than the available {current!r}. "
        f"A stale profile is itself a signal: use the bare {family!r} alias so the "
        "fingerprint tracks the newest supported build."
    )


class Transport:
    """What the tiers call to make a request.

    Subclassed rather than duck-typed so the two implementations share the
    response adaptation and the cookie surface, which is where the fiddly parts
    are.
    """

    name = "transport"

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        raise NotImplementedError

    @contextmanager
    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        raise NotImplementedError
        yield  # pragma: no cover - unreachable, satisfies the generator contract

    @property
    def cookies(self) -> RequestsCookieJar:
        raise NotImplementedError

    def set_cookie(self, name: str, value: str, *, domain: str = "", path: str = "/") -> None:
        raise NotImplementedError

    def clear_cookies(self, domain: str = "") -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- shared helpers -------------------------------------------------------------

    @staticmethod
    def _call_kwargs(kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: kwargs[key] for key in _FORWARDED if key in kwargs}

    @staticmethod
    def _adapt(
        method: str,
        url: str,
        sent_headers: Mapping[str, str],
        raw: Any,
        *,
        buffered: bool = True,
    ) -> requests.Response:
        """Convert a foreign response object into a :class:`requests.Response`.

        *buffered* must be ``False`` on a streamed response. Reading ``raw.content``
        consumes the stream, so doing it here would leave the chunk iterator the caller
        is about to use with nothing in it.
        """
        out = requests.Response()
        out.status_code = int(getattr(raw, "status_code", 0))
        out.url = str(getattr(raw, "url", url))
        out.reason = getattr(raw, "reason", "") or ""
        out.encoding = getattr(raw, "encoding", None)
        out.headers = CaseInsensitiveDict(dict(getattr(raw, "headers", {}) or {}))
        if buffered:
            out._content = getattr(raw, "content", b"") or b""

        prepared = requests.PreparedRequest()
        prepared.method = method.upper()
        prepared.url = out.url
        # What actually went out, when the client will tell us. Diagnosis reads the
        # User-Agent from here to recognise a block that is really about a declared
        # crawler identity, and an impersonation profile supplies a User-Agent we
        # never wrote ourselves.
        actual = getattr(getattr(raw, "request", None), "headers", None)
        prepared.headers = CaseInsensitiveDict(dict(actual or sent_headers or {}))
        out.request = prepared

        jar = getattr(raw, "cookies", None)
        for cookie in getattr(jar, "jar", []) or []:
            out.cookies.set_cookie(cookie)
        return out


class ImpersonateTransport(Transport):
    """A curl-impersonate backed transport. The default, and the baseline.

    Args:
        target: Impersonation profile. Prefer a family alias — ``"chrome"``,
            ``"firefox"``, ``"safari"``, ``"edge"``, ``"chrome_android"`` — which
            tracks the newest build available.
        prefer_http3: Offer HTTP/3 where the origin advertises it, falling back to
            HTTP/2 otherwise. Current Chrome prefers it, so a client that only
            ever speaks HTTP/2 to an HTTP/3-enabled zone is a mild mismatch.
        verify: TLS verification. Turning it off is a debugging aid, not a
            bypass — nothing in the detection stack cares.
    """

    name = "impersonate"

    def __init__(
        self,
        target: str = "chrome",
        *,
        prefer_http3: bool = False,
        verify: bool = True,
    ) -> None:
        try:
            from curl_cffi import requests as cffi
        except ImportError as exc:  # pragma: no cover - a broken install, not a code path
            raise MissingDependency("impersonate", "the impersonation transport") from exc

        warning = stale_profile_warning(target)
        if warning:
            logger.warning("%s", warning)

        self.target = target
        self.resolved = resolve_target(target)
        self._verify = verify
        self._cookie_lock = threading.Lock()
        # Thread-local curl handles are curl_cffi's own answer to concurrent use,
        # so requests are not serialised here. Only the shared cookie jar is.
        self._session = cffi.Session(
            impersonate=target,  # pyright: ignore[reportArgumentType] - any curl target label
            use_thread_local_curl=True,
        )
        self._http_version: Optional[Any] = None
        if prefer_http3:
            from curl_cffi import CurlHttpVersion

            self._http_version = CurlHttpVersion.V3

    def _prepared(self, kwargs: Mapping[str, Any]) -> Dict[str, Any]:
        call = self._call_kwargs(kwargs)
        call.setdefault("verify", self._verify)
        call.setdefault("allow_redirects", True)
        if self._http_version is not None:
            call["http_version"] = self._http_version
        return call

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        call = self._prepared(kwargs)
        raw = self._session.request(method, url, **call)  # pyright: ignore[reportArgumentType]
        return self._adapt(method, url, call.get("headers") or {}, raw)

    @contextmanager
    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        call = self._prepared(kwargs)
        with self._session.stream(method, url, **call) as raw:  # pyright: ignore[reportArgumentType]
            response = self._adapt(method, url, call.get("headers") or {}, raw, buffered=False)
            yield response, raw.iter_content()

    @property
    def cookies(self) -> RequestsCookieJar:
        jar = RequestsCookieJar()
        for cookie in self._session.cookies.jar:
            jar.set_cookie(cookie)
        return jar

    def set_cookie(self, name: str, value: str, *, domain: str = "", path: str = "/") -> None:
        with self._cookie_lock:
            self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookies(self, domain: str = "") -> None:
        with self._cookie_lock:
            if not domain:
                self._session.cookies.clear()
                return
            for cookie in list(self._session.cookies.jar):
                if cookie.domain and domain.endswith(cookie.domain.lstrip(".")):
                    try:
                        self._session.cookies.delete(cookie.name, domain=cookie.domain)
                    except Exception:  # noqa: BLE001 - jar implementations differ
                        pass

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 - closing must never raise
            pass


class PlainTransport(Transport):
    """An ordinary :mod:`requests` transport.

    Provided for unprotected hosts, localhost, and tests. It is not a fallback for
    a protected site: its ClientHello reads as Python and it speaks HTTP/1.1, so
    it fails the whole transport group at once. Using it against a scored zone is
    not a degraded outcome, it is no outcome.
    """

    name = "plain"

    def __init__(self, *, verify: bool = True) -> None:
        self._session = requests.Session()
        self._verify = verify

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        call = self._call_kwargs(kwargs)
        call.setdefault("verify", self._verify)
        call.setdefault("allow_redirects", True)
        return self._session.request(method, url, **call)

    @contextmanager
    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        call = self._call_kwargs(kwargs)
        call.setdefault("verify", self._verify)
        call["stream"] = True
        response = self._session.request(method, url, **call)
        try:
            yield response, response.iter_content(chunk_size=64 * 1024)
        finally:
            response.close()

    @property
    def cookies(self) -> RequestsCookieJar:
        return self._session.cookies

    def set_cookie(self, name: str, value: str, *, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookies(self, domain: str = "") -> None:
        if not domain:
            self._session.cookies.clear()
            return
        for stored in list(self._session.cookies.list_domains()):
            if domain.endswith(stored.lstrip(".")):
                self._session.cookies.clear(stored)

    def close(self) -> None:
        self._session.close()
