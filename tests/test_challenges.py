"""Tests for the engine/challenges module.

Each handler is exercised in isolation: ``request`` and ``perform_request``
are simple callables that return scripted ``requests.Response`` objects.
``time.sleep`` is always patched to keep the suite fast.
"""

from __future__ import annotations

import pytest
import requests

from scraper.config import CloudflareConfig
from scraper.engine.challenges import (
    CloudflareV1Handler,
    CloudflareV2Handler,
    CloudflareV3Handler,
    TurnstileHandler,
    build_handlers,
)
from scraper.exceptions import (
    CloudflareCaptchaError,
    CloudflareChallengeError,
    CloudflareFirewallBlock,
    CloudflareSolveError,
    CloudflareTurnstileError,
)

BASE = "https://example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(
    status: int = 200,
    url: str = BASE,
    body: str = "",
    headers: dict | None = None,
) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers.update(headers or {})
    # Attach a minimal PreparedRequest so response.request.method works.
    req = requests.PreparedRequest()
    req.method = "GET"
    req.url = url
    r.request = req
    return r


def _cf_resp(status: int = 503, body: str = "", url: str = BASE) -> requests.Response:
    return _resp(status, url, body, {"Server": "cloudflare"})


def _noop_perform(method, url, **kwargs):
    return _resp(200, url)


# ---------------------------------------------------------------------------
# build_handlers
# ---------------------------------------------------------------------------


def test_build_handlers_all_enabled():
    cfg = CloudflareConfig()
    handlers = build_handlers(cfg)
    types = [type(h).__name__ for h in handlers]
    assert types == [
        "TurnstileHandler",
        "CloudflareV3Handler",
        "CloudflareV2Handler",
        "CloudflareV1Handler",
    ]


def test_build_handlers_all_disabled():
    cfg = CloudflareConfig(
        disable_turnstile=True, disable_v3=True, disable_v2=True, disable_v1=True
    )
    assert build_handlers(cfg) == []


def test_build_handlers_selective_disable():
    cfg = CloudflareConfig(disable_turnstile=True, disable_v2=True)
    types = [type(h).__name__ for h in build_handlers(cfg)]
    assert "TurnstileHandler" not in types
    assert "CloudflareV2Handler" not in types
    assert "CloudflareV3Handler" in types
    assert "CloudflareV1Handler" in types


def test_build_handlers_debug_flag_propagated():
    cfg = CloudflareConfig(debug=True)
    handlers = build_handlers(cfg)
    v1 = next(h for h in handlers if isinstance(h, CloudflareV1Handler))
    v2 = next(h for h in handlers if isinstance(h, CloudflareV2Handler))
    v3 = next(h for h in handlers if isinstance(h, CloudflareV3Handler))
    assert v1.debug is True
    assert v2.debug is True
    assert v3.debug is True


def test_build_handlers_double_down_flag():
    cfg = CloudflareConfig(double_down=False)
    v1 = next(h for h in build_handlers(cfg) if isinstance(h, CloudflareV1Handler))
    assert v1.double_down is False


# ---------------------------------------------------------------------------
# TurnstileHandler — detection
# ---------------------------------------------------------------------------

_TURNSTILE_BODIES = [
    # Explicit class embed
    '<div class="cf-turnstile" data-sitekey="abc"></div>',
    # Script src
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>',
    # Sitekey attribute only
    'data-sitekey="ABCdef123456789"',
    # Modern managed: _cf_chl_opt
    "window._cf_chl_opt = {}",
]


@pytest.mark.parametrize("body", _TURNSTILE_BODIES)
def test_turnstile_is_challenge_detects_patterns(body):
    h = TurnstileHandler()
    assert h.is_challenge(_cf_resp(503, body))


def test_turnstile_no_detect_non_cloudflare_server():
    h = TurnstileHandler()
    body = '<div class="cf-turnstile"></div>'
    assert not h.is_challenge(_resp(503, body=body))  # Server header absent


