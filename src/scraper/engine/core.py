"""The :class:`Engine` — a thin async runner over the middleware pipeline.

The engine owns the request collaborators (transport, state, stealth, proxy
manager, challenge handlers) and the ordered middleware chain. It exposes a
small surface the :class:`~scraper.Scraper` facade composes over: :meth:`request`,
:meth:`perform_request`, :meth:`abort`, :meth:`put_cookie`,
:meth:`apply_browser_clearance`, and :meth:`reset`.

The pipeline is **fully async** internally: all middleware and the transport
are coroutines. A persistent daemon :class:`asyncio.BaseEventLoop` runs in a
background thread. The synchronous :meth:`request` method bridges to it via
:func:`asyncio.run_coroutine_threadsafe`, and the returned
:class:`concurrent.futures.Future` is wired into the caller-supplied
:class:`~scraper.utils.cancel_token.CancelToken` so that per-request
cancellation stops the socket almost immediately.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING, List, Optional
from urllib.parse import urlparse

import httpx
from curl_cffi.requests.session import HttpMethod

from ..challenges import build_detector
from ..config import ScraperConfig
from ..exceptions import AbortedException
from .middleware import Middleware, build_chain
from .proxy_manager import ProxyManager
from .state import RequestState, SessionState
from .stealth import StealthMode
from .transport import Transport, build_transport
from .user_agent import build_ua_headers

if TYPE_CHECKING:
    from ..utils.cancel_token import CancelToken

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
    """Drives the async middleware pipeline over a pluggable :class:`Transport`.

    The request pipeline layers adaptive throttling, TLS cipher rotation,
    proxy injection, stealth headers, SSL auto-retry, 403/429 handling, and a
    challenge-handler registry on top of a browser-fingerprinted transport.
    All IO happens on a private asyncio event loop in a daemon thread; the
    public :meth:`request` and :meth:`perform_request` methods are synchronous
    bridges into that loop.
    """

    def __init__(self, config: Optional[ScraperConfig] = None) -> None:
        self.config = config or ScraperConfig()

        self.state = SessionState()
        self.cookies: httpx.Cookies = httpx.Cookies()
        self.headers = build_ua_headers(self.config)

        self.stealth = StealthMode(self.config.stealth)
        self.proxy_manager = ProxyManager(self.config.proxy)

        self.transport: Transport = build_transport(self.config)
        self.transport.bind_headers(self.headers)

        self.cf_detector = build_detector(self.config.cloudflare)
        self.middleware: List[Middleware] = []  # populated after async init

        # Global abort flag — set by abort(); new requests raise immediately.
        self._aborted = False

        # Start the persistent async event loop in a daemon thread.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="scraper-engine-loop", daemon=True
        )
        self._loop_thread.start()

        # Create async resources on the loop (e.g. asyncio.Semaphore).
        future = asyncio.run_coroutine_threadsafe(self._async_init(), self._loop)
        future.result(timeout=10)

        # Build the middleware chain after async init (ConcurrencyMiddleware
        # references self.slots which is set by _async_init).
        self.middleware = build_chain(self)

    # -- Event loop -----------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_init(self) -> None:
        self.slots = asyncio.Semaphore(max(1, self.config.max_concurrent_requests))

    # -- Request pipeline ---------------------------------------------------------

    def request(
        self,
        method: HttpMethod,
        url: str,
        *args: object,
        cancel_token: Optional["CancelToken"] = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Run *method url* through the full middleware pipeline (sync bridge).

        Args:
            method: HTTP method string.
            url: Target URL.
            *args: Forwarded to the middleware chain via ``RequestState.args``.
            cancel_token: Optional per-request :class:`~scraper.utils.cancel_token.CancelToken`.
                When ``token.cancel()`` is called the underlying asyncio task is
                cancelled and :exc:`~scraper.exceptions.AbortedException` is raised here.
            **kwargs: Forwarded to the middleware chain via ``RequestState.kwargs``.

        Returns:
            The :class:`httpx.Response` produced by the transport.

        Raises:
            AbortedException: When the engine is globally aborted or the
                ``cancel_token`` fires.
        """
        if self._aborted:
            raise AbortedException("Engine has been aborted.")

        ctx = RequestState(method=method, url=url, args=args, kwargs=dict(kwargs))
        future = asyncio.run_coroutine_threadsafe(self._run_pipeline(ctx), self._loop)

        if cancel_token is not None:
            cancel_token._bind_future(future, self._loop)

        try:
            return future.result()
        except concurrent.futures.CancelledError:
            raise AbortedException("Request cancelled via CancelToken.")

    async def _run_pipeline(self, ctx: RequestState) -> httpx.Response:
        """Entry point for the async pipeline (also called by retry middleware)."""
        if ctx.depth == 0:
            self.state.reset_403()
        return await self._run(ctx, 0)

    async def _run(self, ctx: RequestState, index: int) -> httpx.Response:
        if index >= len(self.middleware):
            return await self._transport_send(ctx)
        return await self.middleware[index].handle(ctx, lambda c: self._run(c, index + 1))

    async def _transport_send(self, ctx: RequestState) -> httpx.Response:
        response = await self.transport.send(ctx)
        self.transport.export_into(self.cookies)
        return response

    def perform_request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Raw HTTP request straight to the transport, bypassing the pipeline.

        Used by challenge handlers for the doubleDown bypass attempt and by
        :meth:`_refresh_session` for the priming request.
        """
        ctx = RequestState(method=method, url=url, kwargs=dict(kwargs))  # type:ignore
        if "proxy" not in ctx.kwargs:
            proxy = self.proxy_manager.get_proxy()
            if proxy:
                ctx.kwargs["proxy"] = proxy
        future = asyncio.run_coroutine_threadsafe(self._transport_send(ctx), self._loop)
        return future.result()

    # -- Session refresh ----------------------------------------------------------

    async def _refresh_session(self, url: str) -> bool:
        """Clear CF cookies, rotate the UA, and re-prime the origin."""
        try:
            for name in CF_COOKIE_NAMES:
                self.transport.clear_cookie(name)
                self.cookies.delete(name)
            loop = asyncio.get_event_loop()
            new_headers = await loop.run_in_executor(None, build_ua_headers, self.config)
            self.headers.update(new_headers)
            parsed = urlparse(url)
            ctx = RequestState(
                method="GET",
                url=f"{parsed.scheme}://{parsed.netloc}",
                kwargs={"timeout": 30},
            )
            resp = await self._transport_send(ctx)
            return resp.status_code in (200, 301, 302, 304)
        except Exception:
            return False

    # -- Public controls ----------------------------------------------------------

    def abort(self) -> None:
        """Signal all pending and in-progress requests to stop, then close."""
        self._aborted = True
        asyncio.run_coroutine_threadsafe(self._cancel_all_tasks(), self._loop)

    async def _cancel_all_tasks(self) -> None:
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on both the canonical jar and the transport's jar."""
        self.cookies.set(name, value, domain=domain)
        self.transport.put_cookie(name, value, domain=domain, path=path)

    def reset(self) -> None:
        """Clear all cookies (canonical + transport) and rebuild the session headers."""
        self.cookies = httpx.Cookies()
        self.transport.clear_all_cookies()
        self.headers.clear()
        self.headers.update(build_ua_headers(self.config))

    def rotate_proxy(self, disable_current: bool = False) -> None:
        """Rotate to the next proxy and reset the transport connection pool.

        For a :class:`~scraper.config.TorProxyUrl` with a ``control_port``,
        sends ``SIGNAL NEWNYM`` to obtain a new Tor exit circuit. For any
        other proxy type (or when NEWNYM fails), advances the round-robin
        index to the next configured proxy. In both cases the transport
        session is recreated so subsequent requests open fresh TCP connections
        through the new proxy or circuit.
        """
        self.proxy_manager.rotate(disable=disable_current)
        self.transport.reset_session()

    def apply_browser_clearance(
        self,
        domain: str,
        *,
        cf_clearance: Optional[str] = None,
        user_agent: Optional[str] = None,
        cookies: Optional[dict] = None,
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
            self.transport.force_user_agent(user_agent)
        jar = dict(cookies or {})
        if cf_clearance:
            jar["cf_clearance"] = cf_clearance
        for name, value in jar.items():
            self.put_cookie(name, value, domain=host)

    def close(self) -> None:
        """Release transport resources and stop the event loop."""

        async def _shutdown() -> None:
            await self.transport.aclose()

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=5)
        except Exception:
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5)
