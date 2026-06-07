"""Exception hierarchy for the scraper.

These are part of the public API and live at the package root so that every
layer — the engine, its middleware, and the utilities — depends *up* on them
rather than reaching into private internals.
"""

from __future__ import annotations


class CloudflareException(Exception):
    """Base exception for all Cloudflare-related scraper errors."""


class AbortedException(CloudflareException):
    """Raised when a request is aborted via the abort signal."""


class CloudflareFirewallBlock(CloudflareException):
    """Raised when Cloudflare returns a 1020 (firewall block) response."""


class CloudflareChallengeError(CloudflareException):
    """Raised when a managed Cloudflare JS challenge is detected and no solver is
    configured to pass it."""


class CloudflareSolveError(CloudflareException):
    """Raised when a configured solver was run but failed to obtain clearance."""


class CloudflareCaptchaError(CloudflareException):
    """Raised when an interactive captcha challenge is detected and no solver is
    configured to pass it."""


class CloudflareTurnstileError(CloudflareException):
    """Raised when a Cloudflare Turnstile challenge is detected and no solver is
    configured to pass it."""
