"""The ergonomic surface: soup, JSON, forms, files, images.

Every one of these goes through the same retrieval loop, so a challenged site is
handled identically whether you asked for HTML or a cover image.

    uv run python examples/10_files_and_soup.py
"""

from pathlib import Path

from scraper import PageSoup, Scraper

out = Path("downloads")

with Scraper(origin="https://example.com") as scraper:
    scraper.headers["accept-language"] = "en-GB,en;q=0.9"

    soup = scraper.get_soup("https://example.com/")
    print(soup.select_one("h1").text)
    print(soup.select_one("p").text[:60])

    # Never None: an empty PageSoup is falsy and its accessors return "".
    missing = soup.select_one("#nope")
    print(bool(missing), repr(missing.text), repr(missing.get_attr("href")))

    # Sub-resources are marked as such: different fetch metadata, and they stay out of
    # the referrer chain, because a chain threaded through every image is not one a
    # browser produces.
    scraper.get_file("https://example.com/", out / "page.html")
    print("saved", (out / "page.html").stat().st_size, "bytes")

    # data = scraper.get_json("https://example.com/api/items")
    # response = scraper.submit_form("https://example.com/search", data={"q": "hello"})
    # image = scraper.get_image("https://example.com/cover.jpg")   # needs the image extra

# Parsing without fetching.
standalone = PageSoup.create("<div class='a'><span>hi</span></div>")
print(standalone.select_one(".a span").text)
print([node.name for node in standalone.select_one(".a")])
