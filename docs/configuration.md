# Configuration

`scraper.ScraperConfig` is a dataclass and every field has a working default. The docstrings on
it are the authoritative reference — this page is the map, and points at the source for values
so the two cannot disagree.

The shape is deliberate. What is configurable is the set of **capabilities** available and how
patient the run is allowed to be. Which capability gets used, and when, is decided from
evidence at runtime. The previous generation of this library had thirty-odd settings and most
of them were levers on layers that were rarely the binding constraint — cipher rotation, header
randomisation, per-request User-Agent choice. Tuning those is exactly the activity the bound
says is wasted.

**Two settings change what this library can do; the rest adjust how it does it.**

```python
from scraper import ExitKind, ExitSpec, Scraper, ScraperConfig
from scraper.browser import NoDriverSolver

config = ScraperConfig(
    exits=[ExitSpec(url="http://user:pw@residential.test:8000", kind=ExitKind.RESIDENTIAL)],
    browser=NoDriverSolver(),
)
scraper = Scraper(origin="https://example.com", config=config)
```

`exits` is the only thing that moves layer 1, because reputation is not something a client
emits. `browser` is the only thing that reaches the challenge layers. Everything else is
tuning.

## Transport

| Field | Notes |
| --- | --- |
| `impersonate` | curl-impersonate target. **Keep the family alias** — see below. |
| `prefer_http3` | Offer HTTP/3 where the origin advertises it. Off by default. |
| `verify_tls` | A debugging aid, not a bypass. Nothing in the detection stack cares. |
| `transport` | Inject your own `Transport`. The seam tests use. |

Prefer `"chrome"` over `"chrome136"`. A pinned profile ages into a signal on its own: no real
user runs a two-year-old browser, and the older profile predates the post-quantum key share
current builds all send, so a client claiming to be current Chrome without one contradicts its
own User-Agent. `scraper.transport.stale_profile_warning` checks at construction and logs if you
pinned something older than the installed build offers.

`prefer_http3` is off by default because HTTP/3 through some proxies is worse than the mild
mismatch of never offering it.

## Addresses

| Field | Notes |
| --- | --- |
| `exits` | `ExitSpec` / `TorPoolSpec` list. Sorted by kind; declare the kind honestly. |
| `max_sessions_per_exit` | Concurrent requests per address. Clamped to the low single digits. |
| `allow_rotation` | Whether a spent address may be replaced at all. |
| `retire_exit_for` | Seconds a blamed address stays out of the pool. |

`ExitKind` is not decoration — `ExitKind.reach` is what the planner consults before
recommending a rotation as a cure for layer 1. Claiming `MOBILE` for a datacenter range
does not change what the reputation database thinks; it only stops this library from
telling you that layer 1 is why nothing works. A kind other than `DIRECT` with no `url`
raises, since the address would be the local one either way.

See [layers.md](layers.md#layer-1-addresses).

## Behaviour

| Field | Notes |
| --- | --- |
| `pacing` | A `PacingPolicy`. Defaults live on that dataclass. |
| `remember` | Persist what is learned. On by default; see below. |
| `data_dir` | Where learned state and browser profiles live. |

`remember=False` is right for tests and one-off scripts and costs more than it looks like:
every run then rediscovers the binding layer with the same number of failed requests, and those
failures are themselves what the behavioural layer counts.

`data_dir` defaults to `scraper.default_data_dir()`, which honours `SCRAPER_DATA_DIR` first so a
deployment can place it on a volume. See [behaviour.md](behaviour.md#persistence).

## Capabilities

| Field | Adds |
| --- | --- |
| `browser` | The `clearance` tier: layers 6, 7, 9, 10, 13. |
| `archive` / `archive_max_age` | The `archive` tier. Set a max age. |
| `managed` | The `managed` tier. A provider callable. |
| `botauth` | Signs every request. Layer 18, and the cheapest tier there is. |

See [tiers.md](tiers.md).

## Patience

| Field | Notes |
| --- | --- |
| `max_attempts` | Attempts for one retrieval, across all tiers. |
| `max_rotations` | Addresses to spend on one retrieval. Deliberately small. |
| `promote_after` | Failures at a covered emit layer before re-attributing to the composite. |
| `solve_timeout` | How long a browser may work. |
| `retry_backoff` | Base seconds for the retry wait, doubled per attempt. |
| `max_retry_wait` | Ceiling on that wait. |

`max_rotations` being small is a design position, not caution: burning through a pool one
request at a time is the signature of a misdiagnosis, not of an unlucky exit.

`retry_backoff` only applies when the server named no delay. A `Retry-After` header always
wins, and 408, 502, 504 and the 52x family never send one — so those retries were
previously issued back-to-back against a site already in trouble.

## Content safety

| Field | Notes |
| --- | --- |
| `guard_topic` | Watch for decoy content. On by default. |
| `on_decoy` | `"warn"` (default), `"raise"`, or `"ignore"`. |

`"raise"` is right for anything that trains on or republishes what it collects. See
[decoy-content.md](decoy-content.md).

## Request defaults

| Field | Notes |
| --- | --- |
| `timeout` | `(connect, read)` or a single number. |
| `parser` | BeautifulSoup feature name for `PageSoup`. |
| `raise_for_status` | Raise `requests.HTTPError` on a non-2xx that survived the ladder. |

`raise_for_status` only decides whether a `404` arrives as a return value or an exception; it
reaches you either way, because a `404` is the site's answer about a path and is never
attributed to a layer.

## Extras

```bash
pip install lncrawl-scraper                  # the baseline: impersonated HTTP
pip install "lncrawl-scraper[browser]"       # the clearance tier
pip install "lncrawl-scraper[botauth]"       # signed requests
pip install "lncrawl-scraper[image]"         # get_image
pip install "lncrawl-scraper[all]"
```

Each extra adds *reach* rather than convenience. Impersonation is not an extra: layers 2–5 are
one barrier and an ordinary Python client fails all four in the first round trip, so a build
without it would not be a degraded scraper but one that cannot reach a protected page at all.
