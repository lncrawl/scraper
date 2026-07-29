"""Not walking into the trap, and noticing if you already did.

One layer in the model does not block anything. It inserts hidden ``nofollow``
links into a page, and those lead into a maze of generated decoy pages. The links
are invisible to a human and marked so that a compliant crawler ignores them, so
following them is a deliberate act — and doing it causes two harms at once. The
store fills with plausible, irrelevant content, which poisons anything trained or
published from it. And the session gets flagged network-wide, which then shows up
as unrelated failures on unrelated sites.

What makes this the most dangerous entry in the whole model is that it returns no
error. There is no status code, no challenge, no rate limit. A scraper walking the
maze looks like a scraper that is working. Every other failure mode in this
library announces itself; this one has to be looked for.

Hence two halves. :func:`safe_links` enumerates only links a person could actually
click, which is the entire defence and costs nothing. :class:`TopicGuard` is the
backstop for having got it wrong anyway: it learns what a site's pages read like
and notices when they stop reading like that.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import urldefrag, urljoin

from .utils.url_tools import extract_host

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0(?!\.[1-9])"
    r"|font-size\s*:\s*0"
    r"|(?:width|height)\s*:\s*0(?:px|em|rem)?\s*(?:;|$)"
    r"|clip-path\s*:\s*inset\(\s*100%",
    re.IGNORECASE,
)
_OFFSCREEN = re.compile(
    r"(?:left|top|text-indent|margin-left)\s*:\s*-\s*\d{3,}",
    re.IGNORECASE,
)

_WORD = re.compile(r"[a-z][a-z'’-]{3,}")

_STOPWORDS = frozenset(
    """
    about above after again against because been before being below between both
    cannot could does doing down during each from further have having here into
    itself more most other over same should some such than that their theirs them
    then there these they this those through under until very were what when where
    which while whom will with would your yours
    """.split()
)

MAX_DEPTH = 6
"""How far up the tree to look for a hidden ancestor.

