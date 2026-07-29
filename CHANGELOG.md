# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - Unreleased

A complete rewrite. There are no compatibility shims: almost every import changes.
[docs/migration.md](docs/migration.md) is the mapping, and
[docs/model.md](docs/model.md) is why.

### The change

The library is now organised around a model of what it is up against, rather than
around a request pipeline with anti-detection features bolted on. A mitigation engine
folds many detectors into one score, and admission is close to a conjunction — so the
weakest layer bounds the outcome, and effort spent on any other layer buys nothing.
Detectors that read an artifact the client *emits* are reproducible; detectors that
read a property it must *possess* are not.

Every behaviour below follows from those two statements.

### Breaking

- **`ScraperEngine` is gone**, and `Scraper` is no longer a `requests.Session`
  subclass. The transport is a two-method seam, so the escalation ladder can move
  between transports and every tier is testable without a network.
- **The in-process Cloudflare solvers are gone** (v1, v2, v3, Turnstile), along with
  the `exejs` dependency. They cannot keep up with the challenge format, and the layer
  they targeted is only reachable by a real browser. A challenged site now needs
  `ScraperConfig.browser`; without one it raises and says so, instead of attempting a
  solve that usually failed.
- **TLS cipher rotation is gone.** Reordering the cipher list per request does not
  produce a browser fingerprint, it produces an unstable one — and an unstable TLS
  fingerprint invalidates any clearance bound to it. The feature was breaking the layer
  above it.
- **Header randomisation is gone.** Header *order* is read, not just header values. An
  impersonation profile emits a complete, correctly ordered set;
  `scraper.identity.OVERRIDABLE` now caps what may be written over it.
- **The User-Agent is taken from the transport, not imposed on it.** The generated-UA
  machinery is gone. A profile supplies the User-Agent until a real browser earns a
  clearance, at which point the browser is the source of truth and its exact string is
  reproduced — because that is what the clearance is bound to.
- **Impersonation is a core dependency**, not the `impersonate` extra. An ordinary
  Python client fails layers 2–5 in the first round trip, so a build without it is not
  a degraded scraper but one that cannot reach a protected page.
- `default_config()`, `StealthConfig`, `BrowserConfig`, `ProxyConfig`, `ProxyUrl`,
  `TorProxyUrl`, `apply_browser_clearance()` and the `scraper.engine` package are
  removed. `SharedLimiter` becomes `SharedState`. `AbortedException` becomes `Aborted`,
  and the `CloudflareException` hierarchy becomes `Blocked` / `Impassable` /
  `Exhausted`, each carrying the layer it is attributed to — or `None`, when the
  failure is ours rather than the site's. Code that dereferences `exc.layer` or
  `exc.layer_info` must handle that; a type checker will point at every such place.
- New extras: `browser` (nodriver) and `botauth` (cryptography). `impersonate` is gone.

### Added

- `scraper.layers` — the model as code: nineteen layers, what each reads
  (`Trait`), what this library does about it (`Stance`), the bound (`weakest`) and the
  arithmetic that shows why fixing the wrong layer gains nothing (`marginal_gain`).
  Layers 2–5 are declared as one barrier, and `expand()` keeps any reach set closed
  over the group.
- `scraper.diagnosis` — a response becomes a binding layer plus an action, as a pure
  function over primitives. Three readings that a status-code table gets wrong: a `200`
  carrying a challenge is a failure, a `429` is a pacing problem rather than a spent
  address, and a `403` with error 1010 is about the automation channel rather than the
  address. A `407` is reported as our own proxy credential, not as the site needing a
  login.
- `scraper.planner` — chooses the cheapest capability whose reach covers the binding
  layer. Three rules that contradict the conventional table: a possessed property is
  never rotated away from; rotation requires somewhere better to go, so a pool of
  published ranges stops with an explanation instead of cycling; and escalation only
  goes to a tier that actually reaches the layer. Repeated failure at an
  already-covered emitted layer is re-attributed to the per-zone composite, because
  recurrence is the only evidence available from outside.
- `scraper.identity` — the emitted signals as one indivisible thing.
  `Clearance.usable_by()` refuses to replay a clearance under a different identity,
  which makes the classic rotating-proxy failure structurally impossible rather than
  merely documented.
- `scraper.exits` — addresses described by *kind*, and `ExitKind.reach` deciding what
  layer 1 can be told. Leased per origin and held; rotation happens on evidence, never
  on a timer. tor-pool support is retained, and a failure report now carries the kind
  derived from the binding layer.
