"""Browser fingerprint impersonation (curl_cffi).

A plain requests stack has a fixed OpenSSL TLS fingerprint and only speaks
HTTP/1.1 — both of which modern Cloudflare detects. The curl_cffi transport
reproduces a real browser's TLS (JA3/JA4) and HTTP/2 fingerprint, and
`default_config()` enables it (impersonate.target = "chrome") out of the box.
curl_cffi is a core dependency, so no extra install is needed; if it is somehow
unavailable the engine falls back to the httpx transport.

Run:
    uv run python examples/07_impersonation.py
"""

import json

from scraper import Scraper, default_config


def main() -> None:
    config = default_config()
    # Any curl-impersonate target: "chrome", "firefox", "chrome124", "safari", ...
    config.impersonate.target = "chrome"

    s = Scraper(origin="https://example.com", config=config)

    # The spoofed UA family and Client Hints are auto-aligned to the target.
    print("configured headers:", json.dumps(dict(s.headers), indent=2))

    # Requests now go out with a real Chrome TLS + HTTP/2 fingerprint.
    response = s.get("https://example.com")
    print("request headers:", json.dumps(dict(response.request.headers), indent=2))

    soup = s.make_soup(response)
    print("page title:", soup.select_one("h1").text)


if __name__ == "__main__":
    main()
