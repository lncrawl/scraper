"""The CDP solver: the wire protocol, the launch, and the loop around the browser.

No Chrome is ever launched here. Both edges are stubbed — `subprocess.Popen` and the
WebSocket — which is the whole path this module owns: the flags it launches with, how
it finds the debugging port, how it correlates a reply to a request, the interstitial
loop, the cookie harvest and the shutdown.

What a stub cannot say is whether Chrome clears a real challenge; that is `livetest/`'s
job and `livetest/headless.py` in particular. What it can say is whether this module
drives a browser correctly, and — more to the point — that it never enables a CDP
domain, which is the detection property the module exists to keep.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from scraper.browser import (
    BrowserSolver,
    chrome_proxy,
    clearance_deadline,
    honest_user_agent,
    launch_flags,
)
from scraper.cdp import CdpSolver
from scraper.exceptions import MissingDependency, TierUnavailable
from scraper.wire import ProtocolError, WsClient

CLEARED_PAGE = "<!doctype html><html><body><h1>Chapter 12</h1></body></html>"
SHELL = '<!doctype html><html><body><div id="app"></div></body></html>'
CHALLENGE = (
    "<!doctype html><html><head><title>Just a moment...</title></head>"
    '<body><div id="challenge-running"></div></body></html>'
)

HEADED_UA = "Mozilla/5.0 (Macintosh) Chrome/150.0.0.0 Safari/537.36"
HEADLESS_UA = "Mozilla/5.0 (Macintosh) HeadlessChrome/150.0.0.0 Safari/537.36"


class FakeSocket:
    """A scripted CDP endpoint.

    *handler* is called with each outgoing message and returns the `result` object, or
    raises to produce a protocol error. Anything queued by `emit` is delivered before
    the next reply, which is how the event-interleaving cases are set up.
    """

    def __init__(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._handler = handler
        self._inbox: deque = deque()
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    def emit(self, message: Dict[str, Any]) -> None:
        self._inbox.append(json.dumps(message))

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        try:
            result = self._handler(message)
        except Exception as exc:  # noqa: BLE001 - a raising handler scripts a CDP error
            self._inbox.append(json.dumps({"id": message["id"], "error": {"message": str(exc)}}))
            return
        if result is None:
            return  # scripts "the browser never answers this"
        self._inbox.append(json.dumps({"id": message["id"], "result": result}))

    def recv(self, timeout: Optional[float] = None) -> str:
        if not self._inbox:
            raise TimeoutError("nothing to read")
        return self._inbox.popleft()

    def close(self) -> None:
        self.closed = True

    def methods(self) -> List[str]:
        return [m["method"] for m in self.sent]


class FakeProcess:
    """A launched browser that publishes a port file and exits when asked.

    Writes the port file only when there is not one already, which is what makes the
    stale-file case observable: a real Chrome would eventually overwrite it, but the
    endpoint is read the moment a readable file is there, so leaving one in place
    means connecting to the port a previous run used.
    """

    def __init__(self, argv: List[str], *, port_file: bool = True) -> None:
        self.argv = argv
        self.returncode: Optional[int] = None
        self.killed = False
        profile = next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir="))
        target = Path(profile) / "DevToolsActivePort"
        if port_file and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("9333\n/devtools/browser/abc")

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.killed = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 0


def install_cdp(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[Dict[str, Any]], Any],
    *,
    port_file: bool = True,
) -> Dict[str, Any]:
    """Stub both edges. Returns a dict collecting the socket and the launched process."""
    captured: Dict[str, Any] = {}

    def popen(argv: List[str], **_: Any) -> FakeProcess:
        proc = FakeProcess(argv, port_file=port_file)
        captured.setdefault("processes", []).append(proc)
        captured["argv"] = argv
        return proc

    def connect() -> Callable[..., FakeSocket]:
        def factory(url: str, **_: Any) -> FakeSocket:
            sock = FakeSocket(handler)
            captured.setdefault("sockets", []).append(sock)
            captured["url"] = url
            return sock

        return factory

    monkeypatch.setattr("scraper.cdp.subprocess.Popen", popen)
    monkeypatch.setattr("scraper.wire.connect", connect)
    return captured


def scripted(
    pages: List[str],
    *,
    cookies: Optional[List[Dict[str, Any]]] = None,
    user_agent: str = HEADED_UA,
    selector: bool = True,
) -> Callable[[Dict[str, Any]], Any]:
    """A handler serving *pages* in turn to each HTML read."""
    remaining = list(pages)

    def handle(message: Dict[str, Any]) -> Any:
        method = message["method"]
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "T1"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Page.navigate":
            return {"frameId": "F1"}
        if method == "Browser.getVersion":
            return {"userAgent": user_agent}
        if method == "Storage.getCookies":
            return {
                "cookies": cookies
                if cookies is not None
                else [{"name": "cf_clearance", "value": "x", "expires": 0}]
            }
        if method == "Runtime.evaluate":
            expression = message["params"]["expression"]
            if "outerHTML" in expression:
                return {
                    "result": {"value": remaining.pop(0) if len(remaining) > 1 else remaining[0]}
                }
            if "navigator.userAgent" in expression:
                return {"result": {"value": user_agent}}
            if "querySelector" in expression:
                return {"result": {"value": selector}}
        if method == "Browser.close":
            return {}
        raise AssertionError(f"unscripted method {method}")

    return handle


class TestSharedHelpers:
    """The rules two solvers must not disagree about."""

    def test_the_webrtc_flags_are_always_there(self):
        # Load-bearing: a STUN request reports the host's real address even when every
        # HTTP request goes through the proxy, and nothing fails when it happens.
        flags = launch_flags()
        assert "--disable-webrtc" in flags
        assert "--disable-features=WebRtcHideLocalIpsWithMdns" in flags

    def test_socks5h_is_translated_because_chrome_rejects_it(self):
        # Not a scheme Chrome knows: the flag is rejected whole and every navigation
        # fails with ERR_NO_SUPPORTED_PROXIES. Its socks5 resolves at the proxy
        # already, which is all the h ever asked for.
        assert chrome_proxy("socks5h://127.0.0.1:9250") == "socks5://127.0.0.1:9250"
        assert chrome_proxy("socks4a://127.0.0.1:9250") == "socks4://127.0.0.1:9250"
        assert chrome_proxy("http://p.test:8080") == "http://p.test:8080"
        assert chrome_proxy("") == ""

    def test_a_proxy_needing_credentials_is_refused_rather_than_stripped(self):
        # Measured: userinfo makes Chrome reject the flag for any scheme, so these
        # never worked. Stripping them and launching anyway is the bad fix — for a
        # pool the username is the session key, so the browser would leave by one
        # exit and the requests replaying its clearance by another.
        with pytest.raises(TierUnavailable, match="cannot send them"):
            chrome_proxy("socks5://session-key:token@127.0.0.1:9250")
        with pytest.raises(TierUnavailable):
            chrome_proxy("http://user:pw@proxy.test:8080")

    def test_a_refused_proxy_stops_the_launch(self, monkeypatch: pytest.MonkeyPatch):
        # It has to fail before the browser starts, not after: a browser on the wrong
        # address earns a clearance that is dead on arrival.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        with pytest.raises(TierUnavailable):
            CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
                "https://site.test/", proxy="socks5h://key:tok@127.0.0.1:9250"
            )
        # Not one browser, and that includes the headless User-Agent probe — which
        # runs before the flags are built unless the proxy is settled first.
        assert "processes" not in captured

    def test_the_automation_flag_is_always_off(self):
        # Without this Blink sets `navigator.webdriver` to true — one boolean saying
        # "automated" that every detector reads. Measured over three challenged hosts:
        # nothing cleared in 60s each without it, everything cleared with it.
        assert "--disable-blink-features=AutomationControlled" in launch_flags()

    def test_a_proxy_and_user_agent_become_flags(self):
        flags = launch_flags("socks5://127.0.0.1:9050", user_agent="UA/1", extra=["--mute-audio"])
        assert "--proxy-server=socks5://127.0.0.1:9050" in flags
        assert "--user-agent=UA/1" in flags
        assert flags[-1] == "--mute-audio", "extras go last so a caller can override"

    def test_an_empty_user_agent_adds_no_flag(self):
        # Imposing one on a headed browser pins it to a string that goes stale at the
        # next browser update.
        assert not any(f.startswith("--user-agent=") for f in launch_flags(user_agent=""))

    def test_the_headless_token_is_what_gets_removed(self):
        assert honest_user_agent(HEADLESS_UA) == HEADED_UA
        assert honest_user_agent(HEADED_UA) == HEADED_UA
        assert honest_user_agent("") == ""

    def test_the_clearance_deadline_is_the_soonest_of_the_cookies_that_matter(self):
        now = time.time()
        deadline = clearance_deadline(
            {"cf_clearance": now + 500.0, "__cf_bm": now + 200.0, "other": now + 10.0}
        )
        assert deadline == now + 200.0, "an unrelated cookie expiring sooner must not shorten it"

    def test_a_session_cookie_contributes_no_deadline(self):
        # Overstating the lifetime is the expensive direction: every request after the
        # real expiry carries a dead cookie and the challenge reads as solver failure.
        assert clearance_deadline({"cf_clearance": 0.0}) == 0.0
        assert clearance_deadline({}) == 0.0

    def test_a_cookie_that_has_already_expired_does_not_bound_a_fresh_clearance(self):
        # Found live. A profile directory persists between runs, so a solve reads back
        # the `__cf_bm` a visit half an hour ago left in it. Taking that as the deadline
        # made the clearance born expired, and the tier then re-solved on every request
        # until the attempt budget ran out and the chapter was dropped.
        now = time.time()
        deadline = clearance_deadline({"cf_clearance": now + 3600.0, "__cf_bm": now - 1800.0})
        assert deadline == now + 3600.0

    def test_a_jar_of_only_dead_cookies_falls_back_to_the_default_lifetime(self):
        now = time.time()
        assert clearance_deadline({"__cf_bm": now - 10.0}) == 0.0


class TestWsClient:
    def test_a_reply_is_matched_to_its_request(self, monkeypatch: pytest.MonkeyPatch):
        install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        client = WsClient("ws://127.0.0.1:9333/x")
        assert client.send("Target.attachToTarget") == {"sessionId": "S1"}

    def test_events_arriving_first_are_skipped(self, monkeypatch: pytest.MonkeyPatch):
        # Attaching to a target emits Target.attachedToTarget, so an event landing
        # before the reply is the normal case rather than an edge one.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        client = WsClient("ws://127.0.0.1:9333/x")
        sock: FakeSocket = captured["sockets"][0]
        sock.emit({"method": "Target.attachedToTarget", "params": {}})
        sock.emit({"method": "Inspector.targetCrashed", "params": {}})

        assert client.send("Browser.getVersion") == {"userAgent": HEADED_UA}

    def test_a_protocol_error_is_raised_with_what_the_browser_said(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def handler(_: Dict[str, Any]) -> Any:
            raise RuntimeError("No target with given id found")

        install_cdp(monkeypatch, handler)
        client = WsClient("ws://127.0.0.1:9333/x")
        with pytest.raises(ProtocolError, match="No target with given id found"):
            client.send("Page.navigate")

    def test_the_other_protocols_error_shape_is_understood_too(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # CDP puts an object under `error`; BiDi puts a code string there and the
        # detail in a sibling `message`. Reading one as the other raises an
        # AttributeError from inside the transport and buries the real failure.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        client = WsClient("ws://127.0.0.1:9333/x")
        sock: FakeSocket = captured["sockets"][0]

        original = sock.send

        def bidi_error(raw: str) -> None:
            original(raw)
            sock._inbox.clear()
            sock.emit(
                {
                    "type": "error",
                    "id": json.loads(raw)["id"],
                    "error": "invalid argument",
                    "message": "no such context",
                }
            )

        monkeypatch.setattr(sock, "send", bidi_error)
        with pytest.raises(ProtocolError, match="invalid argument: no such context"):
            client.send("browsingContext.navigate")

    def test_a_browser_that_never_answers_times_out_rather_than_hanging(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        install_cdp(monkeypatch, lambda _: None)
        client = WsClient("ws://127.0.0.1:9333/x")
        with pytest.raises(ProtocolError, match="did not answer"):
            client.send("Page.navigate", timeout=0.05)


class TestLaunch:
    def test_the_endpoint_comes_from_the_profile_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert captured["url"] == "ws://127.0.0.1:9333/devtools/browser/abc"
        assert f"--user-data-dir={tmp_path}" in captured["argv"]
        assert "--remote-debugging-port=0" in captured["argv"]

    def test_a_browser_that_never_publishes_a_port_fails_with_a_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr("scraper.cdp._PORT_WAIT", 0.1)
        install_cdp(monkeypatch, scripted([CLEARED_PAGE]), port_file=False)
        with pytest.raises(ProtocolError, match="debugging port"):
            CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )

    def test_a_stale_port_file_is_removed_before_launching(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Left from a previous run it names a port nothing is listening on any more,
        # and connecting to it fails in a way that says nothing about why.
        (tmp_path / "DevToolsActivePort").write_text("1\n/devtools/browser/stale")
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert captured["url"].endswith("/devtools/browser/abc")

    def test_a_temporary_profile_is_removed_and_a_given_one_is_kept(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Accumulated history is the reason to have a profile, so one the caller named
        # must survive; one minted only to read the port file must not be left behind.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        solver = CdpSolver(executable="/usr/bin/chrome", settle=0.0)

        solver.solve("https://site.test/", profile_dir=tmp_path)
        assert tmp_path.exists()
        assert f"--user-data-dir={tmp_path}" in captured["argv"]

        solver.solve("https://site.test/")
        minted = next(
            a.split("=", 1)[1] for a in captured["argv"] if a.startswith("--user-data-dir=")
        )
        assert minted != str(tmp_path), "the second solve should have made its own"
        assert not Path(minted).exists()

    def test_headless_launches_headless(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE], user_agent=HEADLESS_UA))
        CdpSolver(executable="/usr/bin/chrome", headless=True, settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "--headless=new" in captured["argv"]

    def test_headed_does_not(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", mode="headed", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert not any("headless" in a for a in captured["argv"])

    def test_auto_starts_hidden_and_only_shows_a_window_if_that_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The point of `auto`: a corrected headless browser clears what a headed one
        # does, so the window is spent only on the solves that actually needed a person.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", mode="auto", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "--headless=new" in captured["argv"]

    def test_the_browser_is_stopped_even_when_the_solve_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def handler(message: Dict[str, Any]) -> Any:
            if message["method"] == "Page.navigate":
                raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")
            return scripted([CLEARED_PAGE])(message)

        captured = install_cdp(monkeypatch, handler)
        with pytest.raises(ProtocolError):
            CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )
        assert captured["processes"][0].returncode == 0, "a leaked browser holds the profile"


class TestNoDomainIsEverEnabled:
    def test_solving_enables_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # The reason to own this rather than wrap a driver. Eagerly-enabled domains are
        # a known tell; Runtime.evaluate and Page.navigate are commands, not
        # subscriptions, so neither domain is ever turned on.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        methods = captured["sockets"][0].methods()
        assert not any(m.endswith(".enable") for m in methods), methods

    def test_rendering_enables_nothing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", settle=0.0).render(
            "https://site.test/", wait_for="h1", profile_dir=tmp_path
        )
        methods = captured["sockets"][0].methods()
        assert not any(m.endswith(".enable") for m in methods), methods


class TestSolve:
    def test_a_solve_returns_the_cookies_and_the_user_agent_they_belong_to(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        result = CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.cleared
        assert result.user_agent == HEADED_UA

    def test_the_loop_waits_for_the_interstitial_to_go(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_cdp(monkeypatch, scripted([CHALLENGE, CHALLENGE, CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", headless=False, settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        reads = [
            m
            for m in captured["sockets"][0].sent
            if m["method"] == "Runtime.evaluate" and "outerHTML" in m["params"]["expression"]
        ]
        assert len(reads) == 3

    def test_a_solve_with_no_user_agent_is_an_error_not_a_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The clearance is bound to the User-Agent, so cookies without one cannot be
        # replayed. Returning them would fail later, at the fetch, looking like a block.
        install_cdp(monkeypatch, scripted([CLEARED_PAGE], user_agent=""))
        with pytest.raises(Exception, match="User-Agent"):
            CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )

    def test_the_soonest_clearance_expiry_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        now = time.time()
        cookies = [
            {"name": "cf_clearance", "value": "x", "expires": now + 900.0},
            {"name": "__cf_bm", "value": "y", "expires": now + 300.0},
            {"name": "session", "value": "z", "expires": now + 10.0},
        ]
        install_cdp(monkeypatch, scripted([CLEARED_PAGE], cookies=cookies))
        result = CdpSolver(executable="/usr/bin/chrome", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.expires_at == now + 300.0


class TestRender:
    def test_a_rendered_page_comes_back_once_the_selector_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        html = CdpSolver(executable="/usr/bin/chrome", settle=0.0).render(
            "https://site.test/", wait_for="h1", profile_dir=tmp_path
        )
        assert "Chapter 12" in html

    def test_a_selector_that_never_appears_raises_rather_than_returning_the_shell(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Handing back the shell is the silent failure the whole call exists to
        # prevent: the caller parses it, finds nothing, and reports an empty page.
        monkeypatch.setattr("scraper.cdp._RENDER_POLL", 0.0)
        install_cdp(monkeypatch, scripted([SHELL], selector=False))
        with pytest.raises(Exception, match="never appeared"):
            CdpSolver(executable="/usr/bin/chrome", settle=0.0).render(
                "https://site.test/", wait_for="#chapter", profile_dir=tmp_path, timeout=0.05
            )

    def test_with_no_selector_the_settle_interval_is_all_there_is(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        install_cdp(monkeypatch, scripted([SHELL], selector=False))
        html = CdpSolver(executable="/usr/bin/chrome", settle=0.0).render(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "app" in html


class TestTheHonestUserAgent:
    def test_headless_launches_under_a_corrected_user_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE], user_agent=HEADLESS_UA))
        CdpSolver(executable="/usr/bin/chrome", headless=True, settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert f"--user-agent={HEADED_UA}" in captured["argv"]
        assert "Headless" not in " ".join(captured["argv"])

    def test_it_is_read_once_per_process(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # One throwaway launch to learn the string, then never again — it is a property
        # of the build, and paying a browser start per solve to re-read it is waste.
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE], user_agent=HEADLESS_UA))
        solver = CdpSolver(executable="/usr/bin/chrome", headless=True, settle=0.0)

        solver.solve("https://site.test/", profile_dir=tmp_path)
        solver.solve("https://site.test/", profile_dir=tmp_path)

        assert len(captured["processes"]) == 3, "one probe launch, then one browser per solve"

    def test_a_headed_solve_reads_nothing_and_sets_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        CdpSolver(executable="/usr/bin/chrome", headless=False, settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert len(captured["processes"]) == 1
        assert not any(a.startswith("--user-agent=") for a in captured["argv"])


class TestWhatTheSolverDeclares:
    def test_only_a_headed_solver_says_a_person_can_reach_it(self):
        # Headless is the default, so the interactive budget is opt-in: it is bought
        # by asking for a window, which is the only thing a person can reach into.
        assert not CdpSolver(executable="/x").interactive
        assert CdpSolver(executable="/x", headless=False).interactive

    def test_the_clearance_binds_to_chrome(self):
        # Both bundled solvers drive Chrome, so replaying their cookies over anything
        # else presents a contradiction the binding exists to catch.
        assert CdpSolver(executable="/x").impersonation == "chrome"
        assert BrowserSolver.impersonation == "chrome"

    def test_a_solver_that_cannot_render_says_so_rather_than_returning_an_empty_page(self):
        with pytest.raises(TierUnavailable):
            BrowserSolver().render("https://site.test/")


class TestTheDependency:
    def test_a_missing_websockets_names_the_extra(self, monkeypatch: pytest.MonkeyPatch):
        # Unmarked in pyproject, unlike the browser extra, because this is the driver
        # that has to work on every Python this package supports.
        import scraper.wire as wire

        def no_websockets() -> Any:
            raise MissingDependency("cdp", "driving a browser over CDP")

        monkeypatch.setattr(wire, "connect", no_websockets)
        with pytest.raises(MissingDependency, match=r"lncrawl-scraper\[cdp\]"):
            WsClient("ws://127.0.0.1:9333/x")

    def test_no_executable_anywhere_says_what_to_pass(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("scraper.cdp.pick_chromium", lambda: None)
        with pytest.raises(Exception, match="executable="):
            CdpSolver().solve("https://site.test/")


class TestSayingWhyTheWindowOpened:
    """A browser appearing with nothing said about it reads as the app misbehaving.

    The announcement belongs to whoever opens the window. The tier that grants the budget
    cannot know when that happens — an `auto` solver shows one only after the unattended
    attempt fails, and usually never does — so saying it from there was a guess.
    """

    def test_a_headed_solve_announces_the_window(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
    ):
        import logging

        install_cdp(monkeypatch, scripted([CLEARED_PAGE]))
        with caplog.at_level(logging.INFO):
            CdpSolver(executable="/usr/bin/chrome", mode="headed", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )
        assert any("browser window has opened" in r.getMessage() for r in caplog.records), (
            caplog.text
        )

    def test_a_hidden_solve_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
    ):
        import logging

        install_cdp(monkeypatch, scripted([CLEARED_PAGE], user_agent=HEADLESS_UA))
        with caplog.at_level(logging.INFO):
            CdpSolver(executable="/usr/bin/chrome", mode="headless", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )
        # Nothing was shown, so telling someone to go and click would be a lie.
        assert not any("browser window has opened" in r.getMessage() for r in caplog.records)
