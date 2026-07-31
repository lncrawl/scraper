# Live verification harness

Exercises every code path against **real Cloudflare deployments** rather than fixtures.
This is separate from `tests/` on purpose: the unit suite is offline, fast and
deterministic, and this is none of those things. It earns its keep anyway — nearly every
defect fixed before 1.0 was found here, and two of them (the archive index and the
challenge-marker false positive) made whole features silently useless while every unit
test passed. `compare.py` is the other half: it A/B's this release against the previous
one, which is how the first-contact referrer and the JavaScript-redirect hop were found.

The current output is [report.html](report.html) — open the local copy in a browser, or read
the published one at <https://lncrawl.github.io/scraper/live-report/>. The Pages workflow
republishes it whenever a rebuilt `report.html` lands on `main`.

## Running it

```bash
uv run poe live-targets    # rebuild the host list from lncrawl's source index
uv run poe live-probe      # classify all ~500 hosts, two clients each (~4 min)
uv run poe live-tor        # classify Cloudflare hosts through tor-pool (~2 min)
uv run poe live            # every scenario (~2 min)
uv run poe live-report     # rebuild report.html
uv run poe live-all        # probe + tor + scenarios + report
```

One scenario at a time, which is what you want while iterating:

```bash
uv run python livetest/scenarios.py S05 S14
```

The browser tier runs separately, under its own interpreter — see below.

## What each file is

| File | |
| --- | --- |
| `pool.py` | Reaching the local tor-pool: whether it checks credentials at all, and against one that does, an operator login and a `proxy`-scoped token **minted on demand**. |
| `targets.py` | Reads lncrawl's `sources/_index.json` into a de-duplicated host list. |
| `probe.py` | Hits every host with a plain **and** an impersonated client, and records the library's own diagnosis of each response. The pairing is the measurement. |
| `tor_probe.py` | The same classification through the local tor-pool, on one sticky session. |
| `scenarios.py` | The scenarios. Each declares the layers it exercises and what a pass proves. |
| `clearance.py` | The browser tier. Separate because it needs a different Python and a real Chrome. |
| `render.py` | `render_soup()` against a real single-page application. Same requirements as `clearance.py`. |
| `headless.py` | Headed vs headless over a corpus of hosts that actually challenge. Holds the runs that retired the WebGL and virtual-display advice; `--report` re-reads them offline. |
| `report.py` | Renders `report.html` from whatever JSON exists. No network. |
| `compare.py` | A/B against a previous release. Spawns `arm_v1.py` / `arm_v026.py` under each version's own interpreter, grades both with one classifier. |
| `compare_analyze.py` | Reads `compare.json` and answers whether the new version is better, head to head. No network. |
| `profile_sweep.py` | Which impersonation profile wins, across the corpus. How the default came to be Firefox. |
| `referer_probe.py` | Sizes one header's effect: same transport, same profile, `Referer` the only difference. |
| `probe.json`, `tor_probe.json`, `results.json`, `clearance.json`, `render.json`, `headless.json` | Recorded output. Committed as a baseline — a diff after a change is the fastest way to see what moved. |
| `state/` | Scraper data dir for the runs (learned memory, browser profiles, downloads). Gitignored; safe to delete. |

## Requirements

**Network.** Real sites, and they are being asked for pages.

**tor-pool** on `127.0.0.1` for the exit scenarios (S11–S16, S29). They report
`inconclusive` with the reason when it is absent, and never run without it.

```bash
docker run -d --name tor-pool -p 127.0.0.1:8080:8080 -p 127.0.0.1:9250:9250 \
  -e AUTH_DISABLED=true -e ADMIN_PASSWORD=admin ghcr.io/lncrawl/tor-pool:latest
```

That is all the setup there is. `AUTH_DISABLED=true` turns off every credential check —
both ports are on `127.0.0.1`, so a token to talk to a container on this machine is
friction that buys nothing, and the dashboard opens straight to the pool. Never set it
on a pool anything else can reach: whoever opens a socket gets the Tor bandwidth, the
session table, and the ability to restart instances.

