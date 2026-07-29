"""Not walking into the decoy maze, and noticing content that came out of one."""

from __future__ import annotations

from scraper.links import (
    Link,
    TopicGuard,
    dedupe,
    looks_like_maze,
    safe_links,
    tokens,
)

PAGE = """
<html><body>
  <nav><a href="/chapters/1">Chapter 1</a></nav>
  <a href="/chapters/2">Chapter 2</a>

  <!-- everything below is invisible to a person -->
  <a href="/maze/a" rel="nofollow">bait</a>
  <a href="/maze/b" style="display:none">bait</a>
  <a href="/maze/c" style="visibility: hidden">bait</a>
  <a href="/maze/d" style="opacity:0">bait</a>
  <a href="/maze/e" aria-hidden="true">bait</a>
  <a href="/maze/f" hidden>bait</a>
  <a href="/maze/g" style="position:absolute; left:-9999px">bait</a>
  <div style="display:none"><a href="/maze/h">bait</a></div>
  <a href="/maze/i"></a>

  <a href="#top">anchor</a>
  <a href="javascript:void(0)">script</a>
  <a href="mailto:x@y.z">mail</a>
  <a href="https://other.test/off">off-site</a>
  <a href="/cover"><img src="/c.jpg"></a>
  <a href="/labelled" title="Read more"></a>
</body></html>
"""

BASE = "https://example.com/index.html"


class TestOnlyWhatAPersonCouldClick:
    def test_the_visible_links_come_through(self):
        urls = [link.url for link in safe_links(PAGE, BASE)]
        assert "https://example.com/chapters/1" in urls
        assert "https://example.com/chapters/2" in urls

    def test_no_decoy_link_survives(self):
        urls = [link.url for link in safe_links(PAGE, BASE)]
        assert not [url for url in urls if "/maze/" in url]

    def test_each_rejection_can_be_explained(self):
        rejected = {
            link.url.rsplit("/", 1)[-1]: link.rejected
            for link in safe_links(PAGE, BASE, include_rejected=True)
            if link.rejected
        }
        assert rejected["a"] == "rel=nofollow"
        assert "inline style" in rejected["b"]
        assert "inline style" in rejected["c"]
        assert "inline style" in rejected["d"]
        assert rejected["e"] == "aria-hidden"
        assert rejected["f"] == "hidden attribute"
        assert "off-screen" in rejected["g"]
        assert "inside an element" in rejected["h"]
        assert rejected["i"] == "nothing rendered"

    def test_an_image_link_is_clickable_even_with_no_text(self):
        urls = [link.url for link in safe_links(PAGE, BASE)]
        assert "https://example.com/cover" in urls

    def test_a_labelled_empty_link_is_clickable(self):
        urls = [link.url for link in safe_links(PAGE, BASE)]
        assert "https://example.com/labelled" in urls

    def test_non_navigational_hrefs_are_skipped_entirely(self):
        urls = [link.url for link in safe_links(PAGE, BASE, include_rejected=True)]
        assert not [url for url in urls if url.startswith(("mailto:", "javascript:"))]

    def test_a_fragment_only_link_is_not_a_page(self):
        urls = [link.url for link in safe_links(PAGE, BASE, include_rejected=True)]
        assert BASE not in urls

    def test_leaving_the_site_is_dropped_by_default(self):
        # A crawl that wanders off the origin loses the accumulated standing that
        # made it work.
        urls = [link.url for link in safe_links(PAGE, BASE)]
        assert "https://other.test/off" not in urls

    def test_off_site_links_can_be_asked_for(self):
        urls = [link.url for link in safe_links(PAGE, BASE, same_host=False)]
        assert "https://other.test/off" in urls

    def test_duplicates_collapse(self):
        html = '<a href="/x">one</a><a href="/x#frag">two</a><a href="/x">three</a>'
        assert len(safe_links(html, BASE)) == 1

    def test_relative_hrefs_resolve_against_the_base(self):
        assert safe_links('<a href="page.html">x</a>', BASE)[0].url == (
            "https://example.com/page.html"
        )

    def test_no_base_still_works(self):
        assert safe_links('<a href="https://example.com/x">x</a>')[0].url == "https://example.com/x"

    def test_a_soup_wrapper_is_accepted_too(self):
        from scraper import PageSoup

        soup = PageSoup.create(PAGE)
        assert safe_links(soup, BASE)
        assert safe_links(soup.select_one("nav"), BASE)

    def test_a_page_with_no_anchors_is_not_an_error(self):
        assert safe_links("<html><body><p>nothing</p></body></html>", BASE) == []


