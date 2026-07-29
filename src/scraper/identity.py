"""The emitted half of a request, treated as one indivisible thing.

Everything a detector reads at connection time — the TLS ClientHello, the HTTP/2
frame order, the header order, the User-Agent, the address the packets come from
— is one identity. The reason to model it as a unit rather than as independent
settings is that a solved clearance is bound to the combination, not to any one
part. Change the exit and keep the cookie and the cookie is dead. Change the
User-Agent and it is dead. Rotate a TLS profile mid-session and it is dead.

The classic failure this prevents is a rotating proxy: the solve lands on one
address, the next request leaves from another, the clearance is rejected, and the
scraper concludes the challenge solver is broken. Here it cannot happen, because
:class:`Clearance` records the identity it was earned under and refuses to be
replayed under any other.

One inversion is worth flagging, because it is the opposite of what a scraper
usually does. The User-Agent is not imposed on the transport; it is *taken from*
it. An impersonation profile already emits a complete, correctly ordered browser
header set, and writing a User-Agent of our own over it is how a client ends up
claiming to be one browser while its ClientHello says another. So the profile's
own value stands, and the field here is empty, until a real browser earns a
clearance — at which point the browser becomes the source of truth and its exact
User-Agent has to be reproduced, because that is what the clearance is bound to.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, replace
from typing import Dict, Mapping, Optional

_CHROME_VERSION = re.compile(r"(?:Chrome|CriOS|Chromium)/(\d+)")
_FIREFOX = re.compile(r"(?:Firefox|FxiOS)/(\d+)")

_PLATFORMS = (
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Linux", "Linux"),
)

# Headers an impersonation profile already sends. Overriding one of these
# replaces a value in place; adding anything else appends a header the profile
# never had, in a position no browser puts it, which is the exact signal the
# profile exists to avoid. The allow-list is the enforcement.
OVERRIDABLE = frozenset(
    {
        "user-agent",
        "accept-language",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
    }
)


def client_hints(user_agent: str) -> Dict[str, str]:
    """Derive ``Sec-CH-UA`` headers from *user_agent*.

    Chromium only — Firefox sends no hints at all, and inventing some for it is
    a contradiction a detector reads for free. Returns an empty mapping when the
    User-Agent is not Chromium, which is the correct set for those clients.
    """
    match = _CHROME_VERSION.search(user_agent or "")
    if not match:
        return {}
    major = match.group(1)
    brands = f'"Chromium";v="{major}", "Not;A=Brand";v="99", "Google Chrome";v="{major}"'
    mobile = "?1" if any(token in user_agent for token in ("Android", "iPhone", "iPad")) else "?0"
    platform = "Unknown"
    for token, name in _PLATFORMS:
        if token in user_agent:
            platform = name
            break
    return {
        "sec-ch-ua": brands,
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": f'"{platform}"',
    }


def browser_family(target: str) -> str:
    """The engine family behind an impersonation target such as ``chrome131``."""
    lowered = (target or "").lower()
    for family in ("chrome", "edge", "safari", "firefox"):
        if lowered.startswith(family):
            return family
    return "chrome"


@dataclass(frozen=True)
class Identity:
    """One coherent set of emitted signals, pinned to one exit.

    Args:
        impersonate: A curl-impersonate target. Prefer a bare family alias such
            as ``"chrome"``: it tracks the newest profile the installed build
            supports, and a pinned older profile is a detection signal in its own
            right, both because no real user runs a two-year-old browser and
            because it predates the post-quantum key share that current builds
            all send.
        exit_id: Opaque label for the address this identity leaves from. Any
            stable string; the pool that issues it decides the meaning.
        user_agent: Empty means "whatever the impersonation profile sends", which
            is the correct answer until a browser has earned a clearance.
        accept_language: Empty means the profile's own value.
    """

    impersonate: str = "chrome"
    exit_id: str = ""
    user_agent: str = ""
    accept_language: str = ""
    born_at: float = field(default_factory=time.time, compare=False)

    @property
    def family(self) -> str:
        return browser_family(self.impersonate)

    def token(self) -> str:
        """A stable label for exactly what a clearance is bound to.

        The three components are the address, the User-Agent and the TLS
        fingerprint. Nothing else belongs here: adding a field that a clearance
        does not actually depend on would invalidate cookies on changes the
        server never notices.
        """
        material = "\x1f".join((self.exit_id, self.user_agent, self.impersonate))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def pin(self, user_agent: str, *, accept_language: str = "") -> "Identity":
        """Return a copy that reproduces a specific browser's User-Agent.

        Called with what a real browser reported after solving a challenge. The
        resulting identity has a different :meth:`token`, which is correct: it is
        a different identity, and the clearance belongs to this one.
        """
        return replace(
            self,
            user_agent=user_agent,
            accept_language=accept_language or self.accept_language,
        )

    def on_exit(self, exit_id: str) -> "Identity":
        """Return a copy leaving from a different address."""
        return replace(self, exit_id=exit_id)

    def header_overrides(self) -> Dict[str, str]:
        """Headers to override on the impersonation profile.

        Empty in the common case. Populated only once a User-Agent is pinned, and
        then the client hints are re-derived from it: a pinned Chrome 140
        User-Agent alongside the profile's own ``sec-ch-ua`` for some other
        version is a self-contradiction, and the hint headers are the cheapest
        place for one to show up.
        """
        out: Dict[str, str] = {}
        if self.user_agent:
            out["user-agent"] = self.user_agent
            out.update(client_hints(self.user_agent))
        if self.accept_language:
            out["accept-language"] = self.accept_language
        return {key: value for key, value in out.items() if key in OVERRIDABLE}

    def describe(self) -> str:
        exit_label = self.exit_id or "direct"
        ua = self.user_agent.split(" ")[-1] if self.user_agent else "profile"
        return f"{self.impersonate}/{ua} via {exit_label}"


@dataclass(frozen=True)
class Clearance:
    """A challenge result, and the identity it is only valid under.

    Args:
        cookies: Everything the browser held for the origin after solving, not
            just the clearance cookie. The per-session cookie is set alongside it
            and dropping it makes the pair incomplete.
        identity_token: The :meth:`Identity.token` in force when it was earned.
        expires_at: UNIX seconds. Operators configure the passage duration, so
            this is what the browser reported or a conservative default — never
            an assumption that outlives the cookie.
    """

    origin: str
    cookies: Dict[str, str]
    identity_token: str
    user_agent: str = ""
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    @property
    def expired(self) -> bool:
        return self.expires_at > 0 and time.time() >= self.expires_at

    def usable_by(self, identity: Identity) -> bool:
        """Whether replaying this under *identity* can possibly work.

        A ``False`` here is the whole reason this class exists. Sending the
        cookie anyway produces a challenge, which reads as "the solver failed"
        rather than "the address changed underneath it", and the resulting retry
        loop re-solves forever on a fresh exit each time.
        """
        return not self.expired and identity.token() == self.identity_token

    def why_not(self, identity: Identity) -> str:
        """A one-line reason :meth:`usable_by` said no, for logs."""
        if self.expired:
            return "the clearance has expired"
        if identity.token() != self.identity_token:
            return "the identity changed since the clearance was earned"
        return ""


def merge_headers(
    profile_safe: Mapping[str, str], caller: Optional[Mapping[str, str]]
) -> Dict[str, str]:
    """Combine identity overrides with a caller's headers, caller winning.

    Callers legitimately need request-specific headers — ``Accept`` for a JSON
    endpoint, ``Referer`` for a navigation chain — so they are not filtered. What
    is filtered is the identity's own contribution, which stays inside
    :data:`OVERRIDABLE`, so the two cannot collectively rewrite a profile's
    header set.
    """
    out = {key.lower(): value for key, value in profile_safe.items() if key.lower() in OVERRIDABLE}
    for key, value in (caller or {}).items():
        if value is None:
            out.pop(key.lower(), None)
        else:
            out[key.lower()] = value
    return out
