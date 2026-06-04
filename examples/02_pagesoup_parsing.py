"""PageSoup tour: selection, attributes, navigation, and XPath.

PageSoup wraps a BeautifulSoup Tag. Every selection returns a PageSoup (or a
list of them) and every text/HTML accessor returns a str — never None — so
chained access is always safe.

Run:
    uv run python examples/02_pagesoup_parsing.py
"""

from scraper import PageSoup

HTML = """
<html>
  <body>
    <div class="book" data-id="42">
      <h1 class="title">The Great Novel</h1>
      <span class="author">Jane Doe</span>
      <ul class="chapters">
        <li><a href="/c/1">Chapter 1</a></li>
        <li><a href="/c/2">Chapter 2</a></li>
        <li><a href="/c/3">Chapter 3</a></li>
      </ul>
    </div>
  </body>
</html>
"""


def main() -> None:
    # Build a PageSoup directly from a string (or bytes, or a requests.Response).
    soup = PageSoup.create(HTML)

    book = soup.select_one("div.book")

    # --- Attributes -------------------------------------------------------
    print("data-id attr:", book.get("data-id"))  # "42"
    print("has class?:", book.has_attr("class"))  # True

    # --- Text extraction --------------------------------------------------
    print("title:", book.select_one(".title").text)
    print("author:", book.select_one(".author").text)

    # --- Selecting many ---------------------------------------------------
    links = soup.select("ul.chapters a")
    print("chapter count:", len(links))
    for a in links:
        print(f"  {a.text} -> {a.get('href')}")

    # --- `in` operator (CSS membership) -----------------------------------
    if ".author" in book:
        print("book has an author element")

    # --- Tree navigation --------------------------------------------------
    first_li = soup.select_one("ul.chapters li")
    print("next sibling text:", first_li.next_sibling.text)
    print("closest div class:", first_li.closest("div").get("class"))

    # --- XPath (returns PageSoup wrappers) --------------------------------
    xpath_titles = soup.xpath("//h1[@class='title']")
    print("xpath title:", xpath_titles[0].text if xpath_titles else "(none)")

    # --- Drop to raw BeautifulSoup when you need it -----------------------
    raw_tag = book.tag  # the underlying bs4 Tag
    print("raw tag name:", raw_tag.name if raw_tag else None)


if __name__ == "__main__":
    main()
