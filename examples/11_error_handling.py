"""Error handling: HTTP errors, Cloudflare failures, and aborts.

`Scraper.get/post` call `raise_for_status()`, so non-2xx responses raise
`httpx.HTTPStatusError`. Cloudflare-specific failures raise `CloudflareException`
(or a subclass); aborts raise `AbortedException`.

Run:
    uv run python examples/11_error_handling.py
"""

import httpx

from scraper import AbortedException, CloudflareException, Scraper


def main() -> None:
    s = Scraper()

    # --- HTTP status errors -----------------------------------------------
    try:
        s.get("https://httpbin.org/status/404")
    except httpx.HTTPStatusError as exc:
        print("HTTP error:", exc.response.status_code)

    # --- Catch-all order: AbortedException is a CloudflareException --------
    try:
        s.get_soup("https://example.com")
        print("fetched ok")
    except AbortedException:
        print("aborted")
    except CloudflareException as exc:
        # Raised when a CF challenge cannot be solved (loop protection, etc.).
        print("cloudflare could not be solved:", type(exc).__name__)
    except httpx.HTTPError as exc:
        print("network error:", type(exc).__name__)


if __name__ == "__main__":
    main()
