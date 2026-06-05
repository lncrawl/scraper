# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lncrawl-scraper` (import name `scraper`) is a standalone HTTP scraping library
extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).
`Scraper` is an ergonomic facade that *composes* an in-house Cloudflare-bypass
engine (a middleware pipeline over a pluggable transport), plus a null-safe
BeautifulSoup wrapper and a set of HTTP helpers. By default requests ride a real
browser TLS/HTTP-2 fingerprint via `curl_cffi`, with a urllib3 fallback.

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

The package is an ergonomic facade composed over an in-house Cloudflare-bypass
engine. Dependencies point one way: `engine`/`utils`/`session` → `config`/`exceptions`.

```text
src/scraper/
├── __init__.py         # public API + __version__ (via importlib.metadata)
├── config.py           # config dataclasses (defined here) + default_config() factory
├── exceptions.py       # exception hierarchy (defined here)
├── session.py          # Scraper — composition facade delegating to the engine
├── soup.py             # PageSoup — null-safe BeautifulSoup wrapper
├── py.typed            # PEP 561 marker
├── utils/              # generic helpers (event_lock, url_tools, file_tools)
└── engine/             # the Cloudflare-bypass engine (public extension surface)
    ├── core.py         # Engine — thin middleware-pipeline runner
    ├── context.py      # RequestContext (flows through the chain)
    ├── state.py        # SessionState + RequestChain
    ├── transport/      # Transport ABC + Urllib/CurlCffi transports + build_transport
    ├── middleware/     # one concern per file (throttle, stealth, proxy, …) + build_chain
    ├── challenges/     # CF challenge handlers v1/v2/v3 + Turnstile
    └── user_agent/     # UA selection + cipher suites + client hints
```

### Layers

- **`Scraper`** ([session.py](src/scraper/session.py)) — the public entry point.
  Adds Origin/Referer injection, default timeouts, and helpers: `get_soup`,
  `post_soup`, `get_json`, `post_json`, `get_image` (returns a PIL Image),
  `get_file` (streamed, abortable), `submit_form`, `ping`. It holds an `Engine`
  (as `scraper.engine`) and delegates; it is **not** a `requests.Session` subclass
  but mirrors the common verb methods plus `headers`/`cookies`.
- **`PageSoup`** ([soup.py](src/scraper/soup.py)) — wraps a BeautifulSoup `Tag`.
  Selection methods (`select`, `select_one`, `find`, `xpath`, `closest`, …)
  always return `PageSoup`/`list`, never `None`; text/HTML accessors always
  return `str`. An empty `PageSoup` is falsy. Reach the raw tag via `.tag`.
- **`engine/`** — the engine. `Engine` ([core.py](src/scraper/engine/core.py)) is a
  thin runner: `request()` builds a `RequestContext` and threads it through an
  ordered **middleware** chain (`build_chain`), whose innermost layer calls the
  **transport** (`UrllibTransport` or `CurlCffiTransport`, chosen by
  `build_transport`). Each middleware owns one concern (throttle, TLS rotation,
  session refresh, concurrency, 403 retry, challenge solving, stealth, hooks,
  proxy, SSL retry). It is a documented extension surface, but the curated public
  API stays in `__init__`/`config`/`exceptions`.

### Cloudflare-bypass surface

The realistic ceiling of the urllib3 transport is its TLS (JA3/JA4) and HTTP/1.1
fingerprint — `set_ciphers()` in [tls.py](src/scraper/engine/tls.py) only reorders
ciphers, so its ClientHello still reads as Python. Three features push past that:

- **curl_cffi transport** ([engine/transport/curl.py](src/scraper/engine/transport/curl.py)):
  the **primary** transport, selected by `build_transport` when
  `ScraperConfig.impersonate.target` is set (default `"chrome"` via
  `default_config()`) and curl_cffi imports. It routes requests through
  `curl_cffi` (curl-impersonate) for a real browser TLS + HTTP/2 fingerprint and
  adapts the result back into a `requests.Response` (`adapt_curl_response`). The
  curl_cffi session is the cookie authority; the engine mirrors it into its
  canonical jar after each send via `Transport.export_into`. If curl_cffi is
  unavailable the engine falls back to `UrllibTransport`. curl_cffi is a **core
  dependency**.
- **Client Hints** are derived from the actual UA in
  `UserAgent._client_hints` (Chromium only; Firefox sends none) so `sec-ch-ua`
  version/platform always match the User-Agent. `stealth.py` no longer hardcodes
  them — it only defaults the non-version-specific `Sec-Fetch-*` nav hints.
