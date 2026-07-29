"""The detection model this library is built around.

Everything else in the package is organised by the model in this module, so it is
the right place to start reading.

A modern mitigation engine does not make one decision from one signal. It runs
many largely independent detectors and folds them into a single trust score, and
admission behaves as a near-conjunction: a strong result on most detectors does
not offset a single strongly anomalous one. Two consequences drive the whole
design.

**The weakest layer bounds the outcome.** If ``p_i`` is the chance of passing
detector ``i``, then ``P_evade <= min(p_1 … p_n)``. Repairing a detector that is
not the minimum buys nothing. This is why the library diagnoses *which* layer is
binding (:mod:`scraper.diagnosis`) before it changes anything, instead of
reacting to a status code with a fixed remedy.

**What a detector reads predicts whether it can be satisfied at all.** A
detector either reads an *emitted artifact* — bytes the client chose to send at
connection time, carrying no secret and no history — or a *possessed property*,
something the client must hold continuously and cannot fabricate on demand.
Emitted artifacts are reproducible by a faithful imitator. Possessed properties
are not, and the only way to satisfy one is to actually hold it.

The practical rule that falls out, and the one thing this library does that a
conventional scraper does not: **when the binding layer reads a possessed
property, rotating identity is the wrong move.** Rotation resets the very
history the detector is measuring. See :mod:`scraper.planner`.

Layer numbering is this library's own organising device. The mechanisms and
product names are Cloudflare's; the numbers are ours, and they are stable API.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable, Mapping, NamedTuple, Optional, Tuple


class Layer(int, Enum):
    """One detection mechanism, numbered in the order a request meets it."""

    IP_REPUTATION = 1
    TLS_FINGERPRINT = 2
    POST_QUANTUM = 3
    HTTP_FRAMES = 4
    HEADER_ORDER = 5
    BROWSER_JS = 6
    CDP = 7
    BEHAVIOURAL = 8
    MANAGED_CHALLENGE = 9
    TURNSTILE = 10
    BOT_FIGHT = 11
    SUPER_BOT_FIGHT = 12
    UNDER_ATTACK = 13
    BOT_MANAGEMENT = 14
    WORKERS = 15
    AI_BOT_BLOCKER = 16
    AI_LABYRINTH = 17
    WEB_BOT_AUTH = 18
    ACCESS = 19

    def __str__(self) -> str:
        return f"L{self.value} {LAYERS[self].title}"


class Trait(Enum):
    """What a layer reads, which is what decides whether it can be satisfied.

    ``EMIT``
        An artifact the client transmits. No secret, no history: reproducible.
    ``POSSESS``
        A property the client must hold continuously. Not forgeable; the only
        route is to genuinely hold it.
    ``HYBRID``
        Reads an emitted artifact, but one bound to something possessed — an
        address whose reputation accrued over time, a cookie valid only from the
        context that earned it, an automation channel rather than a byte string.
        Satisfiable, but not by emitting alone.
    ``OUTSIDE``
        Does not sit on the axis: arbitrary operator code, or a trap that reads
        behaviour rather than the request.
    """

    EMIT = "emit"
    POSSESS = "possess"
    HYBRID = "hybrid"
    OUTSIDE = "outside"

    @property
    def forgeable(self) -> bool:
        """Whether emitting the right bytes can, on its own, satisfy the layer."""
        return self is Trait.EMIT


class Stance(Enum):
    """What this library will do about a layer, once it is the binding one.

    The distinction that matters is ``REFUSE``. Two layers rest on a secret the
    caller either holds or does not, and no amount of emulation substitutes for
    it. Treating those as obstacles to grind against produces an infinite retry
    loop against a wall, so they raise instead.
    """

    SATISFY = "satisfy"
    """Reproduce the artifact. Cheap, reliable, and where most effort is wasted."""

    LEASE = "lease"
    """Not technical — obtain a better address. Economic cost, not engineering."""

    ACCUMULATE = "accumulate"
    """Hold identity still and build the history the layer reads. Slow by nature."""

    SOLVE = "solve"
    """Pay a real browser once, then reuse the result within its binding."""

    AVOID = "avoid"
    """Do not trip it. There is nothing to defeat; there is something not to do."""

    DELEGATE = "delegate"
    """Beyond what this library will attempt alone; hand off to a provider."""

    REFUSE = "refuse"
    """No bypass exists. Register, authenticate, or stop."""


class LayerInfo(NamedTuple):
    """Static facts about one layer."""

    layer: Layer
    title: str
    trait: Trait
    stance: Stance
    summary: str


def _info(layer: Layer, title: str, trait: Trait, stance: Stance, summary: str) -> LayerInfo:
    return LayerInfo(layer=layer, title=title, trait=trait, stance=stance, summary=summary)


LAYERS: Dict[Layer, LayerInfo] = {
    info.layer: info
    for info in (
        _info(
            Layer.IP_REPUTATION,
            "IP reputation",
            Trait.HYBRID,
            Stance.LEASE,
            "The address is chosen freely; the reputation attached to it accrued over "
            "time and can be rented but not fabricated. Datacenter ranges are cheap to "
            "block. Mobile-carrier ranges front thousands of real subscribers, so "
            "blocking one causes collateral damage.",
        ),
        _info(
            Layer.TLS_FINGERPRINT,
            "TLS fingerprint (JA3/JA4)",
            Trait.EMIT,
            Stance.SATISFY,
            "The ClientHello exposes version, ordered cipher list, extension set and "
            "order, and curves in cleartext. An ordinary Python client is identified "
            "within the first round trip.",
        ),
        _info(
            Layer.POST_QUANTUM,
            "Post-quantum key share and ECH",
            Trait.EMIT,
            Stance.SATISFY,
            "Current Chrome offers X25519MLKEM768, and ECH where the server supports "
            "it. A client claiming to be current Chrome without them contradicts its "
            "own User-Agent, so a profile a year out of date fails here while passing "
            "plain JA3.",
        ),
        _info(
            Layer.HTTP_FRAMES,
            "HTTP/2 and HTTP/3 frames",
            Trait.EMIT,
            Stance.SATISFY,
            "Browsers open HTTP/2 with a characteristic frame order and "
            "version-specific SETTINGS values. Speaking HTTP/2 is not the same as "
            "speaking it like a browser.",
        ),
        _info(
            Layer.HEADER_ORDER,
            "Header order",
            Trait.EMIT,
            Stance.SATISFY,
            "The order headers are serialised in, and the arrangement of HTTP/2 "
            "pseudo-headers. Correct values in the wrong order are still anomalous.",
        ),
        _info(
            Layer.BROWSER_JS,
            "Browser and JavaScript fingerprint",
            Trait.EMIT,
            Stance.SOLVE,
            "Canvas and WebGL hashes, AudioContext, fonts, navigator.webdriver, "
            "Permissions-API anomalies, screen geometry, JS timing. Emitted, but "
            "high-dimensional and tightly coupled: the surface has to stay internally "
            "consistent across dozens of probes, which is why a real browser is the "
            "practical answer rather than a spoofed value set.",
        ),
        _info(
            Layer.CDP,
            "DevTools-protocol detection",
            Trait.HYBRID,
            Stance.SOLVE,
            "Property descriptors the automation channel modifies, timing anomalies in "
            "instrumented APIs, internal markers. The artifact is the control channel "
            "itself rather than a one-time byte sequence, so patching surface values "
            "does not remove it.",
        ),
        _info(
            Layer.BEHAVIOURAL,
            "Per-zone behavioural model",
            Trait.POSSESS,
            Stance.ACCUMULATE,
            "Request-timing regularity, navigation and referrer chains, cookie and "
            "session age, history depth, concurrent sessions per address — correlated "
            "across a session window and trained per zone. The property is accumulated "
            "and non-portable, so it can only be built, never presented.",
        ),
        _info(
            Layer.MANAGED_CHALLENGE,
            "Managed JavaScript challenge",
            Trait.HYBRID,
            Stance.SOLVE,
            "An interstitial that issues a clearance cookie bound to the address, "
            "User-Agent and TLS fingerprint that earned it. The response is emitted; "
            "the cookie is only useful while the client still holds the context it was "
            "issued to, which couples this layer to the address.",
        ),
        _info(
            Layer.TURNSTILE,
            "Turnstile",
            Trait.HYBRID,
            Stance.SOLVE,
            "A client-side authenticity check, usually invisible on low-risk sessions. "
            "Issues a short-lived token bound to its issuing context, so the same "
            "solve-once-and-reuse shape applies on a tighter clock.",
        ),
        _info(
            Layer.BOT_FIGHT,
            "Bot Fight Mode",
            Trait.EMIT,
            Stance.SATISFY,
            "Heuristics on coarse signals: datacenter origin, obviously non-browser "
            "clients. A faithful transport profile on a residential address clears it.",
        ),
        _info(
            Layer.SUPER_BOT_FIGHT,
            "Super Bot Fight Mode",
            Trait.EMIT,
            Stance.SATISFY,
            "The same in kind as Bot Fight Mode but reading more of the request, "
            "including Sec-Fetch metadata and header-set consistency.",
        ),
        _info(
            Layer.UNDER_ATTACK,
            "Under Attack Mode",
            Trait.HYBRID,
            Stance.SOLVE,
            "Challenges every visitor regardless of reputation, so nothing passes on "
            "transport fingerprint alone. The gate is the managed challenge, and it "
            "inherits its context binding.",
        ),
        _info(
            Layer.BOT_MANAGEMENT,
            "Bot Management",
            Trait.HYBRID,
            Stance.DELEGATE,
            "A per-zone model over every prior layer, emitted and possessed together. "
            "No single technique addresses it and results are inconsistent between "
            "deployments, because the model is tuned per zone.",
        ),
        _info(
            Layer.WORKERS,
            "Operator edge code",
            Trait.OUTSIDE,
            Stance.AVOID,
            "Arbitrary JavaScript at the edge: honeypot endpoints, bespoke rate limits, "
            "custom challenge-response. No generic bypass exists because there is no "
            "generic mechanism.",
        ),
        _info(
            Layer.AI_BOT_BLOCKER,
            "AI bot blocker",
            Trait.EMIT,
            Stance.SATISFY,
            "Blocks labelled crawler User-Agents. It keys on declared identity rather "
            "than behaviour, so it constrains only clients that identify honestly.",
        ),
        _info(
            Layer.AI_LABYRINTH,
            "Decoy-content honeypot",
            Trait.OUTSIDE,
            Stance.AVOID,
            "Hidden nofollow links into a maze of generated decoy pages. Two harms at "
            "once, and neither announces itself: the store fills with plausible "
            "irrelevant content, and the session is flagged network-wide. There is no "
            "error response, so a scraper looks like it is working while it is not.",
        ),
        _info(
            Layer.WEB_BOT_AUTH,
            "Cryptographic agent identity",
            Trait.POSSESS,
            Stance.REFUSE,
            "A signature over the request under a private key, verified against a "
            "published directory. Emulation of any kind is beside the point: the check "
            "reads a secret. Currently deployed fail-open, so an unsigned request falls "
            "back to the rest of the stack — but where a signature is required there is "
            "no bypass, only registration.",
        ),
        _info(
            Layer.ACCESS,
            "Identity-provider gate",
            Trait.POSSESS,
            Stance.REFUSE,
            "Authentication, not bot mitigation. Out of scope: retrieving content "
            "behind it without credentials would be unauthorised access.",
        ),
    )
}


TRANSPORT_LAYERS: FrozenSet[Layer] = frozenset(
    {
        Layer.TLS_FINGERPRINT,
        Layer.POST_QUANTUM,
        Layer.HTTP_FRAMES,
        Layer.HEADER_ORDER,
    }
)
"""Layers 2-5, which are one barrier rather than four.

