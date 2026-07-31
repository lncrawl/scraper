# The escalation ladder

A **tier** is one capability set: a way of getting a page that can pass some subset of the
layers at some cost. A **capability** (`scraper.Capability`) describes a tier as
`{name, cost, reach}` — `reach` being the layers it can pass.

The ladder is walked on evidence, not climbed by default. `scraper.Planner` picks the
cheapest capability whose reach covers whatever is actually binding, which is why a site
that only needs a header profile never pays for a browser launch, and a site that needs the
browser does not spend fifty failed requests discovering that.

| Tier | Cost | Reach | Enabled by |
| --- | --- | --- | --- |
| `archive` | free | everything, but stale | `ScraperConfig.archive=True` |
| `direct` | one HTTP request | layers 2–5, 11, 12, 16 | always |
| `clearance` | a browser launch | adds 6, 7, 9, 10, 13 | `ScraperConfig.browser=…` |
| `managed` | money, per request | everything reachable | `ScraperConfig.managed=…` |

Costs are relative and only used for ordering; the gaps reflect real cost, so a browser
launch is orders of magnitude above an HTTP request rather than one tick. The exact reach
sets live in `scraper.planner.default_capabilities`.

Two things no tier claims: layers 18 and 19. A reach set that listed them would make the
planner offer a stronger tier for something no tier can do.

## `direct` — the baseline

An impersonated HTTP request on a held identity. This should handle the majority of
protected sites, and that is a consequence of the model rather than optimism: layers 2–5 are
one barrier that a faithful transport profile clears in one shot, and the lighter enforcement
tiers read the same artifacts.

It deliberately does nothing clever on failure. It sends what it was told to send and returns
what came back; deciding whether the answer means rotate, slow down or launch a browser
belongs to the planner, which can see the history.

Two things it does *not* do, both of which the previous generation of this library did:

- **No cipher rotation.** Reordering the cipher list per request does not produce a browser
  fingerprint, it produces an unstable one — and an unstable TLS fingerprint invalidates any
  clearance bound to it, so the feature actively breaks the layer above.
- **No header randomisation.** Header *order* is read, not just header values. A profile
  emits a complete, correctly ordered set; writing over it with a hand-assembled dictionary
  is how a client ends up claiming to be Chrome with Python's header order.

`scraper.identity.OVERRIDABLE` is the enforcement: the identity may replace values a profile
already sends and may not add headers of its own. Request-specific headers you pass to
`fetch` are not filtered — `Accept` for a JSON endpoint and `Referer` for a navigation are
legitimate — but they land wherever the transport places additions, which is a bounded
imperfection worth accepting over a navigation with no provenance.

## `clearance` — solve once, reuse many

A challenge result is not portable. It is bound to the address, User-Agent and TLS
fingerprint that earned it, so the browser and the requests that follow are one identity and
the solve is an expensive way of upgrading it.

Which is why this tier does not own a transport. It owns a *solver* and delegates every
actual request to `direct`. The alternative shape — a browser tier that fetches pages itself
— both wastes a browser on pages that no longer need one and quietly allows the solve and
the fetch to run on different identities.

The failure this structure makes impossible is the usual way the pattern is implemented
wrong: solving on one exit and fetching from another produces a clearance rejected on first
use, which reads as "the solver does not work" and leads to re-solving forever.

One solver is bundled: `scraper.CdpSolver`, which drives Chrome over the DevTools protocol
directly and needs only `websockets`, so it works on **every Python this package supports**.
It replaced a driver library that could not be imported below 3.10 or from 3.14 and was no
better at clearing — 12 hosts to 11 head to head, same median. Anything satisfying the
two-method `scraper.BrowserSolver` protocol plugs in besides — a patched Chromium build, a
Firefox speaking a non-CDP protocol, a paid solving service — and
`scraper.browser.CallableSolver` wraps a plain function for the one-off case.

**`CdpSolver` never enables a CDP domain**, and that is the reason to own the wire rather
than wrap a driver. Eagerly-enabled domains are a known tell, and a general-purpose driver
has to enable them because it cannot know what its caller will ask for next. This one does:
`Runtime.evaluate` and `Page.navigate` are commands, not subscriptions, so neither
`Runtime.enable` nor `Page.enable` is ever sent. Going through a higher-level abstraction —
including Chrome's own WebDriver BiDi, implemented over CDP internally — gives that away.

A solver declares two things about itself. `impersonation` is the profile its clearance
binds to, which `ScraperConfig.profile()` then applies to every request; `interactive` says
a person can reach the window, which buys `interactive_solve_timeout` instead of the
unattended `solve_timeout`. The bundled solver sets the second from whether it runs headed.

Two defaults are deliberate and worth not changing:

- **Headless, and it costs nothing.** Measured over 46 challenged hosts, headless clears all
  27 that a headed browser clears. What used to give it away was one substring —
  `HeadlessChrome` in the User-Agent — and the solver strips that itself. The old reason
  given here, a software WebGL renderer, was refuted directly: forcing it changed nothing.
  Pass `headless=False` where a person can reach the window, which also buys the interactive
  solve budget.
- **WebRTC off.** A STUN request reaches the network directly and reports the host's real
  address even when every HTTP request goes through the proxy — unbinding the identity by
  leaking past it, silently.

What *does* decide it is the browser build. Debian's `chromium` omits the `Google Chrome`
brand from `Sec-CH-UA` and cleared nothing in a container — headless, headless with the
User-Agent fixed, or headed under Xvfb alike. Install the browser a real visitor runs; a
virtual display cannot hide a property of the binary.

One browser profile directory per address
(`scraper.browser.profile_dir_for`). Cookie and session age are behavioural signals, so a
profile reused across a run accumulates the history that makes the session look established
— and sharing one between addresses is how a clean exit inherits a burnt one's session.

