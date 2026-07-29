"""tor-pool: many Tor exits behind one sticky endpoint.

A single Tor gives you one exit IP and a ~10 second cooldown between circuit
changes. `tor-pool <https://github.com/lncrawl/tor-pool>`_ runs a pool of Tor
instances behind one SOCKS port: the SOCKS5 username is a session key, you stay
on the same exit until you ask to move, and rotating is near-instant because it
reassigns you to an instance that has already built its circuits.

Start one first:

    docker run -d --name tor-pool \\
      -e POOL_SIZE=5 -v tor_data:/var/lib/tor \\
      -p 127.0.0.1:9250:9250 -p 127.0.0.1:8080:8080 \\
      ghcr.io/lncrawl/tor-pool:latest

It prints a proxy token once, on its first boot:

    docker logs tor-pool | grep 'proxy token'

Then run:

    TOR_POOL_TOKEN=tp_... uv run python examples/11_tor_pool.py
"""

import os

from scraper import Scraper, TorPoolProxyUrl, default_config

TOKEN = os.environ.get("TOR_POOL_TOKEN", "")


def exit_ip(s: Scraper) -> str:
    return s.get_json("https://check.torproject.org/api/ip").get("IP", "?")


def main() -> None:
    if not TOKEN:
        raise SystemExit("set TOR_POOL_TOKEN — tor-pool 0.2+ requires a proxy token")

    config = default_config()
    config.proxy.proxy_urls = [
        TorPoolProxyUrl(
            url="socks5h://127.0.0.1:9250",
            api_url="http://127.0.0.1:8080",
            # The pool's proxy credential. It is the SOCKS5 password and the
            # bearer token on the pool's API — one token authenticates both.
            token=TOKEN,
            # Omit to get a generated key, so each Scraper is its own session.
            session="example",
        )
    ]
    s = Scraper(config=config)

    print("--- sticky: the same exit across requests ---")
    for i in range(3):
        print(f"  request {i + 1}: {exit_ip(s)}")

    print("\n--- rotate: a different instance, immediately ---")
    s.proxy_manager.rotate()
    print(f"  after rotate: {exit_ip(s)}")

    # Report a block so the pool can score that exit. After enough failures it
    # quarantines the instance and moves every session off it — the balancer
    # cannot see a 403 inside an HTTPS tunnel, so this is the only way it finds
    # out. The engine reports 403s, challenges, rate limits and transport errors
    # for you; call it directly when your own code detects a soft block.
    #
    # What you report decides how much it counts: the pool weighs a captcha as
    # evidence the exit IP is burnt, and a 429 as evidence it is working and
    # merely busy. Reporting a throttle as a block spends a healthy exit.
    print("\n--- report a block ---")
    s.proxy_manager.report_failure("http_403")
    print("  reported as 'blocked'; the pool now counts that exit as failing")

    print("\n--- a second session gets its own exit ---")
    other = default_config()
    other.proxy.proxy_urls = [TorPoolProxyUrl(token=TOKEN, session="example-2")]
    print(f"  other session: {exit_ip(Scraper(config=other))}")


if __name__ == "__main__":
    main()
