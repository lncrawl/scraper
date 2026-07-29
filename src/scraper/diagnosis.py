"""Turning a response into a statement about which layer is binding.

A status code alone is not a diagnosis. The same ``403`` can mean the address is
blocklisted, the transport profile is wrong, or a challenge is being served with
an error status; the remedies for those are unrelated, and two of the three are
made *worse* by the usual reflex of rotating the proxy. So the pipeline never
reacts to a status code directly. It reacts to a :class:`Diagnosis`.

Two cases here matter more than the rest, because a scraper that misreads them
degrades silently rather than loudly:

- **A ``200`` can be a challenge.** The interstitial is a normal page with normal
  status. Parsed as content it yields a successful-looking scrape of nothing.
- **A ``429`` is not a bad exit.** It says the address works and is being asked
  for too much. Treated as a block it retires a working exit, and the
  replacement is throttled just the same — the pacing was the problem.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from .layers import Layer, Trait, trait

# Body markers, matched case-insensitively against a bounded prefix of the page.
_CHALLENGE_MARKERS = (
    "__cf_chl_",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies to continue",
)
_TURNSTILE_MARKERS = (
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
)
_ACCESS_MARKERS = (
    "cloudflareaccess.com",
    "cf-access-domain",
)

# Cloudflare surfaces its own code in the page body of a block page.
_ERROR_CODE = re.compile(r"error\D{0,12}(\d{4})", re.IGNORECASE)

# Which layer each Cloudflare block code implicates. The distinction that earns
# its keep is 1010 against the rest: everything else here says "this address is
# unwelcome", while 1010 says the address is fine and the automation channel was
# detected — remedies that swap the exit do nothing for it.
_CF_CODES: Dict[int, Layer] = {
    1005: Layer.IP_REPUTATION,
    1006: Layer.IP_REPUTATION,
    1007: Layer.IP_REPUTATION,
    1008: Layer.IP_REPUTATION,
    1009: Layer.IP_REPUTATION,
    1010: Layer.CDP,
    1012: Layer.IP_REPUTATION,
    1015: Layer.BEHAVIOURAL,
    1020: Layer.IP_REPUTATION,
}

# User-Agent tokens that declare a crawler. The blocker that reads them acts on
# the declaration alone, so a 403 while presenting one of these is a diagnosis
# about the User-Agent and nothing else.
_DECLARED_CRAWLERS = (
    "gptbot",
    "ccbot",
    "claudebot",
    "anthropic-ai",
    "google-extended",
    "perplexitybot",
    "bytespider",
    "amazonbot",
    "applebot-extended",
    "meta-externalagent",
)

_BODY_PEEK = 64 * 1024


class Action(Enum):
    """What the pipeline should do next.

    ``ROTATE`` is deliberately the only action that discards identity, and the
    planner is allowed to veto it. Every other action preserves the identity
    bundle, because most remedies need it preserved to work at all.
    """

    ACCEPT = "accept"
    """The response is content. Nothing to do."""

    RETRY = "retry"
    """Transient. Same identity, same tier, same everything."""

    BACKOFF = "backoff"
    """Asked for too much. Slow down and keep the identity; it is not the problem."""

    SOLVE = "solve"
    """A challenge is being served. Needs a real browser once, then reuse."""

    ROTATE = "rotate"
    """This address is spent. A new one is the only thing that helps."""

    ESCALATE = "escalate"
    """The current tier cannot reach the binding layer. Try a stronger one."""

    REFUSE = "refuse"
    """No bypass exists. Stop."""


@dataclass(frozen=True)
class Diagnosis:
    """What went wrong, expressed as a layer plus what to do about it."""

    action: Action
    layer: Optional[Layer] = None
    detail: str = ""
    retry_after: Optional[float] = None
    """Seconds the server asked for, when it said so. Never guessed."""

    @property
    def ok(self) -> bool:
        return self.action is Action.ACCEPT

    @property
    def trait(self) -> Optional[Trait]:
        """What the binding layer reads, or ``None`` for a clean response."""
        return None if self.layer is None else trait(self.layer)

    def __str__(self) -> str:
        head = self.action.value if self.layer is None else f"{self.action.value} ({self.layer})"
        return f"{head}: {self.detail}" if self.detail else head


def _has(body: str, markers: Tuple[str, ...]) -> str:
    for marker in markers:
        if marker in body:
            return marker
    return ""


def _cf_code(body: str) -> Optional[int]:
    match = _ERROR_CODE.search(body)
    if not match:
        return None
    code = int(match.group(1))
    return code if code in _CF_CODES else None


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = (value or "").strip()
            break
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (moment - dt.datetime.now(dt.timezone.utc)).total_seconds())


def _lower_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {str(key).lower(): str(value or "") for key, value in headers.items()}


def diagnose(
    *,
    status: int,
    headers: Optional[Mapping[str, str]] = None,
    body: str = "",
    url: str = "",
    user_agent: str = "",
) -> Diagnosis:
    """Classify one response.

    Pure by design: it reads primitives, not a response object, so it can be
    exercised against recorded pages without a transport. *body* may be a
    prefix — only the first :data:`_BODY_PEEK` bytes are examined, since every
    marker of interest appears in the head of the document.
    """
    head = _lower_headers(headers or {})
    peek = (body or "")[:_BODY_PEEK].lower()

    mitigated = head.get("cf-mitigated", "")
    challenged = bool(mitigated == "challenge") or bool(_has(peek, _CHALLENGE_MARKERS))
    turnstile = _has(peek, _TURNSTILE_MARKERS)
    code = _cf_code(peek)

    # Authentication first: it is the one answer that is never worth a retry, and
    # a signature requirement is advertised the same way a password is.
    auth = head.get("www-authenticate", "").lower()
    if "signature" in auth:
        return Diagnosis(Action.REFUSE, Layer.WEB_BOT_AUTH, "the site requires a signed request")
    if status == 407:
        # Not a detection layer at all — the proxy in front of us rejected our
        # own credential. Reported as an authentication problem it would look
        # like the site needs a login, and the actual typo would never surface.
        return Diagnosis(Action.REFUSE, None, "the proxy rejected the credential (HTTP 407)")
    if status == 401 or _has(peek, _ACCESS_MARKERS):
        return Diagnosis(Action.REFUSE, Layer.ACCESS, f"authentication required (HTTP {status})")

    if status == 200 or 300 <= status < 400:
        if challenged:
            # The trap this exists for: a challenge interstitial is a normal page
            # with a normal status, and parsing it as content is a scrape that
            # reports success and collects nothing.
            return Diagnosis(
                Action.SOLVE,
                Layer.TURNSTILE if turnstile else Layer.MANAGED_CHALLENGE,
                "challenge served with a success status",
            )
        return Diagnosis(Action.ACCEPT)

    if status == 429 or code == 1015:
        return Diagnosis(
            Action.BACKOFF,
            Layer.BEHAVIOURAL,
            "rate limited",
            retry_after=_retry_after(head),
        )

    if status == 503:
        if challenged:
            return Diagnosis(Action.SOLVE, Layer.UNDER_ATTACK, "every visitor is being challenged")
        return Diagnosis(Action.RETRY, None, "origin unavailable", retry_after=_retry_after(head))

    if status == 403:
        if challenged:
            return Diagnosis(
                Action.SOLVE,
                Layer.TURNSTILE if turnstile else Layer.MANAGED_CHALLENGE,
                "challenge served with 403",
            )
        if code is not None:
            layer = _CF_CODES[code]
            action = Action.ROTATE if layer is Layer.IP_REPUTATION else Action.ESCALATE
            return Diagnosis(action, layer, f"Cloudflare error {code}")
        declared = _declared_crawler(user_agent)
        if declared:
            return Diagnosis(
                Action.ESCALATE,
                Layer.AI_BOT_BLOCKER,
                f"the User-Agent declares a crawler ({declared})",
            )
        if head.get("cf-ray") or "cloudflare" in head.get("server", ""):
            # Nothing distinguishes the scoring tiers from outside, so the
            # diagnosis names the strictest emit-only one. Recurrence after the
            # emit remedy is what promotes it to the composite model, and that
            # decision needs history, so it belongs to the planner.
            return Diagnosis(Action.ESCALATE, Layer.SUPER_BOT_FIGHT, "scored as automated")
        return Diagnosis(Action.ESCALATE, Layer.WORKERS, "forbidden by the origin")

    if status in (408, 502, 504, 520, 521, 522, 523, 524, 525, 526, 530):
        return Diagnosis(Action.RETRY, None, f"upstream error (HTTP {status})")

    if status >= 400:
        # A 404 is the site's answer about a path. It says nothing about the
        # address that asked, so attributing it to a layer would send a healthy
        # exit to be replaced over a typo in a URL.
        return Diagnosis(Action.ACCEPT, None, f"HTTP {status}")

    return Diagnosis(Action.ACCEPT)


def _declared_crawler(user_agent: str) -> str:
    lowered = (user_agent or "").lower()
    for token in _DECLARED_CRAWLERS:
        if token in lowered:
            return token
    return ""


def diagnose_transport(error: BaseException, *, through_proxy: bool) -> Diagnosis:
    """Classify a failure that never produced a response.

    Attribution is different with and without a proxy in the path. Through one, a
    connection that will not complete is evidence about the exit and nothing
    else, so the exit is what changes. Direct, the same error is about the
    network or the origin, and swapping anything client-side is superstition.
    """
    name = type(error).__name__
    if through_proxy:
        return Diagnosis(Action.ROTATE, Layer.IP_REPUTATION, f"exit unusable ({name})")
    return Diagnosis(Action.RETRY, None, f"transport failure ({name})")
