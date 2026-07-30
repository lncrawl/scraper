"""State that belongs to a site rather than to a scraper object.

The behavioural layer is trained per zone and reads what it sees from one visitor.
Two scrapers in one process pointed at the same host, each with its own address,
pacing clock and cookie history, do not present as one visitor going twice as fast
— they present as two visitors who contradict each other, arriving in bursts, one
of them always cold.

So the per-origin state is separable, and anything that runs several scrapers
against one host should share it. What gets shared is deliberately more than a rate
limit: the address, the identity, the accumulated history, the learned interval, the
referrer chain and the decoy list are all properties of the *zone*, and splitting any
one of them re-creates the contradiction.

What stays per-scraper is what genuinely differs: the origin it is pointed at, its
own abort signal, its own default headers, its own parser.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import ScraperConfig
from .exits import ExitPool
from .identity import Identity
from .links import TopicGuard
from .memory import Memory
from .pacing import Pacer, Trail


@dataclass
class SharedState:
    """Everything keyed by origin, shareable between scrapers.

    Build one with :meth:`create` and hand it to every :class:`~scraper.Scraper`
    that talks to the same site.
    """

    memory: Memory
    exits: ExitPool
    pacer: Pacer
    trail: Trail = field(default_factory=Trail)
    identities: Dict[str, Identity] = field(default_factory=dict)
    guards: Dict[str, TopicGuard] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @classmethod
    def create(
        cls,
        config: Optional[ScraperConfig] = None,
        *,
        memory: Optional[Memory] = None,
    ) -> "SharedState":
        """Build shared state from *config*.

        Only the settings that describe the site are read — addresses, pacing,
        persistence. Transport and tier choices stay with the scraper, so two
        scrapers can share a zone's standing while impersonating different browsers
        if there is a reason to.

        Pass *memory* when a process builds more than one state over the same file.
        Each store holds every origin it knows and :meth:`Memory.flush` writes all of
        them, so two stores on one path do not merge — the later write is the whole
        file, and whatever the other one had learned is gone. A caller that wants
        state per site and persistence for the process wants one ``Memory`` here.
        """
        cfg = config or ScraperConfig()
        return cls(
            memory=memory if memory is not None else Memory(cfg.memory_path),
            exits=ExitPool(
                cfg.exits,
                max_sessions_per_exit=cfg.max_sessions_per_exit,
                retire_for=cfg.retire_exit_for,
            ),
            pacer=Pacer(cfg.pacing),
        )

    def close(self) -> None:
        self.memory.close()
