"""Driving Firefox over WebDriver BiDi.

A second browser is not variety for its own sake. ``ScraperConfig.profile()`` has to
make every request present the fingerprint the clearance was earned under, so a
Chrome-only solver forces chrome impersonation everywhere the moment one is configured
— and over a 150-host sweep firefox won four hosts against chrome and lost none. A
Firefox solver declares ``impersonation = "firefox"`` and the trade disappears.

It also clears marginally more: over the same 46 challenged hosts, 29 against Chrome's
28, and faster. Read that as a tie rather than a win — the two disagree on seven hosts,
four to Firefox and three to Chrome, so neither backend dominates and the union clears
more than either. The impersonation above is the reason to prefer this one.

**Firefox does not speak CDP as a supported path.** Mozilla's implementation was always
a partial Puppeteer-targeted subset and is deprecated in favour of WebDriver BiDi, the
W3C standard. So this is a second command vocabulary, not a second client: the
transport in :mod:`scraper.wire` is shared with :mod:`scraper.cdp` unchanged.

| need | this module (BiDi) | :mod:`scraper.cdp` |
| --- | --- | --- |
| session | ``session.new`` + ``browsingContext.getTree`` | ``Target.attachToTarget`` |
| navigate | ``browsingContext.navigate`` | ``Page.navigate`` |
| evaluate | ``script.evaluate`` | ``Runtime.evaluate`` |
| cookies | ``storage.getCookies`` | ``Storage.getCookies`` |

**One cost is real and worth stating plainly.** A WebDriver session sets
``navigator.webdriver`` to true — the spec requires it, and ``dom.webdriver.enabled =
false`` does not override it while a session is open. Measured both ways over the 46
challenged hosts: **10 cleared with the property visible, 29 with it hidden.** So it is
worth nineteen hosts rather than all of them — ten sites challenge without reading it,
and none cleared only when it was visible. Chrome has a launch flag that stops Blink
emitting the property at all; Firefox has
no equivalent, so the only lever is a preload script deleting it before page scripts
run. That is patching a surface value rather than not emitting one, which is a weaker
footing than the Chrome backend's — a site reading the prototype descriptor rather than
the property would see through it. It holds against the corpus today.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .browser import (
    BrowserSolver,
    RenderError,
    SolveError,
    SolveResult,
    chrome_proxy,
    clearance_deadline,
)
from .diagnosis import is_still_challenged
from .wire import ProtocolError, WsClient

logger = logging.getLogger(__name__)

_RENDER_POLL = 0.25
_PORT_FILE = "WebDriverBiDiServer.json"
_PORT_WAIT = 45.0
"""Longer than Chrome's. A fresh Firefox profile does more work on first start."""

_CLOSE_WAIT = 5.0

_HIDE_WEBDRIVER = "() => { delete Object.getPrototypeOf(navigator).webdriver; }"
"""Run before every page's own scripts. See the module docstring for why this exists
and why it is the weakest part of this backend."""


def firefox_prefs(proxy: Optional[str]) -> str:
    """The ``user.js`` a solving profile starts with.

    Firefox takes its proxy from preferences rather than from a command line, which is
    the one place this backend cannot borrow Chrome's approach. The credential rule is
    borrowed though — :func:`~scraper.browser.chrome_proxy` refuses a URL carrying any,
    and it is refused here for the same reason: Firefox would prompt for them, which no
    unattended solve can answer, so the browser would leave by an address the requests
    replaying its clearance will not use.
    """
    lines = [
        # A WebDriver session is not a reason to hand the page a banner, and the
        # notification is one more thing that differs from an ordinary visit.
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        # WebRTC, for the reason every solver here disables it: a STUN request reports
        # the host's real address even when every HTTP request goes through the proxy.
        'user_pref("media.peerconnection.enabled", false);',
    ]
    usable = chrome_proxy(proxy or "")
    if usable:
        scheme, _, hostport = usable.partition("://")
        host, _, port = hostport.rpartition(":")
        lines.append('user_pref("network.proxy.type", 1);')
        if scheme.startswith("socks"):
            lines.append(f'user_pref("network.proxy.socks", "{host}");')
            lines.append(f'user_pref("network.proxy.socks_port", {port});')
            lines.append(
                f'user_pref("network.proxy.socks_version", {5 if scheme == "socks5" else 4});'
            )
            # Resolve at the proxy, which is what socks5h asked for and what an exit
            # is for: a name resolved locally leaks the lookup past the proxy.
            lines.append('user_pref("network.proxy.socks_remote_dns", true);')
        else:
            for key in ("http", "ssl"):
                lines.append(f'user_pref("network.proxy.{key}", "{host}");')
                lines.append(f'user_pref("network.proxy.{key}_port", {port});')
    return "\n".join(lines) + "\n"