def test_turnstile_no_detect_wrong_status():
    h = TurnstileHandler()
    body = '<div class="cf-turnstile"></div>'
    assert not h.is_challenge(_cf_resp(200, body))


def test_turnstile_no_detect_clean_cf_page():
    h = TurnstileHandler()
    assert not h.is_challenge(_cf_resp(200, "<h1>Hello</h1>"))


def test_turnstile_handle_raises():
    h = TurnstileHandler()
    with pytest.raises(CloudflareTurnstileError):
        h.handle(_cf_resp(503), request=_noop_perform, perform_request=_noop_perform)


# ---------------------------------------------------------------------------
# CloudflareV1Handler — detection helpers
# ---------------------------------------------------------------------------

_IUAM_BODY = (
    '<img src="/cdn-cgi/images/trace/jsch/transparent.gif">'
    '<form id="challenge-form" action="/?__cf_chl_f_tk=abc">'
    "</form>"
)

_CAPTCHA_BODY = (
    '<img src="/cdn-cgi/images/trace/captcha/transparent.gif">'
    '<form id="challenge-form" action="/?__cf_chl_f_tk=abc">'
    "</form>"
)

_FIREWALL_BODY = '<span class="cf-error-code">1020</span>'

_V2_IUAM_BODY = _IUAM_BODY + "\ncpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1';"

_V2_CAPTCHA_BODY = (
    _CAPTCHA_BODY + "\ncpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/captcha/v1';"
)


def test_v1_detects_iuam_challenge():
    h = CloudflareV1Handler()
    assert h.is_challenge(_cf_resp(503, _IUAM_BODY))


def test_v1_no_detect_non_cf_server():
    h = CloudflareV1Handler()
    assert not h.is_challenge(_resp(503, body=_IUAM_BODY))


def test_v1_no_detect_clean_response():
    h = CloudflareV1Handler()
    assert not h.is_challenge(_cf_resp(503, "<h1>hi</h1>"))


def test_v1_firewall_block_raises():
    h = CloudflareV1Handler()
    with pytest.raises(CloudflareFirewallBlock):
        h.is_challenge(_cf_resp(403, _FIREWALL_BODY))


def test_v1_captcha_challenge_raises():
    h = CloudflareV1Handler()
    with pytest.raises(CloudflareCaptchaError):
        h.is_challenge(_cf_resp(403, _CAPTCHA_BODY))


def test_v1_v2_iuam_raises_challenge_error():
    h = CloudflareV1Handler()
    with pytest.raises(CloudflareChallengeError):
        h.is_challenge(_cf_resp(503, _V2_IUAM_BODY))


def test_v1_v2_captcha_raises_challenge_error():
    h = CloudflareV1Handler()
    with pytest.raises(CloudflareChallengeError):
        h.is_challenge(_cf_resp(403, _V2_CAPTCHA_BODY))


def test_v1_static_iuam_positive():
    assert CloudflareV1Handler._is_iuam_challenge(_cf_resp(503, _IUAM_BODY))


def test_v1_static_iuam_wrong_status():
    assert not CloudflareV1Handler._is_iuam_challenge(_cf_resp(200, _IUAM_BODY))


def test_v1_static_captcha_positive():
    assert CloudflareV1Handler._is_captcha_challenge(_cf_resp(403, _CAPTCHA_BODY))


def test_v1_static_captcha_wrong_status():
    assert not CloudflareV1Handler._is_captcha_challenge(_cf_resp(503, _CAPTCHA_BODY))


def test_v1_static_firewall_positive():
    assert CloudflareV1Handler._is_firewall_blocked(_cf_resp(403, _FIREWALL_BODY))


def test_v1_static_firewall_wrong_code():
    assert not CloudflareV1Handler._is_firewall_blocked(_cf_resp(403, "<span>1021</span>"))


def test_v1_static_new_iuam_positive():
    assert CloudflareV1Handler._is_new_iuam_challenge(_cf_resp(503, _V2_IUAM_BODY))


def test_v1_static_new_captcha_positive():
    assert CloudflareV1Handler._is_new_captcha_challenge(_cf_resp(403, _V2_CAPTCHA_BODY))


