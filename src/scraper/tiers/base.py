"""The shape every tier has, and the request object they all receive.

A tier is one capability set: a way of getting a page that can pass some subset of
the layers at some cost. They are deliberately uniform and stateless with respect
to the request — everything a tier needs arrives in a :class:`Call`, and anything
it learns goes back through the return value. That is what lets the planner treat
them as interchangeable and pick by cost.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterator, Optional, Tuple

import requests

from ..exceptions import ConfigError
from ..identity import Clearance, Identity
from ..layers import IMPASSABLE, Layer, expand
from ..planner import Capability
from ..utils.signals import AbortSignal


@dataclass
class Call:
    """One attempt at one URL, fully specified.

    Args:
        headers: Request-specific headers — ``Accept``, ``Referer``, fetch
            metadata. Identity contributions are added by the tier, not here, so
            that the two cannot collectively rewrite an impersonation profile's
            header set.
        proxies: Already carrying the lease's credentials. ``None`` is a direct
            connection, which is a decision the exit pool made, not a fallback the
            tier may take on its own.
        clearance: Only ever set when it is valid for *identity*. A tier does not
            re-check the binding; the caller has, and passing a mismatched pair
            here would send a cookie that cannot work.
    """

    method: str
    url: str
    identity: Identity
    headers: Dict[str, str] = field(default_factory=dict)
    proxies: Optional[Dict[str, str]] = None
    clearance: Optional[Clearance] = None
    timeout: Any = None
    options: Dict[str, Any] = field(default_factory=dict)
    signal: Optional[AbortSignal] = None

    @property
    def through_proxy(self) -> bool:
        return bool(self.proxies)

    def merged_headers(self) -> Dict[str, str]:
        """Identity overrides plus the caller's headers, caller winning."""
        out = dict(self.identity.header_overrides())
        for key, value in self.headers.items():
            if value is None:
                out.pop(key.lower(), None)
            else:
                out[key.lower()] = value
        return out

    def cookie_header(self) -> Dict[str, str]:
        """The clearance as request cookies, or nothing."""
        if self.clearance is None or not self.clearance.cookies:
            return {}
        return dict(self.clearance.cookies)


class Tier:
    """A way of retrieving a page.

    Subclass, implement :meth:`send`, declare an honest :attr:`reach`, and pass an
    instance in :attr:`~scraper.ScraperConfig.tiers`. Nothing else is needed — the
    planner sees it through :meth:`capability` and picks it by cost like any other rung.

    Args:
        name: How the planner refers to this tier, and what
            :attr:`~scraper.OriginProfile.tier` records when it works. Must be unique
            among the tiers a scraper is built with.
        cost: Relative expense, for ordering only. The default sits between a browser
            launch and a metered provider, which is where a custom tier usually
            belongs — but the gaps are the meaning, so set it deliberately.
        reach: Layers this tier can actually pass. Be honest: the planner treats this
            as a claim about capability, so an inflated one sends every retrieval to a
            tier that cannot help and stops the ladder before the tier that could.
    """

    name = "tier"
    cost = 500
    reach: FrozenSet[Layer] = frozenset()

    def capability(self) -> Capability:
        """How the planner sees this tier.

        Reach is closed over the transport group, because no technique satisfies one of
        layers 2-5 without the others.
        """
        claimed = frozenset(self.reach)
        secret = claimed & IMPASSABLE
        if secret:
            # Refused rather than filtered. These layers read a secret the caller either
            # holds or does not, so a tier claiming one would make the planner offer a
            # stronger rung for something no rung can do — and dropping the claim
            # quietly would leave the author believing it had been honoured.
            named = ", ".join(str(layer) for layer in sorted(secret, key=lambda x: x.value))
            raise ConfigError(f"tier {self.name!r} cannot claim {named}: those read a secret")
        return Capability(name=self.name, cost=self.cost, reach=expand(claimed))

    def send(self, call: Call) -> requests.Response:
        raise NotImplementedError

    @contextmanager
    def stream(self, call: Call) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        """Retrieve *call* as a byte stream.

        The default buffers through :meth:`send`, so every tier supports downloads
        whether or not its underlying client streams. Tiers that can stream
        properly override this; the ones that cannot are still correct, just
        memory-hungry on a large file.
        """
        response = self.send(call)
        yield response, iter((response.content,))

    def close(self) -> None:
        """Release resources. Called once when the scraper closes."""
