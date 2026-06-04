"""Proxy rotation and Tor integration.

Multiple proxy URLs are cycled round-robin. With a Tor SOCKS proxy you can also
request a fresh exit circuit (SIGNAL NEWNYM) via the control port; this happens
automatically on proxy/403 errors, and `fallback_to_direct` retries directly if
all proxies are unavailable.

Run:
    uv run python examples/09_proxies_and_tor.py
"""

from scraper import ProxyConfig, Scraper, ScraperConfig, default_config


def http_proxies() -> Scraper:
    config = ScraperConfig(
        proxy=ProxyConfig(
            proxy_urls=[
                "http://user:pass@proxy1.example:8080",
                "http://user:pass@proxy2.example:8080",
            ],
            fallback_to_direct=True,
        )
    )
    return Scraper(config=config)


def tor() -> Scraper:
    # Works with e.g. peterdavehello/tor-socks-proxy.
    config = default_config()
    config.proxy = ProxyConfig(
        proxy_urls=["socks5://127.0.0.1:9150"],
        fallback_to_direct=True,
        tor_control_host="127.0.0.1",
        tor_control_port=9151,
        tor_control_password="",  # set if your torrc requires it
    )
    return Scraper(config=config)


def main() -> None:
    s = http_proxies()
    print("proxy configured:", s.proxy_manager.has_proxy)

    # Manually request a new identity (no-op without a Tor control port).
    t = tor()
    t.proxy_manager.rotate_identity()
    print("tor scraper ready (control port:", t.config.proxy.tor_control_port, ")")


if __name__ == "__main__":
    main()
