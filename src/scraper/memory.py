"""What the scraper knows about a site, and keeps knowing after it exits.

The layers that resist circumvention are the ones that read a property the client
has to *hold*, and the largest of those is a per-zone behavioural model: request
timing, navigation chains, cookie and session age, history depth, correlated
across a session window and trained separately for every protected site. There is
no artifact to reproduce. The only way to satisfy it is to genuinely accumulate
the history it inspects.

A process that forgets everything on exit can never accumulate anything, which is
why this store exists and why it is on by default. What it holds per origin:

- the layer that was last found binding, so the next run starts from the
  conclusion the last one paid for instead of rediscovering it;
- the tier that worked, for the same reason;
- a clearance and the identity it is bound to, so a solve is reused rather than
  repeated;
- observed timing, so pacing converges on what the site tolerates;
- JSON endpoints seen behind the HTML, which are the cheapest route to the same
  data;
- URLs that behaved like decoys, which is the only durable defence against a trap
  that returns no error.

Stored as one JSON file per profile directory, written atomically. Nothing here is
a secret except the clearance cookies, and the file is created ``0600`` for that
reason.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .identity import Clearance
from .layers import Layer
from .utils.file_tools import atomic_write
from .utils.url_tools import extract_host

logger = logging.getLogger(__name__)

SCHEMA = 2
"""Bumped when a field changes meaning. An unknown schema is discarded, not guessed at."""

MAX_ENDPOINTS = 32
MAX_DECOYS = 256

DECOY_TTL = 7 * 24 * 60 * 60
"""How long a decoy verdict stands before the URL is judged again.

The check is a heuristic over vocabulary overlap, so a verdict is a guess about a page
at a moment: it can be wrong, and a site that was reorganised can turn a right one
stale. Kept permanently, one wrong guess removes a URL from every future run with no
signal that it happened and no way back except editing the store by hand.

A week is long enough that a live trap stays remembered across a job and its retries,
and short enough that a mistake costs a re-check rather than a chapter."""

MAX_VALIDATORS = 64
"""Endpoints per origin to keep an ``ETag``/``Last-Modified`` pair for.

Least recently recorded go first. Sized for the shape that uses them: a caller asking
"has this table of contents moved?" across the novels it follows on one site, which is
tens of endpoints per origin rather than one or thousands."""

MAX_ORIGINS = 512
"""How many origins a store keeps. A long-running process crawling a wide frontier
otherwise grows one profile per host it ever touched, and every flush rewrites all of
them."""

FORGET_AFTER = 30 * 86400.0
"""Seconds of silence after which an origin is dropped. What is stored is a conclusion
about a site's current configuration, and a month-old conclusion is worth less than the
cold start that replaces it — edges get reconfigured, and a stale binding layer sends the
next run up the ladder for a site that has stopped challenging anyone."""


@dataclass
class OriginProfile:
    """Everything learned about one origin.

    Args:
        binding_layer: The layer last found to be the minimum in the bound. The
            single most valuable thing to persist: it is what stops the next run
            from spending a browser launch on a site that only needed a header
            profile, or a hundred retries on one that needed the browser.
        tier: Name of the capability set that last succeeded.
        interval: Observed sustainable seconds between requests. Grows on a
            throttle and decays slowly on success, so a site that tightened its
            limits is learned once rather than every run.
        successes / failures: A ledger, not a score. Used to decide when a
            recurrence promotes a diagnosis from "scored as automated" to the
            per-zone composite, which is a judgement that needs history.
    """

    origin: str
    binding_layer: Optional[int] = None
    tier: str = ""
    interval: float = 0.0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    warmed_at: float = 0.0
    endpoints: List[str] = field(default_factory=list)
    #: URL to the time it was judged a decoy. A time rather than a bare list because a
    #: verdict expires; see DECOY_TTL.
    decoys: Dict[str, float] = field(default_factory=dict)
    clearance: Optional[Dict[str, Any]] = None
    validators: Dict[str, Dict[str, str]] = field(default_factory=dict)

    @property
    def binding(self) -> Optional[Layer]:
        if self.binding_layer is None:
            return None
        try:
            return Layer(self.binding_layer)
        except ValueError:
            # A file written by a newer version that added a layer. Treating the
            # unknown number as "no knowledge" degrades to a cold start, which is
            # slow but correct; guessing a neighbouring layer would not be.
            return None

    @binding.setter
    def binding(self, layer: Optional[Layer]) -> None:
        self.binding_layer = None if layer is None else int(layer)

    def clearance_for(self, origin: str) -> Optional[Clearance]:
        """Rebuild the stored clearance, or ``None`` if there is none usable."""
        raw = self.clearance
        if not raw:
            return None
        try:
            found = Clearance(
                origin=str(raw.get("origin") or origin),
                cookies=dict(raw.get("cookies") or {}),
                identity_token=str(raw.get("identity_token") or ""),
                user_agent=str(raw.get("user_agent") or ""),
                issued_at=float(raw.get("issued_at") or 0.0),
                expires_at=float(raw.get("expires_at") or 0.0),
            )
        except (TypeError, ValueError):
            return None
        return None if found.expired else found

    def remember_clearance(self, clearance: Optional[Clearance]) -> None:
        self.clearance = None if clearance is None else asdict(clearance)

    def note_endpoint(self, url: str) -> bool:
        """Record a JSON endpoint seen behind this origin's HTML. True if new."""
        if url in self.endpoints:
            return False
        self.endpoints.append(url)
        del self.endpoints[:-MAX_ENDPOINTS]
        return True

    def note_validators(self, url: str, *, etag: str = "", last_modified: str = "") -> bool:
        """Remember what would let *url* answer ``304`` next time. True if new.

        Recording is automatic and sending is not: a validator in the store costs
        nothing, while sending one turns a response into a ``304`` with no body, which
        only a caller who asked for that can handle.
        """
        if not (etag or last_modified):
            return False
        found = {"etag": etag, "last_modified": last_modified}
        known = self.validators.pop(url, None)
        # Re-inserted either way, because insertion order is the recency the cap
        # evicts by: refreshing an endpoint has to move it to the back.
        self.validators[url] = found
        while len(self.validators) > MAX_VALIDATORS:
            self.validators.pop(next(iter(self.validators)))
        return known != found

    def validators_for(self, url: str) -> Dict[str, str]:
        """The stored ``ETag``/``Last-Modified`` pair for *url*, or an empty mapping."""
        return dict(self.validators.get(url) or {})

    def note_decoy(self, url: str) -> None:
        self.decoys[url] = time.time()
        for stale in self._expired_decoys():
            self.decoys.pop(stale, None)
        for oldest in sorted(self.decoys, key=lambda key: self.decoys[key])[:-MAX_DECOYS]:
            self.decoys.pop(oldest, None)

    def is_decoy(self, url: str) -> bool:
        noted = self.decoys.get(url)
        return noted is not None and (time.time() - noted) < DECOY_TTL

    def _expired_decoys(self) -> List[str]:
        cutoff = time.time() - DECOY_TTL
        return [url for url, noted in self.decoys.items() if noted < cutoff]


