# Decoy content

This page documents the defence against layer 17, and it is separated out because the layer
is different in kind from every other one in the model.

A honeypot inserts hidden `nofollow` links into a page. Those lead into a maze of
pre-generated, machine-written decoy pages. The links are invisible to a person and marked so
a compliant crawler ignores them, so following them is a deliberate act — and doing it causes
two harms at once.

**The store is poisoned.** It fills with plausible, irrelevant content, which degrades
anything trained on or published from it.

**The session is flagged.** Network-wide, which then shows up later as unrelated failures on
unrelated sites.

What makes this the most dangerous entry in the model is that **there is no error response**.
No status code, no challenge, no rate limit. A scraper walking the maze looks exactly like a
scraper that is working. Every other failure mode in this library announces itself; this one
has to be looked for.

## The defence: enumerate only what is clickable

`scraper.safe_links` is the whole first half, and it costs nothing.

```python
from scraper import safe_links

for link in safe_links(html, "https://example.com/index.html"):
    crawl(link.url)
```

Rejected: `rel=nofollow`; hidden by inline style (`display:none`, `visibility:hidden`,
`opacity:0`, zero dimensions, `clip-path`); positioned off-screen; `aria-hidden="true"`; the
`hidden` attribute; anything inside a hidden ancestor; and anchors that render nothing at all —
no text, no image, no label. Off-site links are dropped by default too, because a crawl that
wanders off the origin loses the accumulated standing that made it work.

Rejection is conservative in one direction only, and on purpose: a decoy link that is followed
causes lasting harm, while a real link that is skipped costs one page.

Pass `include_rejected=True` to see the reasoning. Without it, a page that yielded nothing is
indistinguishable from a page that had no links:

```python
for link in safe_links(html, base, include_rejected=True):
    if not link.followable:
        print(link.rejected, link.url)  # rel=nofollow, hidden by inline style, …
```

`Scraper.links()` wraps this and additionally drops URLs recorded as decoys on an earlier run.

Two related habits the mechanism cannot enforce for you: build URL lists from rendered, visible
content rather than raw markup enumeration, and keep the session in good standing — the
honeypot is served preferentially to sessions already considered suspect.

## The backstop: topic drift

Decoy pages are generated to read as plausible prose, so structure will not give them away.
But they are not *about* what the site is about, and vocabulary overlap catches that cheaply.

`scraper.TopicGuard` learns the vocabulary of accepted pages and flags one whose overlap
collapses. It is a heuristic and is treated as one: it stays silent until it has seen
`min_samples` accepted pages, because a guard that fires on page two of a crawl is a guard that
gets turned off.

The guard runs automatically on every successful HTML response when `guard_topic` is on, which
is the default. It has to run on the way out rather than on demand — a caller who has to
remember to ask will find out from a poisoned dataset instead.

`ScraperConfig.on_decoy` decides what a suspicion does:

| Value | Behaviour | When |
| --- | --- | --- |
| `"warn"` | Logs a warning, records the URL, returns the page. | Default. A false positive should not fail a job. |
| `"raise"` | Raises `scraper.Poisoned`. | Anything that trains on or republishes what it collects. |
| `"ignore"` | Nothing. | You have your own check downstream. |

Either way the URL is recorded in the origin's profile and is not fetched again, and it is
filtered out of `Scraper.links()`. That memory is the durable half of the defence: a trap that
returns no error can only be avoided by remembering it.

The guard reads *visible* text — script and style content are stripped first. Feeding raw
markup to it would measure the vocabulary of the site's JavaScript, which is identical across
every page and would make every page look on-topic.

## A second opinion on the frontier

`scraper.links.looks_like_maze` asks whether a set of URLs looks produced rather than authored.
A maze's paths share a shape: same depth, same segment pattern, unbounded in number. Useful
when a crawl frontier suddenly grows.

```python
from scraper.links import looks_like_maze

if looks_like_maze(newly_discovered):
    ...  # stop expanding and look at what was just added
```
