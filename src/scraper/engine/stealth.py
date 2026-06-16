from __future__ import annotations

import logging
import random
from collections import OrderedDict
from typing import Any, Dict

from ..config import StealthConfig

logger = logging.getLogger(__name__)

_CHROME_QUIRKS: Dict[str, Any] = {
    "order": [
        "Host",
        "Connection",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "User-Agent",
        "Accept",
        "Sec-Fetch-Site",
        "Sec-Fetch-Mode",
        "Sec-Fetch-User",
        "Sec-Fetch-Dest",
        "Referer",
        "Accept-Encoding",
        "Accept-Language",
        "Cookie",
    ],
    "headers": {
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Language": "en-US,en;q=0.9",
    },
}

_FIREFOX_QUIRKS: Dict[str, Any] = {
    "order": [
        "Host",
        "User-Agent",
        "Accept",
        "Accept-Language",
        "Accept-Encoding",
        "Connection",
        "Upgrade-Insecure-Requests",
        "Referer",
        "Cookie",
    ],
    "headers": {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    },
}

_ACCEPTS = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
]

_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-CA,en;q=0.9,en-US;q=0.8",
    "en-AU,en;q=0.9,en-US;q=0.8",
]


class StealthMode:
    """Stateless stealth helper — no back-reference to the scraper engine.

    Receives only what it needs at call time via :meth:`apply` and
    :meth:`compute_delay`.
    """

    def __init__(self, config: StealthConfig) -> None:
        self._config = config
        self._request_count = 0
        self._dnt: str | None = random.choice(["1", None])

    def compute_delay(self, cf_active: bool) -> float:
        """Return the next human-like delay in seconds (does not sleep).

        Returns 0.0 when delays are disabled or for the very first request.
        The caller (:class:`~scraper.engine.middleware.stealth.StealthMiddleware`)
        is responsible for ``await asyncio.sleep(delay)``.
        """
        if not self._config.human_like_delays or self._request_count == 0:
            return 0.0
        if cf_active:
            delay = random.uniform(self._config.min_delay, self._config.max_delay)
            if random.random() < 0.1:
                delay = min(delay * 1.5, 10.0)
        else:
            delay = random.uniform(self._config.min_delay_fast, self._config.max_delay_fast)
        return delay if delay >= 0.05 else 0.0

    def apply(
        self,
        method: str,
        url: str,
        *,
        cf_active: bool = False,
        headers: dict | None = None,
        **kwargs: Any,
    ) -> dict:
        """Apply header tweaks and return updated kwargs.

        Does **not** sleep; the caller must call :meth:`compute_delay` and
        ``await asyncio.sleep(delay)`` separately.

        Args:
            method: HTTP method (reserved for future per-method logic).
            url: Request URL (reserved for future per-host logic).
            cf_active: Whether Cloudflare protection has been detected.
            headers: Current request headers dict (merged into kwargs['headers']).
            **kwargs: Remaining request kwargs passed through.
        """
        headers = dict(headers or {})

        if self._config.randomize_headers:
            headers = self._randomize_headers(headers)

        if self._config.browser_quirks:
            headers = self._apply_browser_quirks(headers)

        self._request_count += 1
        kwargs["headers"] = headers
        return kwargs

    def _randomize_headers(self, headers: dict) -> dict:
        if "Accept" not in headers:
            headers["Accept"] = random.choice(_ACCEPTS)
        if "Accept-Language" not in headers:
            headers["Accept-Language"] = random.choice(_LANGUAGES)
        if self._dnt:
            headers.setdefault("DNT", self._dnt)
        return headers

    def _apply_browser_quirks(self, headers: dict) -> dict:
        ua = headers.get("User-Agent", "")
        quirks = _FIREFOX_QUIRKS if "Firefox/" in ua else _CHROME_QUIRKS

        for header, value in quirks["headers"].items():
            headers.setdefault(header, value)

        ordered: dict = OrderedDict()
        for name in quirks["order"]:
            if name in headers:
                ordered[name] = headers[name]
        for name, value in headers.items():
            if name not in ordered:
                ordered[name] = value

        return ordered
