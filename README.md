# LNCrawl Scraper

[![PyPI](https://img.shields.io/pypi/v/lncrawl-scraper.svg)](https://pypi.org/project/lncrawl-scraper/)
[![codecov](https://codecov.io/gh/lncrawl/scraper/branch/main/graph/badge.svg)](https://codecov.io/gh/lncrawl/scraper)
[![CI](https://github.com/lncrawl/scraper/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lncrawl/scraper/actions/workflows/ci.yml)
[![CodeQL](https://github.com/lncrawl/scraper/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/lncrawl/scraper/actions/workflows/github-code-scanning/codeql)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/lncrawl-scraper)

A scraper that works out **which detection layer is blocking it** and escalates only as far as
that layer requires — instead of running a fixed remedy per status code.

```python
from scraper import Scraper

with Scraper(origin="https://example.com") as scraper:
    soup = scraper.get_soup("https://example.com/")
    print(soup.select_one("h1").text)
```

That already reproduces a real browser's TLS and HTTP/2 fingerprint, holds one address per
origin, paces itself like a person reading, and refuses to follow decoy links.

## The idea

A modern mitigation engine runs many largely independent detectors and folds them into one trust
score. Admission behaves as a near-conjunction, so:

**The weakest layer bounds the outcome.** `P(evade) ≲ min(p₁ … pₙ)`. If a strategy fails on
address reputation, perfecting its TLS profile gains *nothing* — not a little, zero — until
reputation stops being the minimum. So this library diagnoses which layer is binding before it
changes anything.

**What a detector reads decides whether it can be satisfied.** Some read an artifact the client
**emits** — a TLS `ClientHello`, an HTTP/2 frame order, a header order — which a faithful
imitator can reproduce. Others read a property the client must **possess** — accumulated
per-zone history, a private signing key — which it cannot.

That second distinction produces the behaviour that most sets this library apart: **when the
binding layer reads a possessed property, it does not rotate.** Rotating discards the very
history the detector is measuring, so it holds the address still and slows down. And two layers
raise instead of retrying, because they read a secret you either hold or do not.

Full treatment: [docs/model.md](docs/model.md).

## What it does

- **Diagnoses instead of reacting.** `scraper.diagnose` maps a response to one of nineteen
  layers. A `200` carrying a challenge is a failure; a `429` is a pacing problem, not a bad
  address; a `403` with error 1010 is about the automation channel and rotating the exit changes
  nothing.
- **Escalates on evidence.** Four tiers — archive, impersonated HTTP, browser solve, managed
  provider — ordered by real cost. The cheapest one whose reach covers the binding layer is
  chosen, so a site needing only a header profile never pays for a browser launch.
- **Treats identity as indivisible.** A clearance is bound to the address, User-Agent and TLS
  fingerprint that earned it, so `Clearance.usable_by()` refuses to replay it under any other —
  which makes the classic rotating-proxy failure structurally impossible.
- **Solves once and reuses.** A browser runs for the challenge, its exact User-Agent is adopted,
  and everything after is a cheap request on the same identity until the cookie expires.
- **Accumulates rather than fakes.** Gamma-distributed pacing, homepage warm-up, real referrer
  chains, one address per origin, capped concurrency — and it all persists between runs, because
  a process that forgets cannot accumulate.
- **Avoids the trap with no error response.** `safe_links` enumerates only anchors a person could
  click; `TopicGuard` notices content that stopped being about the site.
- **Signs requests, if you want to be welcome.** RFC 9421 / Ed25519 Web Bot Auth. A valid
  signature skips the challenge machinery entirely, making it the cheapest tier there is.
- **Tells you why.** `scraper.explain(url)` names the binding layer, the working tier, the
  learned pacing and the ladder available. Exceptions carry `.layer`, not just a status code.
- **`PageSoup`** — null-safe BeautifulSoup wrapper; selectors never return `None`.

## Installation

```bash
pip install lncrawl-scraper

pip install "lncrawl-scraper[browser]"   # challenge solving (nodriver)
pip install "lncrawl-scraper[botauth]"   # signed requests (cryptography)
pip install "lncrawl-scraper[image]"     # get_image() (Pillow)
pip install "lncrawl-scraper[all]"
```

Impersonation is **not** an extra. Layers 2–5 are one barrier and an ordinary Python client fails
all four in the first round trip, so a build without it would not be a degraded scraper but one
that cannot reach a protected page.

## Adding reach

Two settings change what this library can *do*. The rest adjust how it does it.

```python
from scraper import ExitKind, ExitSpec, Scraper, ScraperConfig
from scraper.browser import NoDriverSolver

config = ScraperConfig(
    # The only thing that moves layer 1: reputation is not something a client emits.
    # Declare the kind honestly — claiming MOBILE for a datacenter range only stops
    # this library from telling you that layer 1 is why nothing works.
    exits=[ExitSpec(url="http://user:pw@residential.test:8000", kind=ExitKind.RESIDENTIAL)],
    # The only thing that reaches the challenge layers.
    browser=NoDriverSolver(),
)

with Scraper(origin="https://site.test", config=config) as scraper:
    scraper.get("https://site.test/deep/page")
    print(scraper.explain("https://site.test/deep/page"))
```

```
site.test
  binding layer : L9 Managed JavaScript challenge — reads a hybrid property, solve
  tier          : clearance
  pacing        : 4.2s mean interval
  requests      : 48 ok / 3 failed
  clearance     : 712s left
  ladder        : direct(10) clearance(100)
  exits         : residential
```

## When it stops

Failures name the layer and what would move it, because "403 after 3 retries" is the message that
sends people to rewrite the part that was already working.

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

| Page | |
| --- | --- |
| [docs/model.md](docs/model.md) | The bound, and emit vs. possess. **Start here.** |
| [docs/layers.md](docs/layers.md) | The nineteen layers and what moves each. |
| [docs/tiers.md](docs/tiers.md) | The escalation ladder; writing a tier. |
| [docs/configuration.md](docs/configuration.md) | Every `ScraperConfig` field. |
| [docs/behaviour.md](docs/behaviour.md) | Pacing, warm-up, persistence, shared state. |
| [docs/decoy-content.md](docs/decoy-content.md) | The layer that returns no error. |
| [docs/web-bot-auth.md](docs/web-bot-auth.md) | Signed requests and the key directory. |
| [docs/diagnostics.md](docs/diagnostics.md) | `explain()`, exceptions, common conclusions. |
| [docs/migration.md](docs/migration.md) | Porting from 0.2.x. |
| [examples/](examples/) | Ten runnable programs, ordered to explain the design. |

## Scope

This library is for retrieving **publicly accessible** content. It does not attempt
authentication bypass, credential abuse, or circumvention of access controls protecting
non-public data — layer 19 raises rather than trying, and layer 18 raises where a signature is
mandated. Where a site publishes an API or an archive holds what you need, both are cheaper than
anything else here and are supported first-class for that reason.

## Development

```bash
uv sync                 # deps + editable install
uv run poe lint         # ruff + pyright
uv run poe test         # pytest
uv run poe cov          # with coverage
```

Tests are offline: the pipeline talks to a two-method `Transport`, so `tests/conftest.py`'s
`FakeTransport` covers every tier without a network. The modules that encode judgement —
`diagnosis`, `planner`, `layers` — are pure functions over primitives and are tested as such.

## Credits

The layer model, the emit/possess distinction and the reference patterns are drawn from
*A Layered Model of Modern Web Bot Protection and the Structural Limits of Its Circumvention*
(Sudipto Chandra, 2026). This package is that paper's argument implemented.

Extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).
