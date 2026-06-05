"""UA inference and dataset filtering utilities."""

from __future__ import annotations

import random
import re

from ..config import PlatformType

_MIN_CHROME = 120
_MIN_FIREFOX = 120
_MIN_EDGE = 120
_MIN_SAFARI = 17


def infer_ch_platform(ua: str | None) -> str | None:
    """Map a UA string to a Sec-CH-UA-Platform value."""
    if ua is None:
        return None
    if "Android" in ua:
        return "Android"
    if "iPhone" in ua or "iPad" in ua or "CriOS" in ua:
        return "iOS"
    if "Windows" in ua:
        return "Windows"
    if "Mac OS X" in ua or "Macintosh" in ua:
        return "macOS"
    if "CrOS" in ua:
        return "Chrome OS"
    return "Linux"  # fallback


def infer_browser(ua: str) -> str | None:
    if "Edg" in ua:
        return "edge"
    if "Firefox/" in ua:
        return "firefox"
    if "Chrome/" in ua or "Chromium" in ua:
        return "chrome"
    if "Safari/" in ua:
        return "safari"
    return None


def infer_platform(ua: str) -> PlatformType:
    if "Android" in ua:
        return "android"
    if "iPhone" in ua or "iPad" in ua or "CriOS" in ua:
        return "ios"
    if "Windows" in ua:
        return "windows"
    if "Mac OS X" in ua or "Macintosh" in ua:
        return "darwin"
    return "linux"  # fallback


def filter_ua_data(
    data: list[dict],
    browser: str | None,
    platform: str | None,
) -> list[dict]:
    entries: list[dict] = []
    for entry in data:
        ua = entry.get("userAgent", "")

        # Browser + version filter
        if "Edg" in ua:
            m = re.search(r"Edg(?:A|iOS)?/(\d+)", ua)
            if not m or int(m.group(1)) < _MIN_EDGE:
                continue
            entry_browser = "edge"
        elif "Firefox/" in ua:
            m = re.search(r"Firefox/(\d+)", ua)
            if not m or int(m.group(1)) < _MIN_FIREFOX:
                continue
            entry_browser = "firefox"
        elif "Safari/" in ua and "Chrome/" not in ua and "Chromium" not in ua:
            m = re.search(r"Version/(\d+)", ua)
            if not m or int(m.group(1)) < _MIN_SAFARI:
                continue
            entry_browser = "safari"
        elif "Chrome/" in ua and "Chromium" not in ua:
            m = re.search(r"Chrome/(\d+)", ua)
            if not m or int(m.group(1)) < _MIN_CHROME:
                continue
            entry_browser = "chrome"
        else:
            continue

        if browser and entry_browser != browser:
            continue

        # Platform filter
        if platform and infer_platform(ua) != platform:
            continue

        entries.append(entry)
    return entries


def weighted_choice(entries: list[dict], rng: random.Random) -> str | None:
    if not entries:
        return None
    weights = [e.get("weight", 1e-6) for e in entries]
    total = sum(weights)
    threshold = rng.random() * total
    cumulative = 0.0
    chosen = entries[-1]
    for entry, w in zip(entries, weights):
        cumulative += w
        if cumulative >= threshold:
            chosen = entry
            break
    return chosen.get("userAgent")
