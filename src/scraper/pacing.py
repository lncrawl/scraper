"""How fast to go, and what the traffic should look like on the way.

This is the module that addresses the hardest layer, and it does so by not trying
to defeat it. A per-zone behavioural model reads accumulated, non-portable
history: timing regularity, navigation and referrer chains, session age and depth,
concurrent sessions per address. None of that can be presented on demand, so the
only thing that works is to actually behave the way the model expects and let the
history accrue.

Three specifics, each fixing something a conventional scraper gets wrong:

**Delays are drawn from a distribution, not set to a constant.** A fixed
``min_request_interval`` produces perfectly regular arrivals, which is a stronger
signal than being fast. Inter-request gaps come from a gamma distribution, so they
are positive, clustered near a mode, and occasionally long — which is what reading
a page looks like.

**A deep page is not the first thing a visitor sees.** Landing directly on a
chapter URL with no referrer and no prior history is a navigation pattern no human
produces. The first request to an origin warms up on its homepage.

**The referrer chain is real.** Each navigation cites the page it plausibly came
from, tracked per origin.

Rate limiting is handled here rather than in the proxy layer, and that placement is
the point: a ``429`` says the address works and is being asked for too much, so the
remedy is arithmetic in this module, not a new address. That arithmetic has to run in
both directions — see :meth:`Pacer.eased`, without which one throttled minute costs an
origin its speed permanently, including in every later run that reads the profile.
"""

from __future__ import annotations

import os
import random
import struct
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .exceptions import Aborted
from .utils.signals import AbortSignal
from .utils.url_tools import extract_base, extract_host


@dataclass
class PacingPolicy:
    """Timing and navigation behaviour.

    Args:
        interval: Target mean seconds between requests to one origin. The mean of
            the distribution, not a floor — individual gaps land both sides of it.
        shape: Gamma shape parameter. Low values give a long right tail (bursty,
            with occasional long gaps); high values approach a constant, which is
            the thing being avoided. Around 2-3 looks like browsing.
        floor: Hard minimum gap. Guards against a shape/interval combination
            drawing something implausibly small.
        ceiling: Hard maximum gap, so the tail cannot stall a run.
        pause_chance: Probability that a gap is a "reading pause" instead, drawn
            at ``pause_scale`` times the interval. Real sessions have these and a
            pure gamma stream does not.
        warmup: Visit the origin's homepage before the first deep page.
        warmup_ttl: Seconds a warm-up counts for. Beyond this the session is old
            enough that arriving cold looks like a new visit, which is fine.
        backoff_factor: Multiplier applied to the learned interval on a throttle.
        max_interval: Cap on the learned interval, so a hostile site cannot
            ratchet a run to a standstill.
        recover_factor: Multiplier applied to a widened interval once a run of
            successes says the throttle is behind us. Never narrows past
            ``interval``, which is what the caller asked for in the first place.
        recover_after: Consecutive successes one narrowing step costs. Deliberately
            not every success: a widened interval is *why* those requests are
            getting through, so probing back down has to be slower than the site's
            throttle window or the run just re-earns the 429.
    """

    interval: float = 3.0
    shape: float = 2.5
    floor: float = 0.35
    ceiling: float = 45.0
    pause_chance: float = 0.06
    pause_scale: float = 4.0
    warmup: bool = True
    warmup_ttl: float = 1800.0
    backoff_factor: float = 2.0
    max_interval: float = 120.0
    recover_factor: float = 0.9
    recover_after: int = 10

    def __post_init__(self) -> None:
        if self.interval <= 0:
            self.interval = 0.0
        self.shape = max(0.5, self.shape)
        self.ceiling = max(self.ceiling, self.floor)
        self.recover_factor = min(1.0, max(0.1, self.recover_factor))
        self.recover_after = max(1, self.recover_after)


def _seeded_random(seed: Optional[int]) -> random.Random:
    if seed is not None:
        return random.Random(seed)
    # Independent of the global random module, which callers reseed for their own
    # reasons; a scrape whose timing becomes reproducible because someone called
    # random.seed() elsewhere has lost the property this module provides.
    return random.Random(struct.unpack("<Q", os.urandom(8))[0])


