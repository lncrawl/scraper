# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.1]: https://github.com/lncrawl/scraper/compare/v0.1.2...v0.2.1
[0.1.2]: https://github.com/lncrawl/scraper/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/lncrawl/scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
