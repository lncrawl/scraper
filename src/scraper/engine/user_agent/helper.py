"""UA inference and dataset filtering utilities."""

from __future__ import annotations

from ...config import BrowserType, PlatformType


def infer_browser(ua: str) -> BrowserType | None:
    if "Edg" in ua:
        return "edge"
    if "Firefox/" in ua:
        return "firefox"
    if "Chromium" not in ua:
        if "Chrome/" in ua:
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
