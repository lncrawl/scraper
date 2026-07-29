# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

  The reason is what the pool weighs the report by, so a 429 that no challenge
  handler claimed is sent as `rate_limited` rather than as a generic failure. A
  throttle says the exit works and is being asked for too much: reported as a
  block it would retire a working exit, and the next one is throttled just the
  same.
- `examples/11_tor_pool.py`.

### Fixed

- Rotating a proxy now drops pooled connections. A live keep-alive stays bound
  to its original exit, so without this the exit IP appeared not to change until
  the socket happened to be evicted.

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

[0.2.4]: https://github.com/lncrawl/scraper/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/lncrawl/scraper/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/lncrawl/scraper/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/lncrawl/scraper/compare/v0.1.2...v0.2.1
[0.1.2]: https://github.com/lncrawl/scraper/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/lncrawl/scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
