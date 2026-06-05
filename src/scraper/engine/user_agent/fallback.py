"""Offline UA string generator — no network or disk I/O required."""

from __future__ import annotations

import random

from .data import (
    ANDROID_DEVICES,
    ANDROID_VERSIONS,
    CHROME_VERSIONS,
    EDGE_VERSIONS,
    FIREFOX_VERSIONS,
    IOS_VERSIONS,
    MAC_VERSIONS,
    SAFARI_VERSIONS,
    WEBKIT_VERSIONS,
)


def generate_ua_fallback(
    browser: str,
    platform: str | None,
    version: int | None,
    rng: random.Random,
) -> str | None:
    """Generate a realistic modern UA string without any network or disk I/O."""
    if browser == "firefox":
        v = version or rng.choice(FIREFOX_VERSIONS)
        if platform == "windows":
            return (
                f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"
            )
        if platform == "darwin":
            mac = rng.choice(MAC_VERSIONS)
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"
        if platform == "android":
            av = rng.choice(ANDROID_VERSIONS)
            return f"Mozilla/5.0 (Android {av}; Mobile; rv:{v}.0) Gecko/{v}.0 Firefox/{v}.0"
        if platform == "ios":
            iv = rng.choice(IOS_VERSIONS)
            return f"Mozilla/5.0 (iPhone; CPU iPhone OS {iv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/{v}.0 Mobile/15E148 Safari/605.1.15"
        return f"Mozilla/5.0 (X11; Linux x86_64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"

    if browser == "safari":
        sv = version or rng.choice(SAFARI_VERSIONS)
        wv = rng.choice(WEBKIT_VERSIONS)
        if platform == "ios":
            iv = rng.choice(IOS_VERSIONS)
            return f"Mozilla/5.0 (iPhone; CPU iPhone OS {iv} like Mac OS X) AppleWebKit/{wv} (KHTML, like Gecko) Version/{sv} Mobile/15E148 Safari/{wv}"
        mac = rng.choice(MAC_VERSIONS)
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}) AppleWebKit/{wv} (KHTML, like Gecko) Version/{sv} Safari/{wv}"

    if browser == "edge":
        v = version or rng.choice(EDGE_VERSIONS)
        webkit = "AppleWebKit/537.36 (KHTML, like Gecko)"
        if platform == "darwin":
            mac = rng.choice(MAC_VERSIONS)
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}) {webkit} Chrome/{v}.0.0.0 Safari/537.36 Edg/{v}.0.0.0"
        if platform == "linux":
            return f"Mozilla/5.0 (X11; Linux x86_64) {webkit} Chrome/{v}.0.0.0 Safari/537.36 Edg/{v}.0.0.0"
        if platform == "android":
            av = rng.choice(ANDROID_VERSIONS)
            device = rng.choice(ANDROID_DEVICES)
            return f"Mozilla/5.0 (Linux; Android {av}; {device}) {webkit} Chrome/{v}.0.0.0 Mobile Safari/537.36 EdgA/{v}.0.0.0"
        if platform == "ios":
            iv = rng.choice(IOS_VERSIONS)
            return f"Mozilla/5.0 (iPhone; CPU iPhone OS {iv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1 EdgiOS/{v}.0.0.0"
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {webkit} Chrome/{v}.0.0.0 Safari/537.36 Edg/{v}.0.0.0"

    if browser == "chrome":
        v = version or rng.choice(CHROME_VERSIONS)
        webkit = "AppleWebKit/537.36 (KHTML, like Gecko)"
        if platform == "darwin":
            mac = rng.choice(MAC_VERSIONS)
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac}) {webkit} Chrome/{v}.0.0.0 Safari/537.36"
        if platform == "linux":
            return f"Mozilla/5.0 (X11; Linux x86_64) {webkit} Chrome/{v}.0.0.0 Safari/537.36"
        if platform == "android":
            av = rng.choice(ANDROID_VERSIONS)
            device = rng.choice(ANDROID_DEVICES)
            return f"Mozilla/5.0 (Linux; Android {av}; {device}) {webkit} Chrome/{v}.0.0.0 Mobile Safari/537.36"
        if platform == "ios":
            iv = rng.choice(IOS_VERSIONS)
            return f"Mozilla/5.0 (iPhone; CPU iPhone OS {iv} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/{v}.0.0.0 Mobile/15E148 Safari/604.1"
        return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {webkit} Chrome/{v}.0.0.0 Safari/537.36"

    return None
