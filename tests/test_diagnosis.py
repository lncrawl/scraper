"""Reading a response correctly, which is the input to every other decision."""

from __future__ import annotations

import datetime as dt
import time
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


def test_a_success_code_with_no_body_to_read_is_still_content():
    # A 204 or a 201 goes through none of the challenge checks — there is nothing to
    # inspect — and must fall through to accepted rather than to an attribution.
    for status in (201, 204):
        result = diagnose(status=status, body="")
        assert result.ok
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

    def test_the_injected_detections_script_is_not_a_challenge(self):
        """Found live, and the most expensive false positive available.

        Cloudflare injects a JavaScript-Detections script into ordinary successful
        pages. Treating its path as a challenge marker reported content as a challenge
        on 9 of 10 real pages sampled — so the caller pays for a browser launch it does
        not need, or abandons a page it already has.
        """
        served = (
            "<!doctype html><html><head><title>Chapter 12</title>"
            '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
            "</head><body><h1>Chapter 12</h1><p>Real content.</p></body></html>"
        )
        assert diagnose(status=200, body=served).ok

    def test_the_orchestrate_path_still_is_a_challenge(self):
        # The `/h/` sub-path belongs to an actual interstitial.
        interstitial = (
            "<!doctype html><html><body><script>"
            '"/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=abc"'
            "</script></body></html>"
        )
        assert diagnose(status=200, body=interstitial).action is Action.SOLVE


