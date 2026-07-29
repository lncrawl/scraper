"""Signing requests: the honest route through the one layer with no bypass.

A verifier checks a signature over the request under a private key, resolved against
a directory you publish. There is nothing to imitate, because the check is arithmetic
over a secret — which is also why a valid signature is the *cheapest* tier in the
whole stack: no browser, no proxy reputation, no pacing games.

Current deployments fail open, so an unsigned request is not blocked, it just gets
scored by everything else. Where a signature is required there is no bypass, only
registration.

Needs the `botauth` extra:  pip install lncrawl-scraper[botauth]

    uv run python examples/07_web_bot_auth.py
"""

import json
from pathlib import Path

from scraper import BotAuthConfig, BotAuthKey, Scraper, ScraperConfig
from scraper.botauth import DIRECTORY_PATH

key_path = Path("botauth.key")
if key_path.exists():
    key = BotAuthKey.load(key_path)
else:
    key = BotAuthKey.generate()
    key.save(key_path)  # written 0600
    print(f"generated a new key at {key_path}")

print("key id (JWK thumbprint):", key.key_id)
print()
print(f"serve this at {DIRECTORY_PATH} on a host you control:")
print(json.dumps(key.directory(), indent=2))

print()
signed = key.sign("https://example.com/some/page", agent="https://crawler.example/")
for name, value in signed.as_headers().items():
    print(f"{name}: {value}")

# Proof the setup works, without waiting for a site to tell you it does not.
print()
print("verifies:", key.verify("https://example.com/x", signed, agent="https://crawler.example/"))
print("but not for another host:", key.verify("https://elsewhere.test/x", signed))

config = ScraperConfig(
    botauth=BotAuthConfig(
        key=key,
        agent="https://crawler.example/",
        # Roll out one site at a time; empty signs everywhere.
        only_hosts=("example.com",),
    )
)
with Scraper(config=config) as scraper:
    scraper.get("https://example.com/")