The bundled solver does not synthesise mouse, scroll or keystroke dynamics, so both clear the
control-channel layer and leaves the behavioural one entirely to `scraper.pacing`. That
division is why they are separate modules.

### `render_soup()` — a browser, but not a tier

A solver has a second use, and it is not escalation:

```python
soup = scraper.render_soup(url, wait_for="#chapter-list")
```

Some pages answer 200 with a shell that JavaScript fills in. **Nothing is blocking**, no
layer is binding, and a clearance changes nothing — plain HTTP carrying the cookie returns the
same empty shell. So this is not a rung on the ladder: no diagnosis leads here, because there
is no detection event to diagnose. The caller knows this about the site; the model cannot
infer it.

It goes through the same lease, identity, gate and clock as a fetch, so a render is paced like
any other request and leaves from the address the origin is already held on. What it does
*not* do is touch the tier or the success counters: a page the browser rendered is no evidence
that the HTTP ladder works, and recording it as one would zero the consecutive failures that
promote a diagnosis.

No solver, or a solver that only solves, raises `TierUnavailable` — never `Blocked`, which
would be a claim about defences that are not there.

**Give it a `wait_for`.** Without one the only stand-in for "the page has run" is a fixed
settle interval, which is both slower than necessary and unreliable. With one the wait ends on
evidence, and a selector that never appears raises `RenderError` rather than handing back the
shell — returning it is the silent failure this exists to prevent, since the caller parses it,
finds nothing, and reports an empty page rather than a problem.

Choosing the selector is the part that takes care: it must name an element that **cannot exist
before the data does**. Measured on one live single-page application: the cards hydrate as
empty skeletons and fill in afterwards, so `a.line-clamp-2` matched at 1.8s with 457
characters of a page that settles at 9538. Where a site has no such element, no selector is
the honest answer and the settle interval is what you have.

## `archive` — free, but stale

An archived snapshot is served from a host with no mitigation stack in front of it, usually
as static HTML. Where the content is not time-sensitive this is strictly better than every
other tier: no proxy, no browser, no challenge, no standing to protect.

Off by default for two honest reasons rather than any detection problem: coverage is
incomplete and captures are stale, so the caller has to have said stale is acceptable. Set
`archive_max_age` — a default of "any age" would quietly serve a decade-old page to someone
who asked for the current one.

The response carries the **original** URL, not the archive URL, and the capture timestamp
arrives in the `scraper.tiers.archive.SOURCE_HEADER` response header. That matters for
anything that parses the result: relative links resolved against a `web.archive.org` base
point back into the archive, which silently turns a scrape of a site into a scrape of a
snapshot of a site.

## `managed` — delegation

Against a per-zone composite model that is actively tuned, maintaining a bypass becomes a
standing engineering cost rather than a piece of work with an end. So the last rung is
handing the request to a service, and it is last because it is the only tier that costs
money per request.

No provider is bundled. Their request formats differ, they change, and a wrapper that
guesses wrong fails in a way that looks like the site blocking you. `scraper.tiers.Provider`
is the whole contract:

```python
def provider(method: str, url: str, **options) -> requests.Response: ...
```

It must return the **origin's** status and body. Returning the provider's own status instead
breaks diagnosis: a 200 from the provider wrapping a 403 from the site reads as a successful
scrape of a block page.

`scraper.tiers.http_provider` covers the several services shaped as "GET this endpoint with
the target as a query parameter".

## Promotion

The scoring tiers (11, 12, 14) cannot be told apart from outside. So when a transport profile
keeps being rejected while a stronger tier exists, `Planner.promote_after` consecutive
failures re-attribute the diagnosis to the per-zone composite and escalate. Recurrence is the
only evidence available, and it needs history to be visible at all — which is why
`scraper.memory` counts consecutive failures per origin.

A declared-crawler block is never promoted. That one is about the User-Agent, and no number
of repetitions turns it into a machine-learning verdict.

## Writing a tier

Subclass `scraper.tiers.Tier`, implement `send(call) -> requests.Response`, declare a cost
and an honest reach, and pass an instance in `ScraperConfig.tiers`:

```python
from scraper import Layer, Scraper, ScraperConfig
from scraper.tiers import Call, Tier

class CacheTier(Tier):
    name = "cache"                                  # what OriginProfile.tier records
    cost = 5                                        # cheaper than direct, so tried first
    reach = frozenset({Layer.IP_REPUTATION})        # what it can actually get past

    def send(self, call: Call) -> requests.Response:
        ...

scraper = Scraper(config=ScraperConfig(tiers=[CacheTier()]))
```

Nothing else is needed. The planner sees it through `Tier.capability()` and picks it by cost
like any other rung, `close()` is called with the rest, and the name is refused if it collides
with a built-in one.

**Be honest about `reach`.** The planner treats it as a claim about capability, so an inflated
one sends every retrieval to a tier that cannot help and stops the ladder before the tier that
could. Two claims are enforced rather than trusted: naming one of layers 2–5 names all four
(`layers.expand`, because no technique satisfies one without the others), and naming layer 18
or 19 raises `ConfigError` — those read a secret, and a rung offering one would be offered for
something no rung can do.

Two rules:

- Everything a tier needs arrives in the `Call`; anything it learns goes back through the
  return value. A tier that reacts on its own is a tier that rotates a proxy over a pacing
  problem.
- Raise `scraper.TierUnavailable` when the tier cannot serve a call at all — no archive
  snapshot, a method the provider does not forward. That escalates without attributing
  anything to a layer, because an archive gap says nothing about the site's defences and
  recording it as a block would teach the memory something false.

`stream()` has a working default that buffers through `send()`, so downloads work in every
tier whether or not its client streams.
