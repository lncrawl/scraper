---
name: testing
description: How to write and run tests for lncrawl-scraper — offline fixtures, respx-based HTTP mocking, the iOS user-agent gotcha, and coverage. Use when adding, debugging, or running tests in this repo.
---

# Testing lncrawl-scraper

`pytest` under `tests/`. The src/ layout means tests import the _installed_
package, so always run via uv:

```bash
uv run poe test    # pytest
uv run poe cov     # pytest with coverage (term-missing + html + xml)
```

## Fixtures (tests/conftest.py)

Tests must be **offline and fast**. `conftest.py` provides:

- An **autouse** fixture that stubs `scraper.engine.user_agent.cache.load_ua_data`
  to return `None`, forcing the deterministic embedded UA generator (no network).
- `fast_config` (fixture) / `make_fast_config(**overrides)` (helper) — a
  `ScraperConfig` with stealth delays, throttling, and impersonation disabled
  (`impersonate.target=None` → `HttpxTransport`, which `respx` can intercept).
  Use it whenever a test constructs a `Scraper`, e.g.:

  ```python
  from .conftest import make_fast_config
  s = Scraper(config=make_fast_config())
  # or with impersonation (requires curl_cffi):
  from scraper import ImpersonateConfig
  s = Scraper(config=make_fast_config(impersonate=ImpersonateConfig(target="chrome")))
  ```

## Mocking HTTP

Use `respx`, never real requests. `respx` intercepts `httpx.AsyncClient` calls,
which is what `HttpxTransport` uses internally:

```python
import httpx
import respx

@respx.mock
def test_get_soup(fast_config):
    respx.get("https://example.com/p").mock(
        return_value=httpx.Response(200, html="<h1>Hi</h1>")
    )
    s = Scraper(origin="https://example.com", config=fast_config)
    assert s.get_soup("https://example.com/p").select_one("h1").text == "Hi"
```

Or with the context-manager form:

```python
with respx.mock:
    respx.get(url).mock(return_value=httpx.Response(200, content=b"ok"))
    ...
```

**Important**: `respx` only intercepts the `HttpxTransport`. Tests that use
`fast_config` (which sets `impersonate.target=None`) get `HttpxTransport`
automatically. Tests using `CurlCffiTransport` must mock the `_session.request`
method on the transport directly (see `test_transport_curl.py`).

**Error responses**: use `httpx.HTTPStatusError` (not `requests.HTTPError`) for
4xx/5xx. `Scraper.request()` calls `response.raise_for_status()` automatically.

**Network errors**: use `httpx.ConnectError(...)` as the `side_effect`:
```python
respx.get(url).mock(side_effect=httpx.ConnectError("refused"))
```

## Async solver tests

`ClearanceSolver.solve()` is `async`. Run it with `asyncio.run()`:

```python
import asyncio

def solve(solver, *args, **kwargs):
    return asyncio.run(solver.solve(*args, **kwargs))
```

Drive `RemoteSolver` with `respx` and `BrowserSolver` against a fake `nodriver`
module injected into `sys.modules` (see `tests/test_challenges_remote.py` and
`tests/test_challenges_browser.py`).

## Gotchas

- **iOS UA family**: the offline generator can pick iOS, where Chrome's UA is
  `CriOS/…` and Firefox's is `FxiOS/…` (neither contains `Chrome/` / `Firefox/`).
  When asserting on UA family, pin a desktop platform:
  `BrowserConfig(platform="windows", mobile=False)`.
- **httpx header casing**: httpx lowercases all header names in
  `response.headers` and `request.headers`. Assert with lowercase keys:
  `route.calls[0].request.headers.get("content-type")`.
- **None header values**: httpx rejects `None` header values with a `TypeError`.
  Filter them before passing to the engine: `{k: v for k, v in h.items() if v is not None}`.
- **Optional deps**: gate `curl_cffi`/impersonation tests with
  `pytest.importorskip("curl_cffi")`. For brotli behaviour, monkeypatch
  `scraper.engine.user_agent.cache.is_brotli_available`.
- **Exception/empty branches in PageSoup**: inject a raising stand-in tag
  (`ps._tag = _BoomTag()`) to exercise the defensive `except` paths; see
  `tests/test_soup_edge.py`.
- **Streaming downloads**: `response.iter_bytes()` not `response.iter_content()`.

## Coverage

Config lives in `pyproject.toml` (`[tool.coverage]`, `source = ["scraper"]`,
`relative_files = true`). `uv run poe cov` writes `htmlcov/`, `coverage.xml`, and
a terminal report (all gitignored). CI's `coverage` job uploads `coverage.xml`.

The CF challenge solvers (`challenges/browser_solver.py`, `challenges/remote_solver.py`)
only genuinely run against a real browser / live service, so keep tests offline:
drive `RemoteSolver` with `respx` and `BrowserSolver` against a fake `nodriver`
module injected into `sys.modules`. The pure `CloudflareDetector` (`detector.py`)
is fully unit-testable and should stay high-coverage, as should `soup`, `config`,
`utils`, and `session`.
