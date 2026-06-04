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
uv run poe build        # lint + test + uv build (wheel/sdist)
uv run poe publish      # build + uv publish
```

Always run `uv run poe lint` before considering a change done — CI (`.github/workflows/ci.yml`) runs it across Python 3.9–3.14, and pyright is part of it.

## Architecture

The package is a thin, ergonomic layer over an in-house Cloudflare-bypass engine.

```
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
- **Dependencies**: core runtime deps live in `[project.dependencies]`; `Pillow`
  is an optional extra (`pip install lncrawl-scraper[image]`) and `get_image`
  imports it lazily. Add deps via `uv add` / `uv add --dev`.
- **Public API** is whatever `src/scraper/__init__.py` exports in `__all__`.
  Update it (and the README) when adding user-facing surface.

## Testing

`pytest` under [tests/](tests/). The src/ layout means tests import the
*installed* package, so run them via `uv run poe test` (which uses the editable
install). Prefer the `responses` library for mocking HTTP rather than hitting
the network.
