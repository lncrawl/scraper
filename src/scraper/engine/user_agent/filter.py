"""UA inference and dataset filtering utilities."""

from __future__ import annotations

import random
import re

from .helper import infer_browser, infer_platform

_EDGE_RE = re.compile(r"Edg(?:A|iOS)?/(\d+)", re.I)
_FIREFOX_RE = re.compile(r"Firefox/(\d+)", re.I)
_SAFARI_RE = re.compile(r"Version/(\d+)", re.I)
_CHROME_RE = re.compile(r"Chrome/(\d+)", re.I)


def _match_version(
    ua: str,
    matcher: re.Pattern,
    version: int | None,
    min_version: int,
) -> bool:
    m = matcher.search(ua)
    if not m:
        return False
    v = int(m.group(1))
    if version and version == v:
        return True
    if v >= min_version:
        return True
    return False


def filter_ua_data(
    data: list[dict],
    browser: str | None = None,
    platform: str | None = None,
    version: int | None = None,
    *,
    min_edge: int = 120,
    min_chrome: int = 120,
    min_firefox: int = 120,
    min_safari: int = 17,
) -> list[dict]:
    entries: list[dict] = []
    for entry in data:
        ua = entry.get("userAgent", "")
        device = entry.get("deviceCategory", "")

        # Check device category
        if platform in ("android", "ios") and device != "mobile":
            continue

        # Platform filter
        if platform and infer_platform(ua) != platform:
            continue

        # Infer browser from UA
        inferred = infer_browser(ua)

        # Browser name filter
        if browser and inferred != browser:
            continue

        # Skip unknown browsers when no specific browser is requested
        if not browser and not inferred:
            continue

        # Browser version filter based on inferred or requested browser
        target = browser or inferred
        if target not in ("edge", "firefox", "safari", "chrome"):
            continue
        if target == "edge":
            if not _match_version(ua, _EDGE_RE, version, min_edge):
                continue
        elif target == "firefox":
            if not _match_version(ua, _FIREFOX_RE, version, min_firefox):
                continue
        elif target == "safari":
            if not _match_version(ua, _SAFARI_RE, version, min_safari):
                continue
        else:
            if not _match_version(ua, _CHROME_RE, version, min_chrome):
                continue

        entries.append(entry)

    return entries


def weighted_choice(
    entries: list[dict],
    rng: random.Random,
) -> str | None:
    if not entries:
        return None
    weights = [e.get("weight", 1e-6) for e in entries]
    chosen = rng.choices(entries, weights=weights, k=1)
    return chosen[0].get("userAgent")
