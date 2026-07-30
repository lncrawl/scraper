# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

`lncrawl-scraper` (import name `scraper`) is a scraping library organised around a model
of bot detection rather than around a request pipeline. Published to PyPI as
`lncrawl-scraper`, imported as `scraper`. Targets Python **3.9+**.

**Read [docs/model.md](docs/model.md) before changing anything.** Almost every design
decision in this package is a consequence of two statements in it, and a change that
looks like an obvious improvement is usually a re-introduction of something that was
deliberately removed. The removals are listed in the 1.0.0 section of
[CHANGELOG.md](CHANGELOG.md) with reasons.

## Commands

```bash
uv sync                 # deps + editable install
uv run poe lint         # ruff check + ruff format --check + pyright
uv run poe lint-fix     # ruff check --fix + ruff format
uv run poe test         # pytest
uv run poe cov          # pytest with coverage
uv run poe build        # lint + test + uv build
```

Always run `uv run poe lint` before considering a change done. CI runs `lint`, then a
matrix across every supported Python version that **tests and builds** on each, then
`coverage`. The two ends of the matrix are the ones worth watching: 3.9 and 3.14 resolve
without nodriver, since the browser extra is marked for 3.10 to 3.13.

## Architecture

```text
src/scraper/
├── layers.py       # THE MODEL: 19 layers, Trait (emit/possess), Stance, the bound
├── diagnosis.py    # response -> (binding layer, action). Pure.
├── planner.py      # (binding layer, context) -> what to change. Pure.
├── identity.py     # the emitted signals as one indivisible thing + Clearance binding
├── exits.py        # addresses by kind, sticky per-origin leases, tor-pool
├── pacing.py       # gamma-distributed gaps, warm-up, referrer chain
├── memory.py       # per-origin state that survives the process
├── state.py        # SharedState: what belongs to a site, not to a scraper object
├── transport.py    # Transport seam + curl_cffi impersonation + plain requests
├── browser.py      # BrowserSolver protocol + nodriver adapter
├── botauth.py      # RFC 9421 Ed25519 signing (layer 18)
├── links.py        # safe link extraction + TopicGuard (layer 17)
├── tiers/          # archive, direct, clearance, managed
├── session.py      # Scraper: the retrieval loop. Small on purpose.
├── config.py       # ScraperConfig
├── soup.py         # PageSoup — null-safe BeautifulSoup wrapper
└── utils/          # url_tools, file_tools, signals
```

The loop in `session.py` is deliberately thin. Everything that encodes judgement lives in
`layers`, `diagnosis` and `planner`, which are pure and tested as such. **Put new
judgement there, not in the session.**

### Invariants

Each of these is a place where a plausible change is wrong.

1. **Nothing reacts to a status code.** Responses go through `diagnose()` to a layer, and
   layers go through `Planner.react()` to a decision. A new `if response.status_code ==`
   in the session is a bug.
2. **A possessed-property layer is never rotated away from.** Rotation resets the history
   the layer measures. `Planner._slow_down` handles that axis; do not add a rotation path
   around it.
3. **Rotation requires somewhere better to go.** If `ExitKind.reach` says no configured
   address clears layer 1, rotating cannot help and the planner stops with an explanation.
4. **No tier claims layers 18 or 19.** They read a secret. A reach set listing them would
   make the planner offer a stronger tier for something no tier can do.
5. **The transport owns the header set.** Identity contributions stay inside
   `identity.OVERRIDABLE` — values a profile already sends. Never add headers there;
   order is read.
6. **The impersonation profile owns the User-Agent** until a browser earns a clearance.
   Do not reintroduce a UA generator.
7. **A clearance is only ever sent under the identity that earned it.**
   `Clearance.usable_by()` is the gate; nothing may bypass it.
8. **Layers 2–5 travel together.** Build reach sets through `layers.expand()`.
9. **A tier that cannot serve a call raises `TierUnavailable`**, never `Blocked`. Only a
   real detection event may be attributed to a layer and written to memory. This binds
   the transport path too: a proxy that refuses our credential, or an origin that never
   answered, gets `layer=None`. Substituting a plausible layer is not a cosmetic choice
   — the attribution rotates an innocent address, reports it to the pool as blocked,
   and persists to the origin's profile, where it outlives the typo that caused it.
10. **`diagnosis` and `planner` stay pure** — primitives in, dataclasses out, no I/O, no
    clock beyond `time` in the modules that must have one.