- `scraper.pacing` — inter-request gaps drawn from a gamma distribution rather than set
  to a constant, occasional reading pauses, homepage warm-up, and a real referrer chain
  with fetch metadata. Throttles widen a learned per-origin interval that persists.
- `scraper.memory` — per-origin state that survives the process: the binding layer, the
  working tier, a clearance and the identity it belongs to, the learned interval,
  observed JSON endpoints, and recorded decoy URLs. On by default, because the layer it
  exists for cannot be satisfied by a process that forgets.
- `scraper.state.SharedState` — shares the address, identity, history, pacing, referrer
  chain and decoy list between scrapers pointed at one site. Two scrapers with separate
  state do not look like one visitor going faster; they look like two who contradict
  each other.
- `scraper.tiers` — `archive` (Wayback, serving the original URL so links resolve
  against the real site), `direct` (the baseline), `clearance` (solve once, reuse many,
  delegating every request to `direct` so the solve and the fetch cannot diverge), and
  `managed` (a provider callable; none bundled, since a wrapper that guesses a vendor
  format wrong fails in a way that looks like the site blocking you).
- `scraper.browser` — a two-method `BrowserSolver` protocol, a `nodriver` adapter, and
  `CallableSolver` for anything else. Headed and WebRTC-disabled by default, both
  deliberately: a headless build reports a software renderer, and a STUN request reports
  the host's real address past the proxy without any request failing. One browser
  profile directory per address.
- `scraper.links` — `safe_links` enumerates only anchors a person could click, and
  `TopicGuard` notices content that stopped being about the site. This is the only
  defence against the one layer that returns no error, so the guard runs on the way out
  rather than on demand.
- `scraper.botauth` — RFC 9421 Ed25519 request signing with the `web-bot-auth` tag,
  plus the key directory document to publish. The one layer with no bypass, and for a
  crawler willing to identify itself, the cheapest tier in the stack.
- `Scraper.explain(url)` and `Scraper.knows(url)` — what the library concluded is
  binding, which tier settled, how fast it has learned it can go, and what it has
  available.
- `livetest/` — a live verification harness that exercises every path against real
  Cloudflare deployments, using every host in lightnovel-crawler's source index as
  the corpus. Separate from `tests/`, which stays offline. See
  [livetest/README.md](livetest/README.md); the current run is
  `livetest/report.html`.

### Fixed before release, found by live traffic

Eleven of the fifteen defects fixed before release were found this way. These are not
regressions from 0.2.x — they are defects in the new code that a
stubbed transport cannot see. Each has a regression test.

- **Cloudflare's injected JavaScript-Detections script was read as a challenge.**
  The script is served from `/cdn-cgi/challenge-platform/scripts/jsd/…` on ordinary
  *successful* pages, and that path was a challenge marker — so content pages were
  diagnosed as interstitials. Measured across two live populations before changing
  it: the bare prefix appeared on 9 of 10 normally-served pages, the challenge-only
  `/h/` orchestrate sub-path on none. 18 of 22 hosts reported as challenged were
  serving content fine. The same marker also stopped the browser solve loop from
  ever detecting "cleared", so every solve burned its full timeout.
- **The archive tier could never find a capture.** A negative Wayback CDX `limit`
  is documented as "the last N rows" but returns an empty body once a filter is
  applied. The query is now bounded server-side and the newest rows taken from the
  tail. Separately, an unbounded query timed out on popular URLs, and a
  rate-limited index was reported as "nothing archived" — all three produced the
  same misleading message, so a lookup failure, an empty index and an age limit now
  say which they are, and the index is retried once.
- **Real navigation was being dropped as decoy content.** An anchor containing only
  an icon-font element counted as "nothing rendered", and a URL took the verdict of
  whichever anchor appeared first — so a card's empty overlay anchor rejected a page
  its own text anchor linked to. On one real page 11 of 11 rejections were wrong.
- **A stop could advise configuring a capability that was already configured.** A
  browser solver that ran and produced no clearance yielded "Configure a browser
  solver". The message now says the tier ran and failed, and quotes what it
  reported.
- **Rotating with nowhere to go spent the rotation budget on one address.** Found
  against a host that bans this machine's ASN outright. The pool now reports whether
  an alternative exists, and with none the stop is immediate.
