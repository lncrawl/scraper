"""UrllibTransport — the legacy urllib3/requests transport (fallback).

Wraps a private :class:`requests.Session`, mounts the
:class:`~scraper.engine.tls.CipherSuiteAdapter`, and owns TLS cipher rotation. Its
TLS ClientHello reads as Python (JA3/JA4), so it is the weaker fallback used when
curl_cffi is unavailable or impersonation is disabled.
"""

from __future__ import annotations

import requests
from requests.cookies import RequestsCookieJar

from ...config import ScraperConfig
from ..context import RequestContext
from ..tls import CipherRotator, CipherSuiteAdapter
from ..user_agent.data import CIPHER_SUITES
from .base import Transport


class UrllibTransport(Transport):
    """A :class:`Transport` backed by a standard :class:`requests.Session`."""

    def __init__(self, config: ScraperConfig) -> None:
        self._config = config
        self._session = requests.Session()

        pool: list[str] = []
        if config.browser.browser in CIPHER_SUITES:
            pool = CIPHER_SUITES[config.browser.browser]

        self._rotator = CipherRotator(pool)
        self._rotation = 0

        configured = config.cipher_suite
        self._cipher_suite = (
            ":".join(configured) if isinstance(configured, list) else (configured or ":".join(pool))
        )
        self._mount_adapter()

    # -- TLS ----------------------------------------------------------------------

    def _mount_adapter(self) -> None:
        cfg = self._config
        self._session.mount(
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

    def rotate_ciphers(self) -> None:
        self._rotation += 1
        suite = self._rotator.suite_for(self._rotation)
        if suite and suite != self._cipher_suite:
            self._cipher_suite = suite
            self._mount_adapter()

    # -- Headers ------------------------------------------------------------------

    def bind_headers(self, headers) -> None:
        # Share the engine's live header mapping so the requests.Session merges the
        # browser-spoofing headers (and any later refresh) automatically.
        self._session_headers = headers
        self._session.headers = headers  # type: ignore[assignment]

    # -- Cookies ------------------------------------------------------------------

    def put_cookie(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def clear_cookie(self, name: str, domain: str = "") -> None:
        domains = [domain] if domain else list(self._session.cookies.list_domains())
        for dom in domains:
            try:
                self._session.cookies.clear(dom, "/", name)
            except KeyError:
                pass

    def clear_all_cookies(self) -> None:
        self._session.cookies.clear()

    def export_into(self, jar: RequestsCookieJar) -> None:
        jar.clear()
        for cookie in self._session.cookies:
            jar.set_cookie(cookie)

    # -- Send / lifecycle ---------------------------------------------------------

    def send(self, ctx: RequestContext) -> requests.Response:
        return self._session.request(ctx.method, ctx.url, *ctx.args, **ctx.kwargs)

    def close(self) -> None:
        self._session.close()
