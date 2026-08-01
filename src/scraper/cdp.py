"""Driving Chrome directly, over the protocol it already speaks.

The only solver bundled here, and it depends on nothing but a WebSocket. It replaced
a driver library that could not be imported below Python 3.10 or from 3.14, which left
both ends of the supported range with no solver at all — a challenged origin failing
honestly rather than being solved, on exactly the interpreters the frozen builds and
the server image run.

A solve needs seven calls, counted rather than guessed: start a browser, open a page,
wait, read the HTML, evaluate an expression, read the cookies, stop. Every one is a
single CDP command. The library this replaced was fifty thousand lines, forty thousand
of them generated bindings for the rest of the protocol. Head to head over the
challenged corpus it was no better: 12 hosts cleared to 11, at the same median.

**Owning the wire buys a detection property, and it is the reason to do this rather
than wrap something.** Eagerly-enabled CDP domains are a known tell, and a
general-purpose driver has to enable them because it cannot know what its caller will
ask for next. This one does know: ``Runtime.evaluate`` and ``Page.navigate`` are
commands, not subscriptions, so neither ``Runtime.enable`` nor ``Page.enable`` is ever
sent and no domain is ever turned on. Going through a higher-level abstraction —
including Chrome's own WebDriver BiDi, which is implemented over CDP internally —
gives that control away.

**What this does not do** is synthesise mouse, scroll or keystroke dynamics. Behaviour
is :mod:`scraper.pacing`'s job and stays there.

The split into a transport, a backend and a solver was not premature: the transport is
:mod:`scraper.wire`, and :mod:`scraper.bidi` drives Firefox over it without changing a
line of it — a second vocabulary rather than a second client.
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
    HEADLESS_FIRST_SHARE,
    BrowserSolver,
    RenderError,
    SolveError,
    SolveResult,
    browser_slot,
    chrome_proxy,
    clearance_deadline,
    has_display,
    honest_user_agent,
    launch_flags,
    raise_window,
    resolve_mode,
)
from .browsers import pick_chromium
from .diagnosis import is_still_challenged
from .wire import ProtocolError, WsClient

logger = logging.getLogger(__name__)

_RENDER_POLL = 0.25
"""How often to ask whether a rendering page is done. Matches ``browser.py``."""

_PORT_FILE = "DevToolsActivePort"

_PORT_WAIT = 30.0
"""How long to wait for the browser to publish its debugging port.

