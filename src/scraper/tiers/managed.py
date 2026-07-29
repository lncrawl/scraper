"""Handing the request to someone else.

There is one zone configuration where a do-it-yourself stack stops being the
rational choice: the per-zone composite model, actively tuned. It reads emitted and
possessed signals together, no single technique addresses it, and because the model
is trained on the specific site a configuration that works against one deployment
tells you nothing about the next. Maintaining a bypass becomes a standing
engineering cost rather than a piece of work with an end.

So the last rung is delegation, and it is last for the honest reason: it is the only
tier that costs money per request.

No provider is bundled. Their request formats differ, they change, and a wrapper
that guesses wrong fails in a way that looks like the site blocking you. Instead the
tier takes a callable, and the docstring below is the whole integration contract.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

import requests

from ..exceptions import TierUnavailable
from .base import Call, Tier

logger = logging.getLogger(__name__)

Provider = Callable[..., requests.Response]
"""What a managed tier needs.

Called as ``provider(method, url, headers=..., timeout=..., **options)`` and must
return a :class:`requests.Response` carrying the origin's own status code and body.
Returning the provider's status instead of the origin's breaks diagnosis: a 200 from
the provider wrapping a 403 from the site reads as a successful scrape of a block
page.
"""


class ManagedTier(Tier):
    """Delegates retrieval to an external provider.

    Args:
        provider: See :data:`Provider`.
        name: Shown in logs and in the reason string when a run stops here, so
            "gave up at scrapfly" is distinguishable from "gave up at direct".
        pass_identity: Whether to forward this library's identity headers. Off by
            default — a provider that manages its own fingerprint does not want a
            User-Agent imposed on it, and forcing one is how a provider's carefully
            matched profile gets contradicted.
    """

    name = "managed"

    def __init__(
        self,
        provider: Provider,
        *,
        name: str = "managed",
        pass_identity: bool = False,
    ) -> None:
        self.provider = provider
        self.name = name
        self.pass_identity = pass_identity

    def send(self, call: Call) -> requests.Response:
        headers: Dict[str, str] = dict(call.headers)
        if self.pass_identity:
            headers = call.merged_headers()

        options: Dict[str, Any] = dict(call.options)
        options["headers"] = headers
        if call.timeout is not None:
            options["timeout"] = call.timeout

        response = self.provider(call.method, call.url, **options)
        if not isinstance(response, requests.Response):
            raise TierUnavailable(
                self.name,
                f"the provider returned {type(response).__name__}, not a requests.Response",
                call.url,
            )
        return response


def http_provider(
    endpoint: str,
    *,
    token: str = "",
    url_param: str = "url",
    extra: Optional[Dict[str, str]] = None,
    transport: Optional[Any] = None,
) -> Provider:
    """A provider for the common "GET the endpoint with the target as a parameter" API.

    Enough for the several services shaped that way, and a readable starting point
    for one that is not. It forwards only GET: a service that tunnels other methods
    does so in its own format, and guessing that format is exactly the failure this
    module refuses to build in.
    """

    def call(method: str, url: str, **options: Any) -> requests.Response:
        if method.upper() != "GET":
            raise TierUnavailable("managed", f"this provider only forwards GET, not {method}", url)
        params = {url_param: url}
        if token:
            params["key"] = token
        params.update(extra or {})
        timeout = options.get("timeout") or 90.0
        if transport is not None:
            return transport.send("GET", endpoint, params=params, timeout=timeout)
        return requests.get(endpoint, params=params, timeout=timeout)

    return call
