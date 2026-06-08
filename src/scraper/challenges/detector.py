"""Cloudflare challenge detection.

Cloudflare's modern challenges (managed challenge / Turnstile) cannot be solved
in pure Python — even a perfect emulation of the challenge JS fails CF's TLS,
HTTP/2, canvas and behavioural cross-checks. This module therefore only
*classifies* a response into a :class:`CloudflareChallengeKind`; passing it is
delegated to a :class:`~scraper.config.ClearanceSolver` (a real browser, in
process or remote) or to a clear, actionable exception.
"""

from __future__ import annotations

import enum
import logging
import re
from typing import NoReturn

from httpx import Response, ResponseNotRead

from ..exceptions import (
    CloudflareCaptchaError,
    CloudflareChallengeError,
    CloudflareFirewallBlock,
    CloudflareTurnstileError,
)

logger = logging.getLogger(__name__)

_CHALLENGE_STATUSES = (403, 429, 503)

# Compiled once — detection runs on every response.
_FIREWALL_1020 = re.compile(r'cf-error-code">\s*1020', re.S)
_TURNSTILE = re.compile(
    r'class="cf-turnstile"'
    r"|challenges\.cloudflare\.com/turnstile/v0"
    r'|data-sitekey="[0-9A-Za-z_-]{10,}"',
    re.S,
)
_CAPTCHA_TRACE = re.compile(r"/cdn-cgi/images/trace/(?:captcha|managed)/", re.S)
_CHALLENGE_FORM = re.compile(r'id="challenge-form"', re.S)
_MANAGED = re.compile(
    r"window\._cf_chl_opt"
    r"|/cdn-cgi/challenge-platform/\S+orchestrate/"
    r"|__cf_chl_[a-z]*tk="
    r"|/cdn-cgi/images/trace/jsch/",
    re.S,
)

_SOLVER_HINT = (
    "Set ScraperConfig.cloudflare.solver (RemoteSolver or BrowserSolver) to "
    "solve it automatically, or pass a cf_clearance solved in a real browser via "
    "Scraper.apply_browser_clearance()."
)


class CloudflareChallengeKind(enum.Enum):
    """The kind of Cloudflare interstitial detected on a response."""

    NONE = "none"
    FIREWALL_BLOCK = "firewall_block"
    TURNSTILE = "turnstile"
    CAPTCHA = "captcha"
    MANAGED = "managed"


class CloudflareDetector:
    """Classifies responses as Cloudflare challenges and maps them to exceptions."""

    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug

    def classify(self, response: Response) -> CloudflareChallengeKind:
        """Return the :class:`CloudflareChallengeKind` for *response* (pure, no I/O)."""
        try:
            status = response.status_code

            if status not in _CHALLENGE_STATUSES:
                return CloudflareChallengeKind.NONE

            text = response.text

            if status == 403 and _FIREWALL_1020.search(text):
                return CloudflareChallengeKind.FIREWALL_BLOCK

            if _TURNSTILE.search(text):
                return CloudflareChallengeKind.TURNSTILE

            if _CAPTCHA_TRACE.search(text) and _CHALLENGE_FORM.search(text):
                return CloudflareChallengeKind.CAPTCHA

            if _MANAGED.search(text):
                return CloudflareChallengeKind.MANAGED

            return CloudflareChallengeKind.NONE
        except (AttributeError, ResponseNotRead):
            return CloudflareChallengeKind.NONE

    def raise_for(self, kind: CloudflareChallengeKind, response: Response) -> NoReturn:
        """Raise the exception mapped to *kind* with an actionable message."""
        if self.debug:
            logger.debug(f"CloudflareDetector: {kind.name!r} at {response.url!r}")

        if kind is CloudflareChallengeKind.FIREWALL_BLOCK:
            raise CloudflareFirewallBlock(
                "Cloudflare firewall blocked this request (error 1020). "
                "The IP/UA is banned by a WAF rule; a challenge solver cannot bypass it."
            )
        if kind is CloudflareChallengeKind.TURNSTILE:
            raise CloudflareTurnstileError(
                f"Cloudflare Turnstile challenge detected. {_SOLVER_HINT}"
            )
        if kind is CloudflareChallengeKind.CAPTCHA:
            raise CloudflareCaptchaError(
                f"Cloudflare interactive captcha challenge detected. {_SOLVER_HINT}"
            )
        if kind is CloudflareChallengeKind.MANAGED:
            raise CloudflareChallengeError(
                f"Cloudflare managed JS challenge detected. {_SOLVER_HINT}"
            )
        raise CloudflareChallengeError(f"Cloudflare challenge detected. {_SOLVER_HINT}")