- **`apply_browser_clearance(domain, cf_clearance=, user_agent=, cookies=)`**
  injects a clearance solved by an external real browser; the UA must match the
  one that obtained it. `Engine.put_cookie` writes to both the canonical jar and
  the transport's authoritative jar.

### Configuration

All config flows through `ScraperConfig` (a dataclass with nested
`StealthConfig`, `ProxyConfig`, `BrowserConfig`, `ImpersonateConfig`). The
dataclasses are **defined** in [config.py](src/scraper/config.py) (a top-level
shared module the engine depends up on), which also provides the `default_config()`
factory:

```python
from scraper import Scraper, default_config
from scraper.config import BrowserConfig, StealthConfig

cfg = default_config()                 # fresh, fully-populated defaults
cfg.browser = BrowserConfig(browser="chrome", platform="darwin")
s = Scraper(origin="https://site.com", config=cfg)
```

- **`default_config()` returns a fresh instance every call.** Never reintroduce
  a shared module-level config singleton — `Engine` hands the nested
  `proxy`/`stealth`/`impersonate` objects to managers that may mutate them, so
  sharing would leak state across `Scraper` instances.
- **Impersonation is on by default.** `default_config()` sets
  `impersonate.target = "chrome"`; a bare `ScraperConfig()` leaves it `None`
  (urllib transport). The UA browser family is aligned to the impersonation target
  in `Engine.__init__`.

## Conventions

- **Python 3.9 compatibility is mandatory.** Bare `X | Y` unions must not be
  _evaluated at runtime_ — only use them in files that have
  `from __future__ import annotations`, or in pure annotations. Prefer
  `typing.Optional/Union` in new non-future-annotated modules. `importlib`,
  dataclasses, etc. must all work on 3.9.
- **Layering & dependency direction.** Shared domain types live at the package
  root (`config.py`, `exceptions.py`); `engine/`, `utils/`, and `session.py` import
  *up* from them, never the reverse (keep the graph acyclic — `__init__.py` imports
  `exceptions`/`config` before `session`/`soup`). `engine/` and `utils/` are public
  (no underscore) and `engine` is a documented extension surface, but the curated
  primary API lives in `__init__.py`/`config.py`/`exceptions.py` and is listed in
  `__all__`. Update `__all__` and the README when changing that surface.
- **Explicit relative imports.** All intra-package imports inside `src/scraper/`
  must use explicit relative paths (e.g. `from .config import X`,
  `from ..exceptions import Y`, `from ...engine.context import RequestContext`).
  Never import your own package with an absolute `scraper.*` path from within
  `src/scraper/` — that path only works after install and breaks editable installs
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
  — keep it clean (use real `isinstance` narrowing rather than `is_dataclass`,
  which pyright doesn't narrow on).
- **Dependencies**: core deps in `[project.dependencies]` (now including
  `curl_cffi`, the default transport); optional extras (`brotli`, `image`, plus
  `all`) are imported lazily and degrade gracefully when absent — e.g.
  `UserAgent.load` drops `br` from Accept-Encoding when `is_brotli_available()` is
  false, and `build_transport` falls back to urllib3 if `curl_cffi` is missing. Add
  deps via `uv add` / `uv add --dev`.
- **Public API** is whatever `src/scraper/__init__.py` exports in `__all__`.
  Update it (and the README) when adding user-facing surface.
- **Never `git push` automatically.** Commit locally and stop; let the user
  push when ready. This applies even when asked to "make a commit" — stop
  after the commit unless a push is explicitly requested.
- **Never commit automatically after making changes.** Always stop after
  editing files and wait for the user to explicitly ask for a commit.

## Before every commit

Run the **`pre-commit-review`** skill before creating any commit — no
exceptions, including "small" fixes. It covers lint, tests, diff review, and
the staging checklist.

## Commit messages

Plain capitalized imperative subjects (no Conventional Commits prefix) and **no
`Co-Authored-By` trailer**. See the **`commit-messages`** skill for the full
convention and examples — consult it whenever writing a commit message.

## Testing

Tests live in [tests/](tests/); run via uv (`uv run poe test` / `uv run poe cov`).
They are offline and mock HTTP with `responses`. For fixtures, the iOS UA gotcha,
optional-dependency gating, and coverage details, use the **`testing`** skill.

**All existing tests in `tests/` must continue to pass after every change —
no exceptions, no skips added without cause.** If a refactor breaks a test,
fix the test *or* the code; never delete a test to make the suite green unless
the tested behaviour was intentionally removed. Run `uv run poe test` before
considering any change complete.

## Releasing

Releases are automated: bump → tag → GitHub Release (artifacts + changelog) →
PyPI. Update `CHANGELOG.md`, then run the **Bump Version** workflow. For the full
pipeline, pre-release options, and gotchas, use the **`releasing`** skill.
