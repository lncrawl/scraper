"""Proxy rotation and Tor integration.

Multiple proxy URLs are cycled round-robin. With a Tor SOCKS proxy you can also
request a fresh exit circuit (SIGNAL NEWNYM) via the control port; this happens
automatically on proxy/403 errors, and `fallback_to_direct` retries directly if
all proxies are unavailable.

Run:
    uv run python examples/09_proxies_and_tor.py
"""

import time

from scraper import ProxyUrl, Scraper, TorProxyUrl, default_config


def main() -> None:
    config = default_config()
    config.proxy.failure_tolerance = 1
    config.proxy.disable_cooldown = 3
    config.proxy.proxy_urls += [
        "http://user:pass@proxy1.example:8080",
        ProxyUrl(
            url="http://user:pass@proxy2.example:8080",
            http_only=True,
        ),
        TorProxyUrl(
            url="socks5://127.0.0.1:9150",
            control_host="127.0.0.1",
            control_port=9151,
            control_password="",  # set if your torrc requires it
        ),
    ]
    s = Scraper(config=config)

    # First url proxy
    print("proxy configured:", s.proxy_manager.has_proxy)
    print("current proxy:", s.proxy_manager.get_proxy())

    # Report failure to try next proxy (scond url proxy)
    s.proxy_manager.report_failure()
    print("current proxy:", s.proxy_manager.get_proxy())

    # Report failure to try next proxy (this is tor proxy)
    s.proxy_manager.report_failure()
    print("current proxy:", s.proxy_manager.get_proxy())

    # Report failure to try tor identity rotation
    # on failure it moves to the first proxy again in circular manner
    s.proxy_manager.report_failure()
    print("current proxy:", s.proxy_manager.get_proxy())
    print("-" * 25)

    # Now let's report subsequent failure which should disable all proxies
    s.proxy_manager.report_failure()
    s.proxy_manager.report_failure()
    s.proxy_manager.report_failure()
    print("current proxy:", s.proxy_manager.get_proxy())
    print("-" * 25)

    # wait for cooldown to get one enabled again
    time.sleep(3)
    print("current proxy:", s.proxy_manager.get_proxy())


if __name__ == "__main__":
    main()
