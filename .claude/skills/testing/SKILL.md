---
name: testing
description: How to write and run tests for lncrawl-scraper — offline fixtures, responses-based HTTP mocking, the iOS user-agent gotcha, and coverage. Use when adding, debugging, or running tests in this repo.
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

- An **autouse** fixture that stubs `scraper._engine.user_agent.cache.load_ua_data`
  to return `None`, forcing the deterministic embedded UA generator (no network).
- `fast_config` (fixture) / `make_fast_config(**overrides)` (helper) — a
  `ScraperConfig` with stealth delays, throttling, and session refresh disabled.
  Use it whenever a test constructs a `Scraper`, e.g.:

  ```python
  from .conftest import make_fast_config
  s = Scraper(config=make_fast_config(impersonate="chrome"))
  ```

## Mocking HTTP

Use `responses`, never real requests:

```python
import responses

def test_get_soup(fast_config):
    with responses.RequestsMock() as rsps:
        rsps.add(rsps.GET, "https://example.com/p", body="<h1>Hi</h1>")
        s = Scraper(origin="https://example.com", config=fast_config)
        assert s.get_soup("https://example.com/p").select_one("h1").text == "Hi"
```

`responses` patches `HTTPAdapter.send`, so it intercepts the mounted TLS adapter
too. **Gotcha:** a set abort signal trips the pre-send check, so the request
never fires — use `responses.RequestsMock(assert_all_requests_are_fired=False)`
in abort tests.

## Gotchas

- **iOS UA family**: the offline generator can pick iOS, where Chrome's UA is
  `CriOS/…` and Firefox's is `FxiOS/…` (neither contains `Chrome/` / `Firefox/`).
  When asserting on UA family, pin a desktop platform:
  `BrowserConfig(platform="windows", mobile=False)`.
- **`requests` header typing**: `Session.headers["X"]` is typed `str | bytes`;
  wrap membership/`startswith` checks in `str(...)` to satisfy pyright on CI.
- **Optional deps**: gate `curl_cffi`/impersonation tests with
  `pytest.importorskip("curl_cffi")`. For brotli behaviour, monkeypatch
  `scraper._engine.user_agent.cache.is_brotli_available`.
- **Exception/empty branches in PageSoup**: inject a raising stand-in tag
  (`ps._tag = _BoomTag()`) to exercise the defensive `except` paths; see
  `tests/test_soup_edge.py`.

## Coverage

Config lives in `pyproject.toml` (`[tool.coverage]`, `source = ["scraper"]`,
`relative_files = true`). `uv run poe cov` writes `htmlcov/`, `coverage.xml`, and
a terminal report (all gitignored). CI's `coverage` job uploads `coverage.xml`.

The CF challenge solvers (`engine/challenges/browser_solver.py`,
`remote_solver.py`) only genuinely run against a real browser / live service, so
keep tests offline: drive `RemoteSolver` with `responses` and `BrowserSolver`
against a fake `nodriver` module injected into `sys.modules` (see
`tests/test_challenges.py`). The pure `CloudflareDetector` (`detector.py`) is
fully unit-testable and should stay high-coverage, as should `soup`, `config`,
`_utils`, and `session`.
