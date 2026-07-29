"""Where the packets come from, and keeping it still.

The first layer scores the source address, and the difficulty there is economic
rather than technical: the address is chosen freely, but the reputation attached
to it accrued over time and can be rented, never fabricated. So an exit is
described by *kind*, and the kind decides which layers it can pass. A datacenter
range is cheap to block because almost no human traffic originates there. A
mobile-carrier address fronts thousands of real subscribers behind one NAT, so
blocking it causes collateral damage — which is what makes it the good one.

The second thing this module exists for is stickiness. A rotating proxy is
actively harmful once anything is bound to the address: a clearance earned on one
exit is rejected from the next, and the accumulated per-zone history that the
behavioural layer reads is reset every time the address moves. So exits are
leased **per origin** and held: all traffic to one zone leaves from one address
for as long as that address keeps working, and rotation is something that happens
on evidence, not on a timer.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional

from .layers import Layer, expand
from .utils.url_tools import extract_host

logger = logging.getLogger(__name__)

POOL_API_TIMEOUT = 5.0
"""Pool bookkeeping is never the critical path, so it gets a short leash."""


class ExitKind(str, Enum):
    """What an address looks like to a reputation database.

    Ordered worst to best deliberately: comparison is meaningful, and the pool
    prefers the highest-ranked kind that is available.
    """

    TOR = "tor"
    DATACENTER = "datacenter"
    DIRECT = "direct"
    ISP = "isp"
    RESIDENTIAL = "residential"
    MOBILE = "mobile"

    @property
    def rank(self) -> int:
        order = list(ExitKind)
        return order.index(self)

    @property
    def reach(self) -> FrozenSet[Layer]:
        """Which layers this kind of address can get past.

        The one that matters: a datacenter or Tor address does not clear the
        reputation layer, and no amount of transport fidelity compensates, because
        reputation is not something the client emits. A tier running on one of
        those cannot honestly claim to reach layer 1, and the planner uses exactly
        that to stop recommending remedies that cannot work.
        """
        if self in (ExitKind.TOR, ExitKind.DATACENTER):
            return frozenset()
        return frozenset({Layer.IP_REPUTATION})


@dataclass(frozen=True)
class ExitSpec:
    """One configured way out.

    Args:
        url: A proxy URL. ``socks5h://`` keeps DNS resolution at the exit, which
            is what you want: resolving locally leaks the lookup and can pin you
            to a different answer than the exit would have got.
        kind: Be honest here. Claiming ``MOBILE`` for a datacenter range does not
            change what the reputation database thinks, it only stops this
            library from telling you that layer 1 is the reason nothing works.
        label: Optional human-readable name for logs.
    """

    url: str = ""
    kind: ExitKind = ExitKind.DIRECT
    label: str = ""

    def __post_init__(self) -> None:
        if self.url and "://" not in self.url:
            raise ValueError(f"proxy URL needs a scheme: {self.url!r}")

    @property
    def name(self) -> str:
        return self.label or (extract_host(self.url) if self.url else "direct")


@dataclass(frozen=True)
class TorPoolSpec(ExitSpec):
    """A `tor-pool <https://github.com/lncrawl/tor-pool>`_ endpoint.

    Many Tor instances behind one port. The SOCKS5 username is a session key and
    the caller stays pinned to one instance, and therefore one exit IP, until it
    asks to move; rotation goes through the pool's API and reassigns to an
    already-built instance rather than waiting out the circuit-rebuild cooldown.

    Reported as :attr:`ExitKind.TOR`, which is the truth and worth stating
    plainly: Tor exit lists are published, so this clears none of the reputation
    layer. It is the right tool for a site that does not score addresses and the
    wrong one for a site that does.

    Args:
        token: A ``proxy``-scoped token. Sent as the SOCKS5 password and as a
            bearer token on the pool's API. Without it the pool answers 401 and,
            because the API calls are best-effort, failure reporting stops
            silently while both sides still look healthy.
    """

    url: str = "socks5h://127.0.0.1:9250"
    kind: ExitKind = ExitKind.TOR
    api_url: str = "http://127.0.0.1:8080"
    token: str = ""
    report_failures: bool = True


@dataclass
class ExitLease:
    """A held address. Stable for as long as it keeps working."""

    spec: ExitSpec
    session_key: str
    exit_id: str
    leased_at: float = field(default_factory=time.time)
    requests: int = 0

    @property
    def proxies(self) -> Optional[Dict[str, str]]:
        """A requests-style proxies mapping, or ``None`` for a direct connection."""
        if not self.spec.url:
            return None
        url = self.spec.url
        if isinstance(self.spec, TorPoolSpec):
            url = with_credentials(url, self.session_key, self.spec.token)
        # Both schemes must route through the same entry, or http and https
        # requests to one origin leave from different exits and everything bound
        # to the address comes apart.
        return {"http": url, "https": url}

    @property
    def kind(self) -> ExitKind:
        return self.spec.kind


# What tor-pool should weigh a report by. The pool cannot see inside an HTTPS
# tunnel, so this is the only signal that reaches it for a soft block — and the
# weighting is why the layer has to be translated rather than sent as a generic
# failure. A throttle says the exit works and is busy; sent as a block it retires
# a working exit and the replacement is throttled just the same.
_FAILURE_KINDS: Dict[Layer, str] = {
    Layer.IP_REPUTATION: "blocked",
    Layer.BOT_FIGHT: "blocked",
    Layer.SUPER_BOT_FIGHT: "blocked",
    Layer.BOT_MANAGEMENT: "blocked",
    Layer.MANAGED_CHALLENGE: "captcha",
    Layer.TURNSTILE: "captcha",
    Layer.UNDER_ATTACK: "captcha",
    Layer.CDP: "captcha",
    Layer.BEHAVIOURAL: "rate_limited",
}


def failure_kind(layer: Optional[Layer]) -> str:
    """Translate a binding layer into the pool's vocabulary."""
    if layer is None:
        return "transport"
    return _FAILURE_KINDS.get(layer, "other")


