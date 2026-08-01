"""Saying what a failure was, in words, without saying what to do about it.

This library produces `Layer`, `Trait`, `Stance` and an exception taxonomy, and until now
offered no way to render any of it in English — so every consumer wrote that mapping
itself. That is a gap in the library rather than work belonging to the application: the
facts here are derived purely from this package's own types, and two consumers writing
them independently would disagree about the same failure.

**What is deliberately not here is advice.** "Configure a proxy in the crawler settings",
"check that browser crawling is enabled" — those name a particular application's UI, and a
library asserting them would be wrong for the next consumer. The split runs exactly along
that line: this module says what happened and what the binding layer reads, and the caller
says what to do about it.

`reads` returns a *clause*, not a sentence, for the same reason. The natural phrasing is
"It reads {trait}, so {remedy}." — objective first half, consumer's second half. Emitting
half a sentence here and expecting it to be completed would make the seam visible the first
time either side rephrased.
"""

from __future__ import annotations

from typing import List, Optional

from .exceptions import (
    Aborted,
    Blocked,
    Exhausted,
    Impassable,
    MissingDependency,
    Poisoned,
    TierUnavailable,
)
from .layers import LAYERS, Layer, Trait

IMPASSABLE = "impassable"
EXHAUSTED = "exhausted"
BLOCKED = "blocked"
UNREACHABLE = "unreachable"
POISONED = "poisoned"
TIER_UNAVAILABLE = "tier_unavailable"
MISSING_DEPENDENCY = "missing_dependency"
RENDER_FAILED = "render_failed"
SOLVE_FAILED = "solve_failed"
HTTP_ERROR = "http_error"
BAD_IMAGE = "bad_image"
ABORTED = "aborted"
FAILED = "failed"

FAILURE_KINDS = (
    IMPASSABLE,
    EXHAUSTED,
    BLOCKED,
    UNREACHABLE,
    POISONED,
    TIER_UNAVAILABLE,
    MISSING_DEPENDENCY,
    RENDER_FAILED,
    SOLVE_FAILED,
    HTTP_ERROR,
    BAD_IMAGE,
    ABORTED,
    FAILED,
)

_HEADLINE = {
    IMPASSABLE: "The site requires something no scraper can fabricate",
    EXHAUSTED: "Every bypass this configuration reaches was tried and the site still refused",
    BLOCKED: "The site refused the request",
    UNREACHABLE: "The request never reached the site",
    POISONED: "A page came back, but its content looks like decoy filler",
    TIER_UNAVAILABLE: "No configured capability can serve this request",
    MISSING_DEPENDENCY: "An optional dependency is needed and is not installed",
    RENDER_FAILED: "A browser rendered the page but the content this source needs never appeared",
    SOLVE_FAILED: "A browser ran the challenge and came back without a clearance",
    HTTP_ERROR: "The site answered with an error",
    BAD_IMAGE: "What the site served in place of an image could not be decoded",
    ABORTED: "The request was aborted",
    FAILED: "The request failed",
}

_READS = {
    Trait.EMIT: "bytes the client chooses to send, which can be reproduced",
    Trait.POSSESS: "something the client must genuinely hold, which cannot be forged",
    Trait.HYBRID: "an artifact bound to something held, so sending the right bytes is not enough",
    Trait.OUTSIDE: "behaviour over time rather than the request itself",
}

# What a status code says on its own, for the codes that survive the retrieval ladder.
# Anything the ladder diagnosed arrives as a `Blocked` instead, so a code reaching here is
# the site's plain answer rather than a mitigation verdict. Kept free of inference about
# *why* — that a 404 means a source's URLs have changed is a guess only the consumer is
# in a position to make.
_STATUS_NOTE = {
    404: "The page is not there.",
    410: "The page is gone for good, as the site states outright.",
    451: "The site withholds this page for legal reasons.",
}

NO_LAYER_NOTE = (
    "No detection layer was attributed, so the reason above is all there is to go"
    " on: the site may have refused for something only this source recognises, its"
    " own server may not have answered, or the fault is at this end — a proxy that"
    " refused its credentials, an address with no route."
)