Generous because it covers a cold start on a slow disk with a fresh profile, which is
the first thing that happens on a new machine and the worst case there is."""

_CLOSE_WAIT = 5.0
"""How long a browser gets to exit on its own before it is signalled."""


class ChromeBackend:
    """One running Chrome, and the CDP vocabulary for the seven things we need.

    Owns the process as well as the connection, because the two fail together: a
    browser that will not answer has to be signalled, and a socket outliving its
    process is a solve that hangs until the deadline instead of failing.
    """

    def __init__(
        self,
        executable: str,
        *,
        headless: bool,
        profile_dir: Path,
        flags: List[str],
    ) -> None:
        self._client: Optional[WsClient] = None
        self._session = ""

        argv = [executable, f"--user-data-dir={profile_dir}", "--remote-debugging-port=0"]
        if headless:
            argv.append("--headless=new")
        # A fresh profile otherwise opens the welcome flow, and the first-run dialog
        # is modal — the page never navigates and the solve times out with no clue.
        argv.extend(["--no-first-run", "--no-default-browser-check"])
        argv.extend(flags)

        # Stale from a previous run means reading the wrong port and connecting to
        # nothing, so it goes before the browser that writes the new one starts.
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
        """Where to connect, read from the file the browser writes when it is ready.

        From the profile directory rather than by parsing stderr: the file is where
        Chrome puts it on every platform, and stderr is a stream we would otherwise
        have to keep drained to stop the browser blocking on a full pipe.
        """
        deadline = time.monotonic() + _PORT_WAIT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise ProtocolError(
                    f"the browser exited immediately (status {self._proc.returncode})"
                )
            try:
                lines = port_file.read_text(encoding="utf-8").split("\n")
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].strip():
                port, path = lines[0].strip(), lines[1].strip()
                return f"ws://127.0.0.1:{port}{path}"
            time.sleep(0.05)
        raise ProtocolError(f"the browser did not report a debugging port within {_PORT_WAIT:.0f}s")

    def bring_to_front(self) -> None:
        """Ask the browser to raise its own window.

        Chromium implements this as ``WebContents::Activate`` onto the native window, so
        it is a real raise rather than a tab switch, and it is the only mechanism with any
        chance on Wayland — where a client cannot take focus and can only be given it.
        """
        try:
            self._rpc.send("Page.bringToFront", {}, session=self._session)
        except Exception:  # noqa: BLE001
            logger.debug("Page.bringToFront was refused", exc_info=True)

    @property
    def pid(self) -> Optional[int]:
        """The browser process, so a caller can address its window."""
        return getattr(self._proc, "pid", None)

    @property
    def _rpc(self) -> WsClient:
        if self._client is None:
            raise ProtocolError("the browser connection is closed")
        return self._client

    def version(self) -> Dict[str, Any]:
        """What the browser says it is. Needs no page and no session."""
        return self._rpc.send("Browser.getVersion")

    def attach(self) -> None:
        """Take over a tab, so everything after this is scoped to one page.

        Reuses the tab the browser opened rather than adding one. Creating a second
        leaves the first sitting on the new-tab page, which is confusing in a headed
        window a person is expected to look at and solve.
        """
        targets = self._rpc.send("Target.getTargets").get("targetInfos") or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        target_id = page.get("targetId") if page else None
        if not target_id:
            target_id = self._rpc.send("Target.createTarget", {"url": "about:blank"}).get(
                "targetId"
            )
        if not target_id:
            raise ProtocolError("the browser opened no page to attach to")
        # `flatten` puts session-scoped messages on this same socket, keyed by
        # sessionId, instead of wrapping them in Target.sendMessageToTarget.
        result = self._rpc.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self._session = str(result.get("sessionId") or "")
        if not self._session:
            raise ProtocolError("the browser did not open a session on the page")

    def navigate(self, url: str, *, timeout: float) -> None:
        result = self._rpc.send(
            "Page.navigate", {"url": url}, session=self._session, timeout=timeout
        )
        # Page.navigate answers with the failure in the result rather than as a
        # protocol error, so a DNS miss or a refused connection is silent unless read.
        if result.get("errorText"):
            raise ProtocolError(f"{url} could not be loaded: {result['errorText']}")

    def evaluate(self, expression: str, *, timeout: float = 30.0) -> Any:
        result = self._rpc.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session=self._session,
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            return None
        return (result.get("result") or {}).get("value")

    def content(self) -> str:
        """The page's HTML as it stands now, hydration included."""
        return str(self.evaluate("document.documentElement.outerHTML") or "")

    def user_agent(self) -> str:
        return str(self.evaluate("navigator.userAgent") or "")

    def has(self, selector: str) -> bool:
        return bool(self.evaluate(f"!!document.querySelector({json.dumps(selector)})"))

    def cookies(self) -> Tuple[Dict[str, str], float]:
        raw = self._rpc.send("Storage.getCookies").get("cookies") or []
        jar: Dict[str, str] = {}
        expiries: Dict[str, float] = {}
        for cookie in raw:
            name = str(cookie.get("name") or "")
            if not name:
                continue
            jar[name] = str(cookie.get("value") or "")
            expiries[name] = float(cookie.get("expires") or 0)
        return jar, clearance_deadline(expiries)

    def close(self) -> None:
        """Shut the browser down, and never let failing to mask a finished solve.

        Escalating rather than trusting any one step: a browser mid-challenge does
        not always honour ``Browser.close``, and one left running holds a profile
        directory that the next solve on the same address then shares."""
        if self._client is not None:
            try:
                self._client.send("Browser.close", timeout=_CLOSE_WAIT)
            except Exception:  # noqa: BLE001 - the signal below is the real guarantee
                logger.debug("the browser refused Browser.close", exc_info=True)
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


