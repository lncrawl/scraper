# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lncrawl-scraper` (import name `scraper`) is a standalone HTTP scraping library
extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).
`Scraper` is an ergonomic facade that _composes_ an in-house Cloudflare-bypass
engine (a middleware pipeline over a pluggable transport), plus a null-safe
BeautifulSoup wrapper and a set of HTTP helpers. By default requests ride a real
browser TLS/HTTP-2 fingerprint via `curl_cffi`, with an httpx fallback.

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
`build` matrix testing on Python 3.9-3.14, and `coverage` (which posts a PR
comment + badge via `python-coverage-comment-action` and a job-summary table).

## Architecture

The package is an ergonomic facade composed over an in-house Cloudflare-bypass
engine. Dependencies point one way: `engine`/`utils`/`session` -> `config`/`exceptions`.

```text
src/scraper/
├── __init__.py         # public API + __version__ (via importlib.metadata)
├── config.py           # config dataclasses (defined here) + default_config() factory
├── exceptions.py       # exception hierarchy (defined here)
├── session.py          # Scraper — composition facade delegating to the engine
├── soup.py             # PageSoup — null-safe BeautifulSoup wrapper
├── py.typed            # PEP 561 marker
├── challenges/         # CF challenge handlers: ClearanceSolver ABC, BrowserSolver, RemoteSolver
├── utils/              # generic helpers (cancel_token, url_tools, file_tools)
└── engine/             # the Cloudflare-bypass engine (public extension surface)
    ├── core.py         # Engine — async middleware-pipeline runner (background event loop)
    ├── state.py        # RequestState (per-request) + SessionState (session-level)
    ├── clearance_store.py  # in-memory + disk cache for cf_clearance records
    ├── stealth.py      # StealthMode — header randomisation + human-like delays
    ├── tls.py          # CipherRotator + build_ssl_context
    ├── proxy_manager.py # round-robin proxy rotation + Tor integration
    ├── transport/      # Transport ABC (reset_session() no-op) + HttpxTransport (fallback) + CurlCffiTransport (primary, implements reset_session())
    ├── middleware/     # one concern per file (throttle, stealth, proxy, …) + build_chain
    └── user_agent/     # UA selection + cipher suites + client hints
```

### Layers

- **`Scraper`** ([session.py](src/scraper/session.py)) - public entry point. Adds Origin/Referer injection, default timeouts, and helpers: `get_soup`, `post_soup`, `get_json`, `post_json`, `get_image`, `get_file`, `submit_form`, `ping`. `rotate_proxy()` sends Tor NEWNYM or advances the round-robin index. `close()` aborts in-progress requests and releases transport resources. Holds an `Engine` (as `scraper.engine`); is **not** an `httpx.Client` subclass but mirrors the common verb methods plus `headers`/`cookies`.
- **`PageSoup`** ([soup.py](src/scraper/soup.py)) - wraps a BeautifulSoup `Tag`. Selection methods always return `PageSoup`/`list`, never `None`; text/HTML accessors always return `str`. An empty `PageSoup` is falsy. Raw tag via `.tag`.
- **`engine/`** - `Engine` ([core.py](src/scraper/engine/core.py)) runs a private asyncio event loop in a background thread. `request()` builds a `RequestState` and threads it through an ordered **middleware** chain (`build_chain`); the innermost layer calls the **transport** (`CurlCffiTransport` primary, `HttpxTransport` fallback). Each middleware owns one concern (throttle, TLS rotation, concurrency, 403/429 retry, challenge solving, stealth, hooks, proxy, SSL retry). Documented extension surface; curated public API stays in `__init__`/`config`/`exceptions`.

### Cloudflare-bypass surface

Three mechanisms push past the httpx transport's fixed fingerprint:

- **curl_cffi transport** ([engine/transport/curl.py](src/scraper/engine/transport/curl.py)): primary transport when `impersonate.target` is set (default `"chrome"`). Produces a real browser TLS/HTTP-2 fingerprint; falls back to `HttpxTransport` if unavailable. The curl_cffi session is the cookie authority; the engine mirrors it via `Transport.export_into` after each send.
- **Client Hints** derived from the actual UA (`UserAgent._client_hints`): `sec-ch-ua` version/platform always match the User-Agent. `stealth.py` only adds the non-version-specific `Sec-Fetch-*` nav hints.
- **`apply_browser_clearance`**: injects a `cf_clearance` + UA solved by an external browser. `Engine.put_cookie` writes to both the canonical jar and the transport's authoritative jar.

### Configuration

All config flows through `ScraperConfig` (nested `StealthConfig`, `ProxyConfig`, `BrowserConfig`, `ImpersonateConfig`), defined in [config.py](src/scraper/config.py), which also provides `default_config()`.

