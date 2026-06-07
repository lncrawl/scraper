"""The :class:`Engine` — a thin runner over the middleware pipeline.

The engine owns the request collaborators (transport, state, stealth, proxy
manager, challenge handlers) and the ordered middleware chain. It exposes a small
surface the :class:`~scraper.Scraper` facade composes over: :meth:`request`,
:meth:`perform_request`, :meth:`abort`, :meth:`put_cookie`,
:meth:`apply_browser_clearance`, and :meth:`reset`.
"""

from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

import requests
from requests.cookies import RequestsCookieJar

from ..challenges import build_detector
from ..config import ScraperConfig
from ..utils import EventLock
from .context import RequestContext
from .middleware import build_chain
from .proxy_manager import ProxyManager
from .state import RequestChain, SessionState
from .stealth import StealthMode
from .transport import Transport, build_transport
from .user_agent import build_ua_headers

logger = logging.getLogger(__name__)


CF_COOKIE_NAMES = (
    "cf_clearance",
    "cf_chl_2",
    "cf_chl_prog",
    "cf_chl_rc_ni",
    "cf_turnstile",
    "__cf_bm",
)


class Engine:
    """Drives the middleware pipeline over a pluggable :class:`Transport`.

    The request pipeline layers adaptive throttling, TLS cipher rotation, session
    refresh, proxy injection, stealth headers, SSL auto-retry, 403 handling, and a
    challenge-handler registry on top of a browser-fingerprinted transport.
    """

    def __init__(self, config: ScraperConfig | None = None) -> None:
        self._local = threading.local()
        self.config = config or ScraperConfig()

        # Cross-thread abort signal. Keeping it public so callers can swap in a shared Event.
        self.signal = EventLock()

        self.state = SessionState()
        self.slots = threading.BoundedSemaphore(max(1, self.config.max_concurrent_requests))

        self.cookies: RequestsCookieJar = RequestsCookieJar()
        self.headers = build_ua_headers(self.config)

        self.stealth = StealthMode(self.config.stealth)
        self.proxy_manager = ProxyManager(self.config.proxy)

        self.transport: Transport = build_transport(self.config)
        self.transport.bind_headers(self.headers)

        self.cf_detector = build_detector(self.config.cloudflare)
        self.cf_solver = self.config.cloudflare.solver
        self.middleware = build_chain(self)

    # -- Per-thread chain state ---------------------------------------------------

    @property
    def chain(self) -> RequestChain:
        if not hasattr(self._local, "chain"):
            self._local.chain = RequestChain()
        return self._local.chain

    # -- Request pipeline ---------------------------------------------------------

    def request(self, method: str, url: str, *args, **kwargs) -> requests.Response:
        """Run *method url* through the full middleware pipeline."""
        chain = self.chain
        ctx = RequestContext(
            method=method,
            url=url,
            args=args,
            kwargs=kwargs,
            nested=chain.request_depth > 0,
        )
        chain.request_depth += 1
        try:
            return self._run(ctx, 0)
        finally:
            chain.request_depth -= 1

    def _run(self, ctx: RequestContext, index: int) -> requests.Response:
        if index >= len(self.middleware):
            return self._transport_send(ctx)
        return self.middleware[index].handle(ctx, lambda c: self._run(c, index + 1))

    def _transport_send(self, ctx: RequestContext) -> requests.Response:
        response = self.transport.send(ctx)
        self.transport.export_into(self.cookies)
        return response

    def perform_request(self, method: str, url: str, *args, **kwargs) -> requests.Response:
        """Raw HTTP request straight to the transport, bypassing the pipeline.

        Used by challenge handlers for the doubleDown bypass attempt.
        """
        return self._transport_send(
            RequestContext(method=method, url=url, args=args, kwargs=kwargs)
        )

    # -- Session ------------------------------------------------------------------

    def refresh_session(self, url: str) -> bool:
        """Drop Cloudflare cookies, reload the UA, and re-prime the origin."""
        try:
            for domain in list(self.cookies.list_domains()):
                for name in CF_COOKIE_NAMES:
                    try:
                        self.cookies.clear(domain, "/", name)
                    except KeyError:
                        pass
            for name in CF_COOKIE_NAMES:
                self.transport.clear_cookie(name)
            self.state.reset_session_clock()
            self.headers.update(build_ua_headers(self.config))
            parsed = urlparse(url)
            resp = self.perform_request("GET", f"{parsed.scheme}://{parsed.netloc}", timeout=30)
            return resp.status_code in (200, 301, 302, 304)
        except Exception:
            return False

    # -- Public controls ----------------------------------------------------------

    def abort(self) -> None:
        """Signal all pending and in-progress requests (incl. downloads) to stop."""
        self.signal.abort()

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on both the canonical jar and the transport's jar."""
        self.cookies.set(name, value, domain=domain, path=path)
        self.transport.put_cookie(name, value, domain=domain, path=path)

    def reset(self) -> None:
        """Clear all cookies (canonical + transport) and the session headers."""
        self.cookies.clear()
        self.transport.clear_all_cookies()
        self.headers.clear()

    def apply_browser_clearance(
        self,
        domain: str,
        *,
        cf_clearance: str | None = None,
        user_agent: str | None = None,
        cookies: dict | None = None,
    ) -> None:
        """Reuse a Cloudflare challenge solved by a real browser.

        Drive an external browser (e.g. ``nodriver``/Playwright) to pass the
        managed challenge or Turnstile, then hand the resulting ``cf_clearance``
        cookie and the browser's exact User-Agent here so this lightweight session
        can keep using the cleared session.

        Args:
            domain: Cookie domain or a URL to derive the host from.
            cf_clearance: The ``cf_clearance`` cookie value from the browser.
            user_agent: The browser's User-Agent. It MUST match the one used to
                obtain the clearance, or Cloudflare will reject the cookie.
            cookies: Any other cookies harvested from the browser (e.g.
                ``__cf_bm``), as a ``{name: value}`` mapping.
        """
        host = urlparse(domain).hostname or domain.split("/")[0]
        if user_agent:
            self.headers["User-Agent"] = user_agent
            # Pin it on the transport too: curl_cffi otherwise sends its own
            # impersonation UA, and Cloudflare binds cf_clearance to this exact UA.
            self.transport.force_user_agent(user_agent)
        jar = dict(cookies or {})
        if cf_clearance:
            jar["cf_clearance"] = cf_clearance
        for name, value in jar.items():
            self.put_cookie(name, value, domain=host)

    def close(self) -> None:
        """Release transport resources."""
        self.transport.close()
