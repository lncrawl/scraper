"""The Firefox backend: the BiDi vocabulary, the profile it writes, the loop.

No Firefox is launched here. Both edges are stubbed the way `test_cdp.py` stubs
Chrome's, which is the point of the shared transport — the same fake socket drives
either protocol, because the only thing that differs is the words.

What a stub cannot say is whether Firefox clears a real challenge. That is
`livetest/bidi-gate.json`, which answered it before this module was written: 29 of 46
hosts with `navigator.webdriver` hidden, none without.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from scraper.bidi import BidiSolver, FirefoxBackend, firefox_prefs
from scraper.exceptions import TierUnavailable
from scraper.wire import ProtocolError

CLEARED_PAGE = "<!doctype html><html><body><h1>Chapter 12</h1></body></html>"
CHALLENGE = (
    "<!doctype html><html><head><title>Just a moment...</title></head>"
    '<body><div id="challenge-running"></div></body></html>'
)
FIREFOX_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0"


class FakeSocket:
    def __init__(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._handler = handler
        self._inbox: deque = deque()
        self.sent: List[Dict[str, Any]] = []

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        try:
            result = self._handler(message)
        except Exception as exc:  # noqa: BLE001 - a raising handler scripts a BiDi error
            self._inbox.append(
                json.dumps(
                    {
                        "type": "error",
                        "id": message["id"],
                        "error": "invalid argument",
                        "message": str(exc),
                    }
                )
            )
            return
        self._inbox.append(json.dumps({"type": "success", "id": message["id"], "result": result}))

    def recv(self, timeout: Optional[float] = None) -> str:
        if not self._inbox:
            raise TimeoutError("nothing to read")
        return self._inbox.popleft()

    def close(self) -> None:
        pass

    def methods(self) -> List[str]:
        return [m["method"] for m in self.sent]


class FakeProcess:
    def __init__(self, argv: List[str], *, port_file: bool = True) -> None:
        self.argv = argv
        self.returncode: Optional[int] = None
        profile = argv[argv.index("--profile") + 1]
        target = Path(profile) / "WebDriverBiDiServer.json"
        if port_file and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ws_host": "127.0.0.1", "ws_port": 4444}))

    def poll(self) -> Optional[int]:
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


def install_bidi(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[Dict[str, Any]], Any],
    *,
    port_file: bool = True,
) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}

    def popen(argv: List[str], **_: Any) -> FakeProcess:
        proc = FakeProcess(argv, port_file=port_file)
        captured.setdefault("processes", []).append(proc)
        captured["argv"] = argv
        captured["profile"] = Path(argv[argv.index("--profile") + 1])
        return proc

    def connect() -> Callable[..., FakeSocket]:
        def factory(url: str, **_: Any) -> FakeSocket:
            sock = FakeSocket(handler)
            captured.setdefault("sockets", []).append(sock)
            captured["url"] = url
            return sock

        return factory

    monkeypatch.setattr("scraper.bidi.subprocess.Popen", popen)
    monkeypatch.setattr("scraper.wire.connect", connect)
    return captured


def scripted(
    pages: List[str],
    *,
    cookies: Optional[List[Dict[str, Any]]] = None,
    user_agent: str = FIREFOX_UA,
    selector: bool = True,
) -> Callable[[Dict[str, Any]], Any]:
    remaining = list(pages)

    def handle(message: Dict[str, Any]) -> Any:
        method = message["method"]
        if method == "session.new":
            return {"sessionId": "S1", "capabilities": {"userAgent": user_agent}}
        if method == "script.addPreloadScript":
            return {"script": "P1"}
        if method == "browsingContext.getTree":
            return {"contexts": [{"context": "C1"}]}
        if method == "browsingContext.navigate":
            return {"url": "https://site.test/"}
        if method == "storage.getCookies":
            return {
                "cookies": cookies
                if cookies is not None
                else [{"name": "cf_clearance", "value": {"type": "string", "value": "x"}}]
            }
        if method == "script.evaluate":
            expression = message["params"]["expression"]
            if "outerHTML" in expression:
                page = remaining.pop(0) if len(remaining) > 1 else remaining[0]
                return {"type": "success", "result": {"type": "string", "value": page}}
            if "querySelector" in expression:
                return {"type": "success", "result": {"type": "boolean", "value": selector}}
        if method == "session.end":
            return {}
        raise AssertionError(f"unscripted method {method}")

    return handle


class TestTheProfileItWrites:
    def test_webrtc_is_off(self):
        # Same reason as the Chrome backend: a STUN request reports the host's real
        # address even when every request goes through the proxy, and nothing fails.
        assert 'user_pref("media.peerconnection.enabled", false);' in firefox_prefs(None)

    def test_a_socks_proxy_becomes_preferences_not_a_flag(self):
        # The one place this backend cannot borrow Chrome's approach.
        prefs = firefox_prefs("socks5://127.0.0.1:9250")
        assert 'user_pref("network.proxy.type", 1);' in prefs
        assert 'user_pref("network.proxy.socks", "127.0.0.1");' in prefs
        assert 'user_pref("network.proxy.socks_port", 9250);' in prefs
        assert 'user_pref("network.proxy.socks_version", 5);' in prefs

    def test_names_resolve_at_the_proxy(self):
        # A name resolved locally leaks the lookup past the exit, which is most of
        # what having an exit was for.
        assert 'user_pref("network.proxy.socks_remote_dns", true);' in firefox_prefs(
            "socks5h://127.0.0.1:9250"
        )

    def test_an_http_proxy_covers_both_schemes(self):
        prefs = firefox_prefs("http://p.test:8080")
        assert 'user_pref("network.proxy.http", "p.test");' in prefs
        assert 'user_pref("network.proxy.ssl_port", 8080);' in prefs

    def test_no_proxy_writes_no_proxy_preferences(self):
        assert "network.proxy.type" not in firefox_prefs(None)

    def test_a_proxy_needing_credentials_is_refused(self):
        # Firefox would prompt, which no unattended solve can answer — so the browser
        # would leave by an address the requests replaying its clearance will not use.
        with pytest.raises(TierUnavailable):
            firefox_prefs("socks5://session-key:token@127.0.0.1:9250")


class TestDrivingFirefox:
    def test_a_solve_returns_the_cookies_and_the_user_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        result = BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.cleared
        assert result.user_agent == FIREFOX_UA

    def test_the_user_agent_comes_from_the_session_not_a_page(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # session.new reports it, so it costs no page and no navigation — and the
        # clearance is bound to this exact string.
        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        evaluated = [
            m["params"]["expression"]
            for m in captured["sockets"][0].sent
            if m["method"] == "script.evaluate"
        ]
        assert not any("navigator.userAgent" in e for e in evaluated)

    def test_a_cookie_value_is_unwrapped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # BiDi wraps it as {"type": "string", "value": …} where CDP hands back a bare
        # string. Stored unwrapped, the clearance replays as the literal word "dict".
        cookies = [{"name": "cf_clearance", "value": {"type": "string", "value": "abc123"}}]
        install_bidi(monkeypatch, scripted([CLEARED_PAGE], cookies=cookies))
        result = BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.cookies == {"cf_clearance": "abc123"}

    def test_the_soonest_clearance_expiry_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        cookies = [
            {"name": "cf_clearance", "value": {"type": "string", "value": "x"}, "expiry": 900.0},
            {"name": "__cf_bm", "value": {"type": "string", "value": "y"}, "expiry": 300.0},
        ]
        install_bidi(monkeypatch, scripted([CLEARED_PAGE], cookies=cookies))
        result = BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.expires_at == 300.0

    def test_the_loop_waits_for_the_interstitial_to_go(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_bidi(monkeypatch, scripted([CHALLENGE, CHALLENGE, CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        reads = [
            m
            for m in captured["sockets"][0].sent
            if m["method"] == "script.evaluate" and "outerHTML" in m["params"]["expression"]
        ]
        assert len(reads) == 3

    def test_webdriver_is_hidden_before_any_page_script_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # The whole reason this backend clears anything: with the property visible it
        # cleared none of six challenged hosts, and with it hidden, all six.
        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        sent = captured["sockets"][0]
        assert "script.addPreloadScript" in sent.methods()
        preload = next(m for m in sent.sent if m["method"] == "script.addPreloadScript")
        assert "webdriver" in preload["params"]["functionDeclaration"]
        # Before the navigation, or the first page runs without it.
        order = sent.methods()
        assert order.index("script.addPreloadScript") < order.index("browsingContext.navigate")

    def test_every_command_carries_a_params_object(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # BiDi rejects a message with no `params` — "Expected params to be an object,
        # got undefined" — where CDP does not care. The transport always sends one.
        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert all("params" in m for m in captured["sockets"][0].sent)

    def test_a_navigation_that_never_settles_is_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # A challenge page reloads itself partway and often never reaches "complete".
        # The poll loop decides whether the solve worked, not the navigation.
        def handler(message: Dict[str, Any]) -> Any:
            if message["method"] == "browsingContext.navigate":
                raise RuntimeError("navigation timed out")
            return scripted([CLEARED_PAGE])(message)

        install_bidi(monkeypatch, handler)
        result = BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert result.cleared

    def test_headless_launches_headless_and_headed_does_not(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "--headless" in captured["argv"]

        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", headless=False, settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "--headless" not in captured["argv"]

    def test_it_stays_out_of_a_running_firefox(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Without --no-remote a second launch hands its arguments to the first and
        # exits, so the solve would drive somebody's actual browser or nothing at all.
        captured = install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
            "https://site.test/", profile_dir=tmp_path
        )
        assert "--no-remote" in captured["argv"]

    def test_a_browser_that_never_publishes_a_port_fails_with_a_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr("scraper.bidi._PORT_WAIT", 0.1)
        install_bidi(monkeypatch, scripted([CLEARED_PAGE]), port_file=False)
        with pytest.raises(ProtocolError, match="BiDi port"):
            BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )

    def test_the_browser_is_stopped_even_when_the_solve_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        def handler(message: Dict[str, Any]) -> Any:
            if message["method"] == "session.new":
                raise RuntimeError("no session for you")
            return scripted([CLEARED_PAGE])(message)

        captured = install_bidi(monkeypatch, handler)
        with pytest.raises(ProtocolError):
            BidiSolver(executable="/usr/bin/firefox", settle=0.0).solve(
                "https://site.test/", profile_dir=tmp_path
            )
        assert captured["processes"][0].returncode == 0


class TestRendering:
    def test_a_rendered_page_comes_back_once_the_selector_exists(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        install_bidi(monkeypatch, scripted([CLEARED_PAGE]))
        html = BidiSolver(executable="/usr/bin/firefox", settle=0.0).render(
            "https://site.test/", wait_for="h1", profile_dir=tmp_path
        )
        assert "Chapter 12" in html

    def test_a_selector_that_never_appears_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setattr("scraper.bidi._RENDER_POLL", 0.0)
        install_bidi(monkeypatch, scripted([CLEARED_PAGE], selector=False))
        with pytest.raises(Exception, match="never appeared"):
            BidiSolver(executable="/usr/bin/firefox", settle=0.0).render(
                "https://site.test/", wait_for="#nope", profile_dir=tmp_path, timeout=0.05
            )


class TestWhatItDeclares:
    def test_the_clearance_binds_to_firefox(self):
        # The reason this backend exists: a Chrome-only solver forces chrome
        # impersonation everywhere, and firefox measured better on the corpus.
        assert BidiSolver(executable="/x").impersonation == "firefox"

    def test_only_a_headed_solver_says_a_person_can_reach_it(self):
        assert not BidiSolver(executable="/x").interactive
        assert BidiSolver(executable="/x", headless=False).interactive

    def test_no_executable_anywhere_says_what_to_pass(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("scraper.bidi.pick_firefox", lambda: None)
        with pytest.raises(Exception, match="executable="):
            BidiSolver().solve("https://site.test/")


def test_the_two_backends_share_one_transport():
    """The bet the three-layer split made, and the reason a second browser was cheap."""
    import scraper.bidi as bidi
    import scraper.cdp as cdp

    assert bidi.WsClient is cdp.WsClient
    assert issubclass(FirefoxBackend, object) and hasattr(FirefoxBackend, "cookies")
