"""Reading a response correctly, which is the input to every other decision."""

from __future__ import annotations

import datetime as dt
import time
from email.utils import formatdate

import pytest

from scraper.diagnosis import Action, diagnose, diagnose_transport, edge, is_challenge
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

    def test_the_interstitials_own_wording_in_the_prose_is_not_a_challenge(self):
        """Found live, and it cost a chapter every time it happened.

        `Just a moment...` is the interstitial's title, but it is also ordinary English.
        Matched anywhere in the body, a chapter whose narration ran "wait just a moment
        longer" was diagnosed as a challenge behind a 200 — so the retrieval re-solved
        and re-fetched the page it already had until the attempt budget ran out. Five
        browser launches, chapter dropped, and the rest of the book fine, which is what
        made it read as an intermittent browser fault rather than a detection bug.
        """
        page = (
            "<!doctype html><html><head><title>Chapter 96 - A Novel</title></head><body>"
            '<p>"Wait just a moment longer," she said.</p>' + "<p>Prose.</p>" * 4000
        )
        assert len(page) > 32 * 1024
        assert diagnose(status=200, body=page).ok
        assert not is_challenge(page)

    def test_the_wording_in_a_titles_is_not_enough_on_a_page_of_content(self):
        # A novel site puts the chapter title in <title>, so a chapter actually called
        # "Just a Moment" would match on the title alone. An interstitial is a document
        # with no page on it; this is a page.
        page = (
            "<!doctype html><html><head><title>Chapter 96: Just a Moment</title></head>"
            "<body>" + "<p>Prose.</p>" * 4000
        )
        assert diagnose(status=200, body=page).ok

    def test_a_small_document_titled_like_the_interstitial_is_one(self):
        # The other direction: an interstitial variant carrying none of the machine
        # markers is still an interstitial, and missing it abandons a solvable page.
        interstitial = (
            "<!doctype html><html><head><title>Just a moment...</title></head>"
            "<body><div>Enable JavaScript and cookies to continue</div></body></html>"
        )
        assert diagnose(status=200, body=interstitial).action is Action.SOLVE
        assert is_challenge(interstitial)


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

    def test_a_client_that_never_claimed_to_be_a_browser_meets_the_coarse_layer(self):
        # L11 was unreachable: a 403 behind the edge always read as the scoring layer,
        # whatever the client had sent. Naming the coarse one names the remedy — a
        # faithful transport profile rather than a browser launch.
        result = diagnose(
            status=403,
            headers={"cf-ray": "abc123"},
            body="denied",
            user_agent="python-requests/2.32.3",
        )
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.BOT_FIGHT

    def test_a_browser_claim_is_taken_at_face_value_here(self):
        # Whether the claim is *true* is the transport's business; this module never
        # sees a handshake. It only asks whether there was a claim to disbelieve.
        result = diagnose(
            status=403,
            headers={"cf-ray": "abc123"},
            body="denied",
            user_agent="Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/141.0 Safari/537.36",
        )
        assert result.layer is Layer.SUPER_BOT_FIGHT

    def test_an_absent_user_agent_is_not_read_as_a_missing_browser(self):
        # `diagnose` is pure and often called on a recorded page with no User-Agent to
        # hand. Reading silence as "not a browser" would relabel every one of those.
        result = diagnose(status=403, headers={"cf-ray": "abc123"}, body="denied")
        assert result.layer is Layer.SUPER_BOT_FIGHT

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

    def test_a_not_modified_is_an_answer_not_a_block(self):
        # What `unchanged()` relies on. A 304 has no body, so any classifier that
        # reasons from body markers has to reach "accept" on an empty one — and a
        # layer attributed here would be written to the profile of a site whose only
        # offence is that its page is current.
        result = diagnose(status=304, headers={"etag": 'W/"abc"'}, body="")
        assert result.action is Action.ACCEPT
        assert result.layer is None


class TestTransportFailures:
    def test_through_a_proxy_the_exit_is_the_suspect(self):
        result = diagnose_transport(OSError("connection reset"), through_proxy=True)
        assert result.action is Action.ROTATE
        # Blamed on the exit, but attributed to no layer: the site never answered, so
        # there is nothing to conclude about it. Naming layer 1 here wrote a permanent
        # verdict onto the origin's profile that the destination refuses us, and it also
        # made rotation unreachable for any pool of Tor exits — `ExitKind.TOR.reach` is
        # empty, so the planner's reputation check stopped the move every time.
        assert result.layer is None

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
            assert result.layer is None


def test_only_the_head_of_a_large_body_is_examined():
    body = ("x" * 200_000) + "__cf_chl_"
    assert diagnose(status=200, body=body).ok


def test_a_diagnosis_renders_readably():
    assert "managed" in str(diagnose(status=200, body=CHALLENGE_BODY)).lower() or str(
        diagnose(status=200, body=CHALLENGE_BODY)
    ).startswith("solve")