`pool.py` does not assume it. `/api/auth/status` says which kind of pool answered, and
against a closed one it **mints its own `proxy`-scoped token** using the operator login
(`TORPOOL_USER`/`TORPOOL_PASSWORD`, defaulting to `admin`/`admin`) — which is why
`ADMIN_PASSWORD` is still set above: flipping to `AUTH_DISABLED=false` then needs no
second edit.

Minting rather than requiring an exported `TORPOOL_TOKEN` is deliberate, and it is
worth knowing why: an `export` does not survive the shell that ran it, so the second
person to run this — or the same person in a new terminal — got a pool that rejected
every SOCKS5 handshake. That failure is quiet, and it used to read as the *destination*
refusing our address — four scenarios confidently reported a layer-1 reputation block
that did not exist, and `tor_probe.py` would have inverted its own finding. Set
`TORPOOL_TOKEN` explicitly to override, for a real deployment.

**S29 needs a pool that still checks.** Its subject is a *refused* credential, so an
open pool accepts the wrong token and the scenario would report the inverse of what it
measures. It reports `inconclusive` with that reason instead; run it with
`AUTH_DISABLED=false`.

**A browser and a Python that can load nodriver**, for `clearance.py` and `render.py`
only:

```bash
uv venv --python 3.12 /tmp/scr312
uv pip install --python /tmp/scr312/bin/python -e . nodriver cryptography
/tmp/scr312/bin/python livetest/clearance.py
/tmp/scr312/bin/python livetest/render.py
/tmp/scr312/bin/python livetest/headless.py       # ~25 min; --report is offline
```

Python **3.10–3.13**. nodriver raises `TypeError` on 3.9 (it evaluates a PEP 604 union
at import time) and `SyntaxError` on 3.14 (a generated module has a non-UTF-8 byte with
no encoding declaration). The editable install matters while iterating — a built wheel
will not pick up `src/` changes, which cost one confusing run to work out.

Chrome launches **headed**, but not because headless cannot clear — `headless.py`
measured that and it can. Headed is what a person can reach into and solve by hand.
What a container needs is the browser a real visitor runs: Debian's `chromium` omits
the `Google Chrome` brand from `Sec-CH-UA` and cleared nothing under any display
setting, Xvfb included.

## Rules this harness follows

**Politeness.** Single-digit request counts per host. Real pacing — the default
distribution, not zeroed. Every synthetic status code (`429`, `503`, `401`, …) comes
from a public echo service instead of by provoking a real site into producing one.

**Targets are looked up, not hardcoded.** `pick(layer=…)` reads the last probe, so a
scenario runs against whatever is actually presenting that layer today. This is not
tidiness: one host switched from Turnstile to plain scoring between two runs an hour
apart, and a URL-pinned scenario fails for a reason that has nothing to do with the
library.

**A scenario states what a pass proves.** "It returned 200" is not evidence about a
detection layer. If the claim cannot be written down, the scenario is not testing
anything.

**Verdicts are honest.** `inconclusive` exists and is used — when no host is presenting
the condition a scenario needs, that is what it reports. Negative results are recorded
rather than dropped: the reputation layer turning out to be *rare* on this corpus is a
finding, and it changes what is worth configuring.

**Missing infrastructure is a precondition, never a failure.** `requires=` on the
decorator returns `""` to run or a reason not to, and the runner records the reason
without executing the body. A scenario that runs without what it needs still emits
steps and a verdict, and those read as findings about the library — which is exactly
how a missing pool credential came to be recorded as a layer-1 reputation block on
four scenarios. The same rule applies inside a body when a third party goes away: S23
reports `inconclusive` when the Wayback index rate-limits it, because "the archive
rescues this host" cannot be tested while the archive is unreachable.

## Adding a scenario

```python
@scenario(
    "S29",
    "One sentence, in the present tense, describing the behaviour",
    ["L9"],                      # layers exercised, for the report
    "What a pass actually proves — not what the code does.",
    pick(layer=9) or SOME_HOST,  # look the target up where you can
    requires=pool.ready,         # omit unless it needs infrastructure
)
def s30(result: Result) -> None:
    result.check("the specific claim", condition, "the observed value")
    result.note("context worth recording", value)
```

`check` fails the scenario; `note` records an observation without judging it. Then
`uv run poe live-report`.
