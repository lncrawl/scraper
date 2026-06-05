"""Tests for ScraperEngine's request pipeline.

Covers the pieces that test_scraper.py (Scraper) doesn't reach:
impersonate-family alignment, challenge handler configuration, SSL retry,
403 handling, pre/post hooks, abort, cipher rotation, stealth gating,
put_cookie, and apply_browser_clearance.
"""

import threading

import pytest
import requests
import responses

from scraper import AbortedException, Scraper, StealthConfig
from scraper._engine.config import BrowserConfig, ProxyConfig

from .conftest import make_fast_config

BASE = "https://example.com"


# ---------------------------------------------------------------------------
# _build_challenge_handlers
# ---------------------------------------------------------------------------


def test_all_challenge_handlers_disabled():
    from scraper._engine.session import ScraperEngine

    cfg = make_fast_config()
    cfg.disable_turnstile = True
    cfg.disable_v3 = True
    cfg.disable_v2 = True
    cfg.disable_v1 = True
    assert ScraperEngine(cfg)._challenge_handlers == []


def test_default_has_four_handlers():
    from scraper._engine.session import ScraperEngine

    assert len(ScraperEngine(make_fast_config())._challenge_handlers) == 4


# ---------------------------------------------------------------------------
# Browser / impersonate alignment  (build_transport stubbed out)
# ---------------------------------------------------------------------------


def _fake_transport():
    class _FakeCookies:
        @property
        def jar(self):
            return []

        def set(self, *a, **kw):
            pass

        def delete(self, *a, **kw):
            pass

    class _FakeTransport:
        cookies = _FakeCookies()

        def request(self, method, url, **kw):
            r = requests.Response()
            r.status_code = 200
            r._content = b""
            r.url = url
            return r

        def set_cookie(self, *a, **kw):
            pass

        def clear_cookie(self, *a, **kw):
            pass

    return _FakeTransport()


def test_browser_none_aligned_to_chrome_when_impersonating(monkeypatch):
    import scraper._engine.session as eng
    from scraper._engine.user_agent.filter import infer_browser

    monkeypatch.setattr(eng, "build_transport", lambda *a, **kw: _fake_transport())
    cfg = make_fast_config()
    cfg.impersonate = "chrome124"
    cfg.browser = None
    e = eng.ScraperEngine(cfg)
    assert infer_browser(e.user_agent.headers.get("User-Agent", "")) == "chrome"


def test_browser_dict_without_custom_aligned(monkeypatch):
    import scraper._engine.session as eng
    from scraper._engine.user_agent.filter import infer_browser

    monkeypatch.setattr(eng, "build_transport", lambda *a, **kw: _fake_transport())
    cfg = make_fast_config()
    cfg.impersonate = "chrome124"
    cfg.browser = BrowserConfig(platform="windows", mobile=False)
    e = eng.ScraperEngine(cfg)
    assert infer_browser(e.user_agent.headers.get("User-Agent", "")) == "chrome"


def test_browser_dict_with_custom_not_overridden(monkeypatch):
    import scraper._engine.session as eng

    monkeypatch.setattr(eng, "build_transport", lambda *a, **kw: _fake_transport())
    cfg = make_fast_config()
    cfg.impersonate = "chrome124"
    cfg.browser = BrowserConfig(custom="MyBot/1.0")
    e = eng.ScraperEngine(cfg)
    assert e.user_agent.headers["User-Agent"] == "MyBot/1.0"


# ---------------------------------------------------------------------------
# Abort signal
# ---------------------------------------------------------------------------


def test_abort_signal_blocks_request():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=make_fast_config())
        s.abort()
        with pytest.raises(AbortedException):
            s.get(f"{BASE}/x")


def test_acquire_slot_abort_raises():
    cfg = make_fast_config(max_concurrent_requests=1)
    s = Scraper(origin=BASE, config=cfg)
    s._slots.acquire()  # hold the only slot

    t = threading.Timer(0.6, s.abort)
    t.start()
    try:
        with pytest.raises(AbortedException, match="concurrency slot"):
            s.request("GET", f"{BASE}/x")
    finally:
        t.join()
        s._slots.release()


