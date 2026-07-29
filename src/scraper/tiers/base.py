"""The shape every tier has, and the request object they all receive.

A tier is one capability set: a way of getting a page that can pass some subset of
the layers at some cost. They are deliberately uniform and stateless with respect
to the request — everything a tier needs arrives in a :class:`Call`, and anything
it learns goes back through the return value. That is what lets the planner treat
them as interchangeable and pick by cost.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Tuple

import requests

from ..identity import Clearance, Identity


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
    signal: Optional[threading.Event] = None

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

    Args:
        name: Must match the :class:`~scraper.planner.Capability` name, since that
            is how the planner refers to it.
    """

    name = "tier"

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