# ---------------------------------------------------------------------------
# CloudflareV1Handler — _build_iuam_payload / _submit
# ---------------------------------------------------------------------------


def test_v1_build_iuam_payload_missing_form_raises():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter

    h = CloudflareV1Handler()
    interp = JavaScriptInterpreter()
    with pytest.raises(CloudflareChallengeError, match="could not extract form parameters"):
        h._build_iuam_payload("<html>no form here</html>", BASE, interp)


def test_v1_submit_400_raises():
    def request(method, url, **kwargs):
        return _resp(400, url)

    submit = {"url": f"{BASE}/challenge", "data": {"jschl_answer": "1"}}
    with pytest.raises(CloudflareSolveError, match="rejected"):
        CloudflareV1Handler._submit(submit, _cf_resp(503, url=BASE), request)


def test_v1_submit_non_redirect_returns_directly():
    final = _resp(200, BASE, "OK")

    def request(method, url, **kwargs):
        return final

    submit = {"url": f"{BASE}/challenge", "data": {"jschl_answer": "1"}}
    out = CloudflareV1Handler._submit(submit, _cf_resp(503, url=BASE), request)
    assert out is final


def test_v1_submit_redirect_is_followed():
    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/challenge"
    redir.headers["Location"] = f"{BASE}/done"
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/challenge"):
            return redir
        return _resp(200, url)

    submit = {"url": f"{BASE}/challenge", "data": {"jschl_answer": "1"}}
    out = CloudflareV1Handler._submit(submit, _cf_resp(503, url=BASE), request)
    assert out.status_code == 200
    assert calls[-1][1] == f"{BASE}/done"


def test_v1_submit_relative_redirect_resolved():
    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/challenge"
    redir.headers["Location"] = "/done"  # relative
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/challenge"):
            return redir
        return _resp(200, url)

    submit = {"url": f"{BASE}/challenge", "data": {"jschl_answer": "1"}}
    CloudflareV1Handler._submit(submit, _cf_resp(503, url=BASE), request)
    assert calls[-1][1] == f"{BASE}/done"


def test_v1_handle_double_down_bypass(monkeypatch):
    """double_down: if second attempt is clean, return it without solving."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v1.time.sleep", lambda s: None)
    h = CloudflareV1Handler(double_down=True)

    clean = _resp(200, BASE, "Welcome")

    def perform(method, url, **kwargs):
        return clean

    out = h.handle(_cf_resp(503, url=BASE), request=_noop_perform, perform_request=perform)
    assert out is clean


def test_v1_handle_no_double_down_raises_on_bad_form(monkeypatch):
    """Without double_down the solver runs immediately; bad HTML → error."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v1.time.sleep", lambda s: None)
    h = CloudflareV1Handler(double_down=False)

    with pytest.raises((CloudflareChallengeError, Exception)):
        h.handle(
            _cf_resp(503, "<html>no form</html>", url=BASE),
            request=_noop_perform,
            perform_request=_noop_perform,
        )


# ---------------------------------------------------------------------------
# CloudflareV2Handler — detection
# ---------------------------------------------------------------------------

_V2_BODY = "some page\ncpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1';\nmore text"

_V2_CAPTCHA_BODY2 = "cpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/captcha/v1';\n"


def test_v2_is_challenge_detects_jsch_v1():
    h = CloudflareV2Handler()
    assert h.is_challenge(_cf_resp(503, _V2_BODY))


def test_v2_is_challenge_non_cf_server():
    h = CloudflareV2Handler()
    assert not h.is_challenge(_resp(503, body=_V2_BODY))


def test_v2_is_challenge_wrong_status():
    h = CloudflareV2Handler()
    assert not h.is_challenge(_cf_resp(200, _V2_BODY))


def test_v2_is_captcha_challenge():
    assert CloudflareV2Handler.is_captcha_challenge(_cf_resp(403, _V2_CAPTCHA_BODY2))


def test_v2_is_captcha_wrong_status():
    assert not CloudflareV2Handler.is_captcha_challenge(_cf_resp(503, _V2_CAPTCHA_BODY2))