They read different parts of the request, but a client built to reproduce one
browser's network stack passes all of them together. In the bound they are a
single term, so a tier that satisfies one satisfies the group — and a defender
adding a fifth check of the same kind barely moves the result.
"""

FORGEABLE: FrozenSet[Layer] = frozenset(
    layer for layer, info in LAYERS.items() if info.trait.forgeable
)
"""Layers that emitting the right bytes can satisfy outright."""

IMPASSABLE: FrozenSet[Layer] = frozenset(
    layer for layer, info in LAYERS.items() if info.stance is Stance.REFUSE
)
"""Layers with no technical bypass. Reaching one of these is a stop, not a retry."""


def info(layer: Layer) -> LayerInfo:
    """Static facts about *layer*."""
    return LAYERS[layer]


def trait(layer: Layer) -> Trait:
    """What *layer* reads."""
    return LAYERS[layer].trait


def stance(layer: Layer) -> Stance:
    """What this library does about *layer* when it is binding."""
    return LAYERS[layer].stance


def is_forgeable(layer: Layer) -> bool:
    """Whether *layer* can be satisfied by emitting the right artifact."""
    return layer in FORGEABLE


def is_impassable(layer: Layer) -> bool:
    """Whether *layer* admits no technical bypass at all."""
    return layer in IMPASSABLE


def expand(layers: Iterable[Layer]) -> FrozenSet[Layer]:
    """Close *layers* over the transport group.

    Declaring one of layers 2-5 declares all four, because no real technique
    satisfies one without the others. Callers building a tier's reach should go
    through here rather than listing the group by hand, so the two never drift.
    """
    result = set(layers)
    if result & TRANSPORT_LAYERS:
        result |= TRANSPORT_LAYERS
    return frozenset(result)


def weakest(odds: Mapping[Layer, float]) -> Optional[Tuple[Layer, float]]:
    """Return the binding layer and its pass probability, or ``None`` if empty.

    This is the bound itself: admission requires passing every layer that can
    block on its own, so the joint probability cannot exceed the smallest term.
    Ties resolve to the lower-numbered layer, which is the one a request meets
    first and therefore the one worth addressing first.
    """
    if not odds:
        return None
    layer = min(odds, key=lambda key: (odds[key], key.value))
    return layer, odds[layer]


def marginal_gain(odds: Mapping[Layer, float], layer: Layer, improved: float) -> float:
    """How much raising *layer* to *improved* actually moves the bound.

    Almost always zero, which is the point. A strategy that fails layer 1 gains
    nothing from a better TLS profile, and the arithmetic here is the cheapest
    way to see that before spending the effort. Used by the planner to reject
    remedies aimed at a layer that is not binding.
    """
    current = weakest(odds)
    if current is None:
        return 0.0
    revised = dict(odds)
    revised[layer] = max(revised.get(layer, improved), improved)
    after = weakest(revised)
    assert after is not None
    return max(0.0, after[1] - current[1])
