"""Identity coherence, and the binding that makes a clearance reusable."""

from __future__ import annotations

import time

from scraper.identity import OVERRIDABLE, Clearance, Identity, browser_family, client_hints


class TestTheBinding:
    """A clearance is bound to address, User-Agent and TLS profile together.

    Everything in this class is one failure mode: solving on one identity and
    fetching on another. It produces a challenge, which reads as "the solver is
    broken", which leads to re-solving forever on a fresh address each time.
    """

    def test_the_token_covers_exactly_the_three_bound_things(self):
        base = Identity(impersonate="chrome", exit_id="e1", user_agent="UA")
        assert base.token() == Identity(impersonate="chrome", exit_id="e1", user_agent="UA").token()
        assert base.token() != base.on_exit("e2").token()
        assert base.token() != base.pin("other UA").token()
        assert (
            base.token() != Identity(impersonate="firefox", exit_id="e1", user_agent="UA").token()
        )

    def test_the_token_ignores_things_a_clearance_does_not_depend_on(self):
        # A field that the server never sees must not invalidate cookies. Birth time
        # is bookkeeping, so two identities differing only in it are the same identity.
        first = Identity(impersonate="chrome", exit_id="e1", born_at=1.0)
        second = Identity(impersonate="chrome", exit_id="e1", born_at=9999.0)
        assert first.token() == second.token()

    def test_a_clearance_is_usable_by_the_identity_that_earned_it(self):
        identity = Identity(exit_id="e1").pin("Mozilla/5.0 Chrome/140")
        clearance = Clearance(
            origin="https://example.com/",
            cookies={"cf_clearance": "abc"},
            identity_token=identity.token(),
            expires_at=time.time() + 600,
        )
        assert clearance.usable_by(identity)
        assert clearance.why_not(identity) == ""

    def test_rotating_the_address_kills_the_clearance(self):
        identity = Identity(exit_id="e1").pin("UA")
        clearance = Clearance(
            origin="https://example.com/",
            cookies={"cf_clearance": "abc"},
            identity_token=identity.token(),
            expires_at=time.time() + 600,
        )
        moved = identity.on_exit("e2")
        assert not clearance.usable_by(moved)
        assert "identity changed" in clearance.why_not(moved)

    def test_an_expired_clearance_is_not_usable(self):
        identity = Identity(exit_id="e1")
        clearance = Clearance(
            origin="https://example.com/",
            cookies={"cf_clearance": "abc"},
            identity_token=identity.token(),
            expires_at=time.time() - 1,
        )
        assert clearance.expired
        assert not clearance.usable_by(identity)
        assert "expired" in clearance.why_not(identity)

    def test_no_expiry_means_no_expiry_check(self):
        identity = Identity(exit_id="e1")
        clearance = Clearance(
            origin="https://example.com/", cookies={"x": "y"}, identity_token=identity.token()
        )
        assert not clearance.expired


class TestHeaderOverrides:
    """The profile owns the header set. We may replace values, never add headers."""

    def test_nothing_is_overridden_until_a_browser_pins_a_user_agent(self):
        # The inversion that matters: the impersonation profile's own User-Agent
        # stands, because writing one over it is how a client claims to be one
        # browser while its ClientHello says another.
        assert Identity(impersonate="chrome").header_overrides() == {}

    def test_pinning_a_user_agent_re_derives_the_client_hints(self):
        pinned = Identity().pin("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0")
        overrides = pinned.header_overrides()
        assert overrides["user-agent"].endswith("Chrome/140.0.0.0")
        assert '"140"' in overrides["sec-ch-ua"]
        assert overrides["sec-ch-ua-platform"] == '"Windows"'
        assert overrides["sec-ch-ua-mobile"] == "?0"

    def test_overrides_stay_inside_the_allow_list(self):
        pinned = Identity(accept_language="en-GB").pin("Chrome/140")
        assert set(pinned.header_overrides()) <= OVERRIDABLE


class TestClientHints:
    def test_firefox_gets_no_hints_because_firefox_sends_none(self):
        # Inventing hints for a browser that does not send them is a contradiction a
        # detector reads for free.
        assert client_hints("Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/141.0") == {}

    def test_a_mobile_chrome_is_marked_mobile(self):
        hints = client_hints("Mozilla/5.0 (Linux; Android 14) Chrome/140.0.0.0 Mobile Safari/537")
        assert hints["sec-ch-ua-mobile"] == "?1"
        assert hints["sec-ch-ua-platform"] == '"Android"'

    def test_macos_is_named_the_way_chrome_names_it(self):
        hints = client_hints("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/140.0.0.0")
        assert hints["sec-ch-ua-platform"] == '"macOS"'

    def test_a_non_browser_string_yields_nothing(self):
        assert client_hints("curl/8.5.0") == {}
        assert client_hints("") == {}


def test_families_are_read_off_the_target():
    assert browser_family("chrome136") == "chrome"
    assert browser_family("firefox135") == "firefox"
    assert browser_family("safari18_4") == "safari"
    assert browser_family("edge101") == "edge"
    assert browser_family("") == "chrome"


def test_describe_is_readable():
    described = Identity(impersonate="chrome", exit_id="pool#s-1").describe()
    assert "chrome" in described and "pool#s-1" in described
    assert "direct" in Identity().describe()