# ---------------------------------------------------------------------------
# CloudflareV2Handler — _extract_challenge_data / _build_payload
# ---------------------------------------------------------------------------

_V2_FULL_BODY = (
    'window._cf_chl_opt={"cvId":"abc","chlPageData":"xyz"};\n'
    '<form method="POST" id="challenge-form" action="/challenge?__cf_chl_f_tk=tok">\n'
    '<input type="hidden" name="r" value="RTOKEN">\n'
    "</form>\n"
)


def test_v2_extract_success():
    resp = _cf_resp(503, _V2_FULL_BODY)
    data = CloudflareV2Handler._extract_challenge_data(resp)
    assert data["challenge_data"]["cvId"] == "abc"
    assert "__cf_chl_f_tk" in data["form_action"]


def test_v2_extract_missing_chl_opt_raises():
    resp = _cf_resp(503, '<form id="challenge-form" action="/x"></form>')
    with pytest.raises(CloudflareChallengeError, match="_cf_chl_opt"):
        CloudflareV2Handler._extract_challenge_data(resp)


def test_v2_extract_malformed_json_raises():
    resp = _cf_resp(503, "window._cf_chl_opt={bad json};\n<form id='x' action='/y'></form>")
    with pytest.raises(CloudflareChallengeError, match="malformed"):
        CloudflareV2Handler._extract_challenge_data(resp)


def test_v2_extract_missing_form_raises():
    resp = _cf_resp(503, 'window._cf_chl_opt={"k":"v"};')
    with pytest.raises(CloudflareChallengeError, match="form"):
        CloudflareV2Handler._extract_challenge_data(resp)


def test_v2_build_payload_missing_r_raises():
    resp = _cf_resp(503, "no r token here")
    with pytest.raises(CloudflareCaptchaError, match="r"):
        CloudflareV2Handler._build_payload({}, resp)


def test_v2_build_payload_includes_optional_fields():
    resp = _cf_resp(503, '<input name="r" value="RTOKEN">\n')
    challenge_data = {"cvId": "CID", "chlPageData": "CPD"}
    payload = CloudflareV2Handler._build_payload(challenge_data, resp)
    assert payload["r"] == "RTOKEN"
    assert payload["cv_chal_id"] == "CID"
    assert payload["cf_chl_page_data"] == "CPD"


def test_v2_build_payload_without_optional_fields():
    resp = _cf_resp(503, '<input name="r" value="RTOKEN">\n')
    payload = CloudflareV2Handler._build_payload({}, resp)
    assert "cv_chal_id" not in payload
    assert "cf_chl_page_data" not in payload


# ---------------------------------------------------------------------------
# CloudflareV2Handler — handle()
# ---------------------------------------------------------------------------


def test_v2_handle_403_raises(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler()
    monkeypatch.setattr(
        CloudflareV2Handler,
        "_extract_challenge_data",
        staticmethod(lambda r: {"challenge_data": {}, "form_action": "/challenge"}),
    )
    monkeypatch.setattr(CloudflareV2Handler, "_build_payload", staticmethod(lambda d, r: {}))

    def request(method, url, **kwargs):
        return _resp(403, url)

    with pytest.raises(CloudflareSolveError, match="rejected"):
        h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)