def failure_kind(error: BaseException) -> str:
    """A short, stable name for the class of failure.

    Stable because consumers key health tallies and API fields on it, and a name that
    drifted would silently split one failure into two in whatever counts them.
    """
    from PIL import UnidentifiedImageError  # noqa: PLC0415
    from requests import HTTPError, RequestException  # noqa: PLC0415

    from .browser import RenderError, SolveError  # noqa: PLC0415

    if isinstance(error, Impassable):
        return IMPASSABLE
    if isinstance(error, Exhausted):
        return EXHAUSTED
    if isinstance(error, Blocked):
        return BLOCKED if error.layer is not None else UNREACHABLE
    if isinstance(error, Poisoned):
        return POISONED
    if isinstance(error, MissingDependency):
        return MISSING_DEPENDENCY
    if isinstance(error, RenderError):
        return RENDER_FAILED
    if isinstance(error, SolveError):
        return SOLVE_FAILED
    if isinstance(error, TierUnavailable):
        return TIER_UNAVAILABLE
    if isinstance(error, Aborted):
        return ABORTED
    if isinstance(error, HTTPError):
        return HTTP_ERROR
    if isinstance(error, UnidentifiedImageError):
        return BAD_IMAGE
    if isinstance(error, RequestException):
        return UNREACHABLE
    return FAILED


def status_code(error: BaseException) -> Optional[int]:
    """The status the site answered with, when it answered at all."""
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def blocking_layer(error: BaseException) -> Optional[Layer]:
    """The layer this failure is attributed to, never a status code.

    ``None`` covers three separate things and deliberately does not tell them apart: not a
    scraper failure at all, one the model declined to attribute, and one that is ours
    rather than the site's. To a caller they mean the same thing — there is no layer to
    look up.
    """
    layer = getattr(error, "layer", None)
    return layer if isinstance(layer, Layer) else None


def is_permanent(error: BaseException) -> bool:
    """Whether asking again, unchanged, cannot succeed.

    True where the binding layer reads a secret, and for content already recorded as
    decoy. Deliberately false for `Exhausted`, which says every tier *this* configuration
    reaches was spent — a proxy, a browser or the archive may still get through, so
    treating it as permanent would retire a source over a setting.
    """
    return isinstance(error, (Impassable, Poisoned))


def headline(kind: str) -> str:
    """One line naming what happened, for a failure kind."""
    return _HEADLINE.get(kind, _HEADLINE[FAILED])


def reads(trait: Trait) -> str:
    """What a layer with this trait reads — a clause, to be completed by the caller.

    Phrased to sit inside "It reads {…}, so {what you intend to do}." because the second
    half is the consumer's policy and cannot be written here.
    """
    return _READS[trait]


def status_note(code: Optional[int]) -> Optional[str]:
    """What a status code says on its own, or ``None`` when it says nothing useful."""
    return _STATUS_NOTE.get(code) if code is not None else None


def summarise(error: BaseException, *, url: str = "") -> List[str]:
    """The objective half of a failure description, as paragraphs.

    Returned as a list rather than joined text so a caller can append its own advice in
    the right place — after the layer, before nothing — without splitting a string back
    apart to find where that is.
    """
    where = url or getattr(error, "url", "") or ""
    kind = failure_kind(error)

    first = headline(kind)
    code = status_code(error)
    if code is not None:
        first += f" (HTTP {code})"
    if where:
        first += f" for {where}"
    parts = [first]

    # `str()` only where there is no `detail`: a failure carrying one has already folded
    # the layer and the URL into its message, so using it here would repeat both of the
    # lines around it.
    detail = getattr(error, "detail", "") or ""
    if not detail and not hasattr(error, "detail"):
        detail = str(error)
    if detail:
        parts.append(detail)

    note = status_note(code)
    if note:
        parts.append(note)

    layer = blocking_layer(error)
    facts = LAYERS.get(layer) if layer is not None else None
    if facts is not None:
        parts.append(f"{layer} — {facts.summary}")
    elif isinstance(error, Blocked):
        parts.append(NO_LAYER_NOTE)
    return parts
