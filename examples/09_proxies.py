"""Proxy rotation and Tor integration.

Multiple proxy URLs are cycled round-robin. With a Tor SOCKS proxy you can also
request a fresh exit circuit (SIGNAL NEWNYM) via the control port; this happens
automatically on proxy/403 errors, and `fallback_to_direct` retries directly if
all proxies are unavailable.

Run:
    uv run python examples/09_proxies_and_tor.py
"""

import time

from scraper import ProxyUrl, Scraper, default_config


def main() -> None:
    config = default_config()
    config.proxy.disable_cooldown = 1
    config.proxy.proxy_urls += [
        "http://user:pass@proxy1.example:8080",
        ProxyUrl(url="http://user:pass@proxy2.example:8080"),
    ]
    s = Scraper(config=config)

    # First url proxy
    print("proxy configured:", s.proxy_manager.has_proxy)
    print("current proxy:", s.proxy_manager.get_proxy())

    # Report failure to try next proxy (scond url proxy)
    s.proxy_manager.disable_current()
    s.proxy_manager.rotate()
    print("current proxy:", s.proxy_manager.get_proxy())

    # Report failure to try next proxy (this will disable all)
    s.proxy_manager.disable_current()
    s.proxy_manager.rotate()
    print("current proxy:", s.proxy_manager.get_proxy())

    # wait for cooldown to get one enabled again
    time.sleep(1)
    print("current proxy:", s.proxy_manager.get_proxy())


if __name__ == "__main__":
    main()
