"""Shared fixtures.

Tests are offline and fast, and the seam that makes that possible is
:class:`FakeTransport`. The previous suite mocked at the HTTP-adapter level, which
tied it to one client; the pipeline now talks to a two-method transport, so a fake
is a dozen lines and covers every tier.

Anything that sleeps is turned off here rather than in each test. Pacing is drawn
from a distribution with a long tail on purpose, so a suite that honoured it would
be slow and occasionally very slow.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from scraper import PacingPolicy, ScraperConfig
from scraper.transport import Transport


def make_response(
    status: int = 200,
    body: str = "ok",
    *,
    url: str = "https://example.com/",
    headers: Optional[Dict[str, str]] = None,
    request_headers: Optional[Dict[str, str]] = None,
) -> requests.Response:
    """A ``requests.Response`` built by hand, with no transport involved."""
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.url = url
    response.encoding = "utf-8"
    response.headers = CaseInsensitiveDict(headers or {"content-type": "text/html"})
    prepared = requests.PreparedRequest()
    prepared.method = "GET"
    prepared.url = url
    prepared.headers = CaseInsensitiveDict(request_headers or {})
    response.request = prepared
    return response


Handler = Callable[[str, str, Dict[str, Any]], requests.Response]


class FakeTransport(Transport):
    """Records what was asked for and replies from a scripted queue.

    Args:
        replies: Responses to return in order. The last one repeats once the queue
            is drained, so a test that only cares about the first exchange does not
            have to pad the list.
        handler: Overrides *replies* entirely when given, for tests that need to
            answer differently per URL.
    """

    name = "fake"

    def __init__(
        self,
        replies: Optional[List[requests.Response]] = None,
        *,
        handler: Optional[Handler] = None,
    ) -> None:
        self.replies = list(replies or [])
        self.handler = handler
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self._jar = RequestsCookieJar()
        self.closed = False

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method.upper(), url, kwargs))
        if self.handler is not None:
            return self.handler(method, url, kwargs)
        if not self.replies:
            return make_response(url=url)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return reply

    @contextmanager
    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> Iterator[Tuple[requests.Response, Iterator[bytes]]]:
        response = self.send(method, url, **kwargs)
        yield response, iter((response.content,))

    @property
    def cookies(self) -> RequestsCookieJar:
        return self._jar

    def set_cookie(self, name: str, value: str, *, domain: str = "", path: str = "/") -> None:
        self._jar.set(name, value, domain=domain, path=path)

    def clear_cookies(self, domain: str = "") -> None:
        self._jar.clear()

    def close(self) -> None:
        self.closed = True

    # -- assertions helpers ----------------------------------------------------------

    @property
    def urls(self) -> List[str]:
        return [url for _, url, _ in self.calls]

    def headers_of(self, index: int = 0) -> Dict[str, str]:
        return dict(self.calls[index][2].get("headers") or {})

    def proxies_of(self, index: int = 0) -> Optional[Dict[str, str]]:
        return self.calls[index][2].get("proxies")


# Shaped after what real interstitials actually send: the orchestrate path carries the
# `/h/` segment, which is what distinguishes a challenge from the detections script
# Cloudflare injects into ordinary pages.
CHALLENGE_BODY = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><div id="cf-wrapper"><script>window._cf_chl_opt={cvId:"3"}</script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=a1"></script>
</body></html>"""

# A page that was served normally but carries the injected detections script. Anything
# that reads this as a challenge is wrong about a page it already has.
SERVED_WITH_JSD = """<!doctype html><html><head><title>Chapter 12</title>
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></head>
<body><h1>Chapter 12</h1><p>Real content.</p></body></html>"""

BLOCK_BODY = """<!doctype html><html><body><h1>Access denied</h1>
<span class="cf-error-code">1020</span><p>Error 1020</p></body></html>"""

TURNSTILE_BODY = """<!doctype html><html><body>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
<div class="cf-turnstile" data-sitekey="0x4"></div>
<script>window.__cf_chl_ = 1</script></body></html>"""


@pytest.fixture
def fast_pacing() -> PacingPolicy:
    """Pacing with every wait removed. Every test that fetches needs this."""
    return PacingPolicy(interval=0.0, floor=0.0, warmup=False, pause_chance=0.0)


@pytest.fixture
def fast_config(fast_pacing: PacingPolicy) -> ScraperConfig:
    """A config that touches neither the network nor the clock nor the disk.

    ``remember=False`` matters: the memory store defaults to a real path, and a
    suite that wrote to it would leak learned state between tests and into the
    developer's cache directory.
    """
    return ScraperConfig(
        transport=FakeTransport(),
        pacing=fast_pacing,
        remember=False,
        guard_topic=False,
        raise_for_status=False,
    )


@pytest.fixture
def make_config(fast_pacing: PacingPolicy):
    """Build a config with overrides, keeping the offline defaults."""

    def build(**overrides: Any) -> ScraperConfig:
        base: Dict[str, Any] = {
            "transport": FakeTransport(),
            "pacing": fast_pacing,
            "remember": False,
            "guard_topic": False,
            "raise_for_status": False,
        }
        base.update(overrides)
        return ScraperConfig(**base)

    return build


def json_body(payload: Any) -> str:
    return json.dumps(payload)
