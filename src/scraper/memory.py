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
from typing import Any, Dict, List, Optional

from .identity import Clearance
from .layers import Layer
from .utils.file_tools import atomic_write
from .utils.url_tools import extract_host

logger = logging.getLogger(__name__)

SCHEMA = 1
"""Bumped when a field changes meaning. An unknown schema is discarded, not guessed at."""

MAX_ENDPOINTS = 32
MAX_DECOYS = 256


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
    decoys: List[str] = field(default_factory=list)
    clearance: Optional[Dict[str, Any]] = None

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

    def note_decoy(self, url: str) -> None:
        if url not in self.decoys:
            self.decoys.append(url)
            del self.decoys[:-MAX_DECOYS]

    def is_decoy(self, url: str) -> bool:
        return url in self.decoys


class Memory:
    """A persistent, thread-safe map of origin to :class:`OriginProfile`.

    Args:
        path: JSON file to keep. ``None`` makes the store in-memory only, which
            is the right choice for tests and the wrong one for anything that
            faces a behavioural model, since forgetting is the failure mode.
        flush_every: Seconds between writes. Mutations are frequent and small, so
            they coalesce; a write also happens on :meth:`close`.
    """

    def __init__(self, path: Optional[Path] = None, *, flush_every: float = 15.0) -> None:
        self.path = Path(path) if path else None
        self._flush_every = flush_every
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
