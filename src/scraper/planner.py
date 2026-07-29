"""Deciding what to change, given what is actually blocking.

This is where the model earns its keep, and it is the part that differs most from
how a scraper normally behaves. The usual shape is a table from status code to
remedy: 403 rotates the proxy, 429 sleeps, a challenge re-solves. That table is
wrong in a specific and expensive way — it spends effort on layers that are not
the binding constraint, and two of its entries make things worse.

The bound says admission is limited by the weakest layer, so raising anything else
changes nothing until the weakest one moves. :func:`~scraper.layers.marginal_gain`
is the arithmetic; this module is the policy built on it. Three rules follow, and
each contradicts something the naive table does:

**A possessed property is never rotated away from.** If the binding layer reads
accumulated history, discarding identity resets exactly what is being measured.
The naive table's reflex — new address, try again — guarantees the history never
gets long enough to pass. So rotation is vetoed there, and the answer is to hold
still and slow down.

**Rotation needs somewhere better to go.** If every configured address is a
datacenter or Tor range, the next one is on the same blocklists as the last one.
The gain is zero, and doing it anyway burns the pool and produces a run that looks
busy and gets nowhere. Better to say so.

**Escalation only goes to a tier that reaches the layer.** Launching a browser
against a reputation block accomplishes nothing except the launch, because a
browser does not change where the packets come from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence

from .diagnosis import Action, Diagnosis
from .layers import FORGEABLE, IMPASSABLE, Layer, Trait, expand, is_impassable, trait

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capability:
    """A tier, described by what it can get past and what it costs.

    Args:
        cost: Relative expense, used only for ordering. The scale is arbitrary;
            the gaps are what matter, and they should reflect real cost — a browser
            launch against an HTTP request is orders of magnitude, not a tick.
        reach: Layers this tier can pass. Closed over the transport group on
            construction, because no technique satisfies one of layers 2-5 without
            the others.
    """

    name: str
    cost: int
    reach: FrozenSet[Layer] = frozenset()

    def covers(self, layer: Optional[Layer]) -> bool:
        return layer is None or layer in self.reach

    def with_reach(self, extra: FrozenSet[Layer]) -> "Capability":
        """A copy that also reaches *extra*.

        Used to fold in what the configured addresses provide: a tier's own reach
        does not include the reputation layer, the exit it runs on does.
        """
        return Capability(name=self.name, cost=self.cost, reach=expand(self.reach | extra))


class Move(Enum):
    """What the pipeline should do about a diagnosis."""

    PROCEED = "proceed"
    RETRY = "retry"
    BACKOFF = "backoff"
    WARM = "warm"
    ACCUMULATE = "accumulate"
    ROTATE = "rotate"
    ESCALATE = "escalate"
    STOP = "stop"


@dataclass(frozen=True)
class Decision:
    """One instruction, with the reasoning attached.

    *reason* is not decoration. Every stop in this library is meant to tell the
    caller which layer ended the attempt and what would change it, because "403
    after 3 retries" is the message that sends people to rewrite the part that was
    already working.
    """

    move: Move
    tier: str = ""
    wait: float = 0.0
    layer: Optional[Layer] = None
    reason: str = ""

    def __str__(self) -> str:
        parts = [self.move.value]
        if self.tier:
            parts.append(f"-> {self.tier}")
        if self.wait:
            parts.append(f"after {self.wait:.1f}s")
        head = " ".join(parts)
        return f"{head}: {self.reason}" if self.reason else head


@dataclass
class Context:
    """What the planner needs to know beyond the diagnosis itself."""

    tier: str
    attempt: int = 1
    exit_reach: FrozenSet[Layer] = frozenset()
    consecutive_failures: int = 0
    rotations: int = 0
    warmed: bool = True
    interval: float = 0.0


class Planner:
    """Chooses the cheapest capability that can reach the binding layer.

    Args:
        capabilities: Available tiers. Order is irrelevant; cost decides.
        max_attempts: Total attempts for one retrieval, across all tiers.
        max_rotations: Addresses to spend on one retrieval. Low on purpose:
            burning through a pool one request at a time is the signature of a
            misdiagnosis, not of an unlucky exit.
        promote_after: Consecutive failures at an already-covered emitted layer
            before the diagnosis is re-attributed to the per-zone composite. The
            scoring tiers are indistinguishable from outside, so recurrence is the
            only evidence available, and it needs history to be visible at all.
    """

    def __init__(
        self,
        capabilities: Sequence[Capability],
        *,
        max_attempts: int = 5,
        max_rotations: int = 2,
        promote_after: int = 3,
        allow_rotation: bool = True,
    ) -> None:
        if not capabilities:
            raise ValueError("a planner needs at least one capability")
        self._by_cost: List[Capability] = sorted(capabilities, key=lambda cap: cap.cost)
        self._by_name: Dict[str, Capability] = {cap.name: cap for cap in self._by_cost}
        self.max_attempts = max(1, max_attempts)
        self.max_rotations = max(0, max_rotations)
        self.promote_after = max(1, promote_after)
        self.allow_rotation = allow_rotation

    @property
    def capabilities(self) -> List[Capability]:
        return list(self._by_cost)

    def cheapest(self) -> Capability:
        return self._by_cost[0]

    def get(self, name: str) -> Optional[Capability]:
        return self._by_name.get(name)

    def ladder(self, exit_reach: FrozenSet[Layer] = frozenset()) -> List[Capability]:
        """Every capability, cheapest first, with the exits' reach folded in."""
        return [cap.with_reach(exit_reach) for cap in self._by_cost]

    def start(
        self,
        *,
        binding: Optional[Layer] = None,
        preferred: str = "",
        exit_reach: FrozenSet[Layer] = frozenset(),
    ) -> Capability:
        """The tier to begin with.

        *binding* and *preferred* come from what a previous run learned. Starting
        from the cheapest tier every time means rediscovering the same conclusion
        with the same number of failed requests, and those requests are what the
        behavioural layer is counting.
        """
        ladder = self.ladder(exit_reach)
        if preferred:
            for cap in ladder:
                if cap.name == preferred and cap.covers(binding):
                    return cap
        if binding is not None and not is_impassable(binding):
            for cap in ladder:
                if cap.covers(binding):
                    return cap
        return ladder[0]

    def react(self, diagnosis: Diagnosis, context: Context) -> Decision:
        """Translate a diagnosis into the next move."""
        if diagnosis.ok:
            return Decision(Move.PROCEED)

        layer = diagnosis.layer
        if diagnosis.action is Action.REFUSE:
            return Decision(Move.STOP, layer=layer, reason=diagnosis.detail or "no bypass exists")
        if layer is not None and is_impassable(layer):
            return Decision(Move.STOP, layer=layer, reason="no bypass exists")

        if context.attempt >= self.max_attempts:
            return Decision(
                Move.STOP,
                layer=layer,
                reason=f"gave up after {context.attempt} attempts; {diagnosis.detail}",
            )

        if diagnosis.action is Action.RETRY:
            return Decision(
                Move.RETRY,
                tier=context.tier,
                wait=diagnosis.retry_after or 0.0,
                layer=layer,
                reason=diagnosis.detail,
            )

        if diagnosis.action is Action.BACKOFF or (
            layer is not None and trait(layer) is Trait.POSSESS
        ):
            return self._slow_down(diagnosis, context)

        current = self._current(context)
        promoted = self._promote(diagnosis, context, current)
        if promoted is not None:
            layer = promoted
            diagnosis = Diagnosis(Action.ESCALATE, layer, "the same layer keeps binding")

        if diagnosis.action is Action.ROTATE:
            return self._rotate(diagnosis, context)

        return self._escalate(diagnosis, context, current)

    # -- individual moves ------------------------------------------------------------

    def _current(self, context: Context) -> Capability:
        found = self._by_name.get(context.tier)
        if found is None:
            return self.cheapest()
        return found.with_reach(context.exit_reach)

    def _slow_down(self, diagnosis: Diagnosis, context: Context) -> Decision:
        """The possess-side answer: hold identity still and let history accrue.

        Notably *not* a rotation, even though the naive reading of a block is that
        the address is spent. Whatever standing this address has built is the only
        asset in play, and discarding it restarts the clock on the one layer that
        cannot be hurried.
        """
        if not context.warmed:
            return Decision(
                Move.WARM,
                tier=context.tier,
                layer=diagnosis.layer,
                reason="arriving cold at a deep page; visit the homepage first",
            )
        wait = diagnosis.retry_after or max(context.interval, 1.0)
        return Decision(
            Move.ACCUMULATE if diagnosis.action is not Action.BACKOFF else Move.BACKOFF,
            tier=context.tier,
            wait=wait,
            layer=diagnosis.layer,
            reason=diagnosis.detail or "the binding layer reads accumulated history",
        )

    def _rotate(self, diagnosis: Diagnosis, context: Context) -> Decision:
        if not self.allow_rotation:
            return Decision(
                Move.STOP,
                layer=diagnosis.layer,
                reason="rotation is disabled and the address is blocked",
            )
        if context.rotations >= self.max_rotations:
            return Decision(
                Move.STOP,
                layer=diagnosis.layer,
                reason=(
                    f"{context.rotations} address(es) blocked for the same reason; "
                    "the address kind is the constraint, not the individual address"
                ),
            )
        if Layer.IP_REPUTATION not in context.exit_reach:
            # The check that stops a pointless loop. Every address on offer is in a
            # published range, so the replacement is blocklisted for the same reason
            # as the one being replaced. Naming that is more useful than proving it
            # one exit at a time.
            return Decision(
                Move.STOP,
                layer=Layer.IP_REPUTATION,
                reason=(
                    "no configured exit clears the reputation layer — datacenter and "
                    "Tor ranges are published, so rotating between them cannot help. "
                    "A residential or mobile-carrier exit is the only thing that moves "
                    "this layer"
                ),
            )
        return Decision(
            Move.ROTATE,
            tier=context.tier,
            layer=diagnosis.layer,
            reason=diagnosis.detail or "the address is spent",
        )

    def _escalate(self, diagnosis: Diagnosis, context: Context, current: Capability) -> Decision:
        layer = diagnosis.layer
        stronger = [
            cap
            for cap in self.ladder(context.exit_reach)
            if cap.cost > current.cost and cap.covers(layer)
        ]
        if stronger:
            return Decision(
                Move.ESCALATE,
                tier=stronger[0].name,
                layer=layer,
                reason=diagnosis.detail or f"{current.name} cannot reach this layer",
            )

        if current.covers(layer) and diagnosis.action is Action.SOLVE:
            # The tier that owns this layer is already in use and the challenge came
            # back anyway. Almost always the clearance was earned under a different
            # identity, so re-solving on the identity actually in force is the fix,
            # not a stronger tier.
            return Decision(
                Move.RETRY,
                tier=current.name,
                layer=layer,
                reason="re-solving on the current identity",
            )

        return Decision(
            Move.STOP,
            layer=layer,
            reason=self._nothing_left(layer, current),
        )

    def _promote(
        self, diagnosis: Diagnosis, context: Context, current: Capability
    ) -> Optional[Layer]:
        """Re-attribute a repeatedly-failing emitted layer to the composite model.

        Returns the new layer, or ``None`` to leave the diagnosis alone. The
        scoring tiers cannot be told apart from outside, so a transport profile
        that keeps being rejected while a stronger tier exists is the only
        available evidence that the zone is running the per-zone composite.
        """
        layer = diagnosis.layer
        if layer is None or layer not in FORGEABLE or layer is Layer.AI_BOT_BLOCKER:
            return None
        if not current.covers(layer):
            return None
        if context.consecutive_failures < self.promote_after:
            return None
        logger.debug(
            "%s keeps binding under %s after %d failures; treating it as the per-zone model",
            layer,
            current.name,
            context.consecutive_failures,
        )
        return Layer.BOT_MANAGEMENT

    def _nothing_left(self, layer: Optional[Layer], current: Capability) -> str:
        if layer is None:
            return f"{current.name} failed and there is no stronger tier configured"
        missing = [
            cap.name for cap in self._by_cost if layer in cap.reach and cap.cost > current.cost
        ]
        if missing:
            return f"{layer} needs {', '.join(missing)}, which is not enabled"
        return (
            f"{layer} is beyond every configured tier ({current.name} is the strongest). "
            f"{_hint(layer)}"
        )