class TestCloudflaresOwnCodes:
    """Codes the classifier used to ignore, and what each of them is really about.

    An unrecognised code fell through to "forbidden by the origin" at layer 15, whose
    stance is *avoid* — no remedy, so the ladder exhausted itself over a zone whose DNS
    was misconfigured, and wrote a detection verdict to its profile on the way out.
    """

    @pytest.mark.parametrize("code", [1000, 1001, 1002, 1003, 1004, 1013, 1016, 1018, 1023])
    def test_a_misconfigured_zone_is_nobodys_detection_layer(self, code: int):
        result = diagnose(status=403, headers={"cf-ray": "x"}, body=f"<p>Error {code}</p>")
        assert result.action is Action.REFUSE
        assert result.layer is None, "attributing this retires a healthy exit"
        assert str(code) in result.detail

    @pytest.mark.parametrize("code", [1101, 1102])
    def test_edge_code_that_crashed_is_the_one_honest_layer_15(self, code: int):
        # Every other route to layer 15 is by elimination. A crash is not a refusal, so
        # this retries: no browser and no address changes what a thrown script returns.
        result = diagnose(status=500, body=f"<p>Error {code}</p>")
        assert result.action is Action.RETRY
        assert result.layer is Layer.WORKERS

    def test_a_cache_failure_is_transient_and_not_about_us(self):
        result = diagnose(status=500, body="<p>Error 1200</p>")
        assert result.action is Action.RETRY
        assert result.layer is None

    def test_hotlink_protection_has_nothing_left_to_try(self):
        # It reads the Referer, and this library already sends one on every request.
        result = diagnose(status=403, body="<p>Error 1011</p>")
        assert result.action is Action.REFUSE
        assert result.layer is Layer.WORKERS

    def test_an_unmapped_code_still_reaches_the_message(self):
        # "Cloudflare error 1024" is actionable for whoever reads the log;
        # "forbidden by the origin" is not.
        result = diagnose(status=403, body="<p>Error 1024</p>")
        assert result.layer is Layer.WORKERS
        assert "1024" in result.detail

    def test_a_rate_limit_code_still_backs_off_at_any_status(self):
        result = diagnose(status=403, body="<p>Error 1015</p>")
        assert result.action is Action.BACKOFF
        assert result.layer is Layer.BEHAVIOURAL


