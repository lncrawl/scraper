"""Exception taxonomy, keyed to the layer that caused the failure.

Every retrieval failure carries the layer it is attributed to, so a caller can
branch on the reason rather than on a status code. The important split is
:class:`Impassable`, which is raised where the model says no bypass exists: a
mandated request signature and an identity-provider gate both read a secret the
caller either holds or does not. Grinding against those produces an infinite
retry loop, so they surface immediately.
"""

from __future__ import annotations

from .layers import Layer, LayerInfo, info


class ScraperError(Exception):
    """Base class for everything this library raises."""


class Aborted(ScraperError):
    """The abort signal was set. Raised from anywhere in a request or download."""


class ConfigError(ScraperError):
    """The configuration cannot produce a working scraper."""


class MissingDependency(ScraperError, ImportError):
    """An optional extra is needed for the requested capability."""

    def __init__(self, extra: str, purpose: str) -> None:
        super().__init__(
            f"{purpose} requires the '{extra}' extra: pip install lncrawl-scraper[{extra}]"
        )
        self.extra = extra


class TierUnavailable(ScraperError):
    """This tier cannot serve this call at all — not a detection failure.

    Kept distinct from :class:`Blocked` because it must not be attributed to a
    layer. An archive with no snapshot of a URL says nothing about the site's
    defences, and recording it as one would teach the memory a conclusion that is
    simply false.
    """

    def __init__(self, tier: str, detail: str, url: str = "") -> None:
        self.tier = tier
        self.detail = detail
        self.url = url
        where = f" for {url}" if url else ""
        super().__init__(f"{tier} cannot serve this request{where}: {detail}")


class Blocked(ScraperError):
    """Retrieval failed and the model attributes it to a specific layer."""

    def __init__(self, layer: Layer, detail: str = "", url: str = "") -> None:
        self.layer = layer
        self.detail = detail
        self.url = url
        where = f" for {url}" if url else ""
        because = f": {detail}" if detail else ""
        super().__init__(f"{layer}{where}{because}")

    @property
    def layer_info(self) -> LayerInfo:
        """Static facts about the layer this failure is attributed to."""
        return info(self.layer)


class Impassable(Blocked):
    """The binding layer reads a secret. There is nothing to retry.

    Raised for a mandated request signature and for authentication. The message
    names the legitimate route, because there is exactly one.
    """

    ROUTES = {
        Layer.WEB_BOT_AUTH: (
            "the site requires a signed request; register as a verified agent and "
            "configure BotAuthConfig with your key"
        ),
        Layer.ACCESS: (
            "the content is behind an identity provider; retrieve it with a properly "
            "obtained account"
        ),
    }

    def __init__(self, layer: Layer, detail: str = "", url: str = "") -> None:
        # The route is appended rather than used as a fallback. It is the only
        # actionable half of the message, and a diagnosis that supplied its own
        # detail — "authentication required (HTTP 401)" — would otherwise suppress it
        # and leave the reader with a status code and no next step.
        route = self.ROUTES.get(layer, "")
        both = "; ".join(part for part in (detail, route) if part)
        super().__init__(layer, both, url)


class Exhausted(Blocked):
    """Every tier the plan allowed was tried and the layer still binds.

    Distinct from :class:`Impassable`: a bypass may well exist, this
    configuration just does not reach it. The message says which capability
    would.
    """

    def __init__(self, layer: Layer, detail: str = "", url: str = "") -> None:
        super().__init__(layer, detail, url)


class Poisoned(ScraperError):
    """Retrieved content is believed to be decoy material.

    The dangerous case in the model, because it has no error response: without
    this check a scraper reports success while filling its store with generated
    filler, and the session is flagged network-wide on the way.
    """

    def __init__(self, url: str, detail: str) -> None:
        self.url = url
        self.detail = detail
        super().__init__(f"{url} looks like decoy content: {detail}")
