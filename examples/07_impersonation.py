"""Browser fingerprint impersonation (curl_cffi).

A plain requests stack has a fixed OpenSSL TLS fingerprint and only speaks
HTTP/1.1 — both of which modern Cloudflare detects. Setting `impersonate`
routes requests through curl_cffi to reproduce a real browser's TLS (JA3/JA4)
and HTTP/2 fingerprint.

Requires the `impersonate` extra:
    pip install "lncrawl-scraper[impersonate]"

Run:
    uv run python examples/07_impersonation.py
"""

from scraper import Scraper, default_config


def main() -> None:
    config = default_config()
    # Any curl-impersonate target: "chrome", "firefox", "chrome124", "safari", ...
    config.impersonate = "chrome"

    s = Scraper(origin="https://example.com", config=config)

    # The spoofed UA family and Client Hints are auto-aligned to the target.
    print("User-Agent:", s.headers["User-Agent"])
    print("sec-ch-ua:", s.headers.get("sec-ch-ua"))

    # Requests now go out with a real Chrome TLS + HTTP/2 fingerprint.
    soup = s.get_soup("https://example.com")
    print("page title:", soup.select_one("h1").text)


if __name__ == "__main__":
    main()