11. **Where fidelity and measurement disagree, measurement wins — and the comment says
    so.** Two behaviours here are deliberately *not* what a browser does: a first
    request carries a synthesised `Referer`, and the default profile is Firefox rather
    than the commonest browser. Both were argued the other way from the model and both
    lost to `livetest/compare.py`. Do not "correct" them back toward fidelity without
    re-running that comparison; the numbers are in the CHANGELOG.

### Things removed on purpose

Do not add back: TLS cipher rotation, header randomisation, in-process challenge solving,
a User-Agent generator, `fallback_to_direct`, pre/post request hooks, or a session-refresh
timer. Each is argued in the CHANGELOG's 1.0.0 section; several were actively breaking a
layer above them.

## Conventions

- **Python 3.9 compatibility is mandatory.** `X | Y` unions only under
  `from __future__ import annotations` or in pure annotations. Prefer
  `typing.Optional/Union` elsewhere.
- **Explicit relative imports** inside `src/scraper/` (`from .config import X`,
  `from ..layers import Y`). Never `import scraper.*` from within the package.
- **Full type annotations** on every function and method. `pyright` in `standard` mode
  over `src`, `tests` and `examples` is the hard gate.
- **ruff**: line length and rules in [pyproject.toml](pyproject.toml).
- **Comments explain why, never what.** The reason a line exists — a constraint, a failure
  mode it prevents, an ordering that is load-bearing — is worth writing down. Restating
  the code is not. Most of this package's comments name the specific bug the code avoids;
  match that.
- **Docstrings carry the model.** A module docstring should say which layer the module
  addresses and why the approach is the one that works. This is the package's primary
  documentation and it is expected to be substantial.
- **Public API** is `src/scraper/__init__.py`'s `__all__`. Update it, the README, and the
  relevant `docs/` page together.
- Dependencies via `uv add` / `uv add --dev`. Core deps must stay minimal; a new
  capability is an extra, imported lazily, raising `MissingDependency`.

## Testing

`pytest` under [tests/](tests/), offline and fast.

- **`tests/conftest.py`'s `FakeTransport`** is the seam. The pipeline talks to a
  two-method `Transport`, so a fake covers every tier with no network and no HTTP-adapter
  patching.
- **`fast_config` / `make_config`** disable pacing, persistence and the topic guard. Any
  test that constructs a `Scraper` needs them: `remember` defaults to a real path, and a
  suite that wrote to it would leak learned state into the developer's cache directory.
- **Test the judgement modules as pure functions.** `test_layers`, `test_diagnosis` and
  `test_planner` need no fixtures at all, and that is the point.
- **Name the failure mode.** These tests are documentation; a test called
  `test_a_throttle_slows_down_and_keeps_the_address` says why it exists in its name, and
  a comment explaining what breaks without it is worth more than an assertion count.
- `cryptography` / `nodriver` tests use `pytest.importorskip`.

### The live harness

`livetest/` runs the same paths against **real Cloudflare deployments**, using every
host in lightnovel-crawler's source index as the corpus. It is not part of `poe test`
— it needs the network, and some scenarios need a local tor-pool and a real browser.
See [livetest/README.md](livetest/README.md).

**Run it after changing anything that talks to a real server** — the transport, the
diagnosis markers, a tier, the exit pool. Almost everything fixed before 1.0 was found
here and was invisible to a stubbed transport; two of them made whole features silently
useless while every unit test passed. The pattern to expect: an offline test
that mocks the thing under test will confirm the code does what it says, not that
what it says is true of the real server.

The harness needs its infrastructure declared, not assumed: a scenario that runs
without a working tor-pool still emits steps and a verdict, and those read as findings
about the library. Use `requires=` for that, and `inconclusive` when a third party goes
away mid-run.

Anything the harness finds gets a unit test with a docstring saying it was found live
— those docstrings are the record of which assumptions turned out to be wrong.

## Commit messages

Plain capitalised imperative subjects, no Conventional Commits prefix, and **no
`Co-Authored-By` trailer**. See the **`commit-messages`** skill.

## Releasing

Automated: bump → tag → GitHub Release → PyPI. Update `CHANGELOG.md` first, then run the
**Bump Version** workflow. See the **`releasing`** skill. Note that some version numbers
are unavailable because tags already exist for them; the skill covers this.

## Never

- **Never commit or push automatically.** Stop when the work is done and draft a message
  for the user. Prior approval does not carry over to the next change.
