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
from typing import Dict, Mapping, NamedTuple, Optional, Tuple

from .layers import Layer, Trait, trait

# Body markers, matched case-insensitively against a bounded prefix of the page.
#
# The `/h/` is load-bearing and was measured, not guessed. Cloudflare injects a
# JavaScript-Detections script from `/cdn-cgi/challenge-platform/scripts/jsd/…` into
# ordinary successful pages, so the bare prefix fires on content: across a sample of
# real hosts it appeared on 9 of 10 pages that were served normally. The `/h/`
# orchestrate path belongs to an actual interstitial and appeared on none of them.
#
# Getting this wrong is expensive in the direction that matters least visibly: a page
# that arrived fine is reported as a challenge, so the caller pays for a browser launch
# it did not need, or gives up on a page it already had.
_CHALLENGE_MARKERS = (
    "__cf_chl_",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/h/",
)

# The interstitial's own visible copy, held apart from the markers above because unlike
# them it is ordinary English and content can contain it. Found live: a novel chapter
# whose prose ran "wait just a moment longer" was diagnosed as a challenge, so the
# retrieval re-solved and re-fetched the page it already had until the attempt budget was
# gone — five browser launches and a dropped chapter, reproducibly, for every chapter
# unlucky enough to contain the phrase. The rest of the book downloaded fine, which is
# what made it read as an intermittent browser fault.
#
# So this copy counts only where an interstitial puts it and content does not: in the
# <title>, and only of a document small enough to have no page on it. The title alone is
# not enough on this corpus — a novel site puts the chapter title in <title>, so a
# chapter actually called "Just a Moment" would match.
_CHALLENGE_COPY = (
    "checking your browser",
    "just a moment",
    "enable javascript and cookies to continue",
)

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_INTERSTITIAL_BYTES = 32 * 1024
"""How big a document may be and still be an interstitial.

A bound rather than a measurement: a challenge page is a standalone document with no site
content on it — a few kilobytes plus an inline script — and this leaves it several times
that much room while staying well under any real page carrying a chapter of a novel.
"""
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

# Codes that are not about the visitor at all. The zone or its origin is
# misconfigured, or the request never reached a policy decision — so there is no layer
# to attribute and nothing a stronger tier or a new address would change. Read as a
# block, each of these retires a healthy exit and writes a verdict to the origin's
# profile that outlives the misconfiguration behind it.
_CF_ZONE_CODES: Dict[int, str] = {
    1000: "the zone's DNS points at a prohibited address",
    1001: "the zone's DNS did not resolve",
    1002: "the zone's DNS points at a restricted address",
    1003: "direct access by IP address is not allowed",
    1004: "the host is not configured to serve web traffic",
    1013: "the Host header and the TLS SNI name disagree",
    1016: "the origin's DNS did not resolve",
    1018: "the zone could not be found",
    1023: "the zone could not be found",
}