class CdpSolver(BrowserSolver):
    """Solves by driving Chrome over CDP, with no driver library in between.

    Works on every Python this package supports, which is the point: the driver
    library this replaced was absent below 3.10 and from 3.14, and those are the
    interpreters the frozen builds and the server image actually run.

    Args:
        executable: Path to the browser. Looked up on ``PATH`` when omitted, which
            finds a Linux install and generally not a macOS or Windows one — a
            caller that already knows where the browser is should say so.
        headless: ``True`` by default. Headless clears
            exactly what headed clears, and most deployments have no display to put a
            window on. Pass ``False`` to get one, which also buys the interactive
            budget.
        args: Extra command-line flags, appended after the defaults.
        settle: Seconds to let a page run before first reading it.
    """

    name = "cdp"
    engine = "chromium"

    def __init__(
        self,
        *,
        executable: Optional[str] = None,
        headless: bool = True,
        mode: Optional[str] = None,
        args: Optional[List[str]] = None,
        settle: float = 3.0,
    ) -> None:
        self._executable = executable
        self._headless = headless
        self._extra = list(args or [])
        self._settle = settle
        self._lock = threading.Lock()
        self._user_agent: Optional[str] = None
        self.mode = resolve_mode(mode, headless)
        self.interactive = self.mode != "headless" and has_display()

    def solve(
        self,
        url: str,
        *,
        proxy: Optional[str] = None,
        profile_dir: Optional[Path] = None,
        timeout: float = 60.0,
    ) -> SolveResult:
        # One browser at a time per solver: two sharing a profile directory corrupt
        # it, and that profile is what carries the accumulated history a solve rests on.
        with browser_slot(self.engine), self._lock:
            if self.mode == "headless" or not self.interactive:
                return self._attempt(url, proxy, profile_dir, headless=True, timeout=timeout)
            if self.mode == "headed":
                return self._attempt(
                    url, proxy, profile_dir, headless=False, timeout=timeout, show=True
                )

            # auto: unattended first. Over 46 challenged hosts a corrected headless
            # browser cleared everything a headed one did, so a window up front spends a
            # person's attention to buy nothing. It earns its place only once the solver
            # has failed and somebody else could answer.
            unattended = max(1.0, timeout * HEADLESS_FIRST_SHARE)
            deadline = time.monotonic() + timeout
            try:
                result = self._attempt(url, proxy, profile_dir, headless=True, timeout=unattended)
                if result.cleared:
                    return result
            except SolveError:
                pass

            remaining = max(1.0, deadline - time.monotonic())
            logger.info(
                "opening a browser window for %s — solve the challenge in it. Waiting up to %.0fs.",
                url,
                remaining,
            )
            return self._attempt(
                url, proxy, profile_dir, headless=False, timeout=remaining, show=True
            )

    def _attempt(
        self,
        url: str,
        proxy: Optional[str],
        profile_dir: Optional[Path],
        *,
        headless: bool,
        timeout: float,
        show: bool = False,
    ) -> SolveResult:
        with self._browser(proxy, profile_dir, headless=headless) as browser:
            # Started before the navigation, so *timeout* bounds the whole call. Two
            # budgets in sequence would let a slow load double what the caller asked for.
            deadline = time.monotonic() + timeout
            browser.attach()
            browser.navigate(url, timeout=timeout)
            if show:
                logger.info(
                    "A browser window has opened for %s — solve the challenge in it. "
                    "Waiting up to %.0fs.",
                    url,
                    timeout,
                )
                # Ask the browser to raise itself first — the only route with any chance
                # on Wayland — then the platform, which wins where the window manager
                # refuses a focus change the browser requested for itself.
                browser.bring_to_front()
                raise_window(self._executable, browser.pid)
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
        with browser_slot(self.engine), self._lock:
            with self._browser(proxy, profile_dir) as browser:
                deadline = time.monotonic() + timeout  # the whole call, as in solve
                browser.attach()
                browser.navigate(url, timeout=timeout)
                if wait_for is None:
                    # With no selector to poll for, the only thing standing in for
                    # "the page has run" is time. With one, the wait ends on evidence
                    # and this delay would be dead time.
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

    def _browser(
        self,
        proxy: Optional[str],
        profile_dir: Optional[Path],
        headless: Optional[bool] = None,
    ) -> "_Session":
        executable = self._resolve_executable()
        # Settled before any browser starts, the User-Agent probe included. A proxy
        # this browser cannot use is not worth launching one to find out about, and
        # running headless there is a whole browser start spent on a refusal.
        usable = chrome_proxy(proxy or "")
        return _Session(
            executable=executable,
            headless=self._headless if headless is None else headless,
            profile_dir=profile_dir,
            flags=launch_flags(usable, user_agent=self._honest_user_agent(), extra=self._extra),
        )

    def _resolve_executable(self) -> str:
        if self._executable:
            return self._executable
        found = pick_chromium()
        if found:
            self._executable = found
            return found
        raise SolveError("no Chromium-family browser is installed; pass executable= to CdpSolver")

    def _honest_user_agent(self) -> str:
        """The User-Agent to launch headless under, or ``""`` when headed.

        Read from the browser rather than composed, because the string is specific to
        this build and platform, and set as a launch flag — so it has to be known
        before the browser that reports it exists. Hence one throwaway launch, cached
        for the life of the solver and never paid at all when running headed.

        Cheap, at least: ``Browser.getVersion`` answers without a page, a session or
        a navigation.
        """
        if not self._headless:
            return ""
        if self._user_agent is None:
            reported = ""
            try:
                with _Session(
                    executable=self._resolve_executable(),
                    headless=True,
                    profile_dir=None,
                    flags=launch_flags(extra=self._extra),
                ) as browser:
                    reported = str(browser.version().get("userAgent") or "")
            except Exception:  # noqa: BLE001 - launching as-is beats not launching
                logger.debug("could not read the browser's User-Agent", exc_info=True)
            self._user_agent = honest_user_agent(reported)
        return self._user_agent


class _Session:
    """A :class:`ChromeBackend` and, when the caller supplied none, the profile it ran in.

    A profile directory is not optional the way it looks: the debugging port is
    published inside it, so there is nowhere else to read the endpoint from. One is
    minted per call when the caller passes none, and removed with the browser — a
    caller that *did* pass one keeps it, since accumulated history is the reason to
    have one at all.
    """

    def __init__(
        self,
        *,
        executable: str,
        headless: bool,
        profile_dir: Optional[Path],
        flags: List[str],
    ) -> None:
        self._executable = executable
        self._headless = headless
        self._given = profile_dir
        self._flags = flags
        self._profile: Optional[Path] = None
        self._backend: Optional[ChromeBackend] = None

    def __enter__(self) -> ChromeBackend:
        self._profile = self._given or Path(tempfile.mkdtemp(prefix="scraper-cdp-"))
        try:
            self._backend = ChromeBackend(
                self._executable,
                headless=self._headless,
                profile_dir=self._profile,
                flags=self._flags,
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
