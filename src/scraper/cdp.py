"""Driving Chrome directly, over the protocol it already speaks.

The bundled :class:`~scraper.browser.NoDriverSolver` cannot run everywhere this
library does. nodriver evaluates a PEP 604 union at import time below Python 3.10 and
its generated ``cdp/network.py`` fails to tokenize from 3.14, so on both ends of the
supported range there is simply no solver — and a challenged origin fails honestly
rather than being solved. This module closes that gap with the seven calls a solve
actually needs, spoken straight to the browser.

Seven, measured rather than guessed: start a browser, open a page, wait, read the
HTML, evaluate an expression, read the cookies, stop. Every one is a single CDP
command. For scale, nodriver is fifty thousand lines of which forty thousand are
generated bindings for the rest of the protocol.

**Owning the wire buys a detection property, and it is the reason to do this rather
than wrap something.** Eagerly-enabled CDP domains are a known tell, and a
general-purpose driver has to enable them because it cannot know what its caller will
ask for next. This one does know: ``Runtime.evaluate`` and ``Page.navigate`` are
commands, not subscriptions, so neither ``Runtime.enable`` nor ``Page.enable`` is ever
sent and no domain is ever turned on. Going through a higher-level abstraction —
including Chrome's own WebDriver BiDi, which is implemented over CDP internally —
gives that control away.

**What this does not do** is synthesise mouse, scroll or keystroke dynamics, exactly
as nodriver does not. Behaviour is :mod:`scraper.pacing`'s job and stays there.

The split into a transport, a backend and a solver is deliberate and not premature:
:class:`_WsClient` is a WebSocket JSON-RPC channel with a request id, which is equally
what WebDriver BiDi is. A Firefox backend reuses it unchanged and only has to speak a
second vocabulary.
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
    clearance_deadline,
    honest_user_agent,
    launch_flags,
)
from .diagnosis import is_still_challenged
from .exceptions import MissingDependency

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

_MAX_FRAME = 64 * 1024 * 1024
"""Cap on one protocol message.