- **`default_config()` returns a fresh instance every call.** Never reintroduce a shared module-level config singleton - `Engine` hands nested objects to managers that may mutate them, leaking state across `Scraper` instances.
- **Impersonation is on by default.** `default_config()` sets `impersonate.target = "chrome"`; a bare `ScraperConfig()` leaves it `None` (httpx transport). The UA browser family is aligned to the impersonation target in `Engine.__init__`.

## Conventions

- **Plain ASCII only in all text files** (`.md`, `.py`, `.toml`, etc.). Never use Unicode
  typographic characters: no smart/curly quotes, no em/en dashes, no ellipsis, no arrows.
  The only exception is box-drawing characters for tree diagrams (`├`, `└`, `─`).
  This keeps diffs clean and prevents garbled rendering on tools that assume ASCII.
- **Python 3.9 compatibility is mandatory.** Bare `X | Y` unions must not be
  _evaluated at runtime_ - only use them in files that have
  `from __future__ import annotations`, or in pure annotations. Prefer
  `typing.Optional/Union` in new non-future-annotated modules. `importlib`,
  dataclasses, etc. must all work on 3.9.
- **Layering & dependency direction.** Shared domain types live at the package
  root (`config.py`, `exceptions.py`); `engine/`, `utils/`, and `session.py` import
  _up_ from them, never the reverse (keep the graph acyclic - `__init__.py` imports
  `exceptions`/`config` before `session`/`soup`). `engine/` and `utils/` are public
  (no underscore) and `engine` is a documented extension surface, but the curated
  primary API lives in `__init__.py`/`config.py`/`exceptions.py` and is listed in
  `__all__`. Update `__all__` and the README when changing that surface.
- **Explicit relative imports.** All intra-package imports inside `src/scraper/`
  must use explicit relative paths (e.g. `from .config import X`,
  `from ..exceptions import Y`, `from ...engine.state import RequestState`).
  Never import your own package with an absolute `scraper.*` path from within
  `src/scraper/` - that path only works after install and breaks editable installs
  in some edge cases.
- **Central config module.** Any magic constant, threshold, or named default that
  is used in more than one place must live in `scraper/config.py` (if it is a
  user-visible tunable) or in the module that owns it (if it is a private
  implementation detail). Do not scatter duplicated literals across files.
- **Type hints on all public functions.** Every function or method that is part of
  `scraper`'s public surface (anything reachable without going through a name that
  starts with `_`) must have fully annotated signatures (parameters + return type).
  Internal helpers should be annotated too, but pyright clean is the hard gate.
- **`ruff`**: line-length 100, double quotes, `force-sort-within-sections`,
  combine-as-imports. **`pyright`** runs in `standard` mode over `src` + `tests`
  - keep it clean (use real `isinstance` narrowing rather than `is_dataclass`,
    which pyright doesn't narrow on).
- **Dependencies**: core deps in `[project.dependencies]` (including `httpx` and
  `curl_cffi`); optional extras (`brotli`, `image`, `browser`, plus `all`) are
  imported lazily and degrade gracefully when absent - e.g. `UserAgent.load` drops
  `br` from Accept-Encoding when `is_brotli_available()` is false, and
  `build_transport` falls back to `HttpxTransport` if `curl_cffi` is missing. Add
  deps via `uv add` / `uv add --dev`.
- **Public API** is whatever `src/scraper/__init__.py` exports in `__all__`.
  Update it (and the README) when adding user-facing surface.
- **Never `git push` automatically.** Commit locally and stop; let the user
  push when ready. This applies even when asked to "make a commit" - stop
  after the commit unless a push is explicitly requested.
- **Never commit automatically after making changes.** Always stop after
  editing files and wait for the user to explicitly ask for a commit.

## Before every commit

Run the **`pre-commit-review`** skill before creating any commit - no
exceptions, including "small" fixes. It covers lint, tests, diff review, and
the staging checklist.

## Commit messages

Plain capitalized imperative subjects (no Conventional Commits prefix) and **no
`Co-Authored-By` trailer**. See the **`commit-messages`** skill for the full
convention and examples - consult it whenever writing a commit message.

## Testing

Tests live in [tests/](tests/); run via uv (`uv run poe test` / `uv run poe cov`).
They are offline and mock HTTP with `respx`. For fixtures, the iOS UA gotcha,
optional-dependency gating, and coverage details, use the **`testing`** skill.

**All existing tests in `tests/` must continue to pass after every change -
no exceptions, no skips added without cause.** If a refactor breaks a test,
fix the test _or_ the code; never delete a test to make the suite green unless
the tested behaviour was intentionally removed. Run `uv run poe test` before
considering any change complete.

## Releasing

Releases are automated: bump -> tag -> GitHub Release (artifacts + changelog) ->
PyPI. Update `CHANGELOG.md`, then run the **Bump Version** workflow. For the full
pipeline, pre-release options, and gotchas, use the **`releasing`** skill.
