# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lncrawl-scraper` (import name `scraper`) is a standalone HTTP scraping library
extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).
It is a `requests.Session` subclass that transparently handles Cloudflare
challenges, plus a null-safe BeautifulSoup wrapper and a set of HTTP helpers.

Published to PyPI as `lncrawl-scraper`; imported as `scraper`. Targets Python
**3.9+**.

## Commands

Tooling is driven by [uv](https://docs.astral.sh/uv/) + [poethepoet](https://poethepoet.natn.io/).

```bash
uv sync                 # install deps + editable package into .venv
uv run poe lint         # ruff check + ruff format --check + pyright
uv run poe lint-fix     # ruff check --fix + ruff format
uv run poe test         # pytest
uv run poe cov          # pytest with coverage (term-missing + html + xml)
uv run poe build        # lint + test + uv build (wheel/sdist)
uv run poe publish      # build + uv publish
```

Always run `uv run poe lint` before considering a change done. CI
(`.github/workflows/ci.yml`) runs three jobs: `lint` (ruff + pyright), a
`build` matrix testing on Python 3.9–3.14, and `coverage` (which posts a PR
comment + badge via `python-coverage-comment-action` and a job-summary table).

## Architecture

The package is a thin, ergonomic layer over an in-house Cloudflare-bypass engine.

```text
src/scraper/
├── __init__.py         # public API + __version__ (via importlib.metadata)
├── session.py          # Scraper — the main class (subclasses ScraperEngine)
├── soup.py             # PageSoup — null-safe BeautifulSoup wrapper
├── config.py           # public config surface + default_config() factory
├── py.typed            # PEP 561 marker
├── _utils/             # internal helpers (event_lock, url_tools, file_tools)
└── _engine/            # internal Cloudflare-bypass engine (private)
```

### Layers

- **`Scraper`** ([session.py](src/scraper/session.py)) — the public entry point.
  Adds Origin/Referer injection, default timeouts, and helpers: `get_soup`,
  `post_soup`, `get_json`, `post_json`, `get_image` (returns a PIL Image),
  `get_file` (streamed, abortable), `submit_form`, `ping`. Subclasses
  `ScraperEngine`, so all of `requests.Session` is available too.
- **`PageSoup`** ([soup.py](src/scraper/soup.py)) — wraps a BeautifulSoup `Tag`.
  Selection methods (`select`, `select_one`, `find`, `xpath`, `closest`, …)
  always return `PageSoup`/`list`, never `None`; text/HTML accessors always
  return `str`. An empty `PageSoup` is falsy. Reach the raw tag via `.tag`.
- **`_engine/`** — the private engine: `ScraperEngine` (the `requests.Session`
  subclass with the full request pipeline) in `_engine/__init__.py`, plus CF
  challenge handlers v1/v2/v3 + Turnstile, TLS cipher rotation, stealth mode,
  proxy/Tor manager, and UA selection. It is implementation detail — nothing
  here is part of the public API except what `config.py`/`__init__.py`
  re-export.

### Cloudflare-bypass surface

The realistic ceiling of a `requests`-based engine is its TLS (JA3/JA4) and
HTTP/1.1 fingerprint — `set_ciphers()` in [tls.py](src/scraper/_engine/tls.py)
only reorders ciphers, so the ClientHello still reads as Python. Three features
push past that:

- **Impersonation transport** ([_engine/impersonate.py](src/scraper/_engine/impersonate.py)):
  when `ScraperConfig.impersonate` is set (e.g. `"chrome"`), `ScraperEngine.perform_request`
  routes through `curl_cffi` (curl-impersonate) for a real browser TLS + HTTP/2
  fingerprint, and adapts the result back into a `requests.Response`. The
  curl_cffi session is the cookie authority and is mirrored into `self.cookies`
  after each request (`_mirror_transport_cookies`). Cipher rotation is skipped
  while impersonating. Requires the `impersonate` extra (`curl_cffi`).
- **Client Hints** are derived from the actual UA in
  `UserAgent._client_hints` (Chromium only; Firefox sends none) so `sec-ch-ua`
  version/platform always match the User-Agent. `stealth.py` no longer hardcodes
  them — it only defaults the non-version-specific `Sec-Fetch-*` nav hints.
- **`apply_browser_clearance(domain, cf_clearance=, user_agent=, cookies=)`**
  injects a clearance solved by an external real browser; the UA must match the
  one that obtained it. `put_cookie` keeps the requests jar and the impersonation
  jar in sync.

### Configuration

All config flows through `ScraperConfig` (a dataclass with nested
`StealthConfig`, `ProxyConfig`, `BrowserConfig`). The public surface is
[config.py](src/scraper/config.py), which re-exports the dataclasses from
`_engine.config` and adds the `default_config()` factory:

```python
from scraper import Scraper, default_config
from scraper.config import BrowserConfig, StealthConfig

cfg = default_config()                 # fresh, fully-populated defaults
cfg.browser = BrowserConfig(browser="chrome", platform="darwin")
s = Scraper(origin="https://site.com", config=cfg)
```

- **`default_config()` returns a fresh instance every call.** Never reintroduce
  a shared module-level config singleton — `ScraperEngine` hands the nested
  `proxy`/`stealth` objects to managers that may mutate them, so sharing would
  leak state across `Scraper` instances.
- `ScraperConfig.browser` accepts `BrowserConfig | dict | None`; the dict form
  is accepted as a convenience and normalized via `asdict` in `UserAgent.load`.

## Conventions

- **Python 3.9 compatibility is mandatory.** Bare `X | Y` unions must not be
  *evaluated at runtime* — only use them in files that have
  `from __future__ import annotations`, or in pure annotations. Prefer
  `typing.Optional/Union` in new non-future-annotated modules. `importlib`,
  dataclasses, etc. must all work on 3.9.
- **Keep the public surface in public modules.** `_engine/` and `_utils/` are
  private; user-facing names live in `__init__.py`/`config.py` and are listed
  in `__all__`. Update `__all__` and the README when changing that surface.
- **`ruff`**: line-length 100, double quotes, `force-sort-within-sections`,
  combine-as-imports. **`pyright`** runs in `standard` mode over `src` + `tests`
  — keep it clean (use real `isinstance` narrowing rather than `is_dataclass`,
  which pyright doesn't narrow on).
- **Dependencies**: core runtime deps live in `[project.dependencies]`. Optional
  extras, all of which degrade gracefully when absent (and `all` installs every
  extra): `brotli` (decode `br` responses — `UserAgent.load` drops `br` from
  Accept-Encoding when `_brotli_available()` is false, so we never request an
  undecodable encoding), `image` (`Pillow`, for `get_image`), and `impersonate`
  (`curl_cffi`, for `ScraperConfig.impersonate`). Each is imported lazily. Add
  deps via `uv add` / `uv add --dev`.
- **Public API** is whatever `src/scraper/__init__.py` exports in `__all__`.
  Update it (and the README) when adding user-facing surface.

## Commit messages

Match the existing history (`git log`):

- **No type prefix.** Do NOT use Conventional Commits (`feat:`, `fix:`,
  `docs:`, …) — subjects are plain capitalized text.
- **Imperative mood**, capitalized first word, no trailing period, subject
  ≤ ~60 chars (e.g. `Add coverage reporting to CI`, `Restructure into src layout`).
- **Body only for non-trivial changes**: a blank line, then a short rationale
  paragraph and/or `-` bullets covering *what* changed and *why* (wrap at ~72
  chars). Small changes are subject-only.
- **Do NOT append a `Co-Authored-By` trailer** — this overrides the default
  Claude Code behaviour; the maintainer's commits never carry it.

## Testing

`pytest` under [tests/](tests/). The src/ layout means tests import the
*installed* package, so run them via `uv run poe test` / `uv run poe cov` (which
use the editable install).

- **Tests must be offline and fast.** [conftest.py](tests/conftest.py) provides
  an autouse fixture that stubs `scraper._engine.user_agent._load_ua_data` to
  `None` (forces the deterministic embedded UA generator, no network), plus
  `fast_config` / `make_fast_config()` which disable stealth delays, throttling,
  and session refresh. Use these in any test that constructs a `Scraper`.
- **Mock HTTP with `responses`** (`responses.RequestsMock()`), never real
  requests. It patches `HTTPAdapter.send`, so it intercepts the mounted TLS
  adapter too. Note: a set abort signal trips the pre-send check, so the request
  never fires — use `assert_all_requests_are_fired=False` in that case.
- **UA-family gotcha**: the offline generator can pick iOS, where Chrome's UA is
  `CriOS/…` and Firefox's is `FxiOS/…` (neither contains `Chrome/` / `Firefox/`).
  When asserting on UA family, pin a desktop platform
  (`BrowserConfig(platform="windows", mobile=False)`).
- `curl_cffi`-dependent tests use `pytest.importorskip("curl_cffi")`.
- **Coverage** config is in `pyproject.toml` (`[tool.coverage]`, `source =
  ["scraper"]`, `relative_files = true`). `uv run poe cov` writes `htmlcov/`,
  `coverage.xml`, and a terminal report (all coverage artifacts are gitignored).
  The deep CF challenge solvers (`cloudflare_v1/v2/v3`, `interpreter`) are
  integration-only and stay low-coverage without live Cloudflare traffic.
