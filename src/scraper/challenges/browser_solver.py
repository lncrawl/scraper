"""In-process browser solver backed by nodriver.

Drives a real Chrome (via `nodriver <https://github.com/ultrafunkamsterdam/nodriver>`_,
the successor to undetected-chromedriver) to pass a Cloudflare challenge and
harvest the ``cf_clearance`` cookie + the browser's exact User-Agent.

Install with the optional extra::

    pip install lncrawl-scraper[browser]

Cloudflare flags genuine ``headless`` Chrome. You can use :class:`RemoteSolver`
if you are running in a headless environment.

nodriver is async-only; :meth:`solve` wraps it in a private event loop so the
public surface stays synchronous. Do not call it from inside a running event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Optional

from ..exceptions import CloudflareSolveError
from ..utils import validate_url
from .browser_exe import pick_executable
from .clearance import ClearanceResult, ClearanceSolver

if TYPE_CHECKING:
    from nodriver import Browser, Tab  # type:ignore


logger = logging.getLogger(__name__)


class BrowserSolver(ClearanceSolver):
    """Solve Cloudflare challenges with an in-process nodriver browser."""

    def __init__(
        self,
        *,
        app_mode: bool = True,
        headless: bool = False,
        timeout: float = 60.0,
        user_data_dir: Optional[str] = None,
    ) -> None:
        """Args:
        app_mode: Open the browser in a window without any addressbar or tabs.
            Default interface to get the captcha solved by the user.
        headless: Run Chrome headless. Default is False to let user solve the
            captcha challenge manually.
        timeout: Max seconds to wait for the ``cf_clearance`` cookie to appear.
        user_data_dir: Persist the Chrome profile at this path across runs. Reusing
            it keeps cookies (incl. cf_clearance), local storage, and a stable
            fingerprint, so subsequent runs often skip the challenge entirely until
            the clearance expires. When omitted, a throwaway profile is used and
            discarded each run.
        """
        self.headless = headless
        self.app_mode = app_mode
        self.timeout = timeout
        self.user_data_dir = user_data_dir

    async def solve(
        self,
        url: str,
        *,
        proxy: str | None = None,
        user_agent: str | None = None,
    ) -> Optional[ClearanceResult]:
        if not validate_url(url):
            raise ValueError(f"Invalid URL: {url!r}")

        try:
            import nodriver as uc  # type: ignore
        except ImportError as e:
            raise CloudflareSolveError(
                "BrowserSolver requires the 'browser' extra. Install it with: "
                "pip install lncrawl-scraper[browser]"
            ) from e

        executable = pick_executable()
        app_mode = self.app_mode and executable

        # https://peter.sh/experiments/chromium-command-line-switches/
        browser_args = []
        browser_args += [
            "--desktop",
            "--window-size=414,725",
            "--force-device-scale-factor=1",
        ]
        if proxy:
            browser_args.append(f"--proxy-server={proxy}")
        if app_mode:
            browser_args.append(f"--app={url}")

        try:
            browser = await uc.start(
                headless=self.headless,
                browser_args=browser_args,
                user_data_dir=self.user_data_dir,
                browser_executable_path=executable,
            )
        except Exception as e:
            raise CloudflareSolveError("Failed to start the browser") from e

        try:
            tab = browser.main_tab
            if not app_mode:
                tab = await browser.get(url)

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                cookie_jar = await browser.cookies.get_all()
                cookies = {c.name: c.value for c in cookie_jar if c.value}
                if "cf_clearance" in cookies:
                    user_agent = await self._read_user_agent(tab)
                    return ClearanceResult(cookies=cookies, user_agent=user_agent)
                await asyncio.sleep(1)

            return None
        except Exception as e:
            raise CloudflareSolveError("Failed to obtain 'cf_clearance' from browser") from e
        finally:
            with suppress(Exception):
                browser.stop()

    @staticmethod
    async def _read_cookies(browser: Browser) -> dict[str, str]:
        cookies = await browser.cookies.get_all()
        return {c.name: c.value for c in cookies if c.value}

    @staticmethod
    async def _read_user_agent(tab: Tab) -> str | None:
        try:
            ua = await tab.evaluate("navigator.userAgent")
            if ua:
                return str(ua)
        except Exception:
            pass
        return None
