"""Proxy rotation and Tor integration.

Multiple proxy URLs are cycled round-robin. With a Tor SOCKS proxy you can also
request a fresh exit circuit (SIGNAL NEWNYM) via the control port; this happens
automatically on proxy/403 errors, and `fallback_to_direct` retries directly if
all proxies are unavailable.

Run:
    uv run python examples/14_tor_proxy.py
"""

from scraper import Scraper, TorProxyUrl, default_config


def main() -> None:
    config = default_config()
    config.proxy.proxy_urls = [
        TorProxyUrl(
            url="socks5h://127.0.0.1:9150",
            control_host="127.0.0.1",
            control_port=9151,
            control_password="changeme",
        ),
    ]
    s = Scraper(config=config)

    # Get the initial request using proxy
    print("current proxy:", s.proxy_manager.get_proxy())
    data = s.get_json("https://httpbin.io/ip")
    print("current ip:", data)

    print("---subsequent request---")
    # The proxy should remain same on the subsequent requests
    print("current proxy:", s.proxy_manager.get_proxy())
    data = s.get_json("https://httpbin.io/ip")
    print("current ip:", data)

    print("---rotate and request ---")
    # Request a new Tor exit circuit and reset the connection pool so the next
    # request opens a fresh TCP connection through the new circuit.
    s.rotate_proxy()

    print("current proxy:", s.proxy_manager.get_proxy())
    data = s.get_json("https://httpbin.io/ip")
    print("current ip:", data)


if __name__ == "__main__":
    main()
