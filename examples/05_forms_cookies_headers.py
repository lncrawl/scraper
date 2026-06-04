"""Forms, cookies, and custom headers.

Run:
    uv run python examples/05_forms_cookies_headers.py
"""

from scraper import Scraper


def main() -> None:
    s = Scraper(origin="https://httpbin.org")

    # --- Default headers / cookies for every subsequent request -----------
    s.set_header("X-Api-Key", "secret-token")
    s.set_cookie("session", "abc123")

    resp = s.get("https://httpbin.org/get")
    seen = resp.json()
    print("server saw header:", seen["headers"].get("X-Api-Key"))
    print("server saw cookie:", seen["headers"].get("Cookie"))

    # --- Submit a urlencoded form -----------------------------------------
    out = s.submit_form(
        "https://httpbin.org/post",
        data={"username": "reader", "password": "hunter2"},
    ).json()
    print("form fields echoed:", out.get("form"))

    # --- Submit multipart form data ---------------------------------------
    out = s.submit_form(
        "https://httpbin.org/post",
        data={"field": "value"},
        multipart=True,
    ).json()
    print("multipart content-type:", out["headers"].get("Content-Type", "")[:30])

    # --- post_soup: POST and parse the HTML response as PageSoup ----------
    # (use it when a POST returns an HTML page, e.g. a search results page)
    #   soup = s.post_soup("https://site.com/search", data={"q": "novel"})
    #   titles = [el.text for el in soup.select(".result .title")]

    # --- Reset clears cookies + headers back to a clean slate -------------
    s.reset()
    print("after reset, cookies:", len(list(s.cookies)))


if __name__ == "__main__":
    main()