def test_a_link_knows_whether_it_is_followable():
    assert Link(url="https://x/").followable
    assert not Link(url="https://x/", rejected="rel=nofollow").followable


class TestTopicGuard:
    """The backstop for the trap that returns no error.

    Decoy pages are generated to read as plausible prose, so structure will not
    give them away — but they are not *about* what the site is about.
    """

    def test_it_says_nothing_until_it_has_seen_enough(self):
        # A guard that fires on page two of a crawl is a guard that gets turned off.
        guard = TopicGuard(min_samples=3)
        guard.learn("novels chapters translation fantasy")
        assert not guard.ready
        assert guard.suspect("entirely unrelated financial derivatives quarterly") is None

    def test_on_topic_pages_pass(self):
        guard = TopicGuard(min_samples=2)
        for _ in range(3):
            guard.learn("chapter translation novel protagonist cultivation sect elder")
        assert guard.suspect("the protagonist entered the sect and met the elder") is None

    def test_a_page_about_something_else_is_flagged(self):
        guard = TopicGuard(min_samples=2, threshold=0.3)
        for _ in range(3):
            guard.learn("chapter translation novel protagonist cultivation sect elder")
        reason = guard.suspect(
            "quarterly amortisation schedules reconciled against depreciating "
            "municipal bond covenants and actuarial tables"
        )
        assert reason is not None
        assert "overlap" in reason

    def test_an_empty_page_is_not_an_accusation(self):
        guard = TopicGuard(min_samples=1)
        guard.learn("chapter novel")
        assert guard.suspect("") is None

    def test_the_vocabulary_is_bounded(self):
        guard = TopicGuard(vocabulary_size=20, min_samples=1)
        for index in range(200):
            guard.learn(f"word{index:04d} filler filler")
        assert len(guard.snapshot()["counts"]) <= 20

    def test_the_vocabulary_survives_the_process(self):
        guard = TopicGuard(min_samples=2)
        for _ in range(3):
            guard.learn("chapter translation novel protagonist")
        restored = TopicGuard.restore(guard.snapshot(), min_samples=2)
        assert restored.ready
        assert restored.samples == guard.samples

    def test_restoring_from_nothing_is_a_fresh_guard(self):
        assert TopicGuard.restore(None).samples == 0


def test_tokens_drop_stopwords_and_short_words():
    found = tokens("The protagonist WENT into that cave")
    assert "protagonist" in found
    assert "that" not in found
    assert "the" not in found


class TestMazeShape:
    def test_generated_paths_share_a_shape(self):
        # A maze is produced, not authored, so its URLs are uniform in a way a real
        # site's are not.
        urls = [f"https://example.com/gen/{i:04d}/page" for i in range(20)]
        assert looks_like_maze(urls)

    def test_a_real_sites_urls_are_not_uniform(self):
        urls = [
            "https://example.com/novel/the-long-title",
            "https://example.com/about",
            "https://example.com/novel/the-long-title/chapter-1",
            "https://example.com/tags/fantasy/page/2",
            "https://example.com/x",
            "https://example.com/a/b/c/d/e",
            "https://example.com/contact-us-today",
            "https://example.com/faq",
            "https://example.com/browse/completed/rating",
        ]
        assert not looks_like_maze(urls)

    def test_a_handful_of_urls_proves_nothing(self):
        assert not looks_like_maze(["https://example.com/gen/1/page"])


def test_dedupe_keeps_order_and_ignores_fragments():
    assert dedupe(["https://x/a", "https://x/a#1", "https://x/b", "https://x/a"]) == [
        "https://x/a",
        "https://x/b",
    ]
