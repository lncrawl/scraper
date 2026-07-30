# Behaviour: pacing, warm-up, and what persists

This is the documentation for layer 8, and the framing matters: it is the layer this
library addresses by *not* trying to defeat it.

A per-zone behavioural model reads accumulated, non-portable history — timing regularity,
navigation and referrer chains, cookie and session age, history depth, concurrent sessions
per address — correlated across a session window and trained separately for every protected
site. None of that can be presented on demand. The only thing that works is to behave the
way the model expects and let the history accrue.

Everything below is off the critical path in the sense that it never blocks a request, and
on the critical path in the sense that a site running this layer will not work without it.

## Gaps come from a distribution

A fixed minimum interval produces perfectly regular arrivals, which is a stronger signal
than being fast. So inter-request gaps are drawn from a gamma distribution: positive by
construction, mode below the mean, tail above it.

`scraper.PacingPolicy` holds the parameters — target mean, shape, floor, ceiling, and the
probability of a longer "reading pause" that a pure gamma stream would never produce. The
defaults are in the dataclass; read them there rather than here, since that is the copy that
is true.

```python
from scraper import Pacer, PacingPolicy

pacer = Pacer(PacingPolicy(interval=4.0, shape=2.5))
[round(pacer.gap("example.com"), 2) for _ in range(6)]
# [2.71, 6.04, 3.19, 1.48, 4.86, 3.55]   — irregular, clustered, occasionally long
```

`gap()` draws; `next_delay()` subtracts time you already spent doing your own work, so a
caller that takes two seconds to parse a page is not made to wait twice. `wait()` sleeps in
slices so an abort is honoured promptly — the tail runs to tens of seconds and a cancelled
job should not have to wait one out.

The pacer's randomness is independent of the global `random` module. A scrape whose timing
becomes reproducible because unrelated code called `random.seed()` has lost the property this
module provides.

## Learning the limit

A `429` says the address works and is being asked for too much. The remedy is arithmetic
here, not a new address.

`Pacer.throttled` widens the interval — multiplying by `backoff_factor`, or adopting the
server's own `Retry-After` when it supplied one, because a number the server chose beats one
this library guessed. The widened value is capped by `max_interval` so a hostile site cannot
ratchet a run to a standstill, and it is written to `scraper.memory` so the next run starts
there.

Convergence downward is slow on purpose. A site that let one request through quickly has not
necessarily raised its limit, and snapping straight to the fast value is how a run earns a
throttle it then blames on the address.

## Arriving the way a visitor arrives

Landing directly on a deep URL with no referrer and no prior history is a navigation pattern
no human produces. Two mechanisms address it.

**Warm-up.** The first request to an origin within `warmup_ttl` visits the homepage first.
`scraper.pacing.needs_warmup` decides; a request already aimed at the homepage never needs
one, or the warm-up recurses. The planner asks for one (`Move.WARM`) when the binding layer
reads accumulated history and the origin has not been warmed — because arriving cold is
cheaper to fix than anything else on that axis.

**The referrer chain.** `scraper.pacing.Trail` tracks the page in view per origin and emits
`Referer` plus fetch metadata for the next navigation. Sub-resources — images, API calls —
are marked `navigation=False`, which changes the fetch metadata and keeps them out of the
chain, because a chain threaded through every image is not one a browser produces.

## One address, held

Addresses are leased **per origin** and held (`scraper.ExitPool`). Rotation happens on
evidence, never on a timer, because both a clearance and the accumulated history are bound to
the address.

`max_sessions_per_exit` caps concurrent requests sharing one address. Concurrent sessions per
address is itself a behavioural signal, so the value is clamped to the low single digits
rather than trusted.

## Persistence

A process that forgets everything on exit can never accumulate anything, which is why
`scraper.Memory` is on by default. Per origin it keeps:

- the layer last found binding — the single most valuable thing to persist, because it is
  what stops the next run from spending a browser launch on a site that only needed a header
  profile, or a hundred retries on one that needed the browser;
- the tier that worked;
- a clearance and the identity it is bound to, so a solve is reused rather than repeated;
- the learned interval;
- JSON endpoints seen behind the HTML;
- URLs that behaved like decoys, which is the only durable defence against a trap that
  returns no error.

One JSON file per data directory, written atomically, created `0600` because the clearance
cookies in it are credentials. Location is `ScraperConfig.data_dir`, defaulting to
`scraper.default_data_dir()` — which honours `SCRAPER_DATA_DIR` first so a deployment can put
it on a volume.

Set `remember=False` for tests and one-off scripts. Understand what it costs: every run then
rediscovers the binding layer with the same number of failed requests, and those failures are
themselves what this layer counts.

A file written by a newer schema is discarded rather than interpreted, and an unknown layer
number degrades to "no knowledge". A cold start is slow but correct; guessing is not.

The store is bounded, by age first and size second. An origin unseen for `FORGET_AFTER` is
dropped, and beyond `MAX_ORIGINS` the least recently seen go. Age comes first because the two
answer different questions: what is stored is a conclusion about a site's *current*
configuration, so an old one is worth less than the cold start that replaces it, and a small
cap should not keep a stale binding layer alive just because the store was quiet. Both are
`Memory` arguments if the defaults do not suit the deployment.

```python
from scraper import Memory

memory = Memory(path, max_origins=4096, forget_after=7 * 86400)
```

## Two scrapers, one site

Two scrapers pointed at the same host, each with its own address, clock and cookie history, do
not present as one visitor going twice as fast. They present as two visitors who contradict
each other, arriving in bursts, one of them always cold.

`scraper.SharedState` is the fix. What it shares is deliberately more than a rate limit: the
address, the identity, the accumulated history, the learned interval, the referrer chain and
the decoy list are all properties of the *zone*, and splitting any one of them re-creates the
contradiction.

```python
from scraper import Scraper, ScraperConfig, SharedState

config = ScraperConfig()
state = SharedState.create(config)

one = Scraper(origin="https://example.com", config=config, state=state)
two = Scraper(origin="https://example.com", config=config, state=state)
```

What stays per-scraper is what genuinely differs: the origin it points at, its abort signal,
its default headers, its parser.

### One state per site, one store for the process

A consumer that crawls many sites at once may want state per site — a separate address, clock
and referrer chain per zone — while still persisting everything to one file. Pass the store:

```python
from scraper import Memory, SharedState

memory = Memory(config.memory_path)
per_site = {host: SharedState.create(config, memory=memory) for host in hosts}
```

Building a `Memory` per state instead is a silent way to lose everything learned. Each store
holds every origin *it* knows and `flush()` writes the whole file, so two stores on one path do
not merge — the later write is the complete file, and whatever the other one had accumulated is
gone. Sharing the store is what makes per-site state safe.

### Cancelling one caller

Sharing has one cost, and this is the answer to it. `abort()` stops everything the scraper is
doing, which is what shutdown wants and not what one job among several wants. So a retrieval can
carry its own switch:

```python
job = threading.Event()

scraper.get(url, signal=job)          # cancelled by job, or by abort()
job.set()                             # stops that retrieval, nothing else
```

Anything with `is_set()` works. It is *combined* with the scraper's own signal rather than
replacing it, so `abort()` keeps its meaning. The signal reaches the two places a cancelled
retrieval actually spends its time — the pacing wait, whose tail is measured in tens of seconds,
and the download loop, which checks between chunks — as well as the pre-send check, so a
cancelled request never reaches the network at all.

Without this the only lever was the shared attribute, so cancelling one job cancelled every job
on the origin — which pushed consumers into a scraper per thread, losing exactly the per-origin
state that sharing exists for.
