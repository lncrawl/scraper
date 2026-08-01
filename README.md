<!-- markdown="1" lets the docs site parse this block; GitHub ignores the attribute. -->
<div align="center" markdown="1">

# lncrawl-scraper

**A scraper that works out _which detection layer_ is blocking it —<br>
and escalates only as far as that layer requires.**

[![PyPI](https://img.shields.io/pypi/v/lncrawl-scraper.svg?logo=pypi&logoColor=white)](https://pypi.org/project/lncrawl-scraper/)
[![Python](https://img.shields.io/pypi/pyversions/lncrawl-scraper.svg?logo=python&logoColor=white)](https://pypi.org/project/lncrawl-scraper/)
[![CI](https://github.com/lncrawl/scraper/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lncrawl/scraper/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/lncrawl/scraper/branch/main/graph/badge.svg)](https://codecov.io/gh/lncrawl/scraper)
[![License](https://img.shields.io/pypi/l/lncrawl-scraper.svg)](https://github.com/lncrawl/scraper/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-lncrawl.github.io-0b0b0b)](https://lncrawl.github.io/scraper/)

[**The model**](https://lncrawl.github.io/scraper/model/) ·
[**Documentation**](https://lncrawl.github.io/scraper/) ·
[**Examples**](https://lncrawl.github.io/scraper/examples/) ·
[**White paper**](https://lncrawl.github.io/scraper/whitepaper/Cloudflare_Bypass.pdf) ·
[**Live report**](https://lncrawl.github.io/scraper/live-report/) ·
[**Changelog**](https://lncrawl.github.io/scraper/changelog/)

</div>

---

```python
from scraper import Scraper

with Scraper(origin="https://example.com") as scraper:
    soup = scraper.get_soup("https://example.com/")
    print(soup.select_one("h1").text)
```

Those four lines already reproduce a real browser's TLS and HTTP/2 fingerprint, hold one
address per origin, pace themselves like a person reading, and refuse to follow decoy links.

## The idea

A modern mitigation engine runs many largely independent detectors and folds them into one
trust score. Admission behaves as a near-conjunction, so:

```text
    P(evade)  ≲  min( p₁ , p₂ , p₃ , … , pₙ )
                       ▲
                       └─ the binding layer. Every other layer is
                          wasted effort until this one stops being
                          the minimum.
```

**The weakest layer bounds the outcome.** If a strategy fails on address reputation,
perfecting its TLS profile gains _nothing_ — not a little, zero. So this library diagnoses
which layer is binding before it changes anything.

**What a detector reads decides whether it can be satisfied.**

|               | The detector reads                                                                          | Reproducible? | What actually moves it              |
| ------------- | ------------------------------------------------------------------------------------------- | ------------- | ----------------------------------- |
| **Emitted**   | an artifact the client _sends_ — a TLS `ClientHello`, an HTTP/2 frame order, a header order | yes           | imitate it faithfully               |
| **Possessed** | a property the client must _hold_ — accumulated per-zone history, a private signing key     | no            | accrue it, rent it, or hold the key |

That second distinction produces the behaviour that most sets this library apart: **when the
binding layer reads a possessed property, it does not rotate.** Rotating discards the very
history the detector is measuring, so it holds the address still and slows down. And two
layers raise instead of retrying, because they read a secret you either hold or do not.

Full treatment: [the model](https://lncrawl.github.io/scraper/model/). It comes from the paper
this library implements — _A Layered Model of Modern Web Bot Protection and the Structural
Limits of Its Circumvention_, included as
[a PDF](https://lncrawl.github.io/scraper/whitepaper/Cloudflare_Bypass.pdf).

## What it does

|                                            |                                                                                                                                                                                                                                                                              |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Diagnoses instead of reacting**          | A response is mapped onto the nineteen-layer model, not onto its status code. A `200` carrying a challenge is a failure; a `429` is a pacing problem, not a bad address; a `403` with error 1010 is about the automation channel, and rotating the exit changes nothing.     |
| **Names the vendor, not just the status**  | DataDome, Kasada, PerimeterX, Akamai, Imperva, DDoS-Guard, Sucuri, AWS WAF and F5 are recognised from their own headers and cookies, and each refusal maps to what it really means. A CDN is named without being blamed: a CloudFront header is on every response it serves. |
| **Escalates on evidence**                  | Four tiers, ordered by real cost. The cheapest one whose reach covers the binding layer is chosen, so a site needing only a header profile never pays for a browser launch.                                                                                                  |
| **Treats identity as indivisible**         | A clearance is bound to the address, User-Agent and TLS fingerprint that earned it, and `Clearance.usable_by()` refuses to replay it under any other — which makes the classic rotating-proxy failure structurally impossible.                                               |
| **Solves once and reuses**                 | A browser runs for the challenge, its exact User-Agent is adopted, and everything after is a cheap request on the same identity until the cookie expires.                                                                                                                    |
| **Accumulates rather than fakes**          | Gamma-distributed pacing, homepage warm-up, real referrer chains, one address per origin, capped concurrency — persisted between runs and shareable across scrapers through `SharedState`, because a process that forgets cannot accumulate.                                 |
| **Avoids the trap with no error response** | `safe_links` enumerates only anchors a person could click; `TopicGuard` notices content that stopped being about the site.                                                                                                                                                   |
| **Signs requests, to be welcome**          | RFC 9421 / Ed25519 Web Bot Auth. A valid signature skips the challenge machinery entirely, making it the cheapest tier there is.                                                                                                                                             |
| **Takes your word for the rest**           | `check_response` faults a `200` whose body says otherwise — the difference lives in a schema only the caller knows, and the loop then treats that verdict as its own.                                                                                                        |
| **Tells you why**                          | `scraper.explain(url)` names the binding layer, the working tier, the learned pacing and the ladder available. Exceptions carry `.layer`, not just a status code.                                                                                                            |

## What you call

Every one of these goes through the ladder, the pacing and the memory described above.

| Method                                   |                                                                                                                                                                   |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get` `post` `head` `ping` `submit_form` | a `requests.Response`                                                                                                                                             |
| `get_soup` `post_soup` `make_soup`       | a `PageSoup` — null-safe BeautifulSoup, selectors never return `None`                                                                                             |
| `get_json` `post_json`                   | parsed JSON                                                                                                                                                       |
| `render` `render_soup`                   | a browser renders the page. For HTML that is a shell JavaScript fills in — not a tier                                                                             |
| `get_file` `get_image`                   | download to disk; `get_image` needs the `[image]` extra                                                                                                           |
| `unchanged`                              | revalidate against the stored `ETag` and skip a whole job. Opt-in: a `304` has no body, so doing it underneath `get_soup` would return an empty page and no error |
| `links`                                  | `safe_links` over a page — the anchors a person could click                                                                                                       |
| `explain` `knows`                        | what has been learned about this origin                                                                                                                           |
| `abort` `close`                          | cancel everything in flight; release exits and browsers. Any call also takes `signal=` to cancel just itself                                                      |

## The ladder

```text
  cost   tier         reaches
  ────   ──────────   ───────────────────────────────────────────────────
     0   archive      everything — when a capture exists
    10   direct       TLS, HTTP/2 frames, header order, post-quantum keyshare
   100   clearance    + the JavaScript, Turnstile and automation-channel layers
  1000   managed      + the per-zone composite, at someone else's price
```

The planner picks the cheapest rung that covers the binding layer, and stops with an
explanation when no configured rung does. Writing your own rung:
[tiers](https://lncrawl.github.io/scraper/tiers/).

## Installation

```bash
pip install lncrawl-scraper
```

Python 3.9 and up; CI tests and builds on every version in that range.

| Extra                      | Pulls in     | Needed for                                            |
| -------------------------- | ------------ | ----------------------------------------------------- |
| `lncrawl-scraper[cdp]`     | websockets   | either bundled solver — both speak over one WebSocket |
| `lncrawl-scraper[botauth]` | cryptography | signed requests (Web Bot Auth)                        |
| `lncrawl-scraper[image]`   | Pillow       | `get_image()`                                         |
| `lncrawl-scraper[all]`     | all three    |                                                       |

A solver drives a browser you already have and never downloads one. Finding it is the
library's job: `find_firefox()` and `find_chromium()` look inside macOS application bundles,
the Windows program directories, and distribution and flatpak paths — not a `PATH` scan, which
answers for Linux and only Linux. `[browser]` is kept as an alias for `[cdp]` so an existing
install keeps resolving.

Impersonation is **not** an extra. Layers 2–5 are one barrier and an ordinary Python client
fails all four in the first round trip, so a build without it would not be a degraded scraper
but one that cannot reach a protected page.

## Adding reach

Two settings change what this library can _do_. The rest adjust how it does it.

```python
from scraper import BidiSolver, ExitKind, ExitSpec, Scraper, ScraperConfig

config = ScraperConfig(
    # The only thing that moves layer 1: reputation is not something a client emits.
    # Declare the kind honestly — claiming MOBILE for a datacenter range only stops
    # this library from telling you that layer 1 is why nothing works.
    exits=[ExitSpec(url="http://user:pw@residential.test:8000", kind=ExitKind.RESIDENTIAL)],
    # The only thing that reaches the challenge layers.
    browser=BidiSolver(),
)

with Scraper(origin="https://site.test", config=config) as scraper:
    scraper.get("https://site.test/deep/page")
    print(scraper.explain("https://site.test/deep/page"))
```

```text
site.test
  binding layer : L9 Managed JavaScript challenge — reads a hybrid property, solve
  tier          : clearance
  pacing        : 4.2s mean interval
  requests      : 48 ok / 3 failed
  clearance     : 712s left
  ladder        : direct(10) clearance(100)
  exits         : residential
```

Two solvers ship: `BidiSolver` drives **Firefox** over WebDriver BiDi, `CdpSolver` drives
**Chrome** over the DevTools protocol. They clear a comparable share of challenged hosts and
disagree on which ones, so neither dominates — prefer `BidiSolver`, because a clearance binds
to the fingerprint that earned it and firefox is the impersonation profile that reaches the
most hosts. A tor-pool is configured the same way, with `TorPoolSpec`.

## When it stops

Failures name the layer and what would move it, because "403 after 3 retries" is the message
that sends people to rewrite the part that was already working.

```python
from scraper import Layer
from scraper.exceptions import Exhausted, Impassable

try:
    scraper.get(url)
except Impassable as exc:
    # Layers 18 and 19 read a secret. Nothing to retry; the message names the route.
    print(exc.detail)
except Exhausted as exc:
    # A bypass may exist; this configuration does not reach it.
    if exc.layer is Layer.IP_REPUTATION:
        print(exc.detail)
        # "no configured exit clears the reputation layer — datacenter and Tor ranges
        #  are published, so rotating between them cannot help."
```

## Documentation

| Page                                                              |                                                       |
| ----------------------------------------------------------------- | ----------------------------------------------------- |
| [The model](https://lncrawl.github.io/scraper/model/)             | The bound, and emit vs. possess. **Start here.**      |
| [Layers](https://lncrawl.github.io/scraper/layers/)               | The nineteen layers, and which are diagnosable.       |
| [Tiers](https://lncrawl.github.io/scraper/tiers/)                 | The escalation ladder; writing a tier.                |
| [Configuration](https://lncrawl.github.io/scraper/configuration/) | Every `ScraperConfig` field.                          |
| [Behaviour](https://lncrawl.github.io/scraper/behaviour/)         | Pacing, warm-up, persistence, shared state.           |
| [Decoy content](https://lncrawl.github.io/scraper/decoy-content/) | The layer that returns no error.                      |
| [Web-bot-auth](https://lncrawl.github.io/scraper/web-bot-auth/)   | Signed requests and the key directory.                |
| [Diagnostics](https://lncrawl.github.io/scraper/diagnostics/)     | `explain()`, exceptions, common conclusions.          |
| [Migration](https://lncrawl.github.io/scraper/migration/)         | Porting from 0.2.x.                                   |
| [Examples](https://lncrawl.github.io/scraper/examples/)           | Ten runnable programs, ordered to explain the design. |

## Scope

This library is for retrieving **publicly accessible** content. It does not attempt
authentication bypass, credential abuse, or circumvention of access controls protecting
non-public data — layer 19 raises rather than trying, and layer 18 raises where a signature is
mandated. Where a site publishes an API or an archive holds what you need, both are cheaper
than anything else here and are supported first-class for that reason.

## Development

```bash
uv sync                 # deps + editable install
uv run poe lint         # ruff + pyright
uv run poe test         # pytest
uv run poe cov          # with coverage
uv run poe docs         # serve the documentation site
```

Tests are offline: the pipeline talks to a two-method `Transport`, so `tests/conftest.py`'s
`FakeTransport` covers every tier without a network. The modules that encode judgement —
`diagnosis`, `planner`, `layers` — are pure functions over primitives and are tested as such.
[AGENTS.md](https://github.com/lncrawl/scraper/blob/main/AGENTS.md) has the architecture and
the invariants that break silently.

<details>
<summary><strong>Verifying against real deployments</strong></summary>

`livetest/` runs the same paths against real Cloudflare deployments, using every host in
[lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler)'s source index as the
corpus. It is not part of `poe test` — it needs the network, and some scenarios need a local
tor-pool and a real browser. See
[livetest/README.md](https://github.com/lncrawl/scraper/blob/main/livetest/README.md).

```bash
uv run poe live-all
```

The recorded output is the [live report](https://lncrawl.github.io/scraper/live-report/) —
scenario results, which layer each client meets across the corpus, and what a Tor exit
actually costs. It is a standalone page, regenerated in place at `livetest/report.html` and
published with the docs.

Nearly every defect found before 1.0 was invisible to a stubbed transport, and two of them made
whole features silently useless while every unit test passed. Anything the harness finds gets a
unit test whose docstring says it was found live, so those docstrings are the record of which
assumptions turned out to be wrong.

</details>

## Credits

Extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler). Apache-2.0;
see [LICENSE](https://github.com/lncrawl/scraper/blob/main/LICENSE).