class TestVendorsBesidesCloudflare:
    """The rest of the mitigation market, and what each refusal is really about.

    Before this, anything that was not Cloudflare reached "forbidden by the origin" at
    layer 15 — stance *avoid*, no remedy, terminal — and a vendor's captcha page served
    with a 200 was read as content, which is the silent half.

    One test per family, because a signature with no test rots the moment a vendor
    renames a cookie, and there is nothing in a passing suite to say it happened.
    """

    def test_datadome_is_a_per_session_model_not_a_header_problem(self):
        result = diagnose(status=403, headers={"x-datadome": "protected"}, body="denied")
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.BOT_MANAGEMENT
        assert "DataDome" in result.detail

    def test_a_datadome_captcha_behind_a_200_is_not_content(self):
        # The expensive one to miss: it arrives with a success status, so a caller
        # parses the captcha page and records a scrape of nothing.
        body = '<html><iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=x">'
        result = diagnose(status=200, headers={"set-cookie": "datadome=abc; Path=/"}, body=body)
        assert result.action is Action.SOLVE
        assert result.layer is Layer.MANAGED_CHALLENGE
        assert "DataDome" in result.detail

    def test_kasada_is_named_from_its_own_headers(self):
        result = diagnose(status=403, headers={"x-kpsdk-ct": "abc123"}, body="")
        assert result.layer is Layer.BOT_MANAGEMENT
        assert "Kasada" in result.detail

    def test_perimeterx_press_and_hold_is_a_challenge(self):
        body = "<html><body><div id='px-captcha'></div>Press &amp; Hold to confirm</body></html>"
        result = diagnose(status=403, headers={"set-cookie": "_pxhd=x"}, body=body)
        assert result.action is Action.SOLVE
        assert "PerimeterX" in result.detail

    def test_akamai_is_recognised_from_its_sensor_cookies(self):
        result = diagnose(
            status=403,
            headers={"set-cookie": "_abck=xyz~-1~||-1||; ak_bmsc=q"},
            body="Access Denied",
        )
        assert result.layer is Layer.BOT_MANAGEMENT
        assert "Akamai" in result.detail

    def test_akamai_refuses_with_400_as_often_as_403(self):
        # Read as the site's answer about a path, a failed sensor check is a silent
        # give-up on a page that is there.
        result = diagnose(status=400, headers={"server": "AkamaiGHost"}, body="Invalid URL")
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.BOT_MANAGEMENT

    def test_imperva_is_recognised_from_its_own_header(self):
        result = diagnose(
            status=403,
            headers={"x-iinfo": "9-12345-0 NNNN CT(0 0 0)", "x-cdn": "Incapsula"},
            body="Pardon Our Interruption",
        )
        assert result.layer is Layer.BOT_MANAGEMENT
        assert "Imperva" in result.detail

    def test_ddos_guard_is_a_coarser_edge_so_a_better_profile_may_serve(self):
        # Six hosts in the source corpus sit behind this one. Layer 12's stance is
        # satisfy, so the ladder still tries the tier that supplies a better profile
        # rather than giving up on a managed provider nobody configured.
        result = diagnose(status=403, headers={"server": "ddos-guard"}, body="")
        assert result.action is Action.ESCALATE
        assert result.layer is Layer.SUPER_BOT_FIGHT
        assert "DDoS-Guard" in result.detail

    def test_a_ddos_guard_interstitial_needs_a_browser(self):
        body = '<html><script src="/.well-known/ddos-guard/id/x"></script></html>'
        result = diagnose(status=200, headers={"server": "ddos-guard"}, body=body)
        assert result.action is Action.SOLVE
        assert result.layer is Layer.MANAGED_CHALLENGE

    def test_sucuri_is_named(self):
        result = diagnose(
            status=403,
            headers={"x-sucuri-id": "12345"},
            body="Sucuri Website Firewall - Access Denied",
        )
        assert result.layer is Layer.SUPER_BOT_FIGHT
        assert "Sucuri" in result.detail

    def test_aws_waf_is_named_from_its_action_header(self):
        result = diagnose(status=403, headers={"x-amzn-waf-action": "captcha"}, body="")
        assert result.layer is Layer.SUPER_BOT_FIGHT
        assert "AWS WAF" in result.detail

    def test_f5_has_one_unmistakable_block_page(self):
        result = diagnose(
            status=403,
            body="<html><body>The requested URL was rejected. Please consult with your "
            "administrator.</body></html>",
        )
        assert result.layer is Layer.SUPER_BOT_FIGHT
        assert "F5" in result.detail

    def test_a_cdn_is_named_without_being_blamed(self):
        # CloudFront's header is on every response it serves, successful ones included.
        # Reading it as a detection layer would attribute an operator's own rule — a
        # signed URL, a geo restriction — to a bot check.
        clean = diagnose(status=200, headers={"x-amz-cf-id": "abc"}, body="<h1>Chapter</h1>")
        assert clean.ok
        refused = diagnose(status=403, headers={"x-amz-cf-id": "abc"}, body="denied")
        assert refused.layer is Layer.WORKERS, "an edge rule is operator code, not a bot layer"
        assert "CloudFront" in refused.detail

    def test_fastly_is_the_same_kind_of_identification(self):
        result = diagnose(status=403, headers={"server": "fastly"}, body="Fastly error: x")
        assert result.layer is Layer.WORKERS
        assert "Fastly" in result.detail

    def test_a_captcha_widget_on_a_working_page_is_just_a_form(self):
        # The trap that mirrors Turnstile: login and comment forms carry a reCAPTCHA,
        # and treating one as an interstitial launches a browser on content that has
        # already arrived.
        body = (
            "<html><body><h1>Chapter 12</h1><p>Prose.</p>"
            '<script src="https://www.google.com/recaptcha/api.js"></script>'
            "</body></html>"
        )
        assert diagnose(status=200, body=body).ok

    def test_the_same_widget_on_a_refusal_is_a_challenge(self):
        body = '<html><body><script src="https://hcaptcha.com/1/api.js"></script></body></html>'
        result = diagnose(status=403, body=body)
        assert result.action is Action.SOLVE

    def test_a_404_behind_a_bot_manager_is_still_a_404(self):
        result = diagnose(status=404, headers={"x-datadome": "protected"}, body="Not found")
        assert result.action is Action.ACCEPT
        assert result.layer is None

    def test_a_cloudflare_error_code_still_wins_over_a_vendor_guess(self):
        result = diagnose(
            status=403,
            headers={"cf-ray": "abc", "set-cookie": "_pxhd=x"},
            body="<p>Error 1005</p>",
        )
        assert result.layer is Layer.IP_REPUTATION


class TestNamingTheEdge:
    def test_a_product_is_named_when_it_announces_itself(self):
        assert edge({"x-datadome": "protected"}) == "DataDome"
        assert edge({"server": "ddos-guard"}) == "DDoS-Guard"

    def test_cloudflare_is_named_from_its_ray_id(self):
        assert edge({"cf-ray": "abc123"}) == "Cloudflare"

    def test_an_ordinary_origin_reports_its_server(self):
        assert edge({"server": "nginx/1.24.0"}) == "nginx"

    def test_nothing_at_all_is_an_empty_string(self):
        assert edge({}) == ""
