# The nineteen layers

Numbered in the order a request meets them. The numbering is this library's own
organising device and is stable API — `scraper.Layer` members are what exceptions,
memory files and log lines refer to. The mechanisms and product names are Cloudflare's.

The authoritative version of this table is `scraper.layers.LAYERS`, which carries a
one-paragraph summary per layer. Print it with
[`examples/02_the_model.py`](../examples/02_the_model.py).

| # | Layer | Reads | Stance | Bypassable |
| --- | --- | --- | --- | --- |
| 1 | IP reputation | hybrid | lease | yes, economically |
| 2 | TLS fingerprint (JA3/JA4) | emit | satisfy | yes |
| 3 | Post-quantum key share and ECH | emit | satisfy | yes — keep the profile current |
| 4 | HTTP/2 and HTTP/3 frames | emit | satisfy | yes |
| 5 | Header order | emit | satisfy | yes |
| 6 | Browser and JavaScript fingerprint | emit, coupled | solve | mostly |
| 7 | DevTools-protocol detection | hybrid | solve | partly |
| 8 | Per-zone behavioural model | **possess** | accumulate | partly, and only by accruing |
| 9 | Managed JavaScript challenge | hybrid | solve | yes, on a held address |
| 10 | Turnstile | hybrid | solve | yes, on a tighter clock |
| 11 | Bot Fight Mode | emit | satisfy | yes |
| 12 | Super Bot Fight Mode | emit | satisfy | yes |
| 13 | Under Attack Mode | hybrid | solve | yes, per request |
| 14 | Bot Management | hybrid | delegate | inconsistently |
| 15 | Operator edge code | outside | avoid | site-specific |
| 16 | AI bot blocker | emit | satisfy | trivially |
| 17 | Decoy-content honeypot | outside | avoid | avoid by not tripping it |
| 18 | Cryptographic agent identity | **possess** | **refuse** | **no**, where mandated |
| 19 | Identity-provider gate | **possess** | **refuse** | **no** |

`Stance` is what this library does when the layer is binding, and the one worth knowing is
`refuse`: layers 18 and 19 read a secret the caller either holds or does not, so grinding
against them is an infinite retry loop against a wall.

## Layers 2–5 are one barrier

They read different parts of the request, but a client built to reproduce one browser's
network stack passes all four at once, and one that is not fails all four at once. In the
bound they are a single term. `scraper.layers.expand` closes any reach set over the group,
so a tier declaring one automatically declares all of them.

The practical consequence is that the default transport handles the whole group with no
configuration, and that adding another emit-reading check of the same kind barely moves the
result for a defender.

The one thing that *does* matter here is not pinning a stale profile. An older
impersonation target is a signal on its own: no real user runs a two-year-old browser, and
the older profile predates the post-quantum key share that current builds all send — so a
client claiming to be current Chrome without one contradicts its own User-Agent. Use the
bare family alias (`"chrome"`, `"firefox"`, `"safari"`, `"edge"`) and it tracks whatever
the installed build considers current. `scraper.transport.stale_profile_warning` checks
this at construction and logs if you pinned something older.

## Layer 1: addresses

Not technical. The address is chosen freely; the reputation attached to it accrued over
time and can be rented, never fabricated. Datacenter ranges are cheap to block because
almost no human traffic originates there. Mobile-carrier ranges front thousands of real
subscribers behind one NAT, so blocking one causes collateral damage — which is what makes
them the good ones.

Declare the kind honestly in `ExitSpec`. Claiming `MOBILE` for a datacenter range does not
change what the reputation database thinks; it only stops this library from telling you
that layer 1 is the reason nothing works. `ExitKind.reach` is what the planner consults
before recommending a rotation, and a pool of published ranges reaches nothing.

## Layer 8: the hard one

Request-timing regularity, navigation and referrer chains, cookie and session age, history
depth, concurrent sessions per address — correlated across a session window and trained
separately per zone. There is no artifact to reproduce.

Everything in `scraper.pacing` and `scraper.memory` exists for this layer. See
[behaviour.md](behaviour.md).

## Layer 17: the one with no error response

A honeypot inserts hidden `nofollow` links into a page, leading into a maze of generated
decoy pages. Following them causes two harms at once, and neither announces itself: the
store fills with plausible irrelevant content, and the session is flagged network-wide.

Unlike every other failure mode in this library, this one has to be looked for.
`scraper.links.safe_links` enumerates only anchors a person could click, and
`scraper.links.TopicGuard` watches for content that stopped being about the site. See
[decoy-content.md](decoy-content.md).

## Layers 18 and 19: no bypass

Layer 18 verifies a signature over the request against a published key directory. Emulation
of any kind is beside the point. Currently deployed fail-open, so an unsigned request falls
back to the rest of the stack — but where a signature is required, the only route is to hold
a key and be registered. That is worth doing on its own merits: a valid signature is a
positive identification that skips the challenge machinery entirely, making it the cheapest
tier in the stack. See [web-bot-auth.md](web-bot-auth.md).

Layer 19 is authentication, not bot mitigation, and is out of scope: retrieving content
behind it without credentials would be unauthorised access.

Both raise `scraper.Impassable`, whose message names the legitimate route.