- **A proxy refusing our own credential was diagnosed as the site's IP reputation.**
  tor-pool 0.2 enforces authentication, and a rejected SOCKS5 handshake never becomes
  an HTTP response — so it reached `diagnose_transport`, which blamed the exit for
  every proxied transport error. Three wrong things followed: the address was rotated
  though nothing was wrong with it, the pool was told a healthy exit was `blocked`,
  and layer 1 was written to the origin's persisted profile — so a missing token left
  behind a permanent verdict that the *site* refuses this address. A proxy that
  refuses us is now `REFUSE` with no layer, matching how HTTP 407 was already handled.
  The distinction is drawn on curl's wording rather than the exception class, because
  an unreachable destination reported through a SOCKS5 reply raises the same
  `ProxyError` and that one really is evidence about the exit.
- **A failure with nothing to attribute was reported as layer 15.** `Blocked`
  required a layer, so a layer-less stop borrowed `Layer.WORKERS` — and "L15 Operator
  edge code" is indistinguishable from a Cloudflare Worker refusing the request.
  `Blocked.layer` is now `Optional` and renders as "no detection layer".
- **The `browser` extra failed with a raw dependency error on unsupported Pythons.**
  nodriver raises `TypeError` before 3.10 and `SyntaxError` on 3.14; both now
  produce a message naming the version floor, and the extra is marked so it does not
  install where it cannot load.

### Fixed

- **One share-button link took out a page's whole crawl frontier.** `extract_host`
  read `urlparse(...).port`, which raises rather than returning `None` when the
  netloc's `:` is followed by something that is not a number — so an ordinary
  `whatsapp:send?text=…` anchor aborted `safe_links` for the entire page. The port is
  now optional and the host survives without it.

## [0.2.6] - 2026-07-29

### Added

