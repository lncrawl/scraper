"""Basic HTML scraping: fetch a page and pull data out with PageSoup.

Run:
    uv run python examples/01_basic_html.py
"""

from scraper import Scraper


def main() -> None:
    # `origin` is used to set sensible Origin/Referer headers automatically.
    s = Scraper(origin="https://example.com")

    # get_soup() returns a PageSoup — a null-safe BeautifulSoup wrapper.
    soup = s.get_soup("https://example.com")

    # Selection methods never return None; .text never raises.
    title = soup.select_one("h1").text
    paragraph = soup.select_one("p").text
    more_info_link = soup.select_one("a").get("href")  # "" if missing

    print("title:", title)
    print("paragraph:", paragraph[:60], "...")
    print("link:", more_info_link)

    # Missing elements degrade gracefully instead of throwing.
    missing = soup.select_one(".does-not-exist").text
    print("missing element text (safe):", repr(missing))


if __name__ == "__main__":
    main()
