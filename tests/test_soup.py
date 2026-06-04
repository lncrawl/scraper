"""Tests for the null-safe PageSoup wrapper."""

from scraper import PageSoup

HTML = """
<html><body>
  <div class="book" data-id="42">
    <h1 class="title">The Great Novel</h1>
    <span class="author">Jane Doe</span>
    <ul class="chapters">
      <li><a href="/c/1">Chapter 1</a></li>
      <li><a href="/c/2">Chapter 2</a></li>
      <li><a href="/c/3">Chapter 3</a></li>
    </ul>
    <div class="ads">buy now</div>
  </div>
</body></html>
"""


def soup() -> PageSoup:
    return PageSoup.create(HTML)


# --- construction ---------------------------------------------------------


def test_create_from_str_bytes_and_response():
    assert PageSoup.create("<p>hi</p>").select_one("p").text == "hi"
    assert PageSoup.create(b"<p>bye</p>").select_one("p").text == "bye"


def test_invalid_data_type_raises():
    import pytest

    with pytest.raises(ValueError):
        PageSoup.create(12345)  # type: ignore[arg-type]


# --- null-safety ----------------------------------------------------------


def test_missing_element_is_falsy_and_safe():
    el = soup().select_one(".nope")
    assert not el
    assert el.text == ""
    assert el.get("href") == ""
    assert el.select("a") == []
    assert el.find("a").text == ""
    assert len(el) == 0


def test_empty_pagesoup_repr():
    assert repr(PageSoup()) == "PageSoup(empty)"
    assert "div" in repr(soup().select_one("div"))


# --- selection ------------------------------------------------------------


def test_select_and_select_one():
    s = soup()
    assert s.select_one(".title").text == "The Great Novel"
    assert len(s.select("ul.chapters a")) == 3


def test_find_and_find_all():
    s = soup()
    assert s.find("span", attrs={"class": "author"}).text == "Jane Doe"
    assert len(s.find_all("li")) == 3


def test_contains_operator():
    assert ".author" in soup().select_one(".book")
    assert ".missing" not in soup().select_one(".book")


def test_xpath():
    titles = soup().xpath("//h1[@class='title']")
    assert len(titles) == 1
    assert titles[0].text == "The Great Novel"


def test_closest_and_parents():
    li = soup().select_one("ul.chapters li")
    assert li.closest(".book").get("data-id") == "42"
    classes = [p.get("class") for p in li.parents(".book")]
    assert "book" in classes


# --- attributes -----------------------------------------------------------


def test_attribute_access():
    book = soup().select_one(".book")
    assert book["data-id"] == "42"
    assert book.get_attr("data-id") == "42"
    assert book.get("missing", "fallback") == "fallback"
    assert book.has_attr("class") is True
    assert "book" in book.attrs["class"]


# --- text / html ----------------------------------------------------------


def test_text_and_html_extraction():
    title = soup().select_one(".title")
    assert title.text == "The Great Novel"
    assert title.get_text() == "The Great Novel"
    assert title.outer_html.startswith("<h1")
    assert title.inner_html == "The Great Novel"


def test_word_count():
    assert soup().select_one(".title").word_count() == 3


# --- navigation -----------------------------------------------------------


def test_sibling_and_child_navigation():
    s = soup()
    first_li = s.select_one("ul.chapters li")
    assert first_li.next_sibling.text == "Chapter 2"
    assert first_li.next_sibling.previous_sibling.text == "Chapter 1"
    assert len(s.select_one("ul.chapters").children) == 3
    assert first_li.parent.name == "ul"


# --- mutation -------------------------------------------------------------


def test_decompose_removes_matches():
    s = soup()
    s.select_one(".book").decompose(".ads")
    assert not s.select_one(".ads")


def test_text_setter_replaces_content():
    s = soup()
    s.select_one(".title").text = "New Title"
    assert s.select_one(".title").text == "New Title"


def test_iteration_yields_children():
    ul = soup().select_one("ul.chapters")
    assert [child.name for child in ul] == ["li", "li", "li"]


# --- html output ----------------------------------------------------------


def test_prettify_and_str():
    title = soup().select_one(".title")
    assert "<h1" in title.prettify()
    assert title.prettify(inner=True).strip() == "The Great Novel"
    assert str(title) == title.outer_html


def test_get_text_with_separator():
    ul = soup().select_one("ul.chapters")
    text = ul.get_text(separator="|")
    assert "Chapter 1" in text and "|" in text


def test_name_and_string_properties():
    title = soup().select_one(".title")
    assert title.name == "h1"
    assert title.string == "The Great Novel"
    assert PageSoup().name == ""
    assert PageSoup().string == ""


# --- root / body ----------------------------------------------------------


def test_root_and_body_access():
    s = soup()
    el = s.select_one(".title")
    assert el.root is not None
    assert el.body.name == "body"


def test_tag_property_exposes_bs4():
    from bs4 import Tag

    assert isinstance(soup().select_one(".title").tag, Tag)
    assert PageSoup().tag is None


# --- creation / mutation --------------------------------------------------


def test_new_tag_append_and_replace():
    s = soup()
    book = s.select_one(".book")
    new = book.new_tag("p", attrs={"class": "note"})
    new.append("hello")
    book.append(new)
    assert s.select_one("p.note").text == "hello"


def test_replace_with():
    s = soup()
    s.select_one(".author").replace_with("Anon")
    assert not s.select_one(".author")
    assert "Anon" in s.select_one(".book").text


def test_new_tag_on_empty_raises():
    import pytest

    with pytest.raises(ValueError):
        PageSoup().new_tag("div")


# --- attribute edge cases -------------------------------------------------


def test_get_attr_joins_list_values():
    # class attributes parse to a list; get_attr joins them
    s = PageSoup.create('<div class="a b c">x</div>')
    assert s.select_one("div").get_attr("class") == "a b c"


def test_contents_includes_text_nodes():
    s = PageSoup.create("<p>hello <b>world</b></p>")
    contents = s.select_one("p").contents
    assert any(isinstance(c, str) for c in contents)
    assert any(isinstance(c, PageSoup) for c in contents)