def test_v2_handle_non_redirect_returns(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler()
    monkeypatch.setattr(
        CloudflareV2Handler,
        "_extract_challenge_data",
        staticmethod(lambda r: {"challenge_data": {}, "form_action": "/challenge"}),
    )
    monkeypatch.setattr(CloudflareV2Handler, "_build_payload", staticmethod(lambda d, r: {}))

    final = _resp(200, BASE, "done")

    def request(method, url, **kwargs):
        return final

    out = h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert out is final


def test_v2_handle_redirect_followed(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler()
    monkeypatch.setattr(
        CloudflareV2Handler,
        "_extract_challenge_data",
        staticmethod(lambda r: {"challenge_data": {}, "form_action": "/challenge"}),
    )
    monkeypatch.setattr(CloudflareV2Handler, "_build_payload", staticmethod(lambda d, r: {}))

    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/challenge"
    redir.headers["Location"] = f"{BASE}/final"
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/challenge"):
            return redir
        return _resp(200, url)

    h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert calls[-1] == f"{BASE}/final"


# ---------------------------------------------------------------------------
# CloudflareV3Handler — detection
# ---------------------------------------------------------------------------

_V3_JSCH_BODY = "cpo.src = '/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v3';"
_V3_CTX_BODY = "window._cf_chl_ctx = {};"
_V3_RTTK_BODY = '<form id="challenge-form" action="/x?__cf_chl_rt_tk=TOKEN">stuff</form>'


@pytest.mark.parametrize(
    "body",
    [_V3_JSCH_BODY, _V3_CTX_BODY, _V3_RTTK_BODY],
)
def test_v3_is_challenge_patterns(body):
    h = CloudflareV3Handler()
    assert h.is_challenge(_cf_resp(503, body))


def test_v3_no_detect_non_cf_server():
    h = CloudflareV3Handler()
    assert not h.is_challenge(_resp(503, body=_V3_CTX_BODY))


def test_v3_no_detect_wrong_status():
    h = CloudflareV3Handler()
    assert not h.is_challenge(_cf_resp(200, _V3_CTX_BODY))


# ---------------------------------------------------------------------------
# CloudflareV3Handler — _extract_data
# ---------------------------------------------------------------------------

_V3_FULL_BODY = (
    'window._cf_chl_ctx = {"cvId": "ctx_val"};\n'
    'window._cf_chl_opt = {"chlPageData": "opt_val"};\n'
    '<form id="challenge-form" action="/v3-challenge?__cf_chl_rt_tk=TOK">\n'
    '<input name="r" value="RTOKEN">\n'
    "</form>\n"
    "<script>some code window._cf_chl_enter() more</script>\n"
)


def test_v3_extract_data_success():
    resp = _cf_resp(503, _V3_FULL_BODY)
    data = CloudflareV3Handler._extract_data(resp)
    assert data["ctx_data"]["cvId"] == "ctx_val"
    assert data["opt_data"]["chlPageData"] == "opt_val"
    assert "__cf_chl_rt_tk" in data["form_action"]
    assert data["vm_script"] is not None


def test_v3_extract_data_missing_form_raises():
    resp = _cf_resp(503, "window._cf_chl_ctx = {};")
    with pytest.raises(CloudflareChallengeError, match="form"):
        CloudflareV3Handler._extract_data(resp)


def test_v3_extract_data_no_vm_script_is_none():
    body = (
        "window._cf_chl_ctx = {};\n"
        '<form id="challenge-form" action="/x">\n</form>\n'
        "no script here\n"
    )
    resp = _cf_resp(503, body)
    data = CloudflareV3Handler._extract_data(resp)
    assert data["vm_script"] is None


# ---------------------------------------------------------------------------
# CloudflareV3Handler — _execute_vm / _fallback_answer
# ---------------------------------------------------------------------------


def test_v3_execute_vm_no_script_uses_fallback():
    data = {"vm_script": None, "opt_data": {"chlPageData": "page"}, "ctx_data": {}}
    interp_calls = []

    class _FakeInterp:
        def eval(self, js):
            interp_calls.append(js)
            return "42"

    result = CloudflareV3Handler._execute_vm(data, "example.com", _FakeInterp())  # type: ignore[arg-type]
    assert result == str(hash("page") % 1_000_000)
    assert not interp_calls  # interpreter not called when vm_script is None


def test_v3_execute_vm_eval_called_with_script():
    data = {"vm_script": "var x=1;", "opt_data": {}, "ctx_data": {}}

    class _FakeInterp:
        def eval(self, js):
            return "answer123"

    result = CloudflareV3Handler._execute_vm(data, "example.com", _FakeInterp())  # type: ignore[arg-type]
    assert result == "answer123"


def test_v3_execute_vm_exception_falls_back_to_fallback():
    data = {"vm_script": "throw new Error('oops');", "opt_data": {}, "ctx_data": {"cvId": "CID"}}

    class _BrokenInterp:
        def eval(self, js):
            raise RuntimeError("broken")

    result = CloudflareV3Handler._execute_vm(data, "example.com", _BrokenInterp())  # type: ignore[arg-type]
    # Should use _fallback_answer → cvId branch
    assert result == str(hash("CID") % 1_000_000)


def test_v3_fallback_chl_page_data():
    data = {"opt_data": {"chlPageData": "pd"}, "ctx_data": {}}
    assert CloudflareV3Handler._fallback_answer(data) == str(hash("pd") % 1_000_000)


def test_v3_fallback_cv_id():
    data = {"opt_data": {}, "ctx_data": {"cvId": "cid"}}
    assert CloudflareV3Handler._fallback_answer(data) == str(hash("cid") % 1_000_000)


def test_v3_fallback_random_is_in_range():
    data: dict = {"opt_data": {}, "ctx_data": {}}
    result = int(CloudflareV3Handler._fallback_answer(data))
    assert 100_000 <= result <= 999_999


# ---------------------------------------------------------------------------
# CloudflareV3Handler — _build_payload
# ---------------------------------------------------------------------------


def test_v3_build_payload_missing_r_raises():
    resp = _cf_resp(503, "no r token")
    with pytest.raises(CloudflareChallengeError, match="'r' token"):
        CloudflareV3Handler._build_payload({}, resp, "42")


def test_v3_build_payload_includes_extra_inputs():
    body = (
        '<input name="r" value="RTOKEN">\n'
        '<input name="extra" value="X">\n'
        '<input name="jschl_answer" value="OLD">\n'  # already in payload → skip
    )
    resp = _cf_resp(503, body)
    payload = CloudflareV3Handler._build_payload({}, resp, "ANSWER")
    assert payload["r"] == "RTOKEN"
    assert payload["jschl_answer"] == "ANSWER"
    assert payload["extra"] == "X"


# ---------------------------------------------------------------------------
# CloudflareV3Handler — handle()
# ---------------------------------------------------------------------------


def test_v3_handle_403_raises(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler()
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": "/v3", "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))

    def request(method, url, **kwargs):
        return _resp(403, url)

    with pytest.raises(CloudflareSolveError, match="rejected"):
        h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)


