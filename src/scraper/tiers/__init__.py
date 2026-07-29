"""The escalation ladder, ordered by real cost.

Four tiers, and the order to try them in follows from the model rather than from
taste. An archived snapshot never meets the live stack at all. An impersonated HTTP
request clears the whole transport group and the lighter enforcement tiers in one
shot. A browser is reserved for a genuine JavaScript challenge, because it costs a
process launch and several seconds. Delegation is last because it costs money.

Each tier declares what it can reach in :func:`scraper.planner.default_capabilities`,
and the planner picks the cheapest one that covers whatever is actually binding — so
the ladder is walked on evidence, not climbed by default.
"""

from .archive import ArchiveTier
from .base import Call, Tier
from .clearance import ClearanceTier
from .direct import DirectTier
from .managed import ManagedTier, Provider, http_provider

__all__ = [
    "ArchiveTier",
    "Call",
    "ClearanceTier",
    "DirectTier",
    "ManagedTier",
    "Provider",
    "Tier",
    "http_provider",
]
