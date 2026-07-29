# Migrating from 0.2.x

1.0 is a rewrite with no compatibility shims. Almost every import changes. This page is the
mapping, and the reasons — which matter, because several of the removals were features that were
making things worse rather than merely being unused.

## Why the break

The 0.2.x engine was a `requests.Session` subclass that solved Cloudflare challenges in-process
with a JavaScript interpreter, rotated TLS ciphers per request, randomised headers, and rotated
proxies on every 403. Against the model in [model.md](model.md), four of those are wrong in ways
that cannot be patched:

- **In-process challenge solving** cannot keep up with the challenge format, and the layer it
  targeted is only reachable by a real browser.
- **Cipher rotation** does not produce a browser fingerprint; it produces an unstable one — and
  an unstable TLS fingerprint invalidates any clearance bound to it. The feature broke the layer
  above it.
- **Header randomisation** overwrites a profile's correctly ordered header set. Order is read,
  not just values.
- **Rotating on 403** discards a working identity in two of the three cases a 403 can mean, and
  resets the accumulated history that layer 8 measures.

And impersonation — the one thing that actually clears layers 2–5 — was an opt-in extra, which
made the common path the broken one.

## Import mapping

| 0.2.x | 1.0 |
| --- | --- |
| `Scraper` | `Scraper` — same name, no longer a `requests.Session` subclass |
| `ScraperEngine` | gone; `Scraper` is the only entry point |
| `default_config()` | `ScraperConfig()` |
| `ScraperConfig` | `ScraperConfig`, fields almost entirely different |
| `StealthConfig` | gone; see `PacingPolicy` |
| `BrowserConfig` | gone; the impersonation profile supplies the User-Agent |
| `ProxyConfig`, `ProxyUrl` | `ExitSpec` in `ScraperConfig.exits` |
| `TorProxyUrl` | `ExitSpec(kind=ExitKind.TOR)`; no control-port rotation |
| `TorPoolProxyUrl` | `TorPoolSpec` |
| `SharedLimiter` | `SharedState` — shares more, see below |
| `AbortedException` | `Aborted` |
| `CloudflareException` and subclasses | `Blocked` / `Impassable` / `Exhausted`, each carrying `.layer` |
| `PageSoup` | `PageSoup` — unchanged |
| `apply_browser_clearance()` | gone; a solver returns a `Clearance` bound to an `Identity` |
| `scraper.engine.*` | gone |

`Scraper`'s ergonomic surface is intact: `get`, `post`, `head`, `ping`, `get_soup`, `post_soup`,
`get_json`, `post_json`, `get_file`, `get_image`, `submit_form`, `make_soup`, `abort`, `close`.
`last_soup_url` is now `last_url`.

## Configuration mapping

| 0.2.x field | 1.0 |
| --- | --- |
| `impersonate="chrome"` | `impersonate="chrome"` — now the default, not an extra |
| `min_request_interval`, `min_request_interval_fast` | `pacing=PacingPolicy(interval=…)`; drawn from a distribution, not a floor |
| `max_concurrent_requests` | `max_sessions_per_exit` — per address, which is what the signal is |
| `rotate_tls_ciphers` | removed. It broke clearance reuse. |
| `stealth.randomize_headers` | removed. It broke header order. |
| `browser=BrowserConfig(...)` | removed. The profile supplies the User-Agent; see below. |
| `cipher_suite`, `ecdh_curve`, `ssl_context` | removed. The impersonation profile owns the ClientHello. |
| `disable_v1/v2/v3/turnstile`, `solve_depth`, `double_down` | removed with the in-process solvers |
| `auto_refresh_on_403`, `max_403_retries` | `max_attempts`, `max_rotations` — and the planner decides which |
| `session_refresh_interval` | removed. Identity is held until evidence says otherwise. |
| `pre_hook`, `post_hook` | removed. Supply a `Transport` or a tier. |
| `verify_ssl` | `verify_tls` |
| `proxy.fallback_to_direct` | removed. An empty `exits` list is a direct connection; a silent fallback hid the reason a scrape failed. |

## Behaviour changes to expect

**Requests are slower by default, on purpose.** Pacing targets a mean interval drawn from a
distribution, and the first request to an origin is preceded by a homepage visit. Both address
layer 8, which 0.2.x did not model. Set `pacing=PacingPolicy(interval=0.0, warmup=False)` to
turn it off and understand you have turned off the answer to the hardest layer.

**State is written to disk.** `ScraperConfig.data_dir`, defaulting to
`scraper.default_data_dir()`. `remember=False` disables it.

**A challenged site needs a solver.** There is no in-process substitute. Without
`ScraperConfig.browser` a challenge raises `Exhausted` naming what is missing, which is
honest — 0.2.x would attempt a solve and usually fail.

**Rotation happens less.** On a throttle it does not happen at all. When every configured
address is a datacenter or Tor range, a reputation block stops with an explanation rather than
cycling the pool.

**A `200` carrying a challenge is now a failure.** 0.2.x would hand you the interstitial.

**Layers 18 and 19 raise immediately.** No retry loop against a wall.

## The User-Agent inversion

0.2.x generated a User-Agent and imposed it on the transport. 1.0 takes it *from* the transport:
an impersonation profile already emits a complete, correctly ordered browser header set, and
writing a User-Agent over it is how a client ends up claiming to be one browser while its
ClientHello says another.

`Identity.user_agent` is therefore empty — meaning "whatever the profile sends" — until a real
browser earns a clearance, at which point the browser becomes the source of truth and its exact
User-Agent is reproduced, because that is what the clearance is bound to. The client hints are
re-derived from it at the same time.

If you need a specific User-Agent, pass it as a request header. It will be honoured, and
`scraper.identity.OVERRIDABLE` is the list of headers the identity may replace on a profile.

## `SharedLimiter` → `SharedState`

`SharedLimiter` shared a throttle clock and a concurrency semaphore. `SharedState` shares the
address, the identity, the accumulated history, the learned interval, the referrer chain and the
decoy list — because those are all properties of the *zone*, and splitting any one of them makes
two scrapers look like two visitors who contradict each other rather than one visitor.

```python
config = ScraperConfig()
state = SharedState.create(config)
first = Scraper(origin="https://site.test", config=config, state=state)
second = Scraper(origin="https://site.test", config=config, state=state)
```

## A minimal port

```python
# 0.2.x
from scraper import Scraper, default_config
from scraper.config import TorProxyUrl

cfg = default_config()
cfg.impersonate = "chrome"
cfg.proxy.proxy_urls = [TorProxyUrl()]
cfg.min_request_interval = 2.0
scraper = Scraper(origin="https://site.test", config=cfg)
```

```python
# 1.0
from scraper import ExitKind, ExitSpec, PacingPolicy, Scraper, ScraperConfig

config = ScraperConfig(
    exits=[ExitSpec(url="socks5h://127.0.0.1:9050", kind=ExitKind.TOR)],
    pacing=PacingPolicy(interval=2.0),
)
scraper = Scraper(origin="https://site.test", config=config)
```

Note what the second one tells you that the first could not: declaring the exit as `TOR` means
`scraper.explain(url)` will report that no configured address clears layer 1, and a reputation
block will say so instead of cycling the pool.

## First thing to do after porting

Run something, then read `scraper.explain(url)`. It names the layer the library concluded is
binding, the tier it settled on, and what it has available — which is the fastest way to find out
whether your configuration reaches the site you care about. See [diagnostics.md](diagnostics.md).
