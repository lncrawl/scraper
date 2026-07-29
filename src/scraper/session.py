"""The scraper itself: one retrieval loop driven by the model.

Reading order for the loop below, because the sequence is the whole design:

1. Look up what is already known about the origin — the binding layer, the tier
   that worked, the pacing it tolerates, a clearance that may still be alive.
2. Ask the planner where to start. Not the cheapest tier: the cheapest tier that
   covers the layer last found binding.
3. Hold an address for the origin and build an identity pinned to it.
4. Pace, send, and *diagnose* — never react to a status code directly.
5. Hand the diagnosis to the planner and do exactly what it says.
6. On success, write back what was learned so the next run starts here.

The loop is small. Everything interesting is in the modules it calls, which is
deliberate: the parts that encode judgement — what a response means, what a layer
reads, what is worth changing — are testable without a network, and this file is
just the wiring.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

from .browser import BrowserSolver
from .config import ScraperConfig
from .diagnosis import Action, Diagnosis, diagnose, diagnose_transport
from .exceptions import (
    Aborted,
    Blocked,
    Exhausted,
    Impassable,
    Poisoned,
    TierUnavailable,
)
from .exits import ExitLease
from .identity import Clearance, Identity
from .layers import Layer, is_impassable
from .links import Link, TopicGuard, safe_links
from .memory import OriginProfile
from .pacing import needs_warmup, warmup_url
from .planner import Attempt, Context, Decision, Move, Planner, default_capabilities
from .soup import PageSoup
from .state import SharedState
from .tiers import ArchiveTier, Call, ClearanceTier, DirectTier, ManagedTier, Tier
from .transport import TRANSPORT_ERRORS, ImpersonateTransport, Transport
from .utils.file_tools import atomic_write
from .utils.url_tools import extract_base

logger = logging.getLogger(__name__)

_TEXTUAL = ("html", "text/", "json", "xml", "javascript")
_PEEK_BYTES = 64 * 1024


class Scraper:
    """Retrieves pages, escalating only as far as the site actually requires.

    Args:
        origin: The site's base URL. Used for relative resolution and as the
            starting point of the referrer chain.
        config: See :class:`~scraper.ScraperConfig`. The defaults are a working
            configuration for an unprotected or lightly protected site; a
            challenged one needs :attr:`~scraper.ScraperConfig.browser`, and a
            scored one needs :attr:`~scraper.ScraperConfig.exits`.

    A scraper is safe to share between threads. Per-origin state — the held
    address, the identity, the pacing clock — is shared on purpose, because that
    sharing is what keeps one zone seeing one coherent visitor instead of several
    contradictory ones.
    """

    def __init__(
        self,
        origin: str = "",
        config: Optional[ScraperConfig] = None,
        parser: Optional[str] = None,
        state: Optional[SharedState] = None,
    ) -> None:
        self.config = config or ScraperConfig()
        self.origin = origin or ""
        self.parser = parser or self.config.parser
        self.signal = threading.Event()
        self.headers: Dict[str, str] = {}
        self.last_url = self.origin

        self.transport: Transport = self.config.transport or ImpersonateTransport(
            self.config.impersonate,
            prefer_http3=self.config.prefer_http3,
            verify=self.config.verify_tls,
        )
        # Per-origin state is shared when the caller says so, because it describes
        # the site rather than this object. See scraper.state.
        self.state = state or SharedState.create(self.config)
        self._owns_state = state is None
        self.memory = self.state.memory
        self.exits = self.state.exits
        self.pacer = self.state.pacer
        self.trail = self.state.trail

        self._tiers: Dict[str, Tier] = self._build_tiers()
        archive, browser, managed = self.config.capabilities_enabled()
        self.planner = Planner(
            default_capabilities(archive=archive, browser=browser, managed=managed),
            max_attempts=self.config.max_attempts,
            max_rotations=self.config.max_rotations,
            promote_after=self.config.promote_after,
            allow_rotation=self.config.allow_rotation,
        )

        self._identities: Dict[str, Identity] = self.state.identities
        self._guards: Dict[str, TopicGuard] = self.state.guards
        self._lock = self.state.lock

    # -- construction ----------------------------------------------------------------

    def _build_tiers(self) -> Dict[str, Tier]:
        direct = DirectTier(self.transport, botauth=self.config.botauth)
        tiers: Dict[str, Tier] = {direct.name: direct}
        if self.config.archive:
            tiers["archive"] = ArchiveTier(self.transport, max_age=self.config.archive_max_age)
        solver: Optional[BrowserSolver] = self.config.browser
        if solver is not None:
            tiers["clearance"] = ClearanceTier(
                solver,
                direct,
                store=self._remember_clearance,
                profile_root=self.config.profile_root,
                solve_timeout=self.config.solve_timeout,
            )
        if self.config.managed is not None:
            tiers["managed"] = ManagedTier(self.config.managed)
        return tiers

    # -- the retrieval loop ------------------------------------------------------------

    def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Any = None,
        navigation: bool = True,
        stream_to: Optional[Path] = None,
        **options: Any,
    ) -> requests.Response:
        """Retrieve *url*, escalating until it works or the model says stop.

        Args:
            navigation: Whether this reads as a page visit. ``False`` for
                sub-resources — an image or an API call — which changes the fetch
                metadata sent and keeps them out of the referrer chain, since a
                chain that threads through every image is not one a browser
                produces.
            stream_to: Write the body to this path instead of buffering it.
        """
        self._check_signal()
        key = self.memory.key(url)
        profile = self.memory.profile(url)
        self.pacer.learn(key, profile.interval)

        if profile.is_decoy(url):
            raise Poisoned(url, "this URL was recorded as decoy content on an earlier run")

        start = self.planner.start(
            binding=profile.binding,
            preferred=profile.tier,
            exit_reach=self.exits.reach(),
        )
        attempt = Attempt(tier=start.name)

        while True:
            tier = self._tier(attempt.tier)
            lease = self.exits.lease(key)
            identity = self._identity(key, lease)
            call = self._call(
                method,
                url,
                identity=identity,
                lease=lease,
                profile=profile,
                headers=headers,
                timeout=timeout,
                navigation=navigation,
                options=options,
            )

            response: Optional[requests.Response] = None
            try:
                response, diagnosis = self._attempt(tier, call, key, stream_to)
            except Aborted:
                raise
            except TierUnavailable as exc:
                # The tier cannot serve this call. Not the site's doing, so nothing
                # is attributed to a layer and nothing is written to memory.
                diagnosis = Diagnosis(Action.ESCALATE, profile.binding, exc.detail)
            except TRANSPORT_ERRORS as exc:
                diagnosis = diagnose_transport(exc, through_proxy=call.through_proxy)

            with self._lock:
                self._identities[key] = call.identity

            decision = self.planner.react(diagnosis, self._context(key, profile, attempt, url))
            attempt.note(decision)
            logger.debug("%s %s [%s] %s", method.upper(), url, attempt.tier, decision)

            if decision.move is Move.PROCEED:
                assert response is not None
                return self._accept(
                    url, key, profile, response, tier=attempt.tier, navigation=navigation
                )

            if diagnosis.layer is not None and diagnosis.action is not Action.RETRY:
                self.memory.record_failure(url, diagnosis.layer)

            if decision.move is Move.STOP:
                raise self._stop(url, decision, attempt)

            self._apply(decision, diagnosis, key, url, attempt)
            attempt.number += 1

    def _attempt(
        self,
        tier: Tier,
        call: Call,
        key: str,
        stream_to: Optional[Path],
    ) -> Tuple[requests.Response, Diagnosis]:
        """Send once, under the address's concurrency gate and the origin's clock."""
        gate = self.exits.slot(self.exits.lease(key))
        while not gate.acquire(timeout=0.25):
            self._check_signal()
        try:
            # Paced inside the gate on purpose: waiting outside it lets several
            # threads finish their waits together and then arrive as a burst, which
            # is the arrival pattern the pacing exists to avoid.
            self.pacer.wait(key, self.signal)
            if stream_to is not None:
                response = self._download(tier, call, stream_to)
            else:
                response = tier.send(call)
        finally:
            try:
                gate.release()
            except ValueError:  # pragma: no cover - a release without an acquire
                pass

        return response, diagnose(
            status=response.status_code,
            headers=response.headers,
            body=self._peek(response),
            url=call.url,
            user_agent=self._sent_user_agent(response, call),
        )

    def _download(self, tier: Tier, call: Call, target: Path) -> requests.Response:
        """Stream a body to *target*, checking the abort signal between chunks."""
        with tier.stream(call) as (response, chunks):
            if response.status_code >= 400:
                # Do not write an error page to the caller's file. The body is still
                # read so diagnosis can look at it — a challenge interstitial is a
                # body, not a status.
                response._content = b"".join(chunks)[:_PEEK_BYTES]  # noqa: SLF001
                return response
            with atomic_write(target) as handle:
                for chunk in chunks:
                    if self.signal.is_set():
                        raise Aborted("aborted during download")
                    handle.write(chunk)
            response._content = b""  # noqa: SLF001 - the body went to disk
        return response

    # -- decisions ---------------------------------------------------------------------

    def _apply(
        self,
        decision: Decision,
        diagnosis: Diagnosis,
        key: str,
        url: str,
        attempt: Attempt,
    ) -> None:
        """Carry out everything but ``PROCEED`` and ``STOP``."""
        if decision.move is Move.WARM:
            self._warmup(url)
            return

        if decision.move in (Move.BACKOFF, Move.ACCUMULATE):
            # The pacer is given the server's own number, not the decision's wait.
            # The latter falls back to the current interval when the server said
            # nothing, and feeding that back in would mean "widen to what it already
            # is" — a throttle that never widens anything.
            widened = self.pacer.throttled(key, diagnosis.retry_after)
            self.memory.record_failure(url, decision.layer, interval=widened)
            self._sleep(decision.wait or widened)
            return

        if decision.move is Move.ROTATE:
            self.exits.rotate(key, decision.layer)
            attempt.rotations += 1
            with self._lock:
                # The identity and anything bound to it belong to the old address.
                # Dropping them here is what makes the next attempt a clean visitor
                # rather than the previous one wearing a new address.
                self._identities.pop(key, None)
            return

        if decision.move is Move.ESCALATE:
            attempt.tier = decision.tier or attempt.tier
            return

        if decision.wait:
            self._sleep(decision.wait)

    def _stop(self, url: str, decision: Decision, attempt: Attempt) -> Blocked:
        layer = decision.layer
        detail = f"{decision.reason} [{attempt.trail()}]"
        if layer is not None and is_impassable(layer):
            return Impassable(layer, decision.reason, url)
        if layer is None:
            # Nothing to attribute — a proxy credential, an origin that will not
            # answer. Reported against the layer that owns "the operator's own
            # setup" rather than inventing a detection story.
            return Exhausted(Layer.WORKERS, detail, url)
        return Exhausted(layer, detail, url)

    def _accept(
        self,
        url: str,
        key: str,
        profile: OriginProfile,
        response: requests.Response,
        *,
        tier: str,
        navigation: bool,
    ) -> requests.Response:
        # The tier that actually worked, not the one remembered from last time.
        # Recording the stale value is how a run that escalated to a browser starts
        # from scratch on every subsequent run.
        self.memory.record_success(url, tier=tier, interval=self.pacer.interval_for(key))
        if navigation:
            self.trail.record(response.url or url)
            self.last_url = response.url or url
        if response.status_code == 200:
            self._inspect_content(url, key, profile, response)
        if self.config.raise_for_status:
            response.raise_for_status()
        return response

    def _inspect_content(
        self,
        url: str,
        key: str,
        profile: OriginProfile,
        response: requests.Response,
    ) -> None:
        """Learn from the page, and check it is not decoy material.

        The check has to happen on the way out rather than on demand, because the
        layer it defends against produces no error: a caller who has to remember to
        ask will find out from a poisoned dataset instead.
        """
        if not self.config.guard_topic:
            return
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(token in content_type for token in _TEXTUAL):
            return
        text = self._peek(response)
        if not text:
            return

        guard = self._guard(key)
        suspicion = guard.suspect(_visible_text(text, self.parser))
        if suspicion is None:
            guard.learn(_visible_text(text, self.parser))
            return

        profile.note_decoy(url)
        self.memory.touch()
        if self.config.on_decoy == "raise":
            raise Poisoned(url, suspicion)
        if self.config.on_decoy == "warn":
            logger.warning("%s may be decoy content: %s", url, suspicion)

    # -- state -------------------------------------------------------------------------

    def _tier(self, name: str) -> Tier:
        found = self._tiers.get(name)
        if found is None:
            # The planner only names capabilities that were built from the same
            # config, so this is a wiring bug rather than a runtime condition.
            raise KeyError(f"no tier named {name!r}; built {sorted(self._tiers)}")
        return found

    def _identity(self, key: str, lease: ExitLease) -> Identity:
        with self._lock:
            held = self._identities.get(key)
            if held is None or held.exit_id != lease.exit_id:
                held = Identity(impersonate=self.config.impersonate, exit_id=lease.exit_id)
                self._identities[key] = held
            return held

    def _guard(self, key: str) -> TopicGuard:
        with self._lock:
            found = self._guards.get(key)
            if found is None:
                found = TopicGuard()
                self._guards[key] = found
            return found

    def _remember_clearance(self, origin: str, clearance: Clearance) -> None:
        profile = self.memory.profile(origin)
        profile.remember_clearance(clearance)
        self.memory.touch()

    def _call(
        self,
        method: str,
        url: str,
        *,
        identity: Identity,
        lease: ExitLease,
        profile: OriginProfile,
        headers: Optional[Mapping[str, str]],
        timeout: Any,
        navigation: bool,
        options: Dict[str, Any],
    ) -> Call:
        merged: Dict[str, str] = {}
        merged.update({key.lower(): value for key, value in self.headers.items()})
        merged.update(self.trail.headers(url, navigation=navigation))
        for key, value in (headers or {}).items():
            merged[key.lower()] = value

        clearance = profile.clearance_for(extract_base(url))
        if clearance is not None and not clearance.usable_by(identity):
            # Kept out of the call rather than sent and rejected. A clearance under
            # the wrong identity produces a challenge, and a challenge here would
            # read as the solver having failed.
            logger.debug("dropping clearance for %s: %s", url, clearance.why_not(identity))
            clearance = None

        return Call(
            method=method,
            url=url,
            identity=identity,
            headers=merged,
            proxies=lease.proxies,
            clearance=clearance,
            timeout=timeout if timeout is not None else self.config.timeout,
            options=options,
            signal=self.signal,
        )

    def _context(self, key: str, profile: OriginProfile, attempt: Attempt, url: str) -> Context:
        return Context(
            tier=attempt.tier,
            attempt=attempt.number,
            exit_reach=self.exits.reach(),
            consecutive_failures=profile.consecutive_failures,
            rotations=attempt.rotations,
            warmed=not needs_warmup(url, profile.warmed_at, self.config.pacing),
            interval=self.pacer.interval_for(key),
        )

    def _warmup(self, url: str) -> None:
        """Visit the origin's homepage, the way a visitor would have arrived."""
        home = warmup_url(url)
        logger.debug("warming up %s before %s", home, url)
        try:
            self.fetch("GET", home, navigation=True)
        except (Blocked, Poisoned, requests.HTTPError) as exc:
            # A homepage that will not load is worth knowing about but is not
            # itself the failure: the deep page may still work, and refusing to
            # try would turn a soft signal into a hard stop.
            logger.debug("warm-up of %s failed: %s", home, exc)
        self.memory.mark_warmed(url)

    # -- helpers ------------------------------------------------------------------------

    def _sleep(self, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            self._check_signal()
            step = min(0.25, remaining)
            time.sleep(step)
            remaining -= step

    def _check_signal(self) -> None:
        if self.signal.is_set():
            raise Aborted("aborted by signal")

    @staticmethod
    def _peek(response: requests.Response) -> str:
        """A bounded, decoded prefix of the body, safe on binary content."""
        try:
            raw = response.content[:_PEEK_BYTES]
        except Exception:  # noqa: BLE001 - a consumed or broken stream is not fatal here
            return ""
        return raw.decode(response.encoding or "utf-8", "ignore")

    @staticmethod
    def _sent_user_agent(response: requests.Response, call: Call) -> str:
        sent = getattr(response.request, "headers", None) or {}
        return str(sent.get("user-agent") or sent.get("User-Agent") or call.identity.user_agent)

    # -- public surface -----------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.fetch("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("navigation", False)
        return self.fetch("POST", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("navigation", False)
        return self.fetch("HEAD", url, **kwargs)

    def ping(self, url: str, **kwargs: Any) -> requests.Response:
        """A reachability check. Cheap, but it still counts as a request to the origin."""
        kwargs.setdefault("timeout", 10)
        return self.head(url, **kwargs)

    def submit_form(
        self,
        url: str,
        data: Any = None,
        *,
        json: Any = None,
        multipart: bool = False,
        headers: Optional[Mapping[str, str]] = None,
        **kwargs: Any,
    ) -> requests.Response:
        merged = {key.lower(): value for key, value in (headers or {}).items()}
        merged.setdefault(
            "content-type",
            "multipart/form-data"
            if multipart
            else "application/x-www-form-urlencoded; charset=UTF-8",
        )
        return self.post(url, data=data, json=json, headers=merged, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        headers = {"accept": "application/json, text/plain, */*"}
        headers.update({k.lower(): v for k, v in (kwargs.pop("headers", None) or {}).items()})
        kwargs.setdefault("navigation", False)
        return self.fetch("GET", url, headers=headers, **kwargs).json()

    def post_json(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
        }
        headers.update({k.lower(): v for k, v in (kwargs.pop("headers", None) or {}).items()})
        return self.post(url, data=data, headers=headers, **kwargs).json()

    def make_soup(self, data: Any, encoding: Optional[str] = None) -> PageSoup:
        return PageSoup.create(data, encoding, self.parser)

    def get_soup(self, url: str, encoding: Optional[str] = None, **kwargs: Any) -> PageSoup:
        headers = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        headers.update({k.lower(): v for k, v in (kwargs.pop("headers", None) or {}).items()})
        response = self.fetch("GET", url, headers=headers, **kwargs)
        return self.make_soup(response, encoding)

    def post_soup(
        self, url: str, data: Any = None, encoding: Optional[str] = None, **kwargs: Any
    ) -> PageSoup:
        response = self.post(url, data=data, **kwargs)
        return self.make_soup(response, encoding)

    def get_file(self, url: str, output_file: Any, **kwargs: Any) -> Path:
        """Download *url* to *output_file*, written atomically."""
        target = Path(output_file)
        kwargs.setdefault("navigation", False)
        self.fetch("GET", url, stream_to=target, **kwargs)
        return target

    def get_image(self, url: str, **kwargs: Any) -> Any:
        """Download *url* and return a PIL image. Needs the ``image`` extra."""
        from PIL import Image

        if url.startswith("data:"):
            return Image.open(BytesIO(base64.b64decode(url.split("base64,")[-1])))
        headers = {"accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
        headers.update({k.lower(): v for k, v in (kwargs.pop("headers", None) or {}).items()})
        kwargs.setdefault("navigation", False)
        response = self.fetch("GET", url, headers=headers, **kwargs)
        return Image.open(BytesIO(response.content))

    def links(self, source: Any, base_url: str = "", **kwargs: Any) -> List[Link]:
        """Links from *source* a person could actually click.

        Recorded decoys are dropped as well as hidden ones, so a URL that poisoned
        an earlier run does not come back through the frontier on this one.
        """
        base = base_url or self.last_url or self.origin
        found = safe_links(source, base, **kwargs)
        profile = self.memory.profile(base) if base else None
        if profile is None:
            return found
        return [link for link in found if not profile.is_decoy(link.url)]

    # -- introspection -------------------------------------------------------------------

    def knows(self, url: str) -> OriginProfile:
        """What has been learned about *url*'s origin."""
        return self.memory.profile(url)

    def explain(self, url: str) -> str:
        """A human-readable account of the current strategy for *url*'s origin.

        The counterpart to a good error message: after a run, this is how you see
        which layer the library concluded was binding and why it is spending effort
        where it is.
        """
        profile = self.knows(url)
        key = self.memory.key(url)
        binding = profile.binding
        lines = [f"{key}"]
        if binding is None:
            lines.append("  binding layer : nothing has blocked yet")
        else:
            detail = binding_summary(binding)
            lines.append(f"  binding layer : {binding} — {detail}")
        lines.append(f"  tier          : {profile.tier or 'direct (unproven)'}")
        lines.append(f"  pacing        : {self.pacer.interval_for(key):.1f}s mean interval")
        lines.append(f"  requests      : {profile.successes} ok / {profile.failures} failed")
        clearance = profile.clearance_for(extract_base(url))
        if clearance is None:
            lines.append("  clearance     : none")
        else:
            lines.append(
                f"  clearance     : {max(0.0, clearance.expires_at - time.time()):.0f}s left"
            )
        ladder = " ".join(
            f"{cap.name}({cap.cost})" for cap in self.planner.ladder(self.exits.reach())
        )
        lines.append(f"  ladder        : {ladder}")
        lines.append(f"  exits         : {self.exits.best_kind.value}")
        guard = self._guards.get(key)
        if guard is not None:
            lines.append(f"  topic guard   : {guard.samples} pages learned")
        return "\n".join(lines)

    # -- lifecycle -------------------------------------------------------------------------

    @property
    def cookies(self) -> Any:
        """The transport's cookie jar.

        Read-mostly. A clearance is *not* installed here — it is bound to one
        identity, and a jar outlives identities.
        """
        return self.transport.cookies

    def set_cookie(self, name: str, value: str, domain: str = "") -> None:
        self.transport.set_cookie(name, value, domain=domain)

    def abort(self) -> None:
        """Stop everything in flight, including downloads mid-stream."""
        self.signal.set()

    def close(self) -> None:
        for tier in set(self._tiers.values()):
            try:
                tier.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                logger.debug("tier %s did not close cleanly", tier.name, exc_info=True)
        if self._owns_state:
            # Shared state outlives any one scraper. Flushing another scraper's
            # memory here would be harmless, but closing it is not.
            self.state.close()
        else:
            self.memory.flush()

    def __enter__(self) -> "Scraper":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def binding_summary(layer: Layer) -> str:
    """One line on what *layer* reads and what moves it."""
    from .layers import info

    facts = info(layer)
    return f"reads a {facts.trait.value} property, {facts.stance.value}"


def _visible_text(html: str, parser: str) -> str:
    """Text a person would read, with script and style content dropped.

    Feeding raw markup to the topic guard measures the vocabulary of the site's
    JavaScript, which is identical across every page and would make every page look
    on-topic.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, parser)
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001 - unparseable markup still has words in it
        return html
