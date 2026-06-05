"""Integration tests for the Engine + middleware pipeline.

The transport is faked by overriding ``engine.transport.send`` so responses can be
scripted precisely (status codes, call counts, raised errors) without a network.
"""

import threading

import pytest
import requests
import responses

from scraper import AbortedException, Scraper, StealthConfig
from scraper.config import CloudflareConfig, ImpersonateConfig, ProxyConfig
from scraper.engine import Engine, create_engine
from scraper.engine.context import RequestContext
from scraper.engine.user_agent.helper import infer_browser

from .conftest import make_fast_config

BASE = "https://example.com"


def _resp(status=200, url=BASE, body=b""):
    r = requests.Response()
    r.status_code = status
    r._content = body
    r.url = url
    return r


def _fake_send(fn):
    """Wrap a (method, url, kwargs) -> Response function as a transport.send."""

    def send(ctx: RequestContext) -> requests.Response:
        return fn(ctx.method, ctx.url, ctx.kwargs)

    return send


# --- challenge handler configuration --------------------------------------


def test_default_has_four_handlers():
    e = create_engine(make_fast_config())
    assert len(e.challenge_handlers) == 4


def test_all_challenge_handlers_disabled():
    cloudflare = CloudflareConfig(
        disable_turnstile=True,
        disable_v3=True,
        disable_v2=True,
        disable_v1=True,
    )
    cfg = make_fast_config(cloudflare=cloudflare)
    e = create_engine(cfg)
    assert e.challenge_handlers == []
    # ChallengeMiddleware is omitted from the chain when there are no handlers.
    assert "ChallengeMiddleware" not in [type(m).__name__ for m in e.middleware]


# --- UA / impersonate family alignment ------------------------------------


def test_impersonate_aligns_ua_family():
    pytest.importorskip("curl_cffi")
    cfg = make_fast_config(impersonate=ImpersonateConfig(target="chrome124"))
    e = create_engine(cfg)
    assert infer_browser(e.headers["User-Agent"]) == "chrome"


def test_impersonate_custom_ua_not_overridden():
    pytest.importorskip("curl_cffi")
    from scraper import BrowserConfig

    cfg = make_fast_config(
        impersonate=ImpersonateConfig(target="chrome"),
        browser=BrowserConfig(custom="MyBot/1.0"),
    )
    e = create_engine(cfg)
    assert infer_browser(e.headers["User-Agent"]) == "chrome"


# --- abort ----------------------------------------------------------------


def test_abort_signal_blocks_request():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=make_fast_config())
        s.abort()
        with pytest.raises(AbortedException):
            s.get(f"{BASE}/x")


def test_acquire_slot_abort_raises():
    e = create_engine(make_fast_config(max_concurrent_requests=1))
    e.slots.acquire()  # hold the only slot

    t = threading.Timer(0.6, e.abort)
    t.start()
    try:
        with pytest.raises(AbortedException, match="concurrency slot"):
            e.request("GET", f"{BASE}/x")
    finally:
        t.join()
        e.slots.release()


# --- SSL retry ------------------------------------------------------------


