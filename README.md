# LNCrawl Scraper

[![CI](https://github.com/lncrawl/scraper/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lncrawl/scraper/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/lncrawl-scraper.svg)](https://pypi.org/project/lncrawl-scraper/)

HTTP scraper with Cloudflare bypass, browser fingerprint impersonation, stealth mode, proxy support, and a null-safe BeautifulSoup wrapper.

## Features

- **Cloudflare bypass** — handles CF challenges v1, v2, v3, and Turnstile transparently
- **Browser fingerprint impersonation (on by default)** — a `curl_cffi` transport
  reproduces a real Chrome/Firefox TLS (JA3/JA4) **and** HTTP/2 fingerprint, with an
  automatic fallback to the legacy urllib3 transport
- **Composable engine** — a middleware pipeline over a pluggable transport
  (`scraper.engine`), so throttling, stealth, proxy, challenge solving, and retries
  are independent, swappable stages
- **Browser-assisted clearance** — reuse a `cf_clearance` cookie solved by a real
  browser for managed-challenge / Turnstile sites
- **Accurate Client Hints** — `sec-ch-ua` / `sec-fetch-*` derived from the chosen UA
- **Stealth mode** — human-like delays, randomized headers, browser quirks
- **Proxy support** — round-robin proxy rotation with Tor integration and direct fallback
- **Rate limiting** — configurable per-request intervals and concurrency cap
- **`PageSoup`** — null-safe BeautifulSoup wrapper; selection methods never return `None`
- **HTTP helpers** — `get_soup`, `get_json`, `get_image`, `get_file`, and more

## Installation

```bash
pip install lncrawl-scraper   # includes curl_cffi for browser fingerprint impersonation

# optional extras:
pip install "lncrawl-scraper[brotli]"   # decode brotli (br) responses (brotli)
pip install "lncrawl-scraper[image]"    # get_image() support (Pillow)
pip install "lncrawl-scraper[all]"      # all of the above
```

`curl_cffi` ships as a core dependency so impersonation works out of the box; the
remaining extras are optional and degrade gracefully when absent — without
`brotli`, the scraper simply stops advertising `br` encoding so responses stay
decodable.

## Quick start

```python
from scraper import Scraper

s = Scraper(origin="https://example.com")

# HTML
soup = s.get_soup("https://example.com/page")
title = soup.select_one("h1.title").text          # "" if not found, never raises
links = [a["href"] for a in soup.select("a")]

# JSON
data = s.get_json("https://example.com/api/data")

# File download
s.get_file("https://example.com/file.zip", output_file="file.zip")

# Image (returns PIL.Image)
img = s.get_image("https://example.com/cover.jpg")
```

## Examples

Runnable examples live in [`examples/`](examples/) — run any with
`uv run python examples/<file>.py`.

| Example                                                             | Shows                                                          |
| ------------------------------------------------------------------- | -------------------------------------------------------------- |
| [01_basic_html.py](examples/01_basic_html.py)                       | Fetch a page and extract data with `get_soup` / `PageSoup`     |
| [02_pagesoup_parsing.py](examples/02_pagesoup_parsing.py)           | PageSoup tour: CSS select, attrs, navigation, XPath            |
| [03_json_api.py](examples/03_json_api.py)                           | `get_json` / `post_json` and raw `Response` access             |
| [04_files_and_images.py](examples/04_files_and_images.py)           | `get_file` (streamed, atomic) and `get_image` (Pillow)         |
| [05_forms_cookies_headers.py](examples/05_forms_cookies_headers.py) | `submit_form`, `set_header`, `set_cookie`, `reset`             |
| [06_configuration.py](examples/06_configuration.py)                 | `ScraperConfig`, `default_config()`, stealth, browser identity |
| [07_impersonation.py](examples/07_impersonation.py)                 | Real browser TLS/HTTP-2 fingerprint via `impersonate`          |
| [08_browser_clearance.py](examples/08_browser_clearance.py)         | Reuse a `cf_clearance` solved by a real browser                |
| [09_proxies_and_tor.py](examples/09_proxies_and_tor.py)             | Proxy rotation and Tor identity refresh                        |
| [10_concurrency_and_abort.py](examples/10_concurrency_and_abort.py) | Threaded fetches and cooperative `abort()`                     |
| [11_error_handling.py](examples/11_error_handling.py)               | HTTP, Cloudflare, and abort error handling                     |

## Configuration

Pass a `ScraperConfig` for full control:

```python
from scraper import Scraper
from scraper.config import ScraperConfig, ProxyConfig, StealthConfig, BrowserConfig

config = ScraperConfig(
    min_request_interval=2.0,
    max_concurrent_requests=1,
    rotate_tls_ciphers=True,
    stealth=StealthConfig(
        enabled=True,
        min_delay=1.0,
        max_delay=3.0,
        human_like_delays=True,
        randomize_headers=True,
        browser_quirks=True,
    ),
    browser=BrowserConfig(
        browser="firefox",
        platform="windows",
        desktop=True,
    ),
    proxy=ProxyConfig(
        fallback_to_direct=True,
        proxy_urls=[
            "socks5://torproxy:9150",
            "http://proxy1:8080",
            "http://proxy2:8080",
        ],
    ),
)

s = Scraper(origin="https://example.com", config=config)
```

Or start from the library's tuned defaults and tweak:

```python
from scraper import Scraper, default_config

config = default_config()
config.max_concurrent_requests = 4
s = Scraper(origin="https://example.com", config=config)
```

## Browser fingerprint impersonation

A plain `requests` stack has a fixed OpenSSL TLS fingerprint and only speaks
HTTP/1.1 — both of which modern Cloudflare detects. The `curl_cffi` transport
reproduces a real browser's TLS (JA3/JA4) and HTTP/2 fingerprint, and
**`default_config()` enables it (`impersonate.target = "chrome"`)** out of the box.
Pick a different target, or disable impersonation to force the legacy urllib3
transport:

```python
from scraper import Scraper, default_config

config = default_config()
config.impersonate.target = "firefox"   # or "chrome124", "safari17_0", "edge", …
s = Scraper(origin="https://example.com", config=config)

# disable impersonation → urllib3 transport
config.impersonate.target = None
```

The spoofed User-Agent family and Client Hints are aligned with the impersonation
target automatically. If `curl_cffi` cannot be imported, the engine transparently
falls back to the urllib3 transport.

## Browser-assisted clearance

For managed challenges / Turnstile that can't be solved headlessly, solve the
challenge once in a real browser (e.g. `nodriver`/Playwright), then hand the
`cf_clearance` cookie and the browser's **exact** User-Agent to the session:

```python
s.apply_browser_clearance(
    "https://protected.example.com",
    cf_clearance="<value from the browser>",
    user_agent="<the browser's exact UA>",
    cookies={"__cf_bm": "<optional>"},
)
```

## `Scraper` API

| Method                            | Description                                         |
| --------------------------------- | --------------------------------------------------- |
| `get(url, **kwargs)`              | GET request, returns `Response`                     |
| `post(url, **kwargs)`             | POST request, returns `Response`                    |
| `ping(url, timeout=5)`            | HEAD request for reachability check                 |
| `submit_form(url, data, ...)`     | POST with form encoding or multipart                |
| `get_json(url, headers, ...)`     | GET and parse response as JSON                      |
| `post_json(url, data, ...)`       | POST and parse response as JSON                     |
| `get_soup(url, headers, ...)`     | GET and return a `PageSoup`                         |
| `post_soup(url, data, ...)`       | POST and return a `PageSoup`                        |
| `get_image(url, ...)`             | GET and return a `PIL.Image`                        |
| `get_file(url, output_file, ...)` | Stream download to file (abort-safe)                |
| `make_soup(data, encoding, ...)`  | Parse `Response`, `bytes`, or `str` into `PageSoup` |
| `set_header(key, value)`          | Set a default session header                        |
| `set_cookie(name, value)`         | Set a session cookie                                |
| `put_cookie(name, value, ...)`    | Set a cookie on session + jar                       |
| `apply_browser_clearance(...)`    | Reuse a browser cf_clearance                        |
| `abort()`                         | Stop pending/in-flight requests                     |
| `reset()`                         | Clear cookies, headers, and state                   |

`Scraper` composes a `scraper.engine.Engine` (available as `scraper.engine`); it is
not a `requests.Session` subclass, but mirrors the common verb methods (`get`,
`post`, `head`, `put`, `patch`, `delete`, `options`) plus `headers`/`cookies`.

## `PageSoup` API

`PageSoup` wraps a BeautifulSoup `Tag`. Every selection method returns a `PageSoup` (never `None`); an empty `PageSoup` is falsy and returns safe defaults for all operations.

```python
soup = s.get_soup("https://example.com")

# Selection
soup.select("ul li")                 # → List[PageSoup]
soup.select_one(".title")            # → PageSoup (empty if not found)
soup.find("div", class_="content")  # → PageSoup
soup.find_all("a")                   # → List[PageSoup]
soup.xpath("//div[@class='body']")  # → List[PageSoup]
soup.closest(".container")          # → nearest matching ancestor
soup.parents(".wrapper")            # → generator of matching ancestors

# Attribute access
el["href"]                           # get_attr shorthand, returns "" if missing
el.get_attr("src", default="/")
el.has_attr("data-id")

# Text / HTML
el.text                              # stripped text, always str
el.get_text(separator="\n")
el.inner_html
el.outer_html

# Navigation
el.parent
el.children                          # List[PageSoup], excludes text nodes
el.next_sibling
el.previous_sibling

# Mutation
soup.decompose(".ads")               # remove elements matching selector
el.replace_with(new_el)
el.append(child)
```

## Development

[uv](https://docs.astral.sh/uv/) is required. Clone the repo and install all dependencies including dev extras:

```bash
git clone https://github.com/lncrawl/scraper.git
cd scraper
uv sync --all-groups --all-extras
```

Tasks are managed with [poethepoet](https://poethepoet.natn.io/):

| Command               | Description                           |
| --------------------- | ------------------------------------- |
| `uv run poe lint`     | Run ruff + pyright                    |
| `uv run poe lint-fix` | Auto-fix ruff violations and reformat |
| `uv run poe test`     | Run the test suite                    |
| `uv run poe build`    | Lint → test → build wheel             |
| `uv run poe publish`  | Build → publish to PyPI               |

## Testing

Tests live in [`tests/`](tests/) and run with [pytest](https://pytest.org):

```bash
uv run poe test

# or directly
uv run pytest
uv run pytest -v                   # verbose
uv run pytest tests/test_dummy.py  # a single file
```

Mock HTTP with [responses](https://github.com/getsentry/responses) (a dev dependency) so tests make no real network calls.

## Acknowledgements

Inspired by,

- [VeNoMouS/cloudscraper](https://github.com/VeNoMouS/cloudscraper)
- [intoli/user-agents](https://github.com/intoli/user-agents)
- [lexiforest/curl_cffi](https://github.com/lexiforest/curl_cffi)

## License

[Apache-2.0](LICENSE)