# The operator's own edge code failing, which is the only honest signal for layer 15
# there is: everything else that reaches it does so by elimination. A crash is not a
# refusal, so this retries rather than escalating — no browser and no address changes
# the outcome of a script that threw.
_CF_WORKER_CODES: Dict[int, str] = {
    1101: "the site's edge code threw an exception",
    1102: "the site's edge code exceeded its resource limits",
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


class _Vendor(NamedTuple):
    """One mitigation product, and what a refusal from it means.

    Args:
        layer: What to attribute a refusal to, or ``None`` when the product is an
            *edge* rather than a verdict. A CloudFront or Fastly header is on every
            response including successful ones, so its presence says who answered and
            nothing about why — reading it as a detection layer would attribute an
            operator's own rule to a bot check.
        values: ``(header, substring)`` pairs. Matched on the value because several
            products announce themselves in a shared header rather than their own.
        cookies: Fragments of a cookie name, matched against ``set-cookie``.
        challenge: Markers that mean this product is *serving* an interstitial rather
            than having refused. These are the expensive ones to miss: they arrive with
            a 200, so a caller parses a captcha page and records a successful scrape.
    """

    name: str
    layer: Optional[Layer]
    headers: Tuple[str, ...] = ()
    values: Tuple[Tuple[str, str], ...] = ()
    cookies: Tuple[str, ...] = ()
    body: Tuple[str, ...] = ()
    challenge: Tuple[str, ...] = ()


# Ordered most specific first: a dedicated bot manager's own header beats a CDN header
# that is present on every response the same site serves.
#
# Two layers do the work here. A product whose verdict is a per-session model over the
# whole request is layer 14, whose stance is *delegate* — not defeatism, but the honest
# reading: no header profile changes it, and where a browser would help, the challenge
# markers below route there first and the planner promotes on recurrence. A WAF or an
# edge acting on coarser rules is layer 12, whose stance is *satisfy*, so the ladder
# still tries the tier that supplies a better profile.
_VENDORS: Tuple[_Vendor, ...] = (
    _Vendor(
        "DataDome",
        Layer.BOT_MANAGEMENT,
        headers=("x-datadome", "x-datadome-cid"),
        cookies=("datadome",),
        body=("datadome",),
        challenge=("geo.captcha-delivery.com", "captcha-delivery.com"),
    ),
    _Vendor(
        "Kasada",
        Layer.BOT_MANAGEMENT,
        headers=("x-kpsdk-ct", "x-kpsdk-cd", "x-kpsdk-r", "x-kpsdk-v"),
        body=("kpsdk",),
    ),
    _Vendor(
        "PerimeterX",
        Layer.BOT_MANAGEMENT,
        headers=("x-px-authorization", "x-px-block"),
        cookies=("_px", "_pxhd", "_pxvid"),
        body=("perimeterx", "px-captcha"),
        challenge=("px-captcha", "press &amp; hold", "press & hold"),
    ),
    _Vendor(
        "Akamai Bot Manager",
        Layer.BOT_MANAGEMENT,
        headers=("akamai-grn", "x-akamai-transformed"),
        values=(("server", "akamaighost"),),
        cookies=("_abck", "ak_bmsc", "bm_sz", "bm_sv"),
    ),
    _Vendor(
        "Imperva",
        Layer.BOT_MANAGEMENT,
        headers=("x-iinfo",),
        values=(("x-cdn", "incapsula"),),
        cookies=("visid_incap_", "incap_ses_", "nlbi_"),
        body=("_incapsula_resource", "pardon our interruption"),
        challenge=("_incapsula_resource",),
    ),
    _Vendor(
        "DDoS-Guard",
        Layer.SUPER_BOT_FIGHT,
        values=(("server", "ddos-guard"),),
        cookies=("__ddg1_", "__ddg2_", "__ddg5_", "__ddgid_"),
        body=("ddos-guard",),
        challenge=("/.well-known/ddos-guard/id/", "ddos-guard.net/link/"),
    ),
    _Vendor(
        "Sucuri",
        Layer.SUPER_BOT_FIGHT,
        headers=("x-sucuri-id", "x-sucuri-cache"),
        values=(("server", "sucuri"),),
        body=("sucuri website firewall", "sucuri_cloudproxy"),
        challenge=("sucuri_cloudproxy_js",),
    ),
    _Vendor(
        "AWS WAF",
        Layer.SUPER_BOT_FIGHT,
        headers=("x-amzn-waf-action",),
        cookies=("aws-waf-token",),
        body=("awswaf",),
        challenge=("awswaf.com/challenge", "awswaf.com/captcha"),
    ),
    _Vendor(
        "F5 BIG-IP",
        Layer.SUPER_BOT_FIGHT,
        cookies=("bigipserver", "ts01"),
        body=("the requested url was rejected",),
    ),
    _Vendor(
        "CloudFront",
        None,
        headers=("x-amz-cf-id",),
        values=(("server", "cloudfront"),),
        body=("generated by cloudfront",),
    ),
    _Vendor(
        "Fastly",
        None,
        headers=("x-fastly-request-id",),
        values=(("server", "fastly"), ("x-served-by", "cache-")),
        body=("fastly error:",),
    ),
)

# A captcha widget, from whoever. Deliberately not a vendor of its own and deliberately
# not read on a 2xx: ordinary login and comment forms carry one of these, and treating
# that as an interstitial would launch a browser on content that had already arrived.
# On a refusal it is unambiguous — nobody serves a login form with a 403.
_CAPTCHA_MARKERS = (
    "hcaptcha.com/1/api.js",
    "google.com/recaptcha/api.js",
    "www.google.com/recaptcha",
    "recaptcha/api2/anchor",
)

_VENDOR_REFUSALS = frozenset({400, 405, 406, 409})
"""Non-403 statuses a mitigation product uses to refuse. 404 is deliberately absent: a
404 behind a bot manager is still the site's answer about a path."""

_BROWSER_ENGINES = ("gecko", "webkit", "chrome", "safari", "firefox", "edg", "trident")
"""Engine tokens a real browser's User-Agent carries alongside the ``Mozilla/5.0``
prefix. Every automation library that impersonates one copies these; the ones that do
not impersonate anything carry none of them."""

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

    FOLLOW = "follow"
    """The page is a redirect expressed in JavaScript. Fetch what it points at."""


@dataclass(frozen=True)
class Diagnosis:
    """What went wrong, expressed as a layer plus what to do about it."""

    action: Action
    layer: Optional[Layer] = None
    detail: str = ""
    retry_after: Optional[float] = None
    """Seconds the server asked for, when it said so. Never guessed."""
    location: str = ""
    """Where ``FOLLOW`` should go. Possibly relative; resolve against the request."""

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


_JS_REDIRECT = re.compile(
    r"""window\.location(?:\.replace|\.assign|\.href\s*=|\s*=)\s*\(?\s*['"]([^'"\s]+)['"]""",
    re.IGNORECASE,
)
_DOC_END = re.compile(r"</html\s*>", re.IGNORECASE)
_MARKUP = re.compile(r"<(script|style|noscript)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)

_STUB_BYTES = 4_000
_STUB_TEXT = 400


def js_redirect(body: str) -> str:
    """Where a JavaScript-only redirect page points, or ``""``.

    A family of bot checks answers the first request with a few hundred bytes whose
    entire content is ``window.location.replace('…?token=…')``. It arrives as ``200``
    with no challenge marker and no Cloudflare header, so everything else here reads
    it as a normal page — and a caller then parses an empty document and records a
    successful scrape. On the source corpus this was 19% of hosts, which made it a
    larger hole than any layer this module knew about.

    The gate is that the document must be *entirely* this and nothing else, because
    ``window.location`` appears in plenty of real pages. Three conditions together:
    the closing tag is present, so the whole document has been seen and its length is
    known rather than guessed from a truncated prefix; the document is tiny; and it
    renders no text. A real page fails at least one.

    Worth doing without a browser precisely because the destination is *emitted* in
    the HTML. Running the script would produce the same URL that is already sitting
    there in plain text, so this is a layer that looks like it needs a JavaScript
    runtime and does not.
    """
    if not _DOC_END.search(body) or len(body) >= _STUB_BYTES:
        return ""
    found = _JS_REDIRECT.search(body)
    if not found:
        return ""
    text = " ".join(_MARKUP.sub(" ", body).split())
    if len(text) >= _STUB_TEXT:
        return ""
    return found.group(1)


def _identify(head: Mapping[str, str], peek: str) -> Optional[_Vendor]:
    """Which mitigation product answered, if one announced itself."""
    cookies = head.get("set-cookie", "").lower()
    for vendor in _VENDORS:
        if any(name in head for name in vendor.headers):
            return vendor
        if any(token in head.get(name, "") for name, token in vendor.values):
            return vendor
        if any(fragment in cookies for fragment in vendor.cookies):
            return vendor
        if _has(peek, vendor.body):
            return vendor
    return None


def edge(
    headers: Optional[Mapping[str, str]] = None,
    body: str = "",
) -> str:
    """The name of the mitigation product or CDN that answered, or ``""``.

    Public because it is useful without a diagnosis — an operator looking at a source
    that has started failing wants to know what is in front of it, and identification
    is a separate question from whether this particular response was a refusal.
    """
    head = _lower_headers(headers or {})
    found = _identify(head, (body or "")[:_BODY_PEEK].lower())
    if found is not None:
        return found.name
    if head.get("cf-ray") or "cloudflare" in head.get("server", ""):
        return "Cloudflare"
    return head.get("server", "").split("/")[0]


def _titled_challenge(peek: str) -> bool:
    """Whether *peek* is a small document whose title is an interstitial's.

    Both halves are load-bearing; see :data:`_CHALLENGE_COPY`. *peek* is expected
    already lowered and truncated.
    """
    if len(peek) > _INTERSTITIAL_BYTES:
        return False
    match = _TITLE.search(peek)
    return bool(match and _has(match.group(1), _CHALLENGE_COPY))


def is_challenge(body: str) -> bool:
    """Whether *body* is an interstitial rather than the page that was asked for.

    Used to decide whether to *start* a solve, so it is deliberately the narrower of
    the two questions this module answers about a challenge page. A false positive here
    costs a browser launch on a page that had already arrived.
    """
    peek = (body or "")[:_BODY_PEEK].lower()
    return bool(_has(peek, _CHALLENGE_MARKERS)) or _titled_challenge(peek)


def is_still_challenged(body: str) -> bool:
    """Whether a page in a browser has not cleared yet.

    Wider than :func:`is_challenge`, because the two questions have opposite costs.
    Deciding to solve too eagerly wastes a browser; deciding a solve has *finished*
    too eagerly abandons it — the browser stops watching, no clearance cookie is
    harvested, and the tier reports itself unavailable on the very layer it exists for.

    A Turnstile widget is the case that separates them. On its own it is not enough to
    call a page an interstitial, since ordinary login and comment forms carry one, but
    it is more than enough to say a page being solved is not finished.
    """
    peek = (body or "")[:_BODY_PEEK].lower()
    return bool(_has(peek, _CHALLENGE_MARKERS) or _has(peek, _TURNSTILE_MARKERS)) or (
        _titled_challenge(peek)
    )


def _cf_code(body: str) -> Optional[int]:
    """The Cloudflare error code in *body*, whatever it means.

    Not filtered to the codes that map to a layer: a code this module has no opinion
    on is still worth knowing, because "Cloudflare error 1024" in a message beats
    "forbidden by the origin" for anyone who has to act on it.
    """
    match = _ERROR_CODE.search(body)
    return int(match.group(1)) if match else None


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
    """Names *and* values folded to lower case.

    Values too, because every comparison in this module is against a token whose case
    the server chose freely — ``Server: AkamaiGHost`` and ``X-CDN: Incapsula`` are both
    written mixed-case, and matching them exactly is a signature that silently never
    fires. Nothing here reads a value where case carries meaning; the one that does,
    ``Retry-After``, is parsed from the original headers.
    """
    return {str(key).lower(): str(value or "").lower() for key, value in headers.items()}


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
    vendor = _identify(head, peek)
    challenged = (
        mitigated == "challenge"
        or bool(_has(peek, _CHALLENGE_MARKERS))
        or _titled_challenge(peek)
        or bool(vendor is not None and _has(peek, vendor.challenge))
        # A bare captcha widget only counts on a refusal. See _CAPTCHA_MARKERS.
        or bool(status >= 400 and _has(peek, _CAPTCHA_MARKERS))
    )
    turnstile = _has(peek, _TURNSTILE_MARKERS)
    code = _cf_code(peek)
    who = "" if vendor is None else f"{vendor.name} "

    # Authentication first: it is the one answer that is never worth a retry, and
    # a signature requirement is advertised the same way a password is.
    auth = head.get("www-authenticate", "")
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
                f"{who}challenge served with a success status",
            )
        # Checked against the unlowered body: the destination is a URL, and lowering
        # it would corrupt the signed token these pages carry in the query string.
        hop = js_redirect(body or "")
        if hop:
            return Diagnosis(
                Action.FOLLOW,
                Layer.BROWSER_JS,
                "the page is a JavaScript-only redirect",
                location=hop,
            )
        return Diagnosis(Action.ACCEPT)

    if status == 429 or code == 1015:
        return Diagnosis(
            Action.BACKOFF,
            Layer.BEHAVIOURAL,
            "rate limited",
            retry_after=_retry_after(head),
        )

    if code in _CF_ZONE_CODES:
        return Diagnosis(Action.REFUSE, None, f"{_CF_ZONE_CODES[code]} (Cloudflare {code})")

    if code in _CF_WORKER_CODES:
        return Diagnosis(
            Action.RETRY, Layer.WORKERS, f"{_CF_WORKER_CODES[code]} (Cloudflare {code})"
        )

    if code == 1200:
        return Diagnosis(Action.RETRY, None, "the edge cache failed (Cloudflare 1200)")

    if code == 1011:
        # Hotlink protection reads the Referer, and this library already sends one on
        # every request. Nothing left to try: a stronger tier and a new address both
        # arrive with the same referrer chain.
        return Diagnosis(
            Action.REFUSE, Layer.WORKERS, "hotlink protection refused the request (Cloudflare 1011)"
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
                f"{who}challenge served with 403",
            )
        if code in _CF_CODES:
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
        if vendor is not None and vendor.layer is not None:
            # Ahead of the Cloudflare fallback below: a product's own header or cookie is
            # evidence about this response, while a `cf-ray` on a site behind two edges
            # only says which one is outermost.
            return Diagnosis(Action.ESCALATE, vendor.layer, f"{vendor.name} refused the request")
        if head.get("cf-ray") or "cloudflare" in head.get("server", ""):
            # Only when a User-Agent was supplied and is not a browser's. An absent one
            # is the caller not saying, and reading silence as "not a browser" would
            # relabel every diagnosis made from a recorded page.
            if user_agent and not _claims_a_browser(user_agent):
                # The coarse layer is the one that fires on a client which never
                # claimed to be a browser, and naming it names the remedy: a faithful
                # transport profile, not a browser launch. Reported as the scoring
                # layer instead, the ladder answers a missing profile by escalating
                # past the tier that would have supplied one.
                return Diagnosis(
                    Action.ESCALATE, Layer.BOT_FIGHT, "refused a client that is not a browser"
                )
            # Nothing distinguishes the scoring tiers from outside, so the
            # diagnosis names the strictest emit-only one. Recurrence after the
            # emit remedy is what promotes it to the composite model, and that
            # decision needs history, so it belongs to the planner.
            return Diagnosis(Action.ESCALATE, Layer.SUPER_BOT_FIGHT, "scored as automated")
        if code is not None:
            detail = f"Cloudflare error {code}"
        elif vendor is not None:
            detail = f"{vendor.name} refused the request"
        else:
            detail = "forbidden by the origin"
        return Diagnosis(Action.ESCALATE, Layer.WORKERS, detail)

    if status in (408, 502, 504, 520, 521, 522, 523, 524, 525, 526, 530):
        return Diagnosis(Action.RETRY, None, f"upstream error (HTTP {status})")

    if status in _VENDOR_REFUSALS and vendor is not None and vendor.layer is not None:
        # These products do not all refuse with 403. Akamai answers a failed sensor
        # check with 400, and a WAF rule commonly answers 405 — read as the site's
        # answer about a path, both are a silent give-up on a page that is there.
        return Diagnosis(
            Action.ESCALATE, vendor.layer, f"{vendor.name} refused the request (HTTP {status})"
        )

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


def _claims_a_browser(user_agent: str) -> bool:
    """Whether the client at least claims to be a browser.

    Not a fidelity check — the transport decides that, and this module never sees the
    handshake. It answers one narrower question: was there any browser claim to
    disbelieve? A client sending ``python-requests/2.32`` is refused by the coarsest
    heuristic there is, and that is a different conclusion from being scored.
    """
    lowered = (user_agent or "").lower()
    if not lowered.startswith("mozilla/"):
        return False
    return any(token in lowered for token in _BROWSER_ENGINES)


def _proxy_fault(error: BaseException) -> str:
    """Whether the proxy refused *us*, rather than the path beyond it failing.

    Matched on the message rather than an exception class because this module stays
    free of transport imports: curl_cffi and requests both raise a ``ProxyError``,
    but so does an unreachable destination reported through a SOCKS5 reply, and the
    class alone cannot tell those apart. The phrases below are curl's own, and each
    describes something on this side of the proxy.
    """
    text = f"{type(error).__name__} {error}".lower()
    if "rejected by the socks5 server" in text:
        # RFC 1929 said no. The SOCKS5 handshake has no status code, so this is the
        # exact analogue of the HTTP 407 above and gets the same answer.
        return "the proxy rejected the credential (SOCKS5 handshake)"
    if "no authentication method was acceptable" in text:
        return "the proxy requires a credential that was not offered"
    if "code 407 from proxy" in text or "proxy authentication" in text:
        return "the proxy rejected the credential (HTTP 407 to CONNECT)"
    if "resolve proxy" in text:
        return "the proxy hostname does not resolve"
    return ""


def diagnose_transport(error: BaseException, *, through_proxy: bool) -> Diagnosis:
    """Classify a failure that never produced a response.

    Attribution is different with and without a proxy in the path. Through one, a
    connection that will not complete is evidence about the exit and nothing
    else, so the exit is what changes. Direct, the same error is about the
    network or the origin, and swapping anything client-side is superstition.

    Unless the proxy is what refused us, which is neither. It is our own
    configuration, and the three things that follow from a layer attribution are
    all wrong for it: the address gets rotated though nothing is wrong with it, a
    pool is told an innocent exit is blocked, and — the durable one — layer 1 is
    written to the origin's profile, so a missing token leaves behind a permanent
    verdict that the *site* refuses us. That outlives the typo that caused it.
    """
    name = type(error).__name__
    if through_proxy:
        fault = _proxy_fault(error)
        if fault:
            return Diagnosis(Action.REFUSE, None, fault)
        # No layer: the site never answered, so there is nothing to attribute to it.
        return Diagnosis(Action.ROTATE, None, f"exit unusable ({name})")
    return Diagnosis(Action.RETRY, None, f"transport failure ({name})")