A link inside a hidden container is hidden, but walking to the document root for
every link on a large page is wasted work — decoy containers sit close to their
links.
"""


@dataclass(frozen=True)
class Link:
    """One anchor, with the verdict on whether a person could reach it."""

    url: str
    text: str = ""
    rel: str = ""
    rejected: str = ""

    @property
    def followable(self) -> bool:
        return not self.rejected


def _attr(tag: Any, name: str) -> str:
    try:
        value = tag.get(name)
    except Exception:  # noqa: BLE001 - foreign node types
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _hidden_reason(tag: Any) -> str:
    style = _attr(tag, "style")
    if _HIDDEN_STYLE.search(style):
        return "hidden by inline style"
    if _OFFSCREEN.search(style):
        return "positioned off-screen"
    if _attr(tag, "aria-hidden").lower() == "true":
        return "aria-hidden"
    try:
        if tag.has_attr("hidden"):
            return "hidden attribute"
    except Exception:  # noqa: BLE001 - foreign node types
        pass
    return ""


def _ancestor_hidden(tag: Any) -> str:
    node = getattr(tag, "parent", None)
    for _ in range(MAX_DEPTH):
        if node is None or not hasattr(node, "get"):
            return ""
        reason = _hidden_reason(node)
        if reason:
            return f"inside an element {reason}"
        node = getattr(node, "parent", None)
    return ""


def _tags(source: Any) -> List[Any]:
    """Pull anchor nodes out of whatever the caller passed."""
    tag = getattr(source, "tag", source)
    if isinstance(tag, str):
        from bs4 import BeautifulSoup

        tag = BeautifulSoup(tag, "lxml")
    finder = getattr(tag, "find_all", None)
    if finder is None:
        return []
    return [node for node in finder("a") if hasattr(node, "get")]


def safe_links(
    source: Any,
    base_url: str = "",
    *,
    same_host: bool = True,
    include_rejected: bool = False,
) -> List[Link]:
    """Anchors from *source* that a person could actually click.

    Args:
        source: A :class:`~scraper.PageSoup`, a BeautifulSoup tag, or raw HTML.
        base_url: Resolves relative hrefs, and defines "same host".
        same_host: Drop off-site links. On by default because a crawl that
            wanders off the origin loses the accumulated per-zone standing that
            makes it work in the first place.
        include_rejected: Return the rejected links too, each carrying its
            :attr:`Link.rejected` reason. For seeing *why* a page yielded nothing,
            which is otherwise indistinguishable from a page with no links.

    Rejection is deliberately conservative in one direction only. A decoy link that
    is followed causes lasting harm; a real link that is skipped costs one page.
    """
    host = extract_host(base_url) if base_url else ""
    seen: Set[str] = set()
    out: List[Link] = []

    for tag in _tags(source):
        href = _attr(tag, "href").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        url = urldefrag(urljoin(base_url, href) if base_url else href)[0]
        if url in seen:
            continue
        seen.add(url)

        rel = _attr(tag, "rel").lower()
        text = ""
        try:
            text = tag.get_text(strip=True) or ""
        except Exception:  # noqa: BLE001 - foreign node types
            pass

        reason = ""
        if "nofollow" in rel.split():
            # The marker the trap relies on. A site that means "do not crawl this"
            # and a site that means "this is bait" both say it this way, and both
            # answers are the same: leave it alone.
            reason = "rel=nofollow"
        elif same_host and host and extract_host(url) != host:
            reason = "off-site"
        else:
            reason = _hidden_reason(tag) or _ancestor_hidden(tag)
            if not reason and not text and not _visible_surrogate(tag):
                # No text, no image, no label: nothing rendered for a person to
                # click. A legitimate empty anchor is rare; a decoy one is not.
                reason = "nothing rendered"

        if reason and not include_rejected:
            continue
        out.append(Link(url=url, text=text, rel=rel, rejected=reason))

    return out


def _visible_surrogate(tag: Any) -> bool:
    """Whether an anchor with no text still renders something clickable."""
    if _attr(tag, "title") or _attr(tag, "aria-label"):
        return True
    finder = getattr(tag, "find", None)
    if finder is None:
        return False
    try:
        return finder(["img", "svg", "picture", "video", "canvas"]) is not None
    except Exception:  # noqa: BLE001 - foreign node types
        return False


def tokens(text: str) -> Counter:
    """Content words in *text*, lowercased, stopwords dropped."""
    return Counter(word for word in _WORD.findall((text or "").lower()) if word not in _STOPWORDS)


@dataclass
class TopicGuard:
    """Learns what a site reads like, then notices when a page does not.

    The backstop for the trap that gives no feedback. Decoy pages are generated to
    be plausible prose, so they are not detectable by structure — but they are not
    *about* the same things as the site, and vocabulary overlap catches that
    cheaply.

    A heuristic, and treated as one: it stays quiet until it has seen
    *min_samples* accepted pages, because a guard that fires on page two of a
    crawl is a guard that gets turned off.

    Args:
        threshold: Overlap below which a page is suspect. Fraction of the page's
            content words that appear in the learned vocabulary.
        min_samples: Accepted pages required before it will flag anything.
        vocabulary_size: Words retained. Large enough to cover a site's subject
            matter, small enough that a few decoy pages cannot dilute it.
    """

    threshold: float = 0.25
    min_samples: int = 5
    vocabulary_size: int = 2000
    _counts: Counter = field(default_factory=Counter, repr=False)
    _samples: int = 0

    @property
    def ready(self) -> bool:
        return self._samples >= self.min_samples

    @property
    def samples(self) -> int:
        return self._samples

    def learn(self, text: str) -> None:
        """Fold an accepted page into the vocabulary."""
        found = tokens(text)
        if not found:
            return
        self._counts.update(found)
        self._samples += 1
        if len(self._counts) > self.vocabulary_size * 2:
            self._counts = Counter(dict(self._counts.most_common(self.vocabulary_size)))

    def score(self, text: str) -> float:
        """Fraction of *text*'s content words the site is known to use.

        ``1.0`` before the guard is ready, so an unready guard never accuses
        anything.
        """
        if not self.ready:
            return 1.0
        found = tokens(text)
        total = sum(found.values())
        if not total:
            return 1.0
        known = set(word for word, _ in self._counts.most_common(self.vocabulary_size))
        overlap = sum(count for word, count in found.items() if word in known)
        return overlap / total

    def suspect(self, text: str) -> Optional[str]:
        """A reason *text* looks like decoy content, or ``None``."""
        value = self.score(text)
        if value >= self.threshold:
            return None
        return (
            f"vocabulary overlap {value:.0%} against {self._samples} known pages "
            f"(threshold {self.threshold:.0%})"
        )

    def snapshot(self) -> Dict[str, Any]:
        """Serialisable state, so the vocabulary survives the process."""
        return {
            "samples": self._samples,
            "counts": dict(self._counts.most_common(self.vocabulary_size)),
        }

    @classmethod
    def restore(cls, state: Optional[Dict[str, Any]], **kwargs: Any) -> "TopicGuard":
        guard = cls(**kwargs)
        if state:
            guard._samples = int(state.get("samples") or 0)
            counts = state.get("counts")
            if isinstance(counts, dict):
                guard._counts = Counter({str(k): int(v) for k, v in counts.items()})
        return guard


def looks_like_maze(urls: Sequence[str], *, threshold: int = 8) -> bool:
    """Whether *urls* look like generated paths rather than a site's own structure.

    A maze is produced, not authored, so its paths share a shape: same depth, same
    segment pattern, unbounded in number. Used as a second opinion alongside
    :class:`TopicGuard` when a crawl frontier suddenly grows.
    """
    if len(urls) < threshold:
        return False
    shapes: Counter = Counter()
    for url in urls:
        segments = [segment for segment in urldefrag(url)[0].split("/")[3:] if segment]
        shape = "/".join(
            "N" if segment.isdigit() else str(len(segment) // 4) for segment in segments
        )
        shapes[shape] += 1
    _, count = shapes.most_common(1)[0]
    return count >= threshold and count / len(urls) > 0.9


def dedupe(urls: Iterable[str]) -> List[str]:
    """Order-preserving deduplication of *urls*, ignoring fragments."""
    seen: Set[str] = set()
    out: List[str] = []
    for url in urls:
        clean = urldefrag(url)[0]
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