class Pacer:
    """Per-origin request spacing with a learned, non-uniform interval.

    Args:
        policy: Timing behaviour.
        seed: Fixes the sequence. For tests only — a deterministic scraper is
            exactly the regular arrival pattern this exists to avoid.
    """

    def __init__(
        self, policy: Optional[PacingPolicy] = None, *, seed: Optional[int] = None
    ) -> None:
        self.policy = policy or PacingPolicy()
        self._random = _seeded_random(seed)
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}
        self._interval: Dict[str, float] = {}
        self._streak: Dict[str, int] = {}

    def interval_for(self, origin: str) -> float:
        """The current target mean for *origin*."""
        with self._lock:
            return self._interval.get(origin, self.policy.interval)

    def learn(self, origin: str, interval: float) -> None:
        """Adopt a mean interval carried over from a previous run."""
        if interval <= 0:
            return
        with self._lock:
            self._interval[origin] = min(interval, self.policy.max_interval)

    def throttled(self, origin: str, retry_after: Optional[float] = None) -> float:
        """Record a throttle and return the interval now in force.

        *retry_after* is used when the server said so, because a number the server
        chose beats one this library guessed. Otherwise the interval is multiplied,
        which converges on the site's real limit in a few observations.
        """
        policy = self.policy
        with self._lock:
            current = self._interval.get(origin, policy.interval) or policy.floor
            if retry_after and retry_after > 0:
                widened = max(current, retry_after)
            else:
                widened = current * policy.backoff_factor
            widened = min(widened, policy.max_interval)
            self._interval[origin] = widened
            self._streak[origin] = 0
            return widened

    def eased(self, origin: str) -> float:
        """Record a success and return the interval now in force.

        The counterpart to :meth:`throttled`, and the reason it exists is that
        without it the learned interval only ever grows. One 429 burst widened an
        origin for the rest of the process, and — because the widened value is what
        gets written to :class:`~scraper.memory.OriginProfile` — for every run after
        it too, so a site that rate-limited once was crawled at up to
        ``max_interval`` for good. The profile's own decay could not undo that: it
        averages towards whatever this pacer reports, which was the widened number.

        Narrowing is bounded below by ``policy.interval``. Going faster than the
        caller asked for is not this module's decision, and an origin already at or
        under that target is left alone.
        """
        policy = self.policy
        with self._lock:
            current = self._interval.get(origin, policy.interval)
            if current <= policy.interval:
                return current

            streak = self._streak.get(origin, 0) + 1
            if streak < policy.recover_after:
                self._streak[origin] = streak
                return current

            self._streak[origin] = 0
            narrowed = max(current * policy.recover_factor, policy.interval)
            self._interval[origin] = narrowed
            return narrowed

    def gap(self, origin: str) -> float:
        """Draw one inter-request gap for *origin*, ignoring elapsed time.

        Separate from :meth:`next_delay` so the distribution is observable on its
        own: the property that matters here is the *shape* of the sequence, and a
        method that also subtracts elapsed time returns zero on a first call and
        hides it.
        """
        policy = self.policy
        mean = self.interval_for(origin)
        if mean <= 0:
            return 0.0

        if self._random.random() < policy.pause_chance:
            gap = mean * policy.pause_scale
        else:
            # Gamma with mean = shape * scale. Positive by construction, mode
            # below the mean, tail above it — arrivals that are irregular in the
            # way real ones are, rather than uniform noise around a constant.
            gap = self._random.gammavariate(policy.shape, mean / policy.shape)
        return min(max(gap, policy.floor), policy.ceiling)

    def next_delay(self, origin: str) -> float:
        """Seconds to wait before the next request to *origin*.

        Zero when enough time has already passed, which is the common case for a
        caller doing its own work between fetches, and always for the first request
        to an origin.
        """
        gap = self.gap(origin)
        if gap <= 0:
            return 0.0
        with self._lock:
            since = time.monotonic() - self._last.get(origin, float("-inf"))
        return max(0.0, gap - since)

    def wait(self, origin: str, signal: Optional[AbortSignal] = None) -> float:
        """Sleep until the next request to *origin* is due. Returns seconds slept.

        Sliced so an abort is honoured promptly: the tail of the distribution can
        be tens of seconds, and a caller cancelling a job should not wait one out.
        """
        if signal is not None and signal.is_set():
            # Checked before the delay is computed, not only inside the loop. A
            # zero-length wait would otherwise let an already-aborted request
            # proceed to send.
            raise Aborted("aborted while pacing")
        delay = self.next_delay(origin)
        remaining = delay
        while remaining > 0:
            if signal is not None and signal.is_set():
                raise Aborted("aborted while pacing")
            step = min(0.25, remaining)
            time.sleep(step)
            remaining -= step
        self.mark(origin)
        return delay

    def mark(self, origin: str) -> None:
        """Record that a request to *origin* just went out."""
        with self._lock:
            self._last[origin] = time.monotonic()


