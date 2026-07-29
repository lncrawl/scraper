# Signed requests

This is the documentation for layer 18, and it is the only page here about making a site
*want* to serve you rather than about not being noticed.

A verifier checks a signature over the request, made with a private key, resolved against a
directory you publish. There is nothing to imitate, because the check is arithmetic over a
secret. That is why the rest of this library treats a mandated signature as a stop rather than
an obstacle — and why implementing it is worth doing anyway.

**Current deployments fail open.** An unsigned request is not blocked; it falls back to being
scored by everything else. A valid signature is a *positive identification* that skips the
challenge machinery entirely. So for a crawler willing to say who it is, this is the cheapest
tier in the whole stack: no browser, no address reputation, no pacing games. It is also the
direction enforcement is moving.

Built on HTTP Message Signatures (RFC 9421) with Ed25519 and the `web-bot-auth` tag. Needs the
`botauth` extra.

## Setting up

```python
from pathlib import Path
from scraper import BotAuthConfig, BotAuthKey, Scraper, ScraperConfig

key_path = Path("botauth.key")
key = BotAuthKey.load(key_path) if key_path.exists() else BotAuthKey.generate()
key.save(key_path)                     # written 0600

config = ScraperConfig(
    botauth=BotAuthConfig(
        key=key,
        agent="https://crawler.example/",   # a URL identifying who is crawling
    )
)
```

Every request then carries `Signature-Input` and `Signature`. `only_hosts` restricts signing to
named hosts, for rolling out one site at a time; empty signs everywhere, which is usually what
a declared crawler wants.

## Publishing the key

Serve `key.directory()` as JSON at `scraper.botauth.DIRECTORY_PATH` on a host you control. That
path is fixed by the specification — a verifier fetches it to resolve the `keyid` in your
signatures.

```python
from scraper.botauth import DIRECTORY_PATH
app.route(DIRECTORY_PATH)(lambda: key.directory())
```

The `keyid` is the JWK thumbprint (RFC 7638): SHA-256 over the canonical, lexicographically
ordered, whitespace-free JSON of the required members. Any other serialisation produces a
different identifier and the directory silently stops matching the signatures, which is why
`BotAuthKey.key_id` computes it rather than letting you supply one.

Publishing the key is necessary but not sufficient. The operator has to be **registered** with
the verifiers that matter; a signature nobody can resolve is just two extra headers.

## What is signed

`@authority` plus a bounded `created`/`expires` window — the minimum that makes a captured
signature useless against a different host and useless later. An `agent`, when configured, is
covered too, so nothing in the path can swap it.

The window is short (`scraper.botauth.DEFAULT_LIFETIME`) on purpose: it is the only thing
limiting replay, since the signature covers the authority but not the path.

## Checking your setup

`BotAuthKey.verify` exists so you can prove the signing works before a site tells you it does
not:

```python
signed = key.sign("https://example.com/page", agent="https://crawler.example/")
key.verify("https://example.com/page", signed, agent="https://crawler.example/")   # True
key.verify("https://elsewhere.test/page", signed)                                  # False
```

The second call failing is the property that matters: the authority is covered, so a captured
signature is useless somewhere else.

## When you see a signature requirement

A site that mandates one answers with `WWW-Authenticate: Signature`. `scraper.diagnose` maps
that to layer 18 and the pipeline raises `scraper.Impassable` immediately rather than retrying —
because if you already have a key configured and still see this, your key is not accepted, and
no number of attempts changes that. Register as a verified agent.
