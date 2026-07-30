"""The baseline: one impersonated HTTP request on a held identity.

This is the tier that should handle the majority of protected sites, and the
reason is the shape of the model rather than optimism. Layers 2 to 5 are one
barrier, and a faithful transport profile clears all four at once; the lighter
enforcement tiers read the same artifacts and fall to the same profile. So the
common case is an ordinary HTTP request that happens to be indistinguishable from
a browser's — no JavaScript, no browser process, no per-request cost.

What it does *not* do is anything clever on failure. It sends what it was told to
send and returns what came back. Deciding whether the answer means "rotate",
"slow down" or "launch a browser" belongs to the planner, which can see the
history; a tier that reacts on its own is a tier that rotates a proxy over a
pacing problem.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

import requests

from ..botauth import BotAuthConfig
from ..exceptions import Aborted
from ..transport import Transport
from .base import Call, Tier


class DirectTier(Tier):
    """Sends *call* through an impersonating transport.

    Args:
        transport: Where the bytes go. Shared with every tier that delegates here.
        owns_transport: Whether closing this tier closes the transport. False for a
            transport handed in through :attr:`ScraperConfig.transport`, which may be
            shared between scrapers.
        botauth: When configured, every request is signed. A signature is a
            positive identification that skips the challenge machinery entirely,
            which makes this tier cheaper still — and it is the only route through
            a zone that mandates one.
    """

    name = "direct"

    def __init__(
        self,
        transport: Transport,
        *,
        botauth: Optional[BotAuthConfig] = None,
        owns_transport: bool = True,
    ) -> None:
        self.transport = transport
        self.botauth = botauth
        self.owns_transport = owns_transport

    def _prepare(self, call: Call) -> Dict[str, Any]:
        if call.signal is not None and call.signal.is_set():
            raise Aborted("aborted before sending")

        headers = call.merged_headers()
        if self.botauth is not None and self.botauth.enabled:
            headers.update(self.botauth.headers_for(call.url))

        kwargs: Dict[str, Any] = dict(call.options)
        kwargs["headers"] = headers
        if call.proxies:
            kwargs["proxies"] = call.proxies
        if call.timeout is not None:
            kwargs["timeout"] = call.timeout
        cookies = call.cookie_header()
        if cookies:
            # Passed per-request rather than installed in the transport's jar. The
            # clearance belongs to one identity, and a jar outlives identities —
            # leaving it there is how a rotated exit keeps presenting the previous
            # exit's cookie and gets challenged for it.
            existing = kwargs.get("cookies") or {}
            kwargs["cookies"] = {**cookies, **dict(existing)}
        return kwargs

    def send(self, call: Call) -> requests.Response:
        return self.transport.send(call.method, call.url, **self._prepare(call))

    @contextmanager
    def stream(self, call: Call) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        with self.transport.stream(call.method, call.url, **self._prepare(call)) as pair:
            yield pair

    def close(self) -> None:
        """Close the transport, unless it was injected and someone else owns it."""
        if self.owns_transport:
            self.transport.close()
