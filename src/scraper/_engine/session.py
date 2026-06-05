from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from urllib.parse import urlparse

import requests

from .challenges import (
    ChallengeHandler,
    CloudflareV1Handler,
    CloudflareV2Handler,
    CloudflareV3Handler,
    TurnstileHandler,
)
from .config import BrowserConfig, ScraperConfig
from .exceptions import AbortedException, CloudflareLoopProtection
from .impersonate import ImpersonateTransport, build_transport
from .proxy_manager import ProxyManager
from .session_state import RequestChain, SessionState
from .stealth import StealthMode
from .tls import CipherRotator, CipherSuiteAdapter
from .user_agent import UserAgent

logger = logging.getLogger(__name__)

_CF_COOKIE_NAMES = (
    "cf_clearance",
    "cf_chl_2",
    "cf_chl_prog",
    "cf_chl_rc_ni",
    "cf_turnstile",
    "__cf_bm",
)


def _impersonate_family(target: str | None) -> str:
    """Map a curl-impersonate target (e.g. ``"chrome124"``) to a UA family."""
    return "firefox" if (target or "").lower().startswith("firefox") else "chrome"


class ScraperEngine(requests.Session):
    """A requests.Session subclass that transparently handles Cloudflare challenges.

    The request pipeline layers adaptive throttling, TLS cipher rotation, session
    refresh, proxy injection, stealth headers, SSL auto-retry, and a challenge-handler
    registry on top of a normal session.
    """

    def __init__(self, config: ScraperConfig | None = None) -> None:
        self.config = config or ScraperConfig()

        # Cross-thread abort signal — public so callers can swap in a shared Event.
        self.signal = threading.Event()

        self._local = threading.local()
        self._state = SessionState()
        self._slots = threading.BoundedSemaphore(max(1, self.config.max_concurrent_requests))

        # When impersonating, route requests through curl_cffi for a real
        # browser TLS/HTTP-2 fingerprint, and keep the spoofed UA family aligned
        # with the impersonation target so headers and TLS tell the same story.
        self._impersonate: ImpersonateTransport | None = build_transport(
            self.config.impersonate, self.config.verify_ssl
        )
        browser = self.config.browser
        if self._impersonate is not None:
            # The UA family MUST match the impersonation target, or the TLS
            # fingerprint and the User-Agent contradict each other. The target
            # wins over a configured browser family (a custom UA is respected).
            family = _impersonate_family(self.config.impersonate)
            if browser is None:
                browser = BrowserConfig(browser=family)
            elif isinstance(browser, BrowserConfig):
                if not browser.custom:
                    browser = replace(browser, browser=family)
            elif isinstance(browser, dict) and not browser.get("custom"):
                browser = {**browser, "browser": family}

        self.user_agent = UserAgent(
            allow_brotli=self.config.allow_brotli,
            browser=browser,
        )
        self._stealth = StealthMode(self.config.stealth)
        self.proxy_manager = ProxyManager(self.config.proxy)

        super().__init__()

        # swap in the browser-spoofing set.
        self.headers = self.user_agent.headers

        configured = self.config.cipher_suite
        self._cipher_suite = (
            ":".join(configured)
            if isinstance(configured, list)
            else (configured or ":".join(self.user_agent.cipher_suite))
        )
        self._cipher_rotator = CipherRotator(self.user_agent.cipher_suite)
        self._mount_tls_adapter()
        self._challenge_handlers = self._build_challenge_handlers()

    # -- Per-thread chain state ---------------------------------------------------

    @property
    def _chain(self) -> RequestChain:
        if not hasattr(self._local, "chain"):
            self._local.chain = RequestChain()
        return self._local.chain

    # -- Initialisation helpers ---------------------------------------------------

    def _build_challenge_handlers(self) -> list[ChallengeHandler]:
        cfg = self.config
        handlers: list[ChallengeHandler] = []
        if not cfg.disable_turnstile:
            handlers.append(TurnstileHandler())
        if not cfg.disable_v3:
            handlers.append(CloudflareV3Handler(debug=cfg.debug))
        if not cfg.disable_v2:
            handlers.append(CloudflareV2Handler(debug=cfg.debug))
        if not cfg.disable_v1:
            handlers.append(CloudflareV1Handler(debug=cfg.debug, double_down=cfg.double_down))
        return handlers

    # -- TLS ----------------------------------------------------------------------

    def _mount_tls_adapter(self) -> None:
        cfg = self.config
        self.mount(
            "https://",
            CipherSuiteAdapter(
                cipher_suite=self._cipher_suite,
                ecdh_curve=cfg.ecdh_curve,
                server_hostname=cfg.server_hostname,
                source_address=cfg.source_address,
                ssl_context=cfg.ssl_context,
                verify_ssl=cfg.verify_ssl,
            ),
        )

    def _rotate_tls_cipher_suite(self) -> None:
        suite = self._cipher_rotator.suite_for(self._state.next_cipher_rotation())
        if suite and suite != self._cipher_suite:
            self._cipher_suite = suite
            self._mount_tls_adapter()

    # -- Session ------------------------------------------------------------------

    def _refresh_session(self, url: str) -> bool:
        try:
            for domain in list(self.cookies.list_domains()):
                for name in _CF_COOKIE_NAMES:
                    try:
                        self.cookies.clear(domain, "/", name)
                    except Exception:
                        pass
            if self._impersonate is not None:
                for name in _CF_COOKIE_NAMES:
                    self._impersonate.clear_cookie(name)
            self._state.reset_session_clock()
            self.user_agent.load(allow_brotli=self.config.allow_brotli, browser=self.config.browser)
            self.headers.update(self.user_agent.headers)
            parsed = urlparse(url)
            resp = self.perform_request("GET", f"{parsed.scheme}://{parsed.netloc}", timeout=30)
            return resp.status_code in (200, 301, 302, 304)
        except Exception:
            return False

    # -- Request pipeline stages --------------------------------------------------

    def _throttle(self) -> None:
        delay = self._state.throttle_delay(
            self.config.min_request_interval_fast, self.config.min_request_interval
        )
        if delay > 0:
            logger.debug("Throttling: sleeping %.2fs", delay)
            time.sleep(delay)
        if self.signal.is_set():
            raise AbortedException("Request aborted before sending.")
        self._state.mark_request_sent()

    def _acquire_slot(self) -> None:
        while not self._slots.acquire(timeout=0.5):
            if self.signal.is_set():
                raise AbortedException("Request aborted while waiting for a concurrency slot.")

    def _release_slot(self) -> None:
        try:
            self._slots.release()
        except ValueError:
            pass

    def _apply_stealth(self, method: str, url: str, kwargs: dict) -> dict:
        if not self.config.stealth.enabled:
            return kwargs
        headers = dict(kwargs.pop("headers", {}) or {})
        return self._stealth.apply(
            method, url, cf_active=self._state.cf_active, headers=headers, **kwargs
        )

    def _send(self, method: str, url: str, args: tuple, kwargs: dict) -> requests.Response:
        """Issue the raw request with SSL auto-retry and proxy error handling."""
        if not self.config.verify_ssl:
            kwargs.setdefault("verify", False)
        try:
            response = self.perform_request(method, url, *args, **kwargs)
        except requests.exceptions.SSLError:
            if "/cdn-cgi/" in url or self._state.cf_active:
                raise
            logger.warning("SSL verification failed for %s — retrying without verification.", url)
            kwargs["verify"] = False
            response = self.perform_request(method, url, *args, **kwargs)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
            if kwargs.get("proxies"):
                self.proxy_manager.rotate_identity()
                try:
                    return self.perform_request(method, url, *args, **kwargs)
                except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
                    pass
                if self.config.proxy.fallback_to_direct:
                    logger.warning(
                        "Proxy still unavailable after rotation (%s) — retrying direct.",
                        type(exc).__name__,
                    )
                    kwargs.pop("proxies", None)
                    return self.perform_request(method, url, *args, **kwargs)
            raise
        return response

    def _dispatch_challenge(
        self, kwargs: dict, response: requests.Response
    ) -> requests.Response | None:
        for handler in self._challenge_handlers:
            if not handler.is_challenge(response):
                continue
            chain = self._chain
            if chain.solve_depth >= self.config.solve_depth:
                chain.solve_depth = 0
                raise CloudflareLoopProtection(
                    f"Loop protection triggered after {self.config.solve_depth} attempts."
                )
            chain.solve_depth += 1
            self._state.mark_cf_active()
            return handler.handle(
                response,
                request=self.request,
                perform_request=self.perform_request,
                **kwargs,
            )
        return None

    def _maybe_handle_403(
        self, method: str, url: str, args: tuple, kwargs: dict, response: requests.Response
    ) -> requests.Response | None:
        if response.status_code != 403:
            return None
        if not self._state.register_403(self.config.max_403_retries):
            return None
        if self.proxy_manager.has_proxy:
            self.proxy_manager.rotate_identity()
            return self.request(method, url, *args, **kwargs)
        if self.config.auto_refresh_on_403 and self._refresh_session(url):
            return self.request(method, url, *args, **kwargs)
        return None

    # -- Core request override ----------------------------------------------------

    def perform_request(self, method: str, url: str, *args, **kwargs) -> requests.Response:
        """Raw HTTP request, bypassing the challenge pipeline.

        Routed through the curl_cffi transport when impersonation is enabled,
        otherwise through the standard urllib3 transport.
        """
        if self._impersonate is not None:
            # Session headers aren't auto-merged by the curl_cffi transport, so
            # fold them under the per-request headers (request wins on conflict).
            merged = dict(self.headers)
            merged.update(kwargs.get("headers") or {})
            kwargs["headers"] = merged
            response = self._impersonate.request(method, url, **kwargs)
            self._mirror_transport_cookies()
            return response
        return super().request(method, url, *args, **kwargs)

    def _mirror_transport_cookies(self) -> None:
        """Reflect the curl_cffi cookie jar (authoritative) into self.cookies."""
        if self._impersonate is None:
            return
        self.cookies.clear()
        for cookie in self._impersonate.cookies.jar:
            self.cookies.set_cookie(cookie)

    def request(self, method: str, url: str, *args, **kwargs) -> requests.Response:  # type: ignore[override]
        if self.signal.is_set():
            raise AbortedException("Request aborted by signal.")

        # Challenge follow-ups and 403 retries recurse through request(); treat them
        # as part of the in-flight chain — skip throttling, rotation, refresh and the
        # concurrency slot (re-acquiring a held slot would deadlock at concurrency 1).
        chain = self._chain
        nested = chain.request_depth > 0
        if not nested:
            self._throttle()
            if self.config.rotate_tls_ciphers and self._impersonate is None:
                self._rotate_tls_cipher_suite()
            if self._state.needs_refresh(self.config.session_refresh_interval):
                self._refresh_session(url)
            self._acquire_slot()

        chain.request_depth += 1
        try:
            if not kwargs.get("proxies") and self.proxy_manager.has_proxy:
                kwargs["proxies"] = self.proxy_manager.get_proxy()
            kwargs = self._apply_stealth(method, url, kwargs)

            if self.config.pre_hook:
                method, url, args, kwargs = self.config.pre_hook(self, method, url, *args, **kwargs)

            response = self._send(method, url, args, kwargs)

            if self.config.post_hook:
                response = self.config.post_hook(self, response) or response

            handled = self._dispatch_challenge(kwargs, response)
            if handled is not None:
                return handled

            if not response.is_redirect and response.status_code not in (429, 503):
                chain.solve_depth = 0
                if response.status_code == 200:
                    self._state.reset_403()

            retried = self._maybe_handle_403(method, url, args, kwargs, response)
            if retried is not None:
                return retried

            return response
        finally:
            chain.request_depth -= 1
            if not nested:
                self._release_slot()

    # -- Public controls ----------------------------------------------------------

    def abort(self) -> None:
        """Signal all pending and in-progress requests (incl. streaming downloads) to stop."""
        self.signal.set()

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        """Set a cookie on this session (and the impersonation jar, if active)."""
        self.cookies.set(name, value, domain=domain, path=path)
        if self._impersonate is not None:
            self._impersonate.set_cookie(name, value, domain=domain, path=path)

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
        cookie and the browser's exact User-Agent here so this lightweight
        session can keep using the cleared session.

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
            self.user_agent.headers["User-Agent"] = user_agent
        jar = dict(cookies or {})
        if cf_clearance:
            jar["cf_clearance"] = cf_clearance
        for name, value in jar.items():
            self.put_cookie(name, value, domain=host)