class Trail:
    """The navigation chain, per origin.

    Kept separate from the pacer because it answers a different question: not
    *when* the next request goes out but *where it appears to come from*. A
    sequence of deep pages each arriving with no referrer is a pattern the
    behavioural layer reads directly, and it costs nothing to avoid.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: Dict[str, str] = {}

    def record(self, url: str) -> None:
        """Note that *url* is now the page in view for its origin."""
        host = extract_host(url)
        if host:
            with self._lock:
                self._last[host] = url

    def referer(self, url: str) -> str:
        """The page a navigation to *url* plausibly came from.

        Falls back to the origin's own front page when nothing is recorded yet, which
        is a deliberate departure from what a browser does. A browser opening a typed
        address sends no referrer at all, and this library sent none for exactly that
        reason — but the reason turned out to be worth less than the header.

        Measured across 85 hosts that refuse an impersonated client: supplying this
        recovered three and cost none, and the three are 403-with-a-challenge before
        the header and a full page after it. The whole `Referer`-less position was
        defended on emulation fidelity, and fidelity lost. Whatever these origins are
        checking, a first-contact referrer satisfies it.

        The value is the origin's front page. For a deep page that is the page a
        visitor would have come through; for the front page itself it is the address
        again, which is what a reload sends — a real shape, and the one the three
        recovered hosts were measured with, since all three are front pages.
        """
        host = extract_host(url)
        with self._lock:
            previous = self._last.get(host, "")
        if previous and previous != url:
            return previous
        # No host, nothing to synthesise from: `extract_base` would produce
        # `http:///`, and sending that is worse than sending nothing.
        return extract_base(url) if host else ""

    def headers(self, url: str, *, navigation: bool = True) -> Dict[str, str]:
        """Referrer and fetch-metadata headers for a request to *url*.

        These are values a real navigation carries, so omitting them is the
        anomaly. They are contributed as request headers rather than as identity
        overrides, which means an impersonation profile keeps its own header
        ordering and these land wherever the transport places additions — an
        imperfection worth accepting, since the alternative is a navigation with
        no provenance at all.
        """
        previous = self.referer(url)
        out: Dict[str, str] = {}
        if previous:
            out["referer"] = previous
            same = extract_host(previous) == extract_host(url)
            out["sec-fetch-site"] = "same-origin" if same else "cross-site"
        else:
            out["sec-fetch-site"] = "none"
        if navigation:
            out["sec-fetch-mode"] = "navigate"
            out["sec-fetch-dest"] = "document"
            out["sec-fetch-user"] = "?1"
            out["upgrade-insecure-requests"] = "1"
        else:
            out["sec-fetch-mode"] = "cors"
            out["sec-fetch-dest"] = "empty"
        return out


def warmup_url(url: str) -> str:
    """The homepage a visitor would have arrived through before reaching *url*."""
    return extract_base(url)


def needs_warmup(url: str, warmed_at: float, policy: PacingPolicy) -> bool:
    """Whether *url* should be preceded by a homepage visit.

    A request already aimed at the homepage never needs one, or the warm-up
    recurses.
    """
    if not policy.warmup:
        return False
    if url.rstrip("/") == warmup_url(url).rstrip("/"):
        return False
    return time.time() - warmed_at > policy.warmup_ttl