def test_v3_handle_non_redirect_returns(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler()
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": "/v3", "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))

    final = _resp(200, BASE, "done")

    def request(method, url, **kwargs):
        return final

    out = h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert out is final


def test_v3_handle_redirect_followed(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler()
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": "/v3", "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))

    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/v3"
    redir.headers["Location"] = f"{BASE}/v3-done"
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/v3"):
            return redir
        return _resp(200, url)

    h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert calls[-1] == f"{BASE}/v3-done"


def test_v3_handle_relative_redirect_resolved(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler()
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": "/v3", "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))

    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/v3"
    redir.headers["Location"] = "/relative-done"
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/v3"):
            return redir
        return _resp(200, url)

    h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert calls[-1] == f"{BASE}/relative-done"


# ---------------------------------------------------------------------------
# JavaScriptInterpreter
# ---------------------------------------------------------------------------


def test_interpreter_eval_simple():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter

    interp = JavaScriptInterpreter()
    assert interp.eval("1 + 1") == "2"


def test_interpreter_eval_string_result():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter

    interp = JavaScriptInterpreter()
    assert interp.eval('"hello"') == "hello"


def test_interpreter_eval_float_formatted():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter

    interp = JavaScriptInterpreter()
    result = float(interp.eval("Math.PI"))
    assert abs(result - 3.14159) < 0.001


def test_interpreter_solve_challenge_invalid_body_raises():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter
    from scraper.exceptions import CloudflareSolveError

    interp = JavaScriptInterpreter()
    with pytest.raises(CloudflareSolveError):
        interp.solve_challenge("<html>no challenge here</html>", "example.com")


