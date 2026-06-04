"""Reusing a Cloudflare clearance solved by a real browser.

Managed challenges / Turnstile can't be solved headlessly. The robust pattern:
solve the challenge ONCE in a real browser (nodriver, Playwright, Selenium,
or even by hand in your own browser's devtools), then hand the resulting
`cf_clearance` cookie + the browser's EXACT User-Agent to the lightweight
scraper session so it can keep reusing the cleared session.

The User-Agent MUST match the one used to obtain the clearance, or Cloudflare
will reject the cookie.

Run:
    uv run python examples/08_browser_clearance.py
"""

from scraper import Scraper


def get_clearance_from_browser(url: str) -> dict:
    """Stand-in for your real-browser automation.

    In practice this would drive nodriver/Playwright to `url`, wait for the
    challenge to clear, then read document.cookie and navigator.userAgent.
    """
    return {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "cf_clearance": "EXAMPLE_CLEARANCE_TOKEN",
        "extra_cookies": {"__cf_bm": "EXAMPLE_BM_TOKEN"},
    }


def main() -> None:
    target = "https://protected.example.com"

    s = Scraper(origin=target)

    creds = get_clearance_from_browser(target)
    s.apply_browser_clearance(
        target,
        cf_clearance=creds["cf_clearance"],
        user_agent=creds["user_agent"],
        cookies=creds["extra_cookies"],
    )

    # The session now carries the clearance cookie + matching UA.
    print("User-Agent set to:", s.headers["User-Agent"])
    print("cf_clearance:", s.cookies.get("cf_clearance", domain="protected.example.com"))

    # Subsequent requests reuse the cleared session:
    #   soup = s.get_soup(f"{target}/novel/123")

    # Tip: pair this with impersonate="chrome" so the TLS fingerprint also
    # matches the browser that obtained the clearance.


if __name__ == "__main__":
    main()
