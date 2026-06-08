# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking Changes

- `requests` dependency replaced with `httpx[http2,socks]`. Code that accesses
  `scraper.engine.transport` or constructs `requests`-style sessions directly
  must be updated.
- `Scraper` no longer has an `abort_event` attribute. Use `CancelToken` (now
  exported from `scraper`) and pass it as `cancel_token=` to any request method.
- `EventLock` removed from `scraper.utils` — it was an internal primitive
  superseded by `CancelToken`.
- `ClearanceSolver.solve_async` renamed to `solve` (the sync `solve` wrapper is
  gone; the solver protocol is now purely async).
- `CloudflareConfig.solver` (single) replaced by `CloudflareConfig.solvers`
  (list). The engine tries each in order.
- `ProxyManager.get_proxy()` now returns `str | None` instead of
  `dict[str, str] | None`.
- `RequestChain` and `RequestContext` (`engine.state` / `engine.context`) merged
  into `RequestState`; import paths change accordingly.
- `UrllibTransport` removed; `HttpxTransport` is the new fallback when
  `curl_cffi` is unavailable.

### Added

- `CancelToken` — thread-safe per-request cancellation. Exported from `scraper`.
- `RequestHeaders` utility (case-insensitive `dict` for HTTP headers) in
  `scraper.utils`.
- `ClearanceStore` — in-memory + optional on-disk cache for `cf_clearance`
  records, keyed by `(domain, proxy_key)`.
- `ClearanceResult` extended with `expires`, `cf_bm_expires`, and `proxy_key`
  fields.
- `HttpxTransport` — `httpx.AsyncClient`-backed fallback transport with
  per-proxy client pooling (replaces `UrllibTransport`).
- Engine pipeline is **fully async**: all middleware and the transport are
  coroutines; a daemon asyncio event loop runs in a background thread.
- `anyio[trio]` added as dev dependency; `respx` replaces `responses` for
  HTTP mocking in tests.

## [0.3.0] - 2026-06-06

### Breaking Changes

- `Scraper` is now a **composition facade** rather than a `requests.Session`
  subclass. The public helper API (`get_soup`, `get_json`, `get_file`, etc.)
  is unchanged, but `isinstance(scraper, requests.Session)` no longer holds
  and Session-internal overrides will not work.
- `engine/` and `utils/` replace the former private `_engine/` and `_utils/`
  packages. Direct imports from `scraper._engine.*` or `scraper._utils.*`
  must be updated to `scraper.engine.*` / `scraper.utils.*`.

### Added

- `CloudflareConfig`, `ImpersonateConfig`, `HttpVersion`, `ProxyUrl`, and
  `TorProxyUrl` are now exported from the top-level `scraper` package.
- `engine/` is now a documented public extension surface: the middleware
  pipeline and pluggable transport layer are importable and subclassable.
- New middleware pipeline with 11 single-concern middleware classes
  (`throttle`, `stealth`, `proxy`, `retry_403`, `challenge`, `tls_rotation`,
  `concurrency`, `refresh`, `ssl_retry`, `hooks`, `abort`) replacing the
  former monolithic engine.
- New `Transport` abstraction with `CurlCffiTransport` (primary) and
  `UrllibTransport` (fallback) implementations.

### Changed

- `curl_cffi` is now a **core dependency** (not an optional extra). The
  `impersonate` extra is retained for backwards compatibility but is a no-op;
  plain `pip install lncrawl-scraper` includes it automatically.
- Default impersonation target is `"chrome"` — requests ride a real Chrome
  TLS/HTTP-2 fingerprint out of the box.
- Updated dependencies to latest compatible versions.

### Fixed

- Atomic-group regex syntax that caused `SyntaxError` on Python 3.9 and 3.10.
- `None`-valued session options no longer forwarded to `curl_cffi`, fixing
  compatibility with older `curl_cffi` releases.

## [0.2.0] - 2026-06-05

### Changed

- `brotli` is now an optional extra instead of a core dependency. Install
  `lncrawl-scraper[brotli]` (or `[all]`) to decode brotli (`br`) responses;
  without it the scraper no longer advertises `br` encoding, so bodies stay
  decodable.
- `default_config()` no longer pins a Firefox/Windows identity — the default
  User-Agent (and its matching Client Hints) is now randomized across desktop
  browsers and platforms.

### Added

- `all` optional extra that installs every optional dependency
  (`brotli`, `image`, `impersonate`).

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

[0.3.0]: https://github.com/lncrawl/scraper/releases/tag/v0.3.0
[0.2.0]: https://github.com/lncrawl/scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