def test_iuam_template_missing_settimeout_raises():
    from scraper.engine.challenges.interpreter import JavaScriptInterpreter

    with pytest.raises(ValueError, match="Unable to identify"):
        JavaScriptInterpreter._iuam_template("<html>no challenge</html>", "example.com")


# ---------------------------------------------------------------------------
# AttributeError guards — all static helpers return False gracefully
# ---------------------------------------------------------------------------


def test_v1_is_challenge_attribute_error_returns_false():
    assert not CloudflareV1Handler().is_challenge(object())  # type: ignore[arg-type]


def test_v1_iuam_challenge_attribute_error_returns_false():
    assert not CloudflareV1Handler._is_iuam_challenge(object())  # type: ignore[arg-type]


def test_v1_captcha_challenge_attribute_error_returns_false():
    assert not CloudflareV1Handler._is_captcha_challenge(object())  # type: ignore[arg-type]


def test_v1_firewall_attribute_error_returns_false():
    assert not CloudflareV1Handler._is_firewall_blocked(object())  # type: ignore[arg-type]


def test_v1_new_iuam_attribute_error_returns_false():
    assert not CloudflareV1Handler._is_new_iuam_challenge(object())  # type: ignore[arg-type]


def test_v1_new_captcha_attribute_error_returns_false():
    assert not CloudflareV1Handler._is_new_captcha_challenge(object())  # type: ignore[arg-type]


def test_v2_is_challenge_attribute_error_returns_false():
    assert not CloudflareV2Handler().is_challenge(object())  # type: ignore[arg-type]


def test_v2_is_captcha_challenge_attribute_error_returns_false():
    assert not CloudflareV2Handler.is_captcha_challenge(object())  # type: ignore[arg-type]


def test_v3_is_challenge_attribute_error_returns_false():
    assert not CloudflareV3Handler().is_challenge(object())  # type: ignore[arg-type]


def test_turnstile_is_challenge_attribute_error_returns_false():
    assert not TurnstileHandler().is_challenge(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# debug=True path — logger.debug line in handle()
# ---------------------------------------------------------------------------


def test_v1_handle_debug_logs_and_double_down_bypasses(monkeypatch):
    """debug=True covers the logger.debug line; clean second response exits early."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v1.time.sleep", lambda s: None)
    h = CloudflareV1Handler(debug=True, double_down=True)
    clean = _resp(200, BASE, "Welcome")
    out = h.handle(
        _cf_resp(503, url=BASE),
        request=_noop_perform,
        perform_request=lambda m, u, **kw: clean,
    )
    assert out is clean


def test_v2_handle_debug_logs(monkeypatch):
    """debug=True covers the logger.debug line in V2.handle()."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler(debug=True)
    monkeypatch.setattr(
        CloudflareV2Handler,
        "_extract_challenge_data",
        staticmethod(lambda r: {"challenge_data": {}, "form_action": "/challenge"}),
    )
    monkeypatch.setattr(CloudflareV2Handler, "_build_payload", staticmethod(lambda d, r: {}))
    out = h.handle(
        _cf_resp(503, url=BASE),
        request=lambda m, u, **kw: _resp(200, u),
        perform_request=_noop_perform,
    )
    assert out.status_code == 200


def test_v3_handle_debug_logs(monkeypatch):
    """debug=True covers the logger.debug line in V3.handle()."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler(debug=True)
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": "/v3", "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))
    out = h.handle(
        _cf_resp(503, url=BASE),
        request=lambda m, u, **kw: _resp(200, u),
        perform_request=_noop_perform,
    )
    assert out.status_code == 200


# ---------------------------------------------------------------------------
# V1: double_down falls through when second response is still a challenge
# ---------------------------------------------------------------------------


def test_v1_handle_double_down_still_challenge_runs_solver(monkeypatch):
    """second response IS still IUAM → falls through to _build_iuam_payload."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v1.time.sleep", lambda s: None)
    h = CloudflareV1Handler(double_down=True)

    # perform_request returns another IUAM page → is_iuam_challenge is True → don't return early
    def perform(method, url, **kwargs):
        return _cf_resp(503, _IUAM_BODY, url=url)

    solver_called = []
    monkeypatch.setattr(
        CloudflareV1Handler,
        "_build_iuam_payload",
        lambda self, body, url, interp: (
            solver_called.append(1) or {"url": f"{BASE}/sub", "data": {}}
        ),
    )
    monkeypatch.setattr(
        CloudflareV1Handler,
        "_submit",
        staticmethod(lambda submit, orig, req, **kw: _resp(200, BASE)),
    )

    out = h.handle(
        _cf_resp(503, _IUAM_BODY, url=BASE), request=_noop_perform, perform_request=perform
    )
    assert solver_called, "_build_iuam_payload should have been called"
    assert out.status_code == 200


