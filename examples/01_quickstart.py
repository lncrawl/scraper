"""The shortest useful program.

Nothing is configured. The default transport already reproduces a real browser's
TLS and HTTP/2 fingerprint, which is what clears the four transport layers that
most protected sites stop at.

    uv run python examples/01_quickstart.py
"""

from scraper import Scraper

with Scraper(origin="https://example.com") as scraper:
    soup = scraper.get_soup("https://example.com/")
    print(soup.select_one("h1").text)

    # Selection never returns None, so chained access is always safe.
    print(repr(soup.select_one(".does-not-exist").text))

    # Links a person could actually click: hidden and nofollow anchors are dropped,
    # which is the whole defence against a decoy maze.
    for link in scraper.links(soup):
        print(link.url, "|", link.text)