class ExitPool:
    """Leases exits, one per origin, and retires them on evidence.

    Args:
        specs: Configured exits, best kind first after sorting. An empty list
            means direct connections, which is fine for an unprotected site and
            hopeless against a scored one.
        max_sessions_per_exit: How many concurrent requests may share one
            address. Concurrent-sessions-per-address is itself a behavioural
            signal, so this stays in the low single digits; the value is clamped
            rather than trusted.
    """

    def __init__(
        self,
        specs: Optional[List[ExitSpec]] = None,
        *,
        max_sessions_per_exit: int = 2,
        retire_for: float = 600.0,
    ) -> None:
        self._specs = sorted(specs or [], key=lambda spec: -spec.kind.rank)
        self._cap = max(1, min(3, max_sessions_per_exit))
        self._retire_for = retire_for
        self._lock = threading.Lock()
        self._leases: Dict[str, ExitLease] = {}
        self._retired: Dict[str, float] = {}
        self._slots: Dict[str, threading.BoundedSemaphore] = {}

    @property
    def configured(self) -> bool:
        return bool(self._specs)

    @property
    def rotatable(self) -> bool:
        """Whether there is anywhere else to go.

        A single plain proxy, or no proxy at all, has no alternative: "rotating" lands
        on the same address, and doing that twice before giving up wastes requests on a
        host that already said no. A pool endpoint is different — it reassigns the
        session to another instance behind the same URL, so one spec is still several
        addresses.
        """
        with self._lock:
            usable = [spec for spec in self._specs if spec.name not in self._retired]
        if any(isinstance(spec, TorPoolSpec) for spec in usable):
            return True
        return len(usable) > 1

    @property
    def best_kind(self) -> ExitKind:
        """The best address kind on offer, which caps what layer 1 can be told."""
        available = [spec.kind for spec in self._specs if spec.name not in self._retired]
        return max(available, key=lambda kind: kind.rank) if available else ExitKind.DIRECT

    def reach(self) -> FrozenSet[Layer]:
        """Layers the currently available addresses can pass."""
        return expand(self.best_kind.reach)

    def lease(self, origin: str) -> ExitLease:
        """The address for *origin*, creating and pinning one on first use."""
        with self._lock:
            self._restore_locked()
            lease = self._leases.get(origin)
            if lease is not None:
                lease.requests += 1
                return lease
            lease = self._new_lease_locked(origin)
            self._leases[origin] = lease
            return lease

    def slot(self, lease: ExitLease) -> threading.BoundedSemaphore:
        """The concurrency gate for *lease*'s address."""
        with self._lock:
            gate = self._slots.get(lease.exit_id)
            if gate is None:
                gate = threading.BoundedSemaphore(self._cap)
                self._slots[lease.exit_id] = gate
            return gate

    def rotate(self, origin: str, layer: Optional[Layer] = None) -> ExitLease:
        """Move *origin* to a different address, reporting why first.

        The report goes out before the move because it is what lets a pool retire
        the instance for every other caller. Skipping it means the next consumer
        leases the exit that just failed.
        """
        with self._lock:
            old = self._leases.get(origin)
        if old is not None:
            self.report(old, layer)
            if isinstance(old.spec, TorPoolSpec) and self._rotate_pool(old):
                # The pool reassigned the session to another instance behind the
                # same endpoint, so the URL is unchanged and only the exit moved.
                # A new lease object still matters: the exit_id is what a
                # clearance is bound to, and it is no longer the same address.
                return self._replace(origin, old.spec)
            with self._lock:
                if len(self._specs) > 1:
                    self._retired[old.spec.name] = time.monotonic()
        return self._replace(origin, None)

    def report(self, lease: ExitLease, layer: Optional[Layer]) -> None:
        """Tell the provider what this exit was blamed for.

        Best-effort by design: a pool that is down must never break a scrape.
        """
        spec = lease.spec
        if not isinstance(spec, TorPoolSpec) or not spec.report_failures:
            return
        kind = failure_kind(layer)
        self._pool_request(
            spec,
            f"/api/sessions/{urllib.parse.quote(lease.session_key, safe='')}/failure",
            {"reason": str(layer) if layer else "transport", "kind": kind},
        )

    def release(self, origin: str) -> None:
        """Give up the address held for *origin* without blaming it."""
        with self._lock:
            lease = self._leases.pop(origin, None)
        if lease is not None:
            self._drop(lease)

    def release_all(self) -> None:
        """Give up every held address. Called when a scraper closes.

        A pooled session that is never released holds its slot until the pool's
        `SESSION_TTL`, and each lease mints a fresh key — so a process that builds
        several scrapers in a row walks the pool out of capacity. What that looks like
        downstream is the part worth avoiding: the next lease cannot connect, a
        transport failure through a proxy is evidence about the exit, and the model
        reports a reputation block on a destination that never saw the request.
        """
        with self._lock:
            leases = list(self._leases.values())
            self._leases.clear()
        for lease in leases:
            self._drop(lease)

    def _drop(self, lease: ExitLease) -> None:
        """Tell a pool we are finished with a session. Best-effort, like `report`."""
        spec = lease.spec
        if not isinstance(spec, TorPoolSpec):
            return
        self._pool_request(
            spec,
            f"/api/sessions/{urllib.parse.quote(lease.session_key, safe='')}",
            method="DELETE",
        )

    # -- internals ----------------------------------------------------------------

    def _replace(self, origin: str, avoid: Optional[ExitSpec]) -> ExitLease:
        with self._lock:
            self._leases.pop(origin, None)
            self._restore_locked()
            lease = self._new_lease_locked(origin, avoid=avoid)
            self._leases[origin] = lease
            return lease

    def _new_lease_locked(self, origin: str, avoid: Optional[ExitSpec] = None) -> ExitLease:
        spec = self._pick_locked(avoid)
        key = f"s-{uuid.uuid4().hex[:12]}"
        # The exit identifier has to change whenever the address might have, and
        # for a pooled endpoint the URL alone never changes. Folding the session
        # key in is what makes a rotation visible to everything downstream that
        # is bound to the address.
        exit_id = f"{spec.name}#{key}" if spec.url else "direct"
        return ExitLease(spec=spec, session_key=key, exit_id=exit_id)

    def _pick_locked(self, avoid: Optional[ExitSpec]) -> ExitSpec:
        candidates = [
            spec
            for spec in self._specs
            if spec.name not in self._retired and (avoid is None or spec is not avoid)
        ]
        if not candidates:
            candidates = [spec for spec in self._specs if spec.name not in self._retired]
        if not candidates:
            # Every exit is retired. Falling back to direct would silently drop
            # the whole reputation strategy, so the caller is told instead.
            if self._specs:
                oldest = min(self._retired, key=lambda name: self._retired[name])
                self._retired.pop(oldest, None)
                candidates = [spec for spec in self._specs if spec.name == oldest]
            else:
                return ExitSpec()
        return candidates[0]

    def _restore_locked(self) -> None:
        if not self._retired:
            return
        now = time.monotonic()
        for name in [n for n, at in self._retired.items() if now - at >= self._retire_for]:
            del self._retired[name]

    def _rotate_pool(self, lease: ExitLease) -> bool:
        spec = lease.spec
        assert isinstance(spec, TorPoolSpec)
        path = f"/api/sessions/{urllib.parse.quote(lease.session_key, safe='')}/rotate"
        body = self._pool_request(spec, path)
        if body is None:
            return False
        logger.debug("tor-pool moved %s to instance %s", lease.session_key, body.get("instance"))
        return True

    def _pool_request(
        self,
        spec: TorPoolSpec,
        path: str,
        payload: Optional[dict] = None,
        *,
        method: str = "POST",
    ) -> Optional[dict]:
        url = spec.api_url.rstrip("/") + path
        data = json.dumps(payload).encode() if payload is not None else b""
        headers = {"content-type": "application/json"}
        if spec.token:
            headers["authorization"] = f"Bearer {spec.token}"
        request = urllib.request.Request(  # noqa: S310 - operator-configured URL
            url, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=POOL_API_TIMEOUT) as resp:  # noqa: S310
                raw = resp.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Called out because it degrades silently otherwise: without a
                # valid token the pool stops hearing about soft blocks, burnt
                # exits keep taking traffic, and neither side looks broken.
                logger.error(
                    "tor-pool rejected the credential for %s (%s). Set TorPoolSpec.token to a "
                    "proxy-scoped token; rotation and failure reporting are not working.",
                    path,
                    exc.code,
                )
            elif exc.code == 404:
                # Routine: acting on a report the pool unpins the session, so the
                # next report has nothing to attach to and the next request
                # re-pins to a healthy instance.
                logger.debug("tor-pool has no session for %s", path)
            else:
                logger.warning("tor-pool request to %s failed: %s", path, exc)
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            logger.warning("tor-pool request to %s failed: %s", path, exc)
            return None


def with_credentials(url: str, username: str, password: str) -> str:
    """Return *url* carrying *username* and *password* as userinfo.

    Credentials already in the URL win: an explicitly configured username is the
    operator naming their own session, and they own the password that goes with
    it.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or not username:
        return url

    host = parsed.hostname or ""
    if ":" in host:  # IPv6 literals must stay bracketed
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"

    userinfo = urllib.parse.quote(username, safe="")
    if password:
        userinfo = f"{userinfo}:{urllib.parse.quote(password, safe='')}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment)
    )