# ---------------------------------------------------------------------------
# V2: re-raise of CloudflareChallengeError from _extract_challenge_data
# ---------------------------------------------------------------------------


def test_v2_handle_extract_challenge_error_propagates(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler()

    def _raise(r):
        raise CloudflareChallengeError("extraction failed")

    monkeypatch.setattr(CloudflareV2Handler, "_extract_challenge_data", staticmethod(_raise))
    with pytest.raises(CloudflareChallengeError, match="extraction failed"):
        h.handle(_cf_resp(503, url=BASE), request=_noop_perform, perform_request=_noop_perform)


# ---------------------------------------------------------------------------
# V2: relative redirect in handle() uses urljoin
# ---------------------------------------------------------------------------


def test_v2_handle_relative_redirect_resolved(monkeypatch):
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v2.time.sleep", lambda s: None)
    h = CloudflareV2Handler()
    monkeypatch.setattr(
        CloudflareV2Handler,
        "_extract_challenge_data",
        staticmethod(lambda r: {"challenge_data": {}, "form_action": "/challenge"}),
    )
    monkeypatch.setattr(CloudflareV2Handler, "_build_payload", staticmethod(lambda d, r: {}))

    redir = requests.Response()
    redir.status_code = 302
    redir.url = f"{BASE}/challenge"
    redir.headers["Location"] = "/v2-done"  # relative URL
    redir._content = b""

    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith("/challenge"):
            return redir
        return _resp(200, url)

    h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert calls[-1] == f"{BASE}/v2-done"


# ---------------------------------------------------------------------------
# V3: absolute form_action skips scheme prepend (branch 79→82)
# ---------------------------------------------------------------------------


def test_v3_handle_absolute_form_action(monkeypatch):
    """form_action already starts with https → no scheme prepend."""
    monkeypatch.setattr("scraper.engine.challenges.cloudflare_v3.time.sleep", lambda s: None)
    h = CloudflareV3Handler()
    abs_url = f"{BASE}/v3-abs"
    monkeypatch.setattr(
        CloudflareV3Handler,
        "_extract_data",
        staticmethod(
            lambda r: {"ctx_data": {}, "opt_data": {}, "form_action": abs_url, "vm_script": None}
        ),
    )
    monkeypatch.setattr(CloudflareV3Handler, "_execute_vm", staticmethod(lambda d, dom, i: "42"))
    monkeypatch.setattr(CloudflareV3Handler, "_build_payload", staticmethod(lambda d, r, a: {}))

    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return _resp(200, url)

    h.handle(_cf_resp(503, url=BASE), request=request, perform_request=_noop_perform)
    assert calls[0] == abs_url


# ---------------------------------------------------------------------------
# V3: malformed JSON in _extract_data falls back to empty dict
# ---------------------------------------------------------------------------


def test_v3_extract_data_malformed_json_returns_empty():
    body = (
        "window._cf_chl_ctx = {bad: json};\n"
        "window._cf_chl_opt = {also: bad};\n"
        '<form id="challenge-form" action="/x">\n</form>\n'
    )
    resp = _cf_resp(503, body)
    data = CloudflareV3Handler._extract_data(resp)
    assert data["ctx_data"] == {}
    assert data["opt_data"] == {}
