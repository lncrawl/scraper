"""UserAgent class — selects a browser UA string and matching TLS cipher suite."""

from __future__ import annotations

import random
import re
from typing import get_args

from requests.structures import CaseInsensitiveDict

from ..config import ArchitectureType, BitnessType, BrowserConfig, BrowserType, PlatformType
from .data import CIPHER_SUITES, DEFAULT_HEADERS
from .fallback import generate_ua_fallback
from .filter import filter_ua_data, infer_browser, infer_ch_platform, weighted_choice


class UserAgent:
    """Selects a browser User-Agent and matching TLS cipher suite.

    Attempts to use fresh data from intoli/user-agents (cached locally with ETag
    validation). Falls back to an embedded generator on network failure.
    """

    def __init__(self, cfg: BrowserConfig | None = None) -> None:
        self.config: BrowserConfig
        self.headers: CaseInsensitiveDict
        self.cipher_suite: list[str] = []
        self.load(cfg or BrowserConfig())

    def load(self, cfg: BrowserConfig) -> None:
        from .cache import is_brotli_available

        self.config = cfg
        self.headers = CaseInsensitiveDict()

        if cfg.allow_brotli and is_brotli_available():
            self.headers["Accept-Encoding"] = "gzip, deflate, br"
        else:
            self.headers["Accept-Encoding"] = "gzip, deflate"

        ua_str = self._get_ua()
        if not ua_str:
            return

        family = infer_browser(ua_str)
        if family:
            self.headers.update(DEFAULT_HEADERS[family])
            self.cipher_suite = list(CIPHER_SUITES[family])

        # _HEADERS uses None as a placeholder; overwrite with the real string.
        self.headers["User-Agent"] = ua_str

        for name, value in self._client_hints(family).items():
            self.headers[name] = value

    def _get_ua(self) -> str | None:
        if self.config.custom:
            return self.config.custom

        rng = random.SystemRandom()
        cfg = self.config
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

        intoli_ua_data = load_ua_data()
        if intoli_ua_data:
            filtered = filter_ua_data(intoli_ua_data, browser_name, platform)
            ua_str = weighted_choice(filtered, rng)
            if ua_str:
                return ua_str

        return generate_ua_fallback(browser_name, platform, rng)

    def _client_hints(self, family: str | None) -> dict[str, str]:
        """Build Sec-CH-UA Client Hints matching *ua* and architectural variants."""
        ua = self.headers.get("User-Agent")
        if not ua or family in ("firefox", "safari", None):
            return {}

        match = re.search(r"Chrome/(\d+)", ua)
        if not match:
            return {}
        version = match.group(1)

        if family == "edge":
            edge_match = re.search(r"Edg(?:A|iOS)?/(\d+)", ua)
            edge_version = edge_match.group(1) if edge_match else version
            brands = f'"Not A;Brand";v="99", "Chromium";v="{version}", "Microsoft Edge";v="{edge_version}"'
        else:
            brands = (
                f'"Chromium";v="{version}", "Google Chrome";v="{version}", "Not_A Brand";v="24"'
            )

        hints = {
            "sec-ch-ua": brands,
            "sec-ch-ua-mobile": "?1" if "Mobile" in ua else "?0",
        }

        ch_platform = infer_ch_platform(ua)
        if ch_platform:
            hints["sec-ch-ua-platform"] = f'"{ch_platform}"'

        if self.config.architecture:
            hints["sec-ch-ua-arch"] = f'"{self.config.architecture}"'
        if self.config.bitness:
            hints["sec-ch-ua-bitness"] = f'"{self.config.bitness}"'

        return hints
