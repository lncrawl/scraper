"""Signed requests: the honest route through the one layer with no bypass."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from scraper.botauth import (
    ALGORITHM,
    DIRECTORY_PATH,
    TAG,
    BotAuthConfig,
    BotAuthKey,
    SignedRequest,
    authority,
    directory_document,
    signature_base,
)
from scraper.exceptions import ConfigError

pytest.importorskip("cryptography")


@pytest.fixture
def key() -> BotAuthKey:
    return BotAuthKey.generate()


class TestTheAuthorityComponent:
    """A verifier reconstructs this from the request it received.

    Every mismatch here produces a signature that fails with no diagnostic anywhere,
    so the default-port rule is worth pinning down.
    """

    def test_a_default_port_is_not_written_out(self):
        assert authority("https://example.com/path") == "example.com"
        assert authority("http://example.com/path") == "example.com"

    def test_a_non_default_port_is(self):
        assert authority("https://example.com:8443/x") == "example.com:8443"
        assert authority("http://example.com:8080/x") == "example.com:8080"

    def test_the_host_is_lowercased(self):
        assert authority("https://Example.COM/x") == "example.com"


class TestSigning:
    def test_a_signature_verifies_against_its_own_key(self):
        signing = BotAuthKey.generate()
        signed = signing.sign("https://example.com/page")
        assert signing.verify("https://example.com/page", signed)

    def test_a_signature_does_not_verify_for_another_authority(self):
        # The authority is covered precisely so a captured signature is useless
        # against a different host.
        signing = BotAuthKey.generate()
        signed = signing.sign("https://example.com/page")
        assert not signing.verify("https://elsewhere.test/page", signed)

    def test_another_key_cannot_verify_it(self):
        signed = BotAuthKey.generate().sign("https://example.com/")
        assert not BotAuthKey.generate().verify("https://example.com/", signed)

    def test_a_tampered_signature_fails(self):
        signing = BotAuthKey.generate()
        signed = signing.sign("https://example.com/")
        broken = SignedRequest(
            signature_input=signed.signature_input,
            signature=signed.signature[:-3] + "AA:",
        )
        assert not signing.verify("https://example.com/", broken)

    def test_a_malformed_signature_header_fails_closed(self):
        signing = BotAuthKey.generate()
        assert not signing.verify(
            "https://example.com/",
            SignedRequest(signature_input="sig1=()", signature="sig1=notwrapped"),
        )

    def test_mismatched_labels_fail(self):
        signing = BotAuthKey.generate()
        signed = signing.sign("https://example.com/")
        assert not signing.verify(
            "https://example.com/",
            SignedRequest(signature_input=signed.signature_input, signature="other=:AAAA:"),
        )

    def test_an_agent_is_covered_by_the_signature(self):
        # Covered rather than merely sent, so nothing in the path can swap it.
        signing = BotAuthKey.generate()
        signed = signing.sign("https://example.com/", agent="https://crawler.test/")
        assert signing.verify("https://example.com/", signed, agent="https://crawler.test/")
        assert not signing.verify("https://example.com/", signed, agent="https://impostor.test/")

    def test_the_headers_are_the_two_the_specification_names(self, key: BotAuthKey):
        headers = key.sign("https://example.com/").as_headers()
        assert set(headers) == {"signature-input", "signature"}


class TestSignatureParameters:
    def test_the_parameters_carry_the_window_and_the_algorithm(self):
        base, params = signature_base(
            "https://example.com/x", key_id="kid", created=1000, lifetime=300
        )
        assert "created=1000" in params
        assert "expires=1300" in params
        assert f'alg="{ALGORITHM}"' in params
        assert f'tag="{TAG}"' in params
        assert 'keyid="kid"' in params

    def test_the_parameters_are_the_last_line_of_what_is_signed(self):
        # They have to agree exactly, or the signature verifies against nothing.
        base, params = signature_base(
            "https://example.com/x", key_id="kid", created=1000, lifetime=60
        )
        assert base.splitlines()[-1] == f'"@signature-params": {params}'

    def test_the_authority_is_the_first_covered_component(self):
        base, _ = signature_base("https://example.com/x", key_id="k", created=1, lifetime=1)
        assert base.splitlines()[0] == '"@authority": example.com'

    def test_an_agent_adds_a_covered_component(self):
        base, params = signature_base(
            "https://example.com/x", key_id="k", created=1, lifetime=1, agent="https://c.test/"
        )
        assert '("signature-agent" "@authority")' in params
        assert '"signature-agent": https://c.test/' in base


class TestTheKey:
    def test_the_key_id_is_a_thumbprint_of_the_public_key(self, key: BotAuthKey):
        # Order and separators are load-bearing: any other serialisation yields a
        # different identifier and the directory stops matching the signatures.
        canonical = json.dumps(key.jwk, separators=(",", ":"), sort_keys=True)
        assert canonical.startswith('{"crv":"Ed25519","kty":"OKP","x":')
        assert key.key_id and "=" not in key.key_id

    def test_the_jwk_publishes_only_the_public_half(self, key: BotAuthKey):
        assert key.jwk["kty"] == "OKP"
        assert key.jwk["crv"] == "Ed25519"
        assert set(key.jwk) == {"kty", "crv", "x"}
        assert len(base64.urlsafe_b64decode(key.jwk["x"] + "==")) == 32

    def test_a_key_round_trips_through_a_file(self, tmp_path: Path, key: BotAuthKey):
        path = tmp_path / "botauth.key"
        key.save(path)
        assert BotAuthKey.load(path).key_id == key.key_id

    def test_a_saved_key_is_owner_only(self, tmp_path: Path, key: BotAuthKey):
        path = tmp_path / "botauth.key"
        key.save(path)
        if os.name != "nt":
            assert oct(path.stat().st_mode)[-3:] == "600"

    def test_a_wrong_sized_key_is_rejected_with_a_useful_message(self):
        with pytest.raises(ConfigError, match="32 bytes"):
            BotAuthKey(b"too short")


class TestTheDirectory:
    def test_the_document_publishes_the_key_under_its_thumbprint(self, key: BotAuthKey):
        document = key.directory()
        assert document["keys"][0]["kid"] == key.key_id
        assert "nbf" in document["keys"][0]

    def test_the_path_is_the_one_verifiers_fetch(self):
        assert DIRECTORY_PATH == "/.well-known/http-message-signatures-directory"

    def test_a_config_without_a_key_cannot_publish_one(self):
        with pytest.raises(ConfigError):
            directory_document(BotAuthConfig())


class TestConfig:
    def test_signing_is_off_until_a_key_is_given(self):
        config = BotAuthConfig()
        assert not config.enabled
        assert config.headers_for("https://example.com/") == {}

    def test_a_configured_key_signs_every_request(self, key: BotAuthKey):
        headers = BotAuthConfig(key=key).headers_for("https://example.com/")
        assert "signature" in headers and "signature-input" in headers

    def test_the_agent_is_sent_as_well_as_covered(self, key: BotAuthKey):
        headers = BotAuthConfig(key=key, agent="https://c.test/").headers_for("https://x.test/")
        assert headers["signature-agent"] == "https://c.test/"

    def test_signing_can_be_rolled_out_one_host_at_a_time(self, key: BotAuthKey):
        config = BotAuthConfig(key=key, only_hosts=("example.com",))
        assert config.headers_for("https://example.com/") != {}
        assert config.headers_for("https://elsewhere.test/") == {}

    def test_a_subdomain_of_an_allowed_host_is_included(self, key: BotAuthKey):
        config = BotAuthConfig(key=key, only_hosts=("example.com",))
        assert config.headers_for("https://api.example.com/") != {}