class TestAJavaScriptOnlyRedirect:
    """Found by comparing 1.0 against 0.2.6 on the source corpus.

    19% of hosts answer a first request with a few hundred bytes of
    `window.location.replace('…?token=…')` — a 200, no challenge marker, no
    Cloudflare header. Both releases handed that to the caller as a successful
    retrieval, so a scraper reported success and collected an empty document. It is
    followable without a browser because the destination is emitted in plain text.
    """

    STUB = (
        "<html><head><title>Loading...</title></head><body>"
        "<script type='text/javascript'>window.location.replace("
        "'https://site.test/?ch=1&js=eyJhbGciOiJIUzI1NiJ9&sid=51db4bb8');"
        "</script></body></html>"
    )

    def test_the_stub_is_a_follow_not_content(self):
        result = diagnose(status=200, body=self.STUB, url="https://site.test/")
        assert result.action is Action.FOLLOW
        assert not result.ok
        assert result.location == ("https://site.test/?ch=1&js=eyJhbGciOiJIUzI1NiJ9&sid=51db4bb8")

    def test_the_token_keeps_its_case(self):
        # The destination carries a signed token. Reading it off the lowercased peek
        # every other marker uses would hand back a URL the server rejects.
        assert "eyJhbGciOiJIUzI1NiJ9" in diagnose(status=200, body=self.STUB).location

    def test_a_real_page_that_mentions_window_location_is_still_content(self):
        # The discriminator is that the stub is the *whole* document. Plenty of real
        # pages assign window.location somewhere in their scripts.
        page = (
            "<html><head><title>Chapter 12</title></head><body><h1>Chapter 12</h1>"
            + "<p>Real prose that a reader would read.</p>" * 40
            + "<script>if (x) window.location = '/next';</script></body></html>"
        )
        assert diagnose(status=200, body=page).ok

    def test_a_truncated_large_page_is_not_mistaken_for_a_stub(self):
        # `diagnose` may be handed a prefix. Without requiring the closing tag, the
        # first 4 KB of a big page with an early redirect script would look like a
        # stub, and the caller would be sent chasing a URL instead of given a page.
        prefix = "<html><head><script>window.location.replace('/x')</script>"
        assert diagnose(status=200, body=prefix).ok

    def test_a_stub_wearing_a_challenge_marker_is_still_a_challenge(self):
        # Order matters: a challenge needs a browser, and treating it as a free hop
        # would loop on the interstitial instead of escalating.
        both = self.STUB.replace("Loading...", "Just a moment")
        assert diagnose(status=200, body=both).action is Action.SOLVE


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

    def test_the_header_is_found_whatever_else_is_alongside_it(self):
        headers = {"Content-Type": "text/html", "Server": "cloudflare", "Retry-After": "9"}
        assert diagnose(status=429, headers=headers).retry_after == 9.0

    def test_an_empty_retry_after_is_no_answer_rather_than_zero(self):
        # Zero would send the next request out immediately, which is the opposite of
        # what a throttle is asking for.
        assert diagnose(status=429, headers={"retry-after": "  "}).retry_after is None

    def test_a_date_without_a_zone_is_read_as_utc(self):
        # RFC 9110 says HTTP-dates are GMT, but the sender may omit the marker and
        # `parsedate_to_datetime` then returns a naive value. Subtracting an aware
        # "now" from a naive moment raises, so the zone has to be supplied.
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=120)
        without_zone = soon.strftime("%a, %d %b %Y %H:%M:%S")
        result = diagnose(status=429, headers={"retry-after": without_zone})
        assert result.retry_after is not None
        assert 0.0 <= result.retry_after <= 130.0

    def test_a_date_already_in_the_past_never_asks_for_a_negative_wait(self):
        past = formatdate(timeval=time.time() - 600, usegmt=True)
        assert diagnose(status=429, headers={"retry-after": past}).retry_after == 0.0


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

    class TestAProxyThatRefusesUsIsNotTheSitesDoing:
        """Found live, by running the harness with the pool credential unset.

        A tor-pool that enforces authentication rejects the SOCKS5 handshake, which
        never becomes a response, so it arrived here and was attributed to layer 1.
        The visible symptom was four scenarios reporting that the *destination*
        blocks datacenter ranges and only a residential exit could help, when the
        real cause was one missing environment variable.
        """

        # curl's own wording, which is what the matcher has to survive.
        REJECTED = "Failed to perform, curl: (97) User was rejected by the SOCKS5 server (1 1)."

        def test_a_socks5_credential_rejection_is_not_layer_one(self):
            result = diagnose_transport(OSError(self.REJECTED), through_proxy=True)
            assert result.action is Action.REFUSE
            assert result.layer is None
            assert "credential" in result.detail

        def test_a_407_to_connect_is_not_layer_one(self):
            error = OSError("Received HTTP code 407 from proxy after CONNECT")
            result = diagnose_transport(error, through_proxy=True)
            assert result.action is Action.REFUSE
            assert result.layer is None

        def test_a_proxy_that_wanted_a_credential_we_never_offered_is_not_layer_one(self):
            # The other side of the handshake: not a wrong password but no password.
            # Same conclusion — a configuration fault, and rotating an address that
            # was never asked for one cannot fix it.
            error = OSError("No authentication method was acceptable")
            result = diagnose_transport(error, through_proxy=True)
            assert result.action is Action.REFUSE
            assert result.layer is None
            assert "credential" in result.detail

        def test_an_unresolvable_proxy_host_is_not_layer_one(self):
            error = OSError("Could not resolve proxy: tor-poool")
            result = diagnose_transport(error, through_proxy=True)
            assert result.action is Action.REFUSE
            assert result.layer is None

        def test_a_dead_destination_behind_a_good_proxy_still_blames_the_exit(self):
            # The other half of the distinction, and the reason this cannot key off
            # the ProxyError class: curl reports an unreachable destination through
            # the same exception. That one really is evidence about the exit.
            error = OSError("Can't complete SOCKS5 connection to example.com:443")
            result = diagnose_transport(error, through_proxy=True)
            assert result.action is Action.ROTATE
            assert result.layer is Layer.IP_REPUTATION


def test_only_the_head_of_a_large_body_is_examined():
    body = ("x" * 200_000) + "__cf_chl_"
    assert diagnose(status=200, body=body).ok


def test_a_diagnosis_renders_readably():
    assert "managed" in str(diagnose(status=200, body=CHALLENGE_BODY)).lower() or str(
        diagnose(status=200, body=CHALLENGE_BODY)
    ).startswith("solve")
