"""Reading a response correctly, which is the input to every other decision."""

from __future__ import annotations

from email.utils import formatdate

import pytest

from scraper.diagnosis import Action, diagnose, diagnose_transport
from scraper.layers import Layer, Trait

from .conftest import BLOCK_BODY, CHALLENGE_BODY, TURNSTILE_BODY


def test_a_normal_page_is_content():
    result = diagnose(status=200, body="<html><h1>Hello</h1></html>")
    assert result.ok
    assert result.action is Action.ACCEPT
    assert result.layer is None


class TestChallengeWithASuccessStatus:
    """The trap worth a class of its own.

    An interstitial is a normal page with a normal status. Parsed as content it is a
    scrape that reports success and collects nothing, and nothing else in the stack
    will notice.
    """

    def test_a_challenge_body_behind_a_200(self):
        result = diagnose(status=200, body=CHALLENGE_BODY)
        assert result.action is Action.SOLVE
        assert result.layer is Layer.MANAGED_CHALLENGE

    def test_the_mitigation_header_alone_is_enough(self):
        result = diagnose(status=200, headers={"cf-mitigated": "challenge"}, body="")
        assert result.action is Action.SOLVE

    def test_turnstile_is_told_apart(self):
        # Different clock, different solver options, so it is worth distinguishing.
        result = diagnose(status=200, body=TURNSTILE_BODY)
        assert result.layer is Layer.TURNSTILE

    def test_a_challenge_can_also_arrive_with_403(self):
        result = diagnose(status=403, body=CHALLENGE_BODY)
        assert result.action is Action.SOLVE
        assert result.layer is Layer.MANAGED_CHALLENGE


class TestThrottling:
    """A 429 is not a bad exit, and conflating the two costs working addresses."""

    def test_a_throttle_slows_down_rather_than_rotating(self):
        result = diagnose(status=429)
        assert result.action is Action.BACKOFF
        assert result.layer is Layer.BEHAVIOURAL
        assert result.trait is Trait.POSSESS

    def test_the_platform_code_for_a_throttle_agrees(self):
        result = diagnose(status=403, body="<p>Error 1015</p>")
        assert result.action is Action.BACKOFF
        assert result.layer is Layer.BEHAVIOURAL

    def test_a_numeric_retry_after_is_believed(self):
        result = diagnose(status=429, headers={"Retry-After": "42"})
        assert result.retry_after == 42.0

    def test_a_dated_retry_after_is_converted(self):
        soon = formatdate(timeval=None, usegmt=True)
        result = diagnose(status=429, headers={"retry-after": soon})
        assert result.retry_after is not None and result.retry_after >= 0.0

    def test_nonsense_in_retry_after_is_ignored_not_guessed(self):
        result = diagnose(status=429, headers={"retry-after": "soon-ish"})
        assert result.retry_after is None


class TestBlocks:
    def test_a_firewall_block_spends_the_address(self):
        result = diagnose(status=403, body=BLOCK_BODY)
        assert result.action is Action.ROTATE
        assert result.layer is Layer.IP_REPUTATION

    def test_a_browser_integrity_failure_does_not_blame_the_address(self):
        # 1010 says the automation channel was detected. Rotating the exit changes
        # nothing about that, so this must not come back as ROTATE.
        result = diagnose(status=403, body="<p>Error 1010</p>")
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.CDP

    def test_a_bare_403_from_the_edge_reads_as_scoring(self):
        result = diagnose(status=403, headers={"cf-ray": "abc123"}, body="denied")
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.SUPER_BOT_FIGHT

    def test_a_403_with_no_edge_fingerprint_is_the_origins_own(self):
        result = diagnose(status=403, body="denied")
        assert result.layer is Layer.WORKERS

    def test_declaring_a_crawler_is_diagnosed_as_such(self):
        # This layer acts on the declaration alone, so the answer is about the
        # User-Agent rather than about anything on the wire.
        result = diagnose(
            status=403,
            headers={"cf-ray": "abc"},
            body="no bots",
            user_agent="Mozilla/5.0 (compatible; GPTBot/1.2)",
        )
        assert result.layer is Layer.AI_BOT_BLOCKER
        assert "GPTBot".lower() in result.detail.lower()


class TestUnderAttack:
    def test_a_challenged_503_is_the_site_challenging_everyone(self):
        result = diagnose(status=503, body=CHALLENGE_BODY)
        assert result.action is Action.SOLVE
        assert result.layer is Layer.UNDER_ATTACK

    def test_a_plain_503_is_just_an_outage(self):
        result = diagnose(status=503, body="maintenance")
        assert result.action is Action.RETRY
        assert result.layer is None


class TestNoBypassExists:
    def test_a_login_gate_is_refused(self):
        result = diagnose(status=401)
        assert result.action is Action.REFUSE
        assert result.layer is Layer.ACCESS

    def test_a_required_signature_is_refused(self):
        result = diagnose(status=401, headers={"www-authenticate": 'Signature realm="x"'})
        assert result.action is Action.REFUSE
        assert result.layer is Layer.WEB_BOT_AUTH

    def test_an_identity_provider_page_is_recognised(self):
        result = diagnose(status=200, body="<a href='https://x.cloudflareaccess.com/'>login</a>")
        assert result.layer is Layer.ACCESS

    def test_our_own_proxy_credential_is_not_a_site_problem(self):
        # A 407 is the proxy in front of us rejecting our password. Reported as
        # Layer 19 it would read as "the site needs a login" and the typo would
        # never surface.
        result = diagnose(status=407)
        assert result.action is Action.REFUSE
        assert result.layer is None
        assert "proxy" in result.detail


class TestNotAboutTheClient:
    @pytest.mark.parametrize("status", [404, 410, 422])
    def test_an_ordinary_client_error_is_returned_not_diagnosed(self, status: int):
        # A 404 is the site's answer about a path. Attributing it to a layer would
        # retire a healthy address over a typo in a URL.
        result = diagnose(status=status)
        assert result.action is Action.ACCEPT
        assert result.layer is None

    @pytest.mark.parametrize("status", [502, 504, 520, 524])
    def test_an_upstream_error_is_worth_one_more_try(self, status: int):
        result = diagnose(status=status)
        assert result.action is Action.RETRY
        assert result.layer is None


class TestTransportFailures:
    def test_through_a_proxy_the_exit_is_the_suspect(self):
        result = diagnose_transport(OSError("connection reset"), through_proxy=True)
        assert result.action is Action.ROTATE
        assert result.layer is Layer.IP_REPUTATION

    def test_without_a_proxy_there_is_nothing_to_blame(self):
        # Swapping anything client-side over a direct connection failure is
        # superstition, so the only honest answer is to try again.
        result = diagnose_transport(OSError("dns failure"), through_proxy=False)
        assert result.action is Action.RETRY
        assert result.layer is None


def test_only_the_head_of_a_large_body_is_examined():
    body = ("x" * 200_000) + "__cf_chl_"
    assert diagnose(status=200, body=body).ok


def test_a_diagnosis_renders_readably():
    assert "managed" in str(diagnose(status=200, body=CHALLENGE_BODY)).lower() or str(
        diagnose(status=200, body=CHALLENGE_BODY)
    ).startswith("solve")
