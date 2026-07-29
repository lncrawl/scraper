"""Signed requests: the one layer with no bypass, and the way through it.

Every other layer in the model reads something a client can either reproduce or
accumulate. This one reads a signature over the request under a private key,
verified against a public directory the edge fetches. There is nothing to imitate,
because the check is arithmetic over a secret. Where a site requires it, the only
route is to hold a key and be registered — which is why the rest of this library
treats that case as a stop rather than a retry.

The reason it is worth *implementing* rather than merely refusing: current
deployments fail open. An unsigned request is not blocked, it just falls back to
being scored by everything else. A valid signature is a positive identification
that skips the challenge machinery entirely. So for a crawler willing to say who
it is, this is the cheapest tier in the whole stack — no browser, no proxy
reputation, no pacing games — and it is the direction enforcement is moving.

Built on HTTP Message Signatures (RFC 9421) with Ed25519, and the ``web-bot-auth``
tag. The signature covers ``@authority`` plus a bounded validity window, which is
the minimum that makes a captured signature useless somewhere else and useless
later.

Needs the ``botauth`` extra.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .exceptions import ConfigError, MissingDependency

DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
"""Where a verifier fetches the public key from. Fixed by the specification."""

TAG = "web-bot-auth"
ALGORITHM = "ed25519"
DEFAULT_LIFETIME = 300.0
"""Seconds a signature stays valid.

Short on purpose: the window is the only thing limiting replay, since the
signature covers the authority but not the path.
"""

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _b64url(raw: bytes) -> str:
    """Unpadded base64url, which is what every field in this specification uses."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:
        raise MissingDependency("botauth", "signing requests for Web Bot Auth") from exc
    return ed25519


