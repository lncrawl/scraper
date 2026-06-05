# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/lncrawl/scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
