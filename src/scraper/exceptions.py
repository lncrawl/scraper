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


class CloudflareLoopProtection(CloudflareException):
    """Raised when the challenge-solve recursion depth limit is exceeded."""


class CloudflareFirewallBlock(CloudflareException):
    """Raised when Cloudflare returns a 1020 (firewall block) response."""


class CloudflareChallengeError(CloudflareException):
    """Raised when an unsupported Cloudflare challenge version is detected."""


class CloudflareSolveError(CloudflareException):
    """Raised when the challenge answer is rejected by Cloudflare."""


class CloudflareCaptchaError(CloudflareException):
    """Raised when a captcha-only CF challenge is detected (no provider available)."""


class CloudflareTurnstileError(CloudflareException):
    """Raised when a Cloudflare Turnstile challenge is detected (requires a captcha provider)."""