class Memory:
    """A persistent, thread-safe map of origin to :class:`OriginProfile`.

    Args:
        path: JSON file to keep. ``None`` makes the store in-memory only, which
            is the right choice for tests and the wrong one for anything that
            faces a behavioural model, since forgetting is the failure mode.
        flush_every: Seconds between writes. Mutations are frequent and small, so
            they coalesce; a write also happens on :meth:`close`.
        max_origins: How many origins to keep. When the store is over this, the
            least recently seen are dropped.
        forget_after: Seconds of silence before an origin is dropped regardless of
            the cap. ``0`` keeps everything until the cap bites.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        flush_every: float = 15.0,
        max_origins: int = MAX_ORIGINS,
        forget_after: float = FORGET_AFTER,
    ) -> None:
        self.path = Path(path) if path else None
        self._flush_every = flush_every
        self._max_origins = max(1, max_origins)
        self._forget_after = max(0.0, forget_after)
        self._lock = threading.RLock()
        self._profiles: Dict[str, OriginProfile] = {}
        self._dirty = False
        self._flushed_at = 0.0
        if self.path:
            self._load()

    def key(self, url: str) -> str:
        """The origin key for *url*.

        Keyed on host, not on scheme or path: the behavioural model is per zone,
        and http and https pages of one site are one zone.
        """
        return extract_host(url) or url

    def profile(self, url: str) -> OriginProfile:
        key = self.key(url)
        with self._lock:
            found = self._profiles.get(key)
            if found is None:
                found = OriginProfile(origin=key)
                self._profiles[key] = found
                self._dirty = True
                self._evict_locked(protect=key)
            return found

    def record_success(self, url: str, *, tier: str, interval: float = 0.0) -> None:
        with self._lock:
            profile = self.profile(url)
            profile.tier = tier
            profile.successes += 1
            profile.consecutive_failures = 0
            profile.last_seen = time.time()
            if interval > 0:
                # Converge downward slowly. A site that let one request through
                # quickly has not necessarily raised its limit, and dropping
                # straight to the fast value is how a run earns a throttle it
                # then attributes to the exit.
                profile.interval = (
                    interval if profile.interval <= 0 else profile.interval * 0.9 + interval * 0.1
                )
            self._maybe_flush()

    def record_failure(self, url: str, layer: Optional[Layer], *, interval: float = 0.0) -> None:
        with self._lock:
            profile = self.profile(url)
            if layer is not None:
                profile.binding = layer
            profile.failures += 1
            profile.consecutive_failures += 1
            profile.last_seen = time.time()
            if interval > 0:
                profile.interval = max(profile.interval, interval)
            self._maybe_flush()

    def mark_warmed(self, url: str) -> None:
        with self._lock:
            self.profile(url).warmed_at = time.time()
            self._maybe_flush()

    def touch(self) -> None:
        """Mark the store dirty after mutating a profile in place."""
        with self._lock:
            self._dirty = True
            self._maybe_flush()

    # -- inventory ----------------------------------------------------------------

    @property
    def count(self) -> int:
        """How many origins are known."""
        with self._lock:
            return len(self._profiles)

    def origins(self) -> List[str]:
        """Every origin known, most recently seen first."""
        with self._lock:
            return [key for key, _ in self._by_age_locked(newest_first=True)]

    def profiles(self) -> List[OriginProfile]:
        """A copy of every profile, most recently seen first.

        Copies rather than the live objects: a caller enumerating the store for a
        status page must not be able to edit what the retrieval loop is reading, and
        the endpoint and decoy lists make that easy to do by accident.
        """
        with self._lock:
            return [_copy(profile) for _, profile in self._by_age_locked(newest_first=True)]

    def export(self) -> Dict[str, Dict[str, Any]]:
        """A JSON-safe view of the store, without the clearance cookies.

        The cookies are the one secret in here, and the question a status page asks is
        whether a clearance is held and for how long — so that is what this reports.
        """
        out: Dict[str, Dict[str, Any]] = {}
        for profile in self.profiles():
            fields = asdict(profile)
            clearance = profile.clearance
            fields["clearance"] = (
                None
                if not clearance
                else {
                    "expires_at": float(clearance.get("expires_at") or 0.0),
                    "user_agent": str(clearance.get("user_agent") or ""),
                }
            )
            out[profile.origin] = fields
        return out

    def forget(self, url: str) -> bool:
        """Drop everything learned about *url*'s origin. True if there was anything.

        The escape hatch for a conclusion that has gone stale in a way the TTL will not
        catch — a site that dropped its edge, or an origin whose profile was written
        while a proxy was misconfigured.
        """
        key = self.key(url)
        with self._lock:
            if self._profiles.pop(key, None) is None:
                return False
            self._dirty = True
            self._maybe_flush()
            return True

    def clear(self) -> None:
        """Forget every origin."""
        with self._lock:
            if not self._profiles:
                return
            self._profiles.clear()
            self._dirty = True
            self._maybe_flush()

    def flush(self) -> None:
        """Write now, if there is anything to write."""
        with self._lock:
            if not self._dirty or not self.path:
                self._dirty = False
                return
            payload = {
                "schema": SCHEMA,
                "profiles": {key: asdict(value) for key, value in self._profiles.items()},
            }
            try:
                with atomic_write(self.path, "w") as handle:
                    json.dump(payload, handle, indent=1, sort_keys=True)
                os.chmod(self.path, 0o600)
            except OSError as exc:
                # Losing the store costs a cold start, not correctness, so a
                # read-only or full disk must not take the scrape down with it.
                logger.warning("could not write %s: %s", self.path, exc)
                return
            self._dirty = False
            self._flushed_at = time.monotonic()

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ----------------------------------------------------------------

    def _maybe_flush(self) -> None:
        self._dirty = True
        if time.monotonic() - self._flushed_at >= self._flush_every:
            self.flush()

    def _by_age_locked(self, *, newest_first: bool) -> List[Tuple[str, OriginProfile]]:
        items = sorted(self._profiles.items(), key=lambda item: item[1].last_seen)
        return list(reversed(items)) if newest_first else items

    def _evict_locked(self, *, protect: str = "") -> int:
        """Drop stale origins, then the least recently seen ones over the cap.

        Age first and the cap second, because they answer different questions: the TTL
        removes conclusions that have gone stale, and the cap removes the tail of a
        frontier wider than the store is meant to hold. Doing it the other way round
        would keep a month-old profile alive purely because the store was small.

        *protect* is never dropped — it is the origin the caller is asking for, and
        handing back a profile that is no longer in the store loses whatever the
        retrieval about to happen learns.
        """
        dropped = 0
        if self._forget_after > 0:
            cutoff = time.time() - self._forget_after
            for key in [
                key
                for key, profile in self._profiles.items()
                if profile.last_seen < cutoff and key != protect
            ]:
                del self._profiles[key]
                dropped += 1
        for key, _ in self._by_age_locked(newest_first=False):
            if len(self._profiles) <= self._max_origins:
                break
            if key == protect:
                continue
            del self._profiles[key]
            dropped += 1
        if dropped:
            self._dirty = True
        return dropped

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("ignoring unreadable memory at %s: %s", self.path, exc)
            return
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            logger.info("discarding memory at %s: schema %s", self.path, raw.get("schema"))
            return
        profiles = raw.get("profiles")
        if not isinstance(profiles, dict):
            return
        known = set(OriginProfile.__dataclass_fields__)
        for key, value in profiles.items():
            if not isinstance(value, dict):
                continue
            fields = {name: item for name, item in value.items() if name in known}
            fields["origin"] = str(fields.get("origin") or key)
            try:
                self._profiles[str(key)] = OriginProfile(**fields)
            except TypeError as exc:
                logger.debug("skipping malformed profile for %s: %s", key, exc)
        dropped = self._evict_locked()
        if dropped:
            logger.debug("dropped %d stale origins from %s", dropped, self.path)


def _copy(profile: OriginProfile) -> OriginProfile:
    return OriginProfile(**asdict(profile))