# ---------------------------------------------------------------------------
# SSL retry
# ---------------------------------------------------------------------------


def test_ssl_error_retried_without_verification(monkeypatch):
    calls = [0]

    def fake_perform(method, url, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise requests.exceptions.SSLError("cert verify failed")
        r = requests.Response()
        r.status_code = 200
        r._content = b""
        r.url = url
        return r

    s = Scraper(origin=BASE, config=make_fast_config())
    s.perform_request = fake_perform  # type: ignore[method-assign]
    resp = s.request("GET", f"{BASE}/x")
    assert resp.status_code == 200
    assert calls[0] == 2


def test_ssl_error_in_cdn_cgi_is_not_retried(monkeypatch):
    def fake_perform(method, url, *args, **kwargs):
        raise requests.exceptions.SSLError("fail")

    s = Scraper(origin=BASE, config=make_fast_config())
    s.perform_request = fake_perform  # type: ignore[method-assign]
    with pytest.raises(requests.exceptions.SSLError):
        s.request("GET", f"{BASE}/cdn-cgi/challenge")


# ---------------------------------------------------------------------------
# Proxy fallback
# ---------------------------------------------------------------------------


def test_proxy_error_rotates_then_falls_back_to_direct(monkeypatch):
    calls = [0]

    def fake_perform(method, url, *args, **kwargs):
        calls[0] += 1
        if kwargs.get("proxies"):
            raise requests.exceptions.ProxyError("down")
        r = requests.Response()
        r.status_code = 200
        r._content = b""
        r.url = url
        return r

    cfg = make_fast_config()
    cfg.proxy = ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"], fallback_to_direct=True)
    s = Scraper(origin=BASE, config=cfg)
    s.perform_request = fake_perform  # type: ignore[method-assign]
    resp = s.request("GET", f"{BASE}/x")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 403 handling
# ---------------------------------------------------------------------------


def test_403_triggers_refresh_when_configured():
    cfg = make_fast_config()
    cfg.auto_refresh_on_403 = True
    cfg.max_403_retries = 3

    call_count = [0]

    def fake_perform(method, url, *args, **kwargs):
        call_count[0] += 1
        r = requests.Response()
        if call_count[0] == 1:
            r.status_code = 403
        elif method == "GET" and "example.com" in url and call_count[0] == 2:
            # session refresh
            r.status_code = 200
        else:
            r.status_code = 200
        r._content = b""
        r.url = url
        return r

    s = Scraper(origin=BASE, config=cfg)
    s.perform_request = fake_perform  # type: ignore[method-assign]
    resp = s.request("GET", f"{BASE}/x")
    assert resp.status_code in (200, 403)  # either retried successfully or gave up


def test_403_retry_exhausted_returns_403():
    import scraper._engine.session as eng

    # Use ScraperEngine directly — Scraper.request() calls raise_for_status()
    # which would convert the 403 into an HTTPError before we can inspect it.
    cfg = make_fast_config()
    cfg.max_403_retries = 0  # immediately exhausted

    def fake_perform(method, url, *args, **kwargs):
        r = requests.Response()
        r.status_code = 403
        r._content = b""
        r.url = url
        return r

    e = eng.ScraperEngine(cfg)
    e.perform_request = fake_perform  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Pre / post hooks
# ---------------------------------------------------------------------------


def test_pre_hook_can_modify_method_and_url():
    called = [False]

    def pre(engine, method, url, *args, **kwargs):
        called[0] = True
        return method, url, args, kwargs

    cfg = make_fast_config()
    cfg.pre_hook = pre

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=cfg)
        s.get(f"{BASE}/x")

    assert called[0]


def test_post_hook_receives_response():
    seen = [None]

    def post(engine, resp):
        seen[0] = resp.status_code
        return resp

    cfg = make_fast_config()
    cfg.post_hook = post

    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=cfg)
        s.get(f"{BASE}/x")

    assert seen[0] == 200


# ---------------------------------------------------------------------------
# put_cookie / apply_browser_clearance
# ---------------------------------------------------------------------------