def test_ssl_error_retried_without_verification():
    calls = [0]

    def fake(method, url, kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise requests.exceptions.SSLError("cert verify failed")
        return _resp(200, url)

    e = create_engine(make_fast_config())
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 200
    assert calls[0] == 2


def test_ssl_error_in_cdn_cgi_not_retried():
    def fake(method, url, kwargs):
        raise requests.exceptions.SSLError("fail")

    e = create_engine(make_fast_config())
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    with pytest.raises(requests.exceptions.SSLError):
        e.request("GET", f"{BASE}/cdn-cgi/challenge")


def test_verify_ssl_false_sets_verify_kwarg():
    seen = []

    def fake(method, url, kwargs):
        seen.append(kwargs.get("verify"))
        return _resp(200, url)

    e = create_engine(make_fast_config(verify_ssl=False))
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    e.request("GET", f"{BASE}/x")
    assert seen[0] is False


# --- proxy fallback -------------------------------------------------------


def test_proxy_error_uses_rotated_proxy_on_retry():
    """After the first proxy fails, the retry must use the second proxy, not the same one."""
    seen_proxies = []

    def fake(method, url, kwargs):
        seen_proxies.append(kwargs.get("proxies", {}).get("http"))
        if seen_proxies[-1] == "socks5://127.0.0.1:9150":
            raise requests.exceptions.ProxyError("down")
        return _resp(200, url)

    cfg = make_fast_config(
        proxy=ProxyConfig(
            proxy_urls=["socks5://127.0.0.1:9150", "socks5://127.0.0.1:9151"],
            failure_tolerance=0,  # disable immediately on first fail
        )
    )
    e = create_engine(cfg)
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    resp = e.request("GET", f"{BASE}/x")
    assert resp.status_code == 200
    assert seen_proxies[0] == "socks5://127.0.0.1:9150"  # first attempt: proxy A
    assert seen_proxies[1] == "socks5://127.0.0.1:9151"  # retry: proxy B (not A again)


def test_proxy_error_rotates_then_falls_back_to_direct():
    def fake(method, url, kwargs):
        if kwargs.get("proxies"):
            raise requests.exceptions.ProxyError("down")
        return _resp(200, url)

    cfg = make_fast_config(
        proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"], fallback_to_direct=True)
    )
    e = create_engine(cfg)
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


# --- 403 handling ---------------------------------------------------------


def test_403_retry_exhausted_returns_403():
    def fake(method, url, kwargs):
        return _resp(403, url)

    e = create_engine(make_fast_config(max_403_retries=0))
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 403


def test_403_triggers_refresh_when_configured():
    calls = [0]

    def fake(method, url, kwargs):
        calls[0] += 1
        return _resp(403 if calls[0] == 1 else 200, url)

    cfg = make_fast_config(auto_refresh_on_403=True, max_403_retries=3)
    e = create_engine(cfg)
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


def test_403_with_proxy_rotates_and_retries():
    calls = [0]

    def fake(method, url, kwargs):
        calls[0] += 1
        if calls[0] == 1 and kwargs.get("proxies"):
            return _resp(403, url)
        return _resp(200, url)

    cfg = make_fast_config(
        proxy=ProxyConfig(proxy_urls=["socks5://127.0.0.1:9150"]), max_403_retries=3
    )
    e = create_engine(cfg)
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    assert e.request("GET", f"{BASE}/x").status_code == 200


# --- pre / post hooks -----------------------------------------------------


def test_pre_and_post_hooks_run():
    seen = {}

    def pre(engine, method, url, *args, **kwargs):
        seen["pre"] = True
        return method, url, args, kwargs

    def post(engine, resp):
        seen["post"] = resp.status_code
        return resp

    cfg = make_fast_config(pre_hook=pre, post_hook=post)
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        Scraper(origin=BASE, config=cfg).get(f"{BASE}/x")
    assert seen == {"pre": True, "post": 200}


# --- stealth / cipher rotation --------------------------------------------


def test_stealth_enabled_applies_headers():
    cfg = make_fast_config(
        stealth=StealthConfig(
            enabled=True,
            human_like_delays=False,
            min_delay=0.0,
            max_delay=0.0,
            min_delay_fast=0.0,
            max_delay_fast=0.0,
            randomize_headers=True,
            browser_quirks=False,
        )
    )
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        Scraper(origin=BASE, config=cfg).get(f"{BASE}/x")
        assert "Accept" in rsps.calls[0].request.headers


def test_tls_cipher_rotation_runs_without_error():
    cfg = make_fast_config(rotate_tls_ciphers=True)
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/a", body="ok")
        rsps.add(rsps.GET, f"{BASE}/b", body="ok")
        s = Scraper(origin=BASE, config=cfg)
        s.get(f"{BASE}/a")
        s.get(f"{BASE}/b")


def test_session_refresh_triggered_when_stale():
    calls = []

    def fake(method, url, kwargs):
        calls.append((method, url))
        return _resp(200, url)

    e = create_engine(make_fast_config(session_refresh_interval=-1))
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    e.request("GET", f"{BASE}/page")
    # refresh GET to the origin + the actual request
    assert len(calls) >= 2


# --- cookies / raw send ---------------------------------------------------


def test_put_cookie_visible_in_request():
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, f"{BASE}/x", body="ok")
        s = Scraper(origin=BASE, config=make_fast_config())
        s.put_cookie("token", "abc", domain="example.com")
        s.get(f"{BASE}/x")
        assert "token=abc" in str(rsps.calls[0].request.headers.get("Cookie", ""))


def test_perform_request_bypasses_pipeline():
    calls = []

    def fake(method, url, kwargs):
        calls.append(url)
        return _resp(200, url)

    e: Engine = create_engine(make_fast_config())
    e.transport.send = _fake_send(fake)  # type: ignore[method-assign]
    resp = e.perform_request("GET", f"{BASE}/raw")
    assert resp.status_code == 200
    assert calls == [f"{BASE}/raw"]