_HINTS: Dict[Layer, str] = {
    Layer.IP_REPUTATION: "Configure a residential or mobile-carrier exit.",
    Layer.BROWSER_JS: "Configure a browser solver.",
    Layer.CDP: "Configure a browser solver that does not drive over the standard "
    "automation channel.",
    Layer.BEHAVIOURAL: "Slow the pacing down and let one identity accumulate history.",
    Layer.MANAGED_CHALLENGE: "Configure a browser solver.",
    Layer.TURNSTILE: "Configure a browser solver or a solving service.",
    Layer.UNDER_ATTACK: "Configure a browser solver; every visitor is being challenged.",
    Layer.BOT_MANAGEMENT: "This zone is running the per-zone composite. Configure a "
    "managed provider, or accept inconsistent results.",
    Layer.WORKERS: "The origin runs its own edge code; this one needs per-site analysis.",
    Layer.AI_BOT_BLOCKER: "The User-Agent is declaring a crawler. Stop declaring one, "
    "or sign the request and be verified.",
}


def _hint(layer: Layer) -> str:
    return _HINTS.get(layer, "")


def default_capabilities(
    *,
    archive: bool = False,
    browser: bool = False,
    managed: bool = False,
) -> List[Capability]:
    """The ladder from the reference patterns, filtered to what is configured.

    Ordered by real cost, which is also the order to try things in. The archive
    comes first when enabled because the cheapest way past a protected site is not
    to touch it, and last-resort delegation comes last because it is the only tier
    that costs money per request.
    """
    # No tier claims layers 18 and 19. A signature and a password are held or they
    # are not, and a reach that listed them would make the planner offer a stronger
    # tier for something no tier can do.
    everything = frozenset(Layer) - IMPASSABLE

    ladder: List[Capability] = []
    if archive:
        ladder.append(
            Capability(
                name="archive",
                cost=0,
                # A snapshot never meets the live stack, so it reaches everything —
                # at the price of being stale and incomplete, which is why it is not
                # simply always first.
                reach=everything,
            )
        )
    ladder.append(
        Capability(
            name="direct",
            cost=10,
            reach=expand(
                {
                    Layer.TLS_FINGERPRINT,
                    Layer.BOT_FIGHT,
                    Layer.SUPER_BOT_FIGHT,
                    Layer.AI_BOT_BLOCKER,
                }
            ),
        )
    )
    if browser:
        ladder.append(
            Capability(
                name="clearance",
                cost=100,
                reach=expand(
                    {
                        Layer.TLS_FINGERPRINT,
                        Layer.BROWSER_JS,
                        Layer.CDP,
                        Layer.MANAGED_CHALLENGE,
                        Layer.TURNSTILE,
                        Layer.BOT_FIGHT,
                        Layer.SUPER_BOT_FIGHT,
                        Layer.UNDER_ATTACK,
                        Layer.AI_BOT_BLOCKER,
                    }
                ),
            )
        )
    if managed:
        ladder.append(Capability(name="managed", cost=1000, reach=everything))
    return ladder


@dataclass
class Attempt:
    """Bookkeeping for one retrieval, threaded through the planner."""

    tier: str
    number: int = 1
    rotations: int = 0
    history: List[str] = field(default_factory=list)

    def note(self, decision: Decision) -> None:
        self.history.append(str(decision))

    def trail(self) -> str:
        return " | ".join(self.history)
