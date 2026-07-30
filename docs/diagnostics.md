# Diagnosing a failure

The design goal here is that you never have to guess. Two habits cover almost everything:
read `explain()` after a run, and read the exception's `layer` rather than its status code.

## `explain()`

```python
print(scraper.explain("https://example.com/novel/chapter-1"))
```

```
example.com
  binding layer : L9 Managed JavaScript challenge — reads a hybrid property, solve
  tier          : clearance
  pacing        : 4.2s mean interval
  requests      : 48 ok / 3 failed
  clearance     : 712s left
  ladder        : archive(0) direct(10) clearance(100)
  exits         : residential
  topic guard   : 12 pages learned
```

Each line answers a question you would otherwise ask by reading source: what the library
concluded is blocking, which capability it settled on, how fast it has learned it can go, what
it has available, and whether the expensive tier's result is still alive.

## The exception taxonomy

All of these derive from `scraper.ScraperError`.

| Exception | Means | What to do |
| --- | --- | --- |
| `Impassable` | The binding layer reads a secret (18, 19). | The message names the only route. Nothing to retry. |
| `Exhausted` | A bypass may exist; this configuration does not reach it. | Read `.layer`; the message says what would. |
| `Blocked` | Base class for both, carrying `.layer`. | Branch on the layer, not the status. |
| `Poisoned` | Content looks like decoy material. | See [decoy-content.md](decoy-content.md). |
| `TierUnavailable` | A tier cannot serve this call at all. | Internal; escalates without blaming a layer. |
| `Aborted` | `abort()` was called, or a per-request `signal` was set. | Expected on cancellation. |
| `ConfigError` / `MissingDependency` | Setup problem. | The message says which extra or field. |

```python
from scraper import Layer
from scraper.exceptions import Exhausted, Impassable

try:
    response = scraper.get(url)
except Impassable as exc:
    print("no bypass:", exc.detail)          # register, or authenticate
except Exhausted as exc:
    if exc.layer is Layer.IP_REPUTATION:
        ...                                   # a better address is the only fix
    elif exc.layer in (Layer.MANAGED_CHALLENGE, Layer.TURNSTILE, Layer.CDP):
        ...                                   # configure a browser solver
```

`Exhausted.detail` carries the full decision trail, so the message shows every move the
planner made and why — not just the last one.

## Diagnosing offline

`scraper.diagnose` is pure: it reads primitives, not a response object. So a page you captured
can be classified with no network at all, which is the fastest way to check what the library
thinks of something you saw in a browser.

```python
from scraper import diagnose

diagnose(status=200, body=saved_html)     # solve (L9 …): challenge served with a success status
diagnose(status=429, headers={"retry-after": "30"})
diagnose(status=403, body="<p>Error 1020</p>")   # rotate (L1 …)
diagnose(status=403, body="<p>Error 1010</p>")   # escalate (L7 …) — not the address
diagnose(status=404)                             # accept — the site's answer about a path
```

Three of those are worth internalising because they are where the conventional reading goes
wrong:

- **A `200` can be a challenge.** The interstitial is a normal page with a normal status.
  Parsed as content it yields a successful-looking scrape of nothing, and nothing else in the
  stack notices.
- **A `403` with code 1010 is not about the address.** It says the automation channel was
  detected. Rotating the exit changes nothing.
- **A `404` is not a layer.** It is the site's answer about a path, and attributing it to one
  would retire a healthy address over a typo in a URL.

## Taking inventory

`explain()` answers for one origin. A long-running process needs the other question — what has
this thing learned overall, and what is it doing with the addresses it was given — so `Memory`
and `ExitPool` both enumerate.

```python
scraper.memory.count                     # how many origins are known
scraper.memory.origins()                 # keys, most recently seen first
scraper.memory.profiles()                # copies of every OriginProfile
scraper.memory.export()                  # the same, JSON-safe, no clearance cookies
scraper.memory.forget("https://example.com/")   # drop one conclusion
```

`profiles()` hands back copies, so a status page iterating the store cannot edit what the
retrieval loop is reading. `export()` reduces a stored clearance to its expiry and the
User-Agent it belongs to: the cookies are the one secret in the file, and the question a status
page asks is whether a clearance is held and for how long.

`forget()` is the escape hatch for a conclusion that has gone stale in a way the store's TTL
will not catch — a site that dropped its edge, or a profile written while a proxy was
misconfigured. The binding layer is the field that misleads longest, because a wrong one sends
every later run up the ladder for nothing.

```python
for exit in scraper.exits.status():
    print(exit.name, exit.kind.value, exit.origins, exit.retired, exit.returns_in)
```

```
mobile-eu    mobile        3  False  0.0
dc-pool-1    datacenter    0  True   418.7
```

`origins` is how many origins currently hold a lease on that address, and `returns_in` is when a
retired one becomes usable again. A scrape that has slowed down for no visible reason is usually
a pool with most of itself resting, and that is otherwise only visible in debug logs. The URL is
deliberately absent — a proxy URL carries its credential, and this view is written to be
displayed.

## Logging

`DEBUG` on the `scraper` logger prints one line per attempt with the decision and its
reasoning:

```
DEBUG scraper.session: GET https://example.com/x [direct] escalate -> clearance: challenge served with 403
DEBUG scraper.session: GET https://example.com/x [clearance] proceed
```

`INFO` is quiet except for the things you want to know happened: a solve completing, with the
clearance lifetime and the identity it belongs to. `WARNING` covers the failures that
otherwise degrade silently — a stale impersonation profile, a pool rejecting a credential,
suspected decoy content, a memory file that could not be written.

## Common conclusions

**`Exhausted` at L1 with "no configured exit clears the reputation layer".** Every address on
offer is a datacenter or Tor range. These are published, so the replacement is blocklisted for
the same reason as the original — the library says so rather than proving it one exit at a
time. Configure a residential or mobile exit.

**`Exhausted` at L9/L10/L13 with "needs clearance, which is not enabled".** The site serves a
challenge and there is no solver. `ScraperConfig.browser=NoDriverSolver()`.

**`Exhausted` at L14.** The zone is running the per-zone composite, reached by promotion after
repeated failures. Results against it are inconsistent by nature; a managed provider is the
rational fallback.

**A solve that appears to run forever.** Almost always the clearance is being earned under one
identity and replayed under another. `Clearance.why_not(identity)` names which half changed,
and it is logged at DEBUG when a clearance is dropped. The usual cause is an address that
moved between the solve and the fetch.

**Everything works, then stops after a while.** Check `explain()` for the interval. A throttle
widens it permanently for the origin, including across runs, which is intended — but if the
site's limit was temporary, `Pacer.learn(origin, value)` resets it.

**A scrape that reports success and collects nothing useful.** Check for decoy content. If
`guard_topic` is off, turn it on; if it is on, look at `knows(url).decoys`.