The library default is 1 MiB, which a page's own HTML clears without difficulty — and
the failure is a closed connection mid-solve rather than anything naming a size."""


def _connect() -> Any:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise MissingDependency("cdp", "driving a browser over CDP") from exc
    return connect


class CdpError(SolveError):
    """The browser answered a command with an error, or stopped answering."""


def _describe_error(reply: Dict[str, Any]) -> str:
    """What went wrong, in whichever way this protocol says it.

    The one place the two vocabularies are not the same shape. CDP puts an object
    under ``error`` with the text in ``message``; BiDi puts a code *string* there and
    the detail in a sibling ``message``. Reading either shape as the other raises an
    ``AttributeError`` from inside the transport, which buries the actual failure.
    """
    error = reply.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    detail = reply.get("message")
    return f"{error}: {detail}" if detail else str(error)


class _WsClient:
    """A WebSocket JSON-RPC channel: send a command, get the matching reply.

    Backend-agnostic on purpose — CDP and WebDriver BiDi are the same shape on the
    wire, an object with an ``id`` going out and an object carrying that ``id`` coming
    back, interleaved with events that carry none. They differ on how an error is
    spelled, which :func:`_describe_error` absorbs.

    Synchronous, and single-caller by construction: the solver holds its lock for the
    whole of a solve, so there is never a second command in flight and correlation
    needs no more than reading until the id matches. Events arriving in between are
    dropped rather than queued, because nothing here subscribes to any.
    """

    def __init__(self, url: str, *, open_timeout: float = 10.0) -> None:
        self._sock = _connect()(url, open_timeout=open_timeout, max_size=_MAX_FRAME)
        self._next_id = 0

    def send(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session: str = "",
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message: Dict[str, Any] = {"id": request_id, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        self._sock.send(json.dumps(message))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(f"{method} did not answer within {timeout:.0f}s")
            try:
                raw = self._sock.recv(timeout=remaining)
            except TimeoutError as exc:
                raise CdpError(f"{method} did not answer within {timeout:.0f}s") from exc
            except Exception as exc:  # noqa: BLE001 - a dropped socket is a solve failure
                raise CdpError(f"the browser stopped answering during {method}") from exc
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            if reply.get("id") != request_id:
                continue  # an event, or a reply to something already abandoned
            if "error" in reply or reply.get("type") == "error":
                raise CdpError(f"{method}: {_describe_error(reply)}")
            return reply.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001 - closing a dead socket is not a failure
            logger.debug("the protocol socket did not close cleanly", exc_info=True)


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
        self._client: Optional[_WsClient] = None
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
            self._client = _WsClient(self._await_endpoint(port_file))
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
                raise CdpError(f"the browser exited immediately (status {self._proc.returncode})")
            try:
                lines = port_file.read_text(encoding="utf-8").split("\n")
            except OSError:
                lines = []
            if len(lines) >= 2 and lines[0].strip():
                port, path = lines[0].strip(), lines[1].strip()
                return f"ws://127.0.0.1:{port}{path}"
            time.sleep(0.05)
        raise CdpError(f"the browser did not report a debugging port within {_PORT_WAIT:.0f}s")

    @property
    def _rpc(self) -> _WsClient:
        if self._client is None:
            raise CdpError("the browser connection is closed")
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
            raise CdpError("the browser opened no page to attach to")
        # `flatten` puts session-scoped messages on this same socket, keyed by
        # sessionId, instead of wrapping them in Target.sendMessageToTarget.
        result = self._rpc.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self._session = str(result.get("sessionId") or "")
        if not self._session:
            raise CdpError("the browser did not open a session on the page")

    def navigate(self, url: str, *, timeout: float) -> None:
        result = self._rpc.send(
            "Page.navigate", {"url": url}, session=self._session, timeout=timeout
        )
        # Page.navigate answers with the failure in the result rather than as a
        # protocol error, so a DNS miss or a refused connection is silent unless read.
        if result.get("errorText"):
            raise CdpError(f"{url} could not be loaded: {result['errorText']}")

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

    Works on every Python this package supports, which is the point: the nodriver
    solver is absent below 3.10 and from 3.14, and those are the interpreters the
    frozen builds and the server image actually run.

    Args:
        executable: Path to the browser. Looked up on ``PATH`` when omitted, which
            finds a Linux install and generally not a macOS or Windows one — a
            caller that already knows where the browser is should say so.
        headless: ``False`` by default, matching the nodriver solver, and for the
            same reason: headless clears just as well, but a visible window is the
            one a person can reach into. On a server there is nobody to reach in.
        args: Extra command-line flags, appended after the defaults.
        settle: Seconds to let a page run before first reading it.
    """

    name = "cdp"

    def __init__(
        self,
        *,
        executable: Optional[str] = None,
        headless: bool = False,
        args: Optional[List[str]] = None,
        settle: float = 3.0,
    ) -> None:
        self._executable = executable
        self._headless = headless
        self._extra = list(args or [])
        self._settle = settle
        self._lock = threading.Lock()
        self._user_agent: Optional[str] = None
        self.interactive = not headless

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
        with self._lock:
            with self._browser(proxy, profile_dir) as browser:
                # Started before the navigation, so *timeout* bounds the whole call.
                # Two budgets in sequence would let a slow load double what the caller
                # asked for, and the interactive budget makes that ten minutes.
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

    def _browser(self, proxy: Optional[str], profile_dir: Optional[Path]) -> "_Session":
        return _Session(
            executable=self._resolve_executable(),
            headless=self._headless,
            profile_dir=profile_dir,
            flags=launch_flags(proxy, user_agent=self._honest_user_agent(), extra=self._extra),
        )

    def _resolve_executable(self) -> str:
        if self._executable:
            return self._executable
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                self._executable = found
                return found
        raise SolveError("no browser executable was found on PATH; pass executable= to CdpSolver")

    def _honest_user_agent(self) -> str:
        """The User-Agent to launch headless under, or ``""`` when headed.

        Read from the browser rather than composed, because the string is specific to
        this build and platform, and set as a launch flag — so it has to be known
        before the browser that reports it exists. Hence one throwaway launch, cached
        for the life of the solver and never paid at all when running headed.

        Cheaper here than under nodriver: ``Browser.getVersion`` answers without a
        page, a session or a navigation.
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
