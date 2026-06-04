# scraper

HTTP scraper with Cloudflare bypass, stealth mode, TLS rotation, proxy support, and a null-safe BeautifulSoup wrapper.

## Features

- **Cloudflare bypass** — handles CF challenges v1, v2, v3, and Turnstile transparently
- **Stealth mode** — human-like delays, randomized headers, browser quirks
- **TLS cipher rotation** — cycles cipher suites to avoid TLS fingerprinting
- **Proxy support** — round-robin proxy rotation with Tor integration and direct fallback
- **Rate limiting** — configurable per-request intervals and concurrency cap
- **`PageSoup`** — null-safe BeautifulSoup wrapper; selection methods never return `None`
- **HTTP helpers** — `get_soup`, `get_json`, `get_image`, `get_file`, and more

## Installation

```bash
pip install lncrawl-scraper
```

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

## Configuration

Pass a `CloudScraperConfig` for full control:

```python
from scraper import Scraper
from scraper.config import CloudScraperConfig, ProxyConfig, StealthConfig

config = CloudScraperConfig(
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
    proxy=ProxyConfig(
        proxy_urls=["http://proxy1:8080", "http://proxy2:8080"],
        fallback_to_direct=True,
    ),
    browser={"browser": "firefox", "platform": "windows", "desktop": True},
)

s = Scraper(origin="https://example.com", config=config)
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
| `reset()`                         | Clear cookies, headers, and state                   |

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
uv run pytest tests/test_soup.py   # single file
uv run pytest -v                   # verbose
```

HTTP-dependent tests use [responses](https://github.com/getsentry/responses) to mock requests — no real network calls are made.

## License

[Apache-2.0](LICENSE)