- `TorPoolProxyUrl`: support for [tor-pool](https://github.com/lncrawl/tor-pool),
  which fronts many Tor instances with one sticky SOCKS port. The SOCKS5
  username is a session key, so a scrape keeps the same exit IP until it
  rotates; rotation goes through the pool's API and skips Tor's ~10s NEWNYM
  cooldown by reassigning to an already-built instance.

  Set `token` to a `proxy`-scoped token from the pool — it is required by
  tor-pool 0.2 and later, and is sent both as the SOCKS5 password and as a
  bearer token on the pool's API. Without it the pool answers `401`, and because
  those calls are best-effort the failure would otherwise pass as a warning while
  the pool quietly stopped hearing about soft blocks; that specific case is
  logged at `error` instead.
- `ProxyManager.report_failure()`: reports 403s, challenges, rate limits and
  transport errors to the pool. This is the only signal that catches a soft block
  — a proxy relaying bytes cannot see a 403 or a captcha inside an HTTPS tunnel —
  and it is what lets the pool quarantine a burnt exit. Sent automatically by the
  engine; call it directly when your own code detects a block.

  The pool weighs a report by what it says went wrong, so each one carries a
  *kind* as well as free text. A 429 that no challenge handler claimed is sent as
  `rate_limited` rather than as a generic failure: a throttle says the exit works
  and is being asked for too much, and reported as a block it would retire a
  working exit while the next one is throttled just the same.
- `scraper.engine.proxy_manager.FAILURE_KINDS`, the mapping from a failure reason
  to the kind sent alongside it. Reasons the engine raises itself are all
  covered, as is the pool's own
  vocabulary for callers passing it straight through; anything else is still
  reported and the pool counts it as unclassified. Sent explicitly rather than
  left to the pool to read out of the free text — its aliases exist for callers
  written before kinds did, so leaning on them means a vocabulary drift on either
  side quietly downgrades every report to unremarkable.
- `examples/13_tor_pool.py`.

### Fixed

- Rotating a proxy now drops pooled connections. A live keep-alive stays bound
  to its original exit, so without this the exit IP appeared not to change until
  the socket happened to be evicted.
- A pool that no longer knows a session is no longer a warning. Acting on a
  report, the pool takes the instance out of rotation and unpins its sessions, so
  the next report about that session answers `404` — routine, and the next
  request re-pins to a healthy instance, but it logged a warning per report for
  exactly the exit that was failing most.

## [0.2.5] - 2026-07-23

### Added

- `SharedLimiter`: a shareable throttle clock + concurrency semaphore. Give the
  same limiter to every `Scraper` that talks to one host — via the new
  `limiter=` constructor argument or `adopt_limiter()` — and the host-wide
  request rate and in-flight cap are enforced across all of them, while each
  scraper keeps its own cookies, headers, and abort signal.
- `Scraper` now forwards extra keyword arguments (e.g. `limiter=`) to the
  underlying `ScraperEngine`.

## [0.2.4] - 2026-06-28

### Fixed

- `ScraperEngine.close()` now closes the curl_cffi impersonation transport.
  Previously `requests.Session.close()` only disposed the standard urllib3
  adapters, leaking the curl_cffi session (libcurl handle + connection pool)
  when impersonation was enabled, causing per-job scrapers to accumulate native
  handles.
- Re-mounting TLS adapter to repalce the existing `https://` adapter in
  `self.adapters`. Close the old one first so its `urllib3` `PoolManager`
  (open sockets) and `SSLContext` are released now; cipher rotation
  re-mounts almost every request, so relying on cyclic GC lets these native
  handles accumulate.

## [0.2.3] - 2026-06-16

### Changed

- When fallback to direct is not allowed and no proxies are available, raising error even before
  making the request.

### Fixed

- The proxy configuration was returning raw string URL. Changed it to proper proxy object format.

## [0.2.2] - 2026-06-16

### Fixed

- On proxy error, re-enable fallback to direct

## [0.2.1] - 2026-06-16

### Changed

- **`scraper._engine` → `scraper.engine`** — the engine sub-package is now a public module; all internal imports updated accordingly
- **`scraper._utils` → `scraper.utils`** — utilities sub-package promoted to public
- **`scraper.exceptions`** — `CloudflareException` and `AbortedException` are now importable directly from `scraper.engine.exceptions` (previously `scraper._engine.exceptions`)
- **`ProxyManager` rewritten** — moved from `scraper._engine.proxy_manager` to `scraper.engine.proxy_manager`; richer proxy rotation, Tor support split into a dedicated example
- **`ScraperConfig` / proxy config consolidated** — `_engine/config.py` merged into `scraper/config.py`; `EventLock` moved to `scraper/utils/event_lock.py`
- **Examples reorganised** — `09_proxies.py`, `10_tor_proxy.py` added; `09_proxies_and_tor.py` removed

## [0.1.2] - 2026-06-13

### Changed

- Replace `quickjs` with `exejs` as the JavaScript engine for solving Cloudflare
  IUAM (V1) challenges. `exejs` is a pure-Python JS evaluator that eliminates the
  compiled C extension dependency, improving cross-platform compatibility and
  installation reliability.

## [0.1.1] - 2026-06-12

### Fixed

- `CipherSuiteAdapter.send()`: clear `check_hostname` and set `verify_mode = CERT_NONE`
  before urllib3 touches the shared SSL context when `verify=False` is requested.
  Previously, the SSL auto-retry in `_send()` would crash with
  `ValueError: Cannot set verify_mode to CERT_NONE when check_hostname is enabled`
  instead of gracefully falling back to unverified mode.

## [0.1.0] - 2026-06-04

Initial public release of `lncrawl-scraper`, extracted from
[lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).

### Added

- `Scraper` — a `requests.Session` subclass with transparent Cloudflare
  challenge handling (v1, v2, v3, Turnstile) and helpers: `get_soup`,
  `post_soup`, `get_json`, `post_json`, `get_file`, `get_image`, `submit_form`,
  `ping`.
- `PageSoup` — a null-safe BeautifulSoup wrapper; selection methods never return
  `None` and text/HTML accessors always return `str`.
- Typed configuration: `ScraperConfig`, `StealthConfig`, `ProxyConfig`,
  `BrowserConfig`, plus the `default_config()` factory.
- **Browser fingerprint impersonation** (`impersonate` extra): route requests
  through `curl_cffi` for a real Chrome/Firefox TLS (JA3/JA4) and HTTP/2
  fingerprint, with the spoofed User-Agent family aligned to the target.
- **Browser-assisted clearance**: `apply_browser_clearance()` to reuse a
  `cf_clearance` cookie + User-Agent solved by an external real browser.
- **Accurate Client Hints**: `sec-ch-ua` / platform / mobile derived from the
  chosen User-Agent (Chromium only) instead of hardcoded values.
- Stealth mode, proxy rotation with Tor identity refresh, TLS cipher rotation,
  rate limiting, and cooperative `abort()`.
- `py.typed` marker (PEP 561) and full type coverage.

[1.0.0]: https://github.com/lncrawl/scraper/compare/v0.2.6...v1.0.0
[0.2.6]: https://github.com/lncrawl/scraper/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/lncrawl/scraper/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/lncrawl/scraper/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/lncrawl/scraper/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/lncrawl/scraper/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/lncrawl/scraper/compare/v0.1.2...v0.2.1
[0.1.2]: https://github.com/lncrawl/scraper/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/lncrawl/scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
