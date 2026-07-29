"""Edge-case and defensive-branch coverage for PageSoup."""

from bs4 import BeautifulSoup

from scraper import PageSoup

HTML = """
<html><body>
  <div class="book"><h1 class="title">T</h1><span class="author">Jane Doe</span>
    <ul><li>a</li><li>b</li></ul><div class="ads">x</div></div>
</body></html>
"""


def soup() -> PageSoup:
    return PageSoup.create(HTML)


class _BoomTag:
    """A truthy stand-in whose every attribute access / str() raises."""

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        raise RuntimeError("boom")

    def __getattr__(self, name):
        raise RuntimeError("boom")


def boom() -> PageSoup:
    ps = PageSoup()
    ps._tag = _BoomTag()  # type: ignore[assignment]
    return ps


# --- empty PageSoup returns safe defaults everywhere ----------------------


def test_empty_returns_safe_defaults():
    e = PageSoup()
    assert e.select("a") == []
    assert e.find_all("a") == []
    assert list(e.parents()) == []
    assert e.xpath("//a") == []
    assert e.attrs == {}
    assert e.has_attr("x") is False
    assert e.get_attr("x") == ""
    assert e.children == []
    assert e.contents == []
    assert e.name == "" and e.string == ""
    assert e.inner_html == "" and e.outer_html == "" and e.prettify() == ""
    assert e.get_text() == ""
    assert not e.next_sibling and not e.previous_sibling and not e.parent
    assert e.root is None
    assert not e.body
    assert e.decompose() is e
    assert e.replace_with("x") is e
    e.append("x")  # no-op
    e.text = "x"  # no-op, exercises the empty branch of the setter


# --- exception branches (underlying tag raises) ---------------------------


def test_exception_branches_return_safe_defaults():
    b = boom()
    assert b.select("a") == []
    assert not b.select_one("a")  # exception → empty (falsy) PageSoup
    assert list(b.parents()) == []
    assert not b.closest("a")
    assert not b.find("a")
    assert b.find_all("a") == []
    assert b.get_text() == ""
    assert b.inner_html == "" and b.outer_html == "" and b.prettify() == ""
    assert b.get_attr("x") == ""
    assert b.decompose(".x") is b
    assert b.decompose() is b
    b.text = "y"  # setter swallows the exception


# --- __len__ / __getattr__ delegation -------------------------------------


def test_len_counts_child_elements():
    assert len(soup().select_one("ul")) == 2


def test_getattr_delegates_to_tag():
    # an unwrapped bs4 method is reachable via __getattr__
    assert callable(soup().select_one(".title").find_next)
    # on an empty PageSoup, unknown attributes resolve to None
    assert PageSoup().find_next is None


# --- find returning a non-Tag --------------------------------------------


def test_find_string_returns_empty():
    # find(string=...) yields a NavigableString, not a Tag → empty PageSoup
    result = soup().find(string="Jane Doe")
    assert not result


# --- xpath edge cases -----------------------------------------------------


def test_xpath_invalid_expression_returns_empty():
    assert soup().xpath("[") == []


def test_xpath_non_element_results_ignored():
    # count() returns a float (not a node list)
    assert soup().xpath("count(//li)") == []
    # attribute selection returns strings, which are skipped
    assert soup().xpath("//div/@class") == []


# --- attrs setter / get_attr default --------------------------------------


def test_attrs_setter_and_default():
    el = soup().select_one(".title")
    el.attrs = {"data-x": "1"}
    assert el.get("data-x") == "1"
    assert el.get_attr("missing") == ""
    PageSoup().attrs = {"a": "b"}  # setter on empty soup is a no-op


def test_create_raises_on_parse_failure():
    import pytest

    with pytest.raises(ValueError):
        PageSoup.create("<p>x</p>", parser="no-such-parser")


# --- root / body variants -------------------------------------------------


def test_root_is_beautifulsoup_when_tag_is_root():
    s = PageSoup.create("<p>x</p>")
    assert isinstance(s.root, BeautifulSoup)


def test_root_none_for_detached_tag():
    detached = BeautifulSoup("", "html.parser").new_tag("div")
    assert PageSoup(detached).root is None


def test_root_none_for_a_tree_never_attached_to_a_document():
    # A subtree assembled by hand has parents but no BeautifulSoup at the top, so
    # there is no document to hand back — and `body` has to degrade with it rather
    # than reaching for `find` on a bare tag.
    document = BeautifulSoup("", "html.parser")
    outer = document.new_tag("div")
    inner = document.new_tag("span")
    outer.append(inner)
    assert PageSoup(inner).root is None
    assert not PageSoup(inner).body


# --- mutation -------------------------------------------------------------


def test_extract_is_decompose():
    s = soup()
    s.select_one(".ads").extract()
    assert not s.select_one(".ads")


def test_replace_with_pagesoup_and_string():
    s = soup()
    new = s.select_one(".book").new_tag("em")
    new.append("hi")
    s.select_one(".author").replace_with(new)
    assert s.select_one("em").text == "hi"


def test_append_ignores_empty_pagesoup():
    s = soup()
    book = s.select_one(".book")
    before = len(book)
    book.append(PageSoup())  # empty → no-op (exercises the elif-false branch)
    assert len(book) == before