def authority(url: str) -> str:
    """The ``@authority`` derived component for *url*.

    Host, lowercased, with the port only when it is not the scheme's default —
    a verifier reconstructs this from the request it received, so an extra ``:443``
    here makes every signature fail with no useful error anywhere.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        return f"{host}:{port}"
    return host


@dataclass(frozen=True)
class SignedRequest:
    """The two headers that carry a signature."""

    signature_input: str
    signature: str

    def as_headers(self) -> Dict[str, str]:
        return {"signature-input": self.signature_input, "signature": self.signature}


class BotAuthKey:
    """An Ed25519 signing key, plus the directory document that publishes it.

    Args:
        private_bytes: 32 raw private-key bytes. Use :meth:`generate` or
            :meth:`load` rather than constructing this by hand.
        label: The signature label. ``sig1`` unless you are carrying more than
            one signature on a request.
    """

    def __init__(self, private_bytes: bytes, *, label: str = "sig1") -> None:
        ed25519 = _ed25519()
        if len(private_bytes) != 32:
            raise ConfigError(f"an Ed25519 private key is 32 bytes, got {len(private_bytes)}")
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes)
        self.label = label

    @classmethod
    def generate(cls, *, label: str = "sig1") -> "BotAuthKey":
        ed25519 = _ed25519()
        from cryptography.hazmat.primitives import serialization

        raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return cls(raw, label=label)

    @classmethod
    def load(cls, path: Path, *, label: str = "sig1") -> "BotAuthKey":
        """Read a key written by :meth:`save`."""
        return cls(_unb64url(Path(path).read_text("utf-8").strip()), label=label)

    def save(self, path: Path) -> None:
        """Write the private key, owner-readable only."""
        from cryptography.hazmat.primitives import serialization

        raw = self._key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_b64url(raw), "utf-8")
        target.chmod(0o600)

    @property
    def public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization

        return self._key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def jwk(self) -> Dict[str, str]:
        """The public key as a JWK."""
        return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(self.public_bytes)}

    @property
    def key_id(self) -> str:
        """The JWK thumbprint (RFC 7638), which is the ``keyid`` a verifier matches.

        The member order below is not cosmetic: a thumbprint is defined over the
        lexicographically ordered, whitespace-free JSON of the required members, so
        any other serialisation produces a different identifier and the directory
        stops matching the signatures.
        """
        canonical = json.dumps(self.jwk, separators=(",", ":"), sort_keys=True)
        return _b64url(hashlib.sha256(canonical.encode("utf-8")).digest())

    def directory(self, *, valid_for: Optional[float] = None) -> Dict[str, Any]:
        """The document to serve at :data:`DIRECTORY_PATH`."""
        key: Dict[str, Any] = dict(self.jwk)
        key["kid"] = self.key_id
        key["nbf"] = int(time.time())
        if valid_for:
            key["exp"] = int(time.time() + valid_for)
        return {"keys": [key]}

    def sign(
        self,
        url: str,
        *,
        created: Optional[int] = None,
        lifetime: float = DEFAULT_LIFETIME,
        agent: str = "",
    ) -> SignedRequest:
        """Sign a request to *url*.

        Args:
            agent: Optional ``Signature-Agent`` value identifying the operator. When
                given it is covered by the signature, so it cannot be swapped by
                anything in the path.
        """
        base, params = signature_base(
            url,
            key_id=self.key_id,
            created=created if created is not None else int(time.time()),
            lifetime=lifetime,
            agent=agent,
        )
        raw = self._key.sign(base.encode("utf-8"))
        return SignedRequest(
            signature_input=f"{self.label}={params}",
            signature=f"{self.label}=:{base64.b64encode(raw).decode('ascii')}:",
        )

    def verify(self, url: str, signed: SignedRequest, *, agent: str = "") -> bool:
        """Check a signature this key produced.

        Here so a caller can prove the setup works before a site does it for them,
        and so the round trip is testable without a network.
        """
        from cryptography.exceptions import InvalidSignature

        try:
            label, _, params = signed.signature_input.partition("=")
            sig_label, _, encoded = signed.signature.partition("=")
            if label != sig_label or not encoded.startswith(":") or not encoded.endswith(":"):
                return False
            raw = base64.b64decode(encoded[1:-1])
            base = _assemble(url, _covered(params), params, agent=agent)
            self._key.public_key().verify(raw, base.encode("utf-8"))
            return True
        except (InvalidSignature, ValueError, IndexError):
            return False


def signature_base(
    url: str,
    *,
    key_id: str,
    created: int,
    lifetime: float,
    agent: str = "",
) -> Tuple[str, str]:
    """Build the string to sign and the ``@signature-params`` that describe it.

    Returned together because they must agree exactly: the parameters are
    themselves the last line of the signed string, so a mismatch between what was
    signed and what is advertised produces a signature that verifies against
    nothing.
    """
    covered: List[str] = ['"@authority"']
    if agent:
        covered.insert(0, '"signature-agent"')
    expires = int(created + lifetime)
    params = (
        f"({' '.join(covered)});created={created};expires={expires};"
        f'keyid="{key_id}";alg="{ALGORITHM}";tag="{TAG}"'
    )
    components = [component.strip('"') for component in covered]
    return _assemble(url, components, params, agent=agent), params


def _covered(params: str) -> List[str]:
    """The component names listed in a ``@signature-params`` string."""
    inner = params[params.find("(") + 1 : params.find(")")]
    return [item.strip('"') for item in inner.split() if item]


def _assemble(url: str, components: List[str], params: str, *, agent: str = "") -> str:
    lines = []
    for name in components:
        if name == "@authority":
            lines.append(f'"@authority": {authority(url)}')
        elif name == "signature-agent":
            lines.append(f'"signature-agent": {agent}')
        else:
            raise ConfigError(f"unsupported signature component: {name!r}")
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines)


@dataclass
class BotAuthConfig:
    """Sign outgoing requests as a declared, verifiable agent.

    Args:
        key: The signing key. Its public half must be reachable at
            :data:`DIRECTORY_PATH` on a host you control, and the operator has to
            be registered with the verifiers that matter — a signature no one can
            resolve is just two extra headers.
        agent: Optional ``Signature-Agent``: a URL identifying who is crawling.
        lifetime: Validity window, in seconds.
        only_hosts: Restrict signing to these hosts. Empty signs everywhere, which
            is usually what a declared crawler wants; a narrower set is for
            rolling it out one site at a time.
    """

    key: Optional[BotAuthKey] = None
    agent: str = ""
    lifetime: float = DEFAULT_LIFETIME
    only_hosts: Tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.key is not None

    def headers_for(self, url: str) -> Dict[str, str]:
        """Signature headers for *url*, or an empty mapping when not signing."""
        if self.key is None:
            return {}
        host = authority(url).split(":")[0]
        if self.only_hosts and not any(
            host == allowed or host.endswith(f".{allowed}") for allowed in self.only_hosts
        ):
            return {}
        signed = self.key.sign(url, lifetime=self.lifetime, agent=self.agent)
        out = signed.as_headers()
        if self.agent:
            out["signature-agent"] = self.agent
        return out


def directory_document(config: BotAuthConfig) -> Mapping[str, Any]:
    """The JSON to publish at :data:`DIRECTORY_PATH` for *config*."""
    if config.key is None:
        raise ConfigError("no signing key configured")
    return config.key.directory()
