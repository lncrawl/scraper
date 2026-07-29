"""The cheapest way past a protected site: don't touch it.

An archived snapshot is served from a host with no bot-mitigation stack in front of
it, usually as static HTML, and it costs nothing but a lookup. Where the content is
not time-sensitive this is strictly better than every other tier — no proxy, no
browser, no challenge, no standing to protect.

The two reasons it is not simply always first are honest limitations rather than
detection problems. Coverage is incomplete and captures are stale, so a caller has
to have said that stale is acceptable. And the archive itself rate-limits, so it is
paced like any other origin.

One detail matters for anything that parses the result: the response carries the
**original** URL, not the archive URL. Relative links resolved against a
``web.archive.org`` base point back into the archive, which silently turns a scrape
of a site into a scrape of a snapshot of a site.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import requests

from ..exceptions import TierUnavailable
from ..transport import Transport
from .base import Call, Tier

logger = logging.getLogger(__name__)

CDX_URL = "http://web.archive.org/cdx/search/cdx"
WAYBACK_URL = "https://web.archive.org/web"

RAW_SUFFIX = "id_"
"""Returns the capture without the archive's own toolbar and rewriting.

Without it every URL in the page comes back rewritten to point at the archive, and
the HTML carries injected banner markup that no selector on the real site expects.
"""

SOURCE_HEADER = "x-scraper-archive"
"""Timestamp of the capture, so a caller can tell how stale the answer is."""


class ArchiveTier(Tier):
    """Serves pages out of the Wayback Machine.

    Args:
        transport: Used for both the index lookup and the capture fetch. The
            archive has no mitigation stack, so the impersonation profile is
            irrelevant here — it is shared purely so there is one place that owns
            connections.
        max_age: Reject captures older than this many seconds. ``0`` accepts any.
            A default of "any" would quietly serve a decade-old page to a caller
            who asked for the current one.
    """

    name = "archive"

    def __init__(
        self, transport: Transport, *, max_age: float = 0.0, timeout: float = 30.0
    ) -> None:
        self.transport = transport
        self.max_age = max_age
        self.timeout = timeout

    def send(self, call: Call) -> requests.Response:
        # Both of these escalate rather than being recorded as a block. Neither says
        # anything about the site's defences, and attributing them to a layer would
        # write a conclusion into memory that is simply not true.
        if call.method.upper() not in ("GET", "HEAD"):
            raise TierUnavailable(
                self.name, f"the archive only serves GET, not {call.method}", call.url
            )
        snapshot = self.latest(call.url)
        if snapshot is None:
            raise TierUnavailable(self.name, "no usable capture", call.url)

        timestamp, original = snapshot
        raw_url = f"{WAYBACK_URL}/{timestamp}{RAW_SUFFIX}/{original}"
        response = self.transport.send("GET", raw_url, timeout=self.timeout, headers=call.headers)
        # Rewritten so relative links resolve against the real site. Everything
        # downstream — link extraction, the referrer chain, the origin key in
        # memory — keys off this value, and leaving it pointing at the archive
        # redirects the whole crawl into the snapshot.
        response.url = original
        response.headers[SOURCE_HEADER] = timestamp
        return response

    def latest(self, url: str) -> Optional["tuple[str, str]"]:
        """The most recent acceptable capture of *url*, as ``(timestamp, url)``."""
        for timestamp, original in reversed(self.captures(url)):
            if not self.max_age or _age(timestamp) <= self.max_age:
                return timestamp, original
        return None

    def captures(self, url: str, *, limit: int = 12) -> List["tuple[str, str]"]:
        """Index entries for *url*, oldest first.

        ``collapse=digest`` drops consecutive identical captures, so the entries
        that come back are the ones where the page actually changed rather than
        every crawl of an unchanged page.
        """
        params: Dict[str, Any] = {
            "url": url,
            "output": "json",
            "collapse": "digest",
            "filter": "statuscode:200",
            "limit": -abs(limit),
            "fl": "timestamp,original",
        }
        try:
            response = self.transport.send("GET", CDX_URL, params=params, timeout=self.timeout)
            rows = json.loads(response.content or b"[]")
        except (ValueError, OSError) as exc:
            logger.debug("archive index lookup failed for %s: %s", url, exc)
            return []
        if not isinstance(rows, list) or len(rows) < 2:
            return []
        out: List["tuple[str, str]"] = []
        for row in rows[1:]:
            if isinstance(row, list) and len(row) >= 2:
                out.append((str(row[0]), str(row[1])))
        return out


def _age(timestamp: str) -> float:
    """Seconds since a ``YYYYMMDDhhmmss`` capture stamp."""
    import datetime as dt

    try:
        moment = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return float("inf")
    return (dt.datetime.now(dt.timezone.utc) - moment).total_seconds()
