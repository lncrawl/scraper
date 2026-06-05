"""UserAgent class — selects a browser UA string and matching TLS cipher suite."""

from __future__ import annotations

import logging
import random
import re
from typing import get_args

from requests.structures import CaseInsensitiveDict

from ...config import (
    ArchitectureType,
    BitnessType,
    BrowserConfig,
    BrowserType,
    PlatformType,
    ScraperConfig,
)
from .data import DEFAULT_HEADERS
from .fallback import generate_ua_fallback
from .filter import filter_ua_data, weighted_choice
from .helper import infer_browser, infer_ch_platform

logger = logging.getLogger(__name__)

_IMPERSONATE_TARGET_RE = re.compile(r"([a-zA-Z]+)([0-9]*)[^_]*(?>_(.+))?")


def _get_impersonate_browser(target: str) -> BrowserConfig | None:
    result = _IMPERSONATE_TARGET_RE.findall(target)
    if not result:
        return None
    browser, version, platform = result[0]
    is_mobile = platform in ("android", "ios")
    return BrowserConfig(
        browser=browser.lower(),
        version=int(version) if version else 0,
        platform=platform or None,
        mobile=is_mobile,
        desktop=not is_mobile,
    )


def _get_user_agent(cfg: BrowserConfig) -> str | None:
    if cfg.custom:
        return cfg.custom

    rng = random.SystemRandom()
    platform = cfg.platform
    browser_name = cfg.browser or rng.choice(get_args(BrowserType))

    if browser_name not in get_args(BrowserType):
        raise RuntimeError(
            f'Browser "{browser_name}" is not valid. Valid choices: {get_args(BrowserType)}'
        )
    if platform and platform not in get_args(PlatformType):
        raise RuntimeError(
            f'Platform "{platform}" is not valid. Valid choices: {get_args(PlatformType)}'
        )
    if cfg.architecture and cfg.architecture not in get_args(ArchitectureType):
        raise RuntimeError(
            f'Architecture "{cfg.architecture}" is not valid. Valid choices: {get_args(ArchitectureType)}'
        )
    if cfg.bitness and cfg.bitness not in get_args(BitnessType):
        raise RuntimeError(
            f'Bitness "{cfg.bitness}" is not valid. Valid choices: {get_args(BitnessType)}'
        )
    if not cfg.desktop and not cfg.mobile:
        raise RuntimeError("Both mobile and desktop cannot be disabled.")

    is_mobile = platform in ("android", "ios")
    is_desktop = not is_mobile

    if is_mobile and not cfg.mobile:
        # redirect to desktop variant of the platform
        if browser_name == "safari":
            platform = "darwin"
        else:
            platform = rng.choice(["windows", "darwin", "linux"])
        is_mobile = False

    if is_desktop and not cfg.desktop:
        # redirect to mobile variant of the platform
        if browser_name == "safari":
            platform = "ios"
        else:
            platform = rng.choice(["android", "ios"])
        is_desktop = False

    from .cache import load_ua_data

    intoli_data = load_ua_data()
    if intoli_data:
        filtered = filter_ua_data(intoli_data, browser_name, platform, cfg.version)
        ua_str = weighted_choice(filtered, rng)
        if ua_str:
            return ua_str

    return generate_ua_fallback(browser_name, platform, cfg.version, rng)


def _add_client_hints(cfg: BrowserConfig, headers: CaseInsensitiveDict) -> None:
    """Build Sec-CH-UA Client Hints matching *ua* and architectural variants."""
    ua = headers["User-Agent"]
    family = infer_browser(ua)
    if family in ("firefox", "safari", None):
        return None

    match = re.search(r"Chrome/(\d+)", ua)
    if not match:
        return
    version = match.group(1)

    if family == "edge":
        edge_match = re.search(r"Edg(?:A|iOS)?/(\d+)", ua)
        ev = edge_match.group(1) if edge_match else version
        ch_ua = f'"Not A;Brand";v="99", "Chromium";v="{version}", "Microsoft Edge";v="{ev}"'
    else:
        ch_ua = f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not_A Brand";v="24"'

    headers["sec-ch-ua"] = ch_ua
    headers["sec-ch-ua-mobile"] = "?1" if "Mobile" in ua else "?0"

    ch_platform = infer_ch_platform(ua)
    if ch_platform:
        headers["sec-ch-ua-platform"] = f'"{ch_platform}"'

    if cfg.architecture:
        headers["sec-ch-ua-arch"] = f'"{cfg.architecture}"'
    if cfg.bitness:
        headers["sec-ch-ua-bitness"] = f'"{cfg.bitness}"'


def build_ua_headers(config: ScraperConfig) -> CaseInsensitiveDict:
    """Selects a browser User-Agent and headers for stealth.

    Attempts to use fresh data from intoli/user-agents (cached locally with ETag
    validation). Falls back to an embedded generator on network failure.
    """
    from .cache import is_brotli_available

    headers = CaseInsensitiveDict()
    if config.browser.allow_brotli and is_brotli_available():
        headers["Accept-Encoding"] = "gzip, deflate, br"
    else:
        headers["Accept-Encoding"] = "gzip, deflate"

    cfg = config.browser
    if config.impersonate.target:
        cfg = _get_impersonate_browser(config.impersonate.target)
        if not cfg:  # unrecognized impersonate type
            return headers

    ua_str = _get_user_agent(cfg)
    if not ua_str:
        return headers

    browser = infer_browser(ua_str)
    if browser:
        headers.update(DEFAULT_HEADERS[browser])

    headers["User-Agent"] = ua_str
    _add_client_hints(cfg, headers)

    return headers