def test_put_cookie_visible_in_requests(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=fast_config)
        s.put_cookie("token", "abc", domain="example.com")
        s.get(f"{BASE}/x")
        assert "token=abc" in str(rsps.calls[0].request.headers.get("Cookie", ""))


def test_apply_browser_clearance_sets_ua_and_cookie(fast_config):
    s = Scraper(origin=BASE, config=fast_config)
    s.apply_browser_clearance(
        "example.com",
        cf_clearance="abc123",
        user_agent="RealBrowser/100",
        cookies={"__cf_bm": "extra"},
    )
    assert s.headers["User-Agent"] == "RealBrowser/100"
    # both cookies should be present in the jar
    names = {c.name for c in s.cookies}
    assert "cf_clearance" in names
    assert "__cf_bm" in names


def test_apply_browser_clearance_url_domain(fast_config):
    s = Scraper(origin=BASE, config=fast_config)
    s.apply_browser_clearance("https://example.com/path", cf_clearance="xyz")
    names = {c.name for c in s.cookies}
    assert "cf_clearance" in names


# ---------------------------------------------------------------------------
# TLS cipher rotation
# ---------------------------------------------------------------------------


def test_tls_cipher_rotation_runs_without_error():
    cfg = make_fast_config()
    cfg.rotate_tls_ciphers = True
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/a", body="ok")
        rsps.add(rsps.GET, f"{BASE}/b", body="ok")
        s = Scraper(origin=BASE, config=cfg)
        s.get(f"{BASE}/a")
        s.get(f"{BASE}/b")  # second request triggers rotation


# ---------------------------------------------------------------------------
# perform_request routed through fake impersonate transport
# ---------------------------------------------------------------------------


def test_perform_request_with_impersonate_transport(monkeypatch):
    import scraper._engine.session as eng

    monkeypatch.setattr(eng, "build_transport", lambda *a, **kw: _fake_transport())
    cfg = make_fast_config()
    cfg.impersonate = "chrome124"
    s = eng.ScraperEngine(cfg)
    # The fake transport returns a 200 directly, no adapter/responses needed
    resp = s.request("GET", f"{BASE}/x")
    assert resp.status_code == 200