class FirefoxBackend:
    """One running Firefox, and the BiDi vocabulary for what a solve needs."""

    def __init__(
        self,
        executable: str,
        *,
        headless: bool,
        profile_dir: Path,
        proxy: Optional[str],
        extra: List[str],
    ) -> None:
        self._client: Optional[WsClient] = None
        self._context = ""
        self._user_agent = ""

        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "user.js").write_text(firefox_prefs(proxy), encoding="utf-8")

        argv = [executable, "--remote-debugging-port", "0", "--profile", str(profile_dir)]
        if headless:
            argv.append("--headless")
        # --no-remote keeps this out of an already-running Firefox on a desktop, where
        # a second launch would otherwise hand its arguments to the first and exit.
        argv.extend(["--no-remote", *extra, "about:blank"])

        port_file = profile_dir / _PORT_FILE
        try:
            port_file.unlink()
        except OSError:
            pass

        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        try:
            self._client = WsClient(self._await_endpoint(port_file))
        except BaseException:
            self.close()
            raise

    def _await_endpoint(self, port_file: Path) -> str:
        """Where to connect, from the file Firefox writes once BiDi is listening.

        Symmetric with Chrome's ``DevToolsActivePort`` despite the different name, so
        neither backend has to read stderr — which would otherwise have to be kept
        drained, since a browser filling the pipe blocks.
        """
        deadline = time.monotonic() + _PORT_WAIT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise ProtocolError(
                    f"the browser exited immediately (status {self._proc.returncode})"
                )
            try:
                published = json.loads(port_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                published = None
            if isinstance(published, dict) and published.get("ws_port"):
                host = published.get("ws_host") or "127.0.0.1"
                return f"ws://{host}:{published['ws_port']}/session"
            time.sleep(0.05)
        raise ProtocolError(f"the browser did not report a BiDi port within {_PORT_WAIT:.0f}s")

    @property
    def _rpc(self) -> WsClient:
        if self._client is None:
            raise ProtocolError("the browser connection is closed")
        return self._client

    def attach(self) -> None:
        """Open the session and take the tab Firefox already has."""
        result = self._rpc.send("session.new", {"capabilities": {}})
        capabilities = result.get("capabilities") or {}
        # Answered here rather than by evaluating navigator.userAgent later: it costs
        # no page, and the clearance is bound to this exact string.
        self._user_agent = str(capabilities.get("userAgent") or "")

        self._rpc.send("script.addPreloadScript", {"functionDeclaration": _HIDE_WEBDRIVER})

        contexts = self._rpc.send("browsingContext.getTree").get("contexts") or []
        self._context = str(contexts[0].get("context") or "") if contexts else ""
        if not self._context:
            created = self._rpc.send("browsingContext.create", {"type": "tab"})
            self._context = str(created.get("context") or "")
        if not self._context:
            raise ProtocolError("the browser opened no page to drive")

    def navigate(self, url: str, *, timeout: float) -> None:
        try:
            self._rpc.send(
                "browsingContext.navigate",
                {"context": self._context, "url": url, "wait": "complete"},
                timeout=timeout,
            )
        except ProtocolError:
            # A challenge page often never reaches "complete" — it reloads itself
            # partway. The poll loop is what decides whether the solve worked, so a
            # navigation that gave up is not on its own a failure.
            logger.debug("navigation to %s did not settle; polling anyway", url, exc_info=True)

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        result = self._rpc.send(
            "script.evaluate",
            {
                "expression": expression,
                "target": {"context": self._context},
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if result.get("type") != "success":
            return None
        return (result.get("result") or {}).get("value")

    def content(self) -> str:
        return str(self.evaluate("document.documentElement.outerHTML") or "")

    def user_agent(self) -> str:
        return self._user_agent

    def has(self, selector: str) -> bool:
        return bool(self.evaluate(f"!!document.querySelector({json.dumps(selector)})"))

    def cookies(self) -> Tuple[Dict[str, str], float]:
        raw = self._rpc.send("storage.getCookies").get("cookies") or []
        jar: Dict[str, str] = {}
        expiries: Dict[str, float] = {}
        for cookie in raw:
            name = str(cookie.get("name") or "")
            if not name:
                continue
            # BiDi wraps a cookie value as {"type": "string", "value": …} rather than
            # handing back a bare string the way CDP does.
            value = cookie.get("value")
            jar[name] = str(value.get("value", "") if isinstance(value, dict) else value or "")
            expiries[name] = float(cookie.get("expiry") or 0)
        return jar, clearance_deadline(expiries)

    def close(self) -> None:
        """Shut the browser down, and never let failing to mask a finished solve."""
        if self._client is not None:
            try:
                self._client.send("session.end", timeout=_CLOSE_WAIT)
            except Exception:  # noqa: BLE001 - the signal below is the real guarantee
                logger.debug("the browser refused session.end", exc_info=True)
            self._client.close()
            self._client = None
        try:
            self._proc.wait(timeout=_CLOSE_WAIT)
        except Exception:  # noqa: BLE001
            for stop in (self._proc.terminate, self._proc.kill):
                try:
                    stop()
                    self._proc.wait(timeout=_CLOSE_WAIT)
                    break
                except Exception:  # noqa: BLE001 - a frozen build has no shell to fall back on
                    logger.debug("the browser did not stop when asked", exc_info=True)


class BidiSolver(BrowserSolver):
    """Solves by driving Firefox over WebDriver BiDi.

    Not the default — :class:`~scraper.cdp.CdpSolver` is — but a real alternative
    rather than a fallback. Choosing it makes ``firefox`` the impersonation profile for
    every request, which is the profile that measured best on the corpus and which a
    Chrome solver otherwise rules out.

    Args:
        executable: Path to Firefox. Looked up on ``PATH`` when omitted, which finds a
            Linux install and generally not a macOS or Windows one.
        headless: ``True`` by default, as with the Chrome solver.
        args: Extra command-line flags, appended after the defaults.
        settle: Seconds to let a page run before first reading it.
    """

    name = "bidi"
    impersonation = "firefox"

    def __init__(
        self,
        *,
        executable: Optional[str] = None,
        headless: bool = True,
        args: Optional[List[str]] = None,
        settle: float = 3.0,
    ) -> None:
        self._executable = executable
        self._headless = headless
        self._extra = list(args or [])
        self._settle = settle
        self._lock = threading.Lock()
        self.interactive = not headless

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        with self._lock:
            with self._browser(proxy, profile_dir) as browser:
                deadline = time.monotonic() + timeout
                browser.attach()
                browser.navigate(url, timeout=timeout)
                while time.monotonic() < deadline:
                    time.sleep(self._settle)
                    if not is_still_challenged(browser.content()):
                        break
                user_agent = browser.user_agent()
                cookies, expires_at = browser.cookies()

        if not user_agent:
            raise SolveError(
                "the browser did not report a User-Agent; the clearance is bound to it "
                "and cannot be replayed without it"
            )
        return SolveResult(cookies=cookies, user_agent=user_agent, expires_at=expires_at)

    def render(
        self,
        url: str,
        *,
        wait_for: Optional[str] = None,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> str:
        with self._lock:
            with self._browser(proxy, profile_dir) as browser:
                deadline = time.monotonic() + timeout
                browser.attach()
                browser.navigate(url, timeout=timeout)
                if wait_for is None:
                    time.sleep(self._settle)
                while True:
                    content = browser.content()
                    if not is_still_challenged(content):
                        if wait_for is None or browser.has(wait_for):
                            return content
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_RENDER_POLL)

        missing = f"{wait_for} never appeared" if wait_for else "it was still a challenge"
        raise RenderError(f"{url} did not render after {timeout:.0f}s: {missing}")

    def close(self) -> None:
        """Nothing outlives a call, so there is nothing here to release."""

    # --------------------------------------------------------------------- #

    def _browser(self, proxy: Optional[str], profile_dir: Optional[Path]) -> "_Session":
        executable = self._resolve_executable()
        # Settled before the browser starts, as in the Chrome backend: a proxy it
        # cannot use is not worth launching one to discover.
        chrome_proxy(proxy or "")
        return _Session(
            executable=executable,
            headless=self._headless,
            profile_dir=profile_dir,
            proxy=proxy,
            extra=self._extra,
        )

    def _resolve_executable(self) -> str:
        if self._executable:
            return self._executable
        for name in ("firefox", "firefox-esr"):
            found = shutil.which(name)
            if found:
                self._executable = found
                return found
        raise SolveError("no firefox executable was found on PATH; pass executable= to BidiSolver")


class _Session:
    """A :class:`FirefoxBackend` and, when the caller supplied none, the profile it ran in.

    A profile directory is not optional: the BiDi port is published inside it, and the
    proxy preferences have to be written into it before launch. One is minted per call
    when the caller passes none, and removed with the browser.
    """

    def __init__(
        self,
        *,
        executable: str,
        headless: bool,
        profile_dir: Optional[Path],
        proxy: Optional[str],
        extra: List[str],
    ) -> None:
        self._executable = executable
        self._headless = headless
        self._given = profile_dir
        self._proxy = proxy
        self._extra = extra
        self._profile: Optional[Path] = None
        self._backend: Optional[FirefoxBackend] = None

    def __enter__(self) -> FirefoxBackend:
        self._profile = self._given or Path(tempfile.mkdtemp(prefix="scraper-bidi-"))
        try:
            self._backend = FirefoxBackend(
                self._executable,
                headless=self._headless,
                profile_dir=self._profile,
                proxy=self._proxy,
                extra=self._extra,
            )
        except BaseException:
            self._discard_profile()
            raise
        return self._backend

    def __exit__(self, *_: object) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self._discard_profile()

    def _discard_profile(self) -> None:
        if self._given is None and self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)
        self._profile = None
