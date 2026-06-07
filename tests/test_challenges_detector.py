"""Tests for CloudflareDetector.classify, raise_for, and build_detector."""

import pytest
import requests

from scraper.challenges import (
    CloudflareChallengeKind,
    CloudflareDetector,
    build_detector,
)
from scraper.config import CloudflareConfig
from scraper.exceptions import (
    CloudflareCaptchaError,
    CloudflareChallengeError,
    CloudflareFirewallBlock,
    CloudflareTurnstileError,
)

BASE = "https://example.com"


def _resp(status=200, url=BASE, body="", headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.url = url
    r.headers.update(headers or {})
    return r


def _cf_resp(status=503, body="", url=BASE):
    return _resp(status, url, body, {"Server": "cloudflare"})


_FIREWALL_BODY = '<span class="cf-error-code">1020</span>'
_TURNSTILE_BODY = '<div class="cf-turnstile" data-sitekey="abc"></div>'
_CAPTCHA_BODY = (
    '<img src="/cdn-cgi/images/trace/captcha/x.gif"><form id="challenge-form" action="/x"></form>'
)
_MANAGED_BODY = "window._cf_chl_opt = {}"


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (403, _FIREWALL_BODY, CloudflareChallengeKind.FIREWALL_BLOCK),
        (503, _TURNSTILE_BODY, CloudflareChallengeKind.TURNSTILE),
        (403, _CAPTCHA_BODY, CloudflareChallengeKind.CAPTCHA),
        (503, _MANAGED_BODY, CloudflareChallengeKind.MANAGED),
        (429, _MANAGED_BODY, CloudflareChallengeKind.MANAGED),
    ],
)
def test_classify_kinds(status, body, expected):
    assert CloudflareDetector().classify(_cf_resp(status, body)) is expected


def test_classify_clean_cf_page_is_none():
    assert (
        CloudflareDetector().classify(_cf_resp(200, "<h1>hi</h1>")) is CloudflareChallengeKind.NONE
    )


def test_classify_non_cloudflare_is_none():
    assert (
        CloudflareDetector().classify(_resp(503, body=_MANAGED_BODY))
        is CloudflareChallengeKind.NONE
    )


def test_classify_wrong_status_is_none():
    assert (
        CloudflareDetector().classify(_cf_resp(200, _MANAGED_BODY)) is CloudflareChallengeKind.NONE
    )


def test_classify_attribute_error_is_none():
    assert CloudflareDetector().classify(object()) is CloudflareChallengeKind.NONE  # type: ignore[arg-type]


def test_classify_cf_server_challenge_status_no_known_marker():
    assert (
        CloudflareDetector().classify(_cf_resp(503, "<h1>Generic error</h1>"))
        is CloudflareChallengeKind.NONE
    )


@pytest.mark.parametrize(
    "kind,exc",
    [
        (CloudflareChallengeKind.FIREWALL_BLOCK, CloudflareFirewallBlock),
        (CloudflareChallengeKind.TURNSTILE, CloudflareTurnstileError),
        (CloudflareChallengeKind.CAPTCHA, CloudflareCaptchaError),
        (CloudflareChallengeKind.MANAGED, CloudflareChallengeError),
    ],
)
def test_raise_for_maps_exception(kind, exc):
    with pytest.raises(exc):
        CloudflareDetector(debug=True).raise_for(kind, _cf_resp())


def test_raise_for_none_kind_hits_fallback():
    with pytest.raises(CloudflareChallengeError):
        CloudflareDetector().raise_for(CloudflareChallengeKind.NONE, _cf_resp())


def test_build_detector_enabled():
    det = build_detector(CloudflareConfig(debug=True))
    assert isinstance(det, CloudflareDetector)
    assert det.debug is True


def test_build_detector_disabled_returns_none():
    assert build_detector(CloudflareConfig(enabled=False)) is None