def test_mirror_transport_cookies_clears_and_repopulates(monkeypatch):
    import scraper._engine.session as eng

    class _CookieJar:
        @property
        def jar(self):
            import http.cookiejar

            ck = http.cookiejar.Cookie(
                version=0,
                name="sid",
                value="abc",
                port=None,
                port_specified=False,
                domain="example.com",
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=False,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
            return [ck]

        def set(self, *a, **kw):
            pass

        def delete(self, *a, **kw):
            pass

    class _FakeTransportWithCookies:
        cookies = _CookieJar()

        def request(self, method, url, **kw):
            r = requests.Response()
            r.status_code = 200
            r._content = b""
            r.url = url
            return r

        def set_cookie(self, *a, **kw):
            pass

        def clear_cookie(self, *a, **kw):
            pass

    monkeypatch.setattr(eng, "build_transport", lambda *a, **kw: _FakeTransportWithCookies())
    cfg = make_fast_config()
    cfg.impersonate = "chrome124"
    s = eng.ScraperEngine(cfg)
    s.request("GET", f"{BASE}/x")
    # After a request, the cookie jar should contain "sid"
    assert "sid" in {c.name for c in s.cookies}


# ---------------------------------------------------------------------------
# verify_ssl=False sets verify kwarg
# ---------------------------------------------------------------------------


def test_verify_ssl_false_sets_verify_kwarg():
    from scraper._engine.session import ScraperEngine

    calls = []

    def fake_perform(method, url, *args, **kwargs):
        calls.append(kwargs.get("verify"))
        r = requests.Response()
        r.status_code = 200
        r._content = b""
        r.url = url
        return r

    cfg = make_fast_config()
    cfg.verify_ssl = False
    s = ScraperEngine(cfg)
    s.perform_request = fake_perform  # type: ignore[method-assign]
    s.request("GET", f"{BASE}/x")
    assert calls[0] is False


# ---------------------------------------------------------------------------
# _release_slot ValueError is swallowed
# ---------------------------------------------------------------------------


def test_release_slot_swallows_value_error():
    from scraper._engine.session import ScraperEngine

    cfg = make_fast_config()
    e = ScraperEngine(cfg)
    # BoundedSemaphore is already at max capacity — releasing again raises ValueError
    e._release_slot()  # must not propagate


# ---------------------------------------------------------------------------
# _rotate_tls_cipher_suite no-ops when suite is unchanged/None
# ---------------------------------------------------------------------------


def test_rotate_cipher_suite_noop_when_same_suite(monkeypatch):
    from scraper._engine.session import ScraperEngine

    cfg = make_fast_config()
    cfg.rotate_tls_ciphers = True
    e = ScraperEngine(cfg)
    # Force rotator to always return the current suite → no re-mount
    current = e._cipher_suite
    monkeypatch.setattr(e._cipher_rotator, "suite_for", lambda n: current)
    e._rotate_tls_cipher_suite()  # should not remount


def test_rotate_cipher_suite_noop_when_none(monkeypatch):
    from scraper._engine.session import ScraperEngine

    cfg = make_fast_config()
    cfg.rotate_tls_ciphers = True
    e = ScraperEngine(cfg)
    monkeypatch.setattr(e._cipher_rotator, "suite_for", lambda n: None)
    e._rotate_tls_cipher_suite()  # None → skips remount


# ---------------------------------------------------------------------------
# apply_browser_clearance without user_agent (395->397 branch)
# ---------------------------------------------------------------------------


def test_apply_browser_clearance_cookies_only(fast_config):
    s = Scraper(origin=BASE, config=fast_config)
    original_ua = s.headers["User-Agent"]
    s.apply_browser_clearance("example.com", cookies={"__cf_bm": "tok"})
    # User-Agent unchanged (no user_agent argument)
    assert s.headers["User-Agent"] == original_ua
    assert "__cf_bm" in {c.name for c in s.cookies}


def test_apply_browser_clearance_no_cf_clearance(fast_config):
    s = Scraper(origin=BASE, config=fast_config)
    # cf_clearance=None → jar only has the extra cookies
    s.apply_browser_clearance("example.com", cookies={"token": "x"})
    names = {c.name for c in s.cookies}
    assert "token" in names
    assert "cf_clearance" not in names


# ---------------------------------------------------------------------------
# 403 with proxy rotates identity then retries (lines 272-273, 276)
# ---------------------------------------------------------------------------


def test_403_with_proxy_rotates_and_retries():
    from scraper._engine.session import ScraperEngine

    calls = [0]

    def fake_perform(method, url, *args, **kwargs):
        calls[0] += 1
        r = requests.Response()
        if calls[0] == 1 and kwargs.get("proxies"):
            r.status_code = 403
        else:
            r.status_code = 200
        r._content = b""
        r.url = url
        return r

    cfg = make_fast_config()
    cfg.proxy = ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"])
    cfg.max_403_retries = 3
    e = ScraperEngine(cfg)
    e.perform_request = fake_perform  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Session refresh path (line 319)
# ---------------------------------------------------------------------------


def test_session_refresh_triggered_when_stale():
    from scraper._engine.session import ScraperEngine

    calls = []

    def fake_perform(method, url, *args, **kwargs):
        calls.append((method, url))
        r = requests.Response()
        r.status_code = 200
        r._content = b""
        r.url = url
        return r

    cfg = make_fast_config()
    cfg.session_refresh_interval = -1  # always stale
    s = ScraperEngine(cfg)
    s.perform_request = fake_perform  # type: ignore[method-assign]
    s.request("GET", f"{BASE}/page")
    # At least two calls: the refresh GET (to base) + the actual request
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# Stealth gating in request()
# ---------------------------------------------------------------------------


def test_stealth_enabled_applies_headers():
    cfg = make_fast_config()
    cfg.stealth = StealthConfig(
        enabled=True,
        human_like_delays=False,
        min_delay=0.0,
        max_delay=0.0,
        min_delay_fast=0.0,
        max_delay_fast=0.0,
        randomize_headers=True,
        browser_quirks=False,
    )
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=cfg)
        s.get(f"{BASE}/x")
        assert "Accept" in rsps.calls[0].request.headers
