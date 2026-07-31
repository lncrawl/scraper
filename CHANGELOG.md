# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-07-31

### Added

- **`check_response` — name a refusal this library cannot see.** A `200` carrying `{"success": false}` is indistinguishable from content on the wire, because the difference lives in a schema only the caller knows. `ScraperConfig.check_response`, also settable per session, returns a `Diagnosis` the loop then treats as its own: layer attributed, address blamed, planner rotates or escalates. Without it such a site reads as unbroken success while every page comes back empty. Consulted only on a response nothing else faulted; one that raises is logged and ignored.

### Fixed

- **A tor-pool with no token collapsed to a single exit.** The session key travels as the SOCKS5 username and the token as the password, and RFC 1929 cannot send one without the other — so the key never arrived and the pool keyed by client address instead, pinning every session to one instance. Worst for a local pool, which is the case least likely to have a token. An unset token now travels as a placeholder.

## [1.2.0] - 2026-07-30

### Added

- **`render_soup()` — a browser, but not a tier.** Some pages answer 200 with a shell that JavaScript fills in. Nothing is blocking there, no layer is binding, and a clearance changes nothing, because plain HTTP carrying the cookie returns the same empty shell. So this is not a rung on the ladder and no diagnosis leads to it; the caller knows this about the site and the model cannot infer it.

`BrowserSolver.render(url, wait_for=…)` and `Scraper.render(url, …)` / `render_soup(url, …)`. A render goes through the same lease, identity, concurrency gate and clock as a fetch, so it leaves from the address the origin is held on and takes its turn on the origin's pacing. It records the referrer chain and passes the rendered text through the decoy guard — a maze that only appears after hydration would otherwise walk straight past it — but writes **nothing** to the tier or the success counters, since a page the browser rendered is no evidence that the HTTP ladder works.

`render` is optional on the solver protocol and defaults to `TierUnavailable`, because the two capabilities are independent: a solving service answers challenges and renders nothing. `CallableSolver` takes a `renderer` separately for the same reason. A missing `wait_for` selector raises `RenderError` rather than handing back the shell — returning it is the silent failure this exists to prevent, since the caller parses it, finds nothing, and reports an empty page rather than a problem.

**Choosing the selector is the part that takes care.** It has to name an element that cannot exist before the data does. Measured against one live single-page application: the cards hydrate as empty skeletons and fill in afterwards, so a novel-card selector matched at 1.8s with 457 characters of a page that settles at 9538. Where a site has no such element, no selector is the honest answer and the solver's settle interval is what you have.

- **`unchanged(url)` — conditional requests, opt-in only.** Every parsed response's `ETag` and `Last-Modified` are recorded per endpoint; sending them is only ever this call. A `304` has no body and this library keeps no response cache to replay one from, so revalidating underneath `get_soup()` would return an empty page — every selector finding nothing, nothing raising — which is worse than the download it saved. The saving available is skipping the _work_, so the question belongs before the work starts.

`False` means do the work: nothing recorded yet, or the site answered with a body. Nothing recorded costs no request. The store is bounded at 64 endpoints per origin, least recently recorded first, and skips non-textual responses — one page can be twenty images, and they would evict the pages that are what anyone revalidates.

- **A per-request abort signal.** `fetch(..., signal=…)`, and every helper above it passes one through. Anything with `is_set()` counts. It is combined with the scraper's own signal rather than substituted for it, so `abort()` still stops everything, and it reaches the pre-send check, the pacing wait and the download loop — respectively so a cancelled request never leaves, so a job does not sit out an interval whose tail is tens of seconds, and so a large file does not have to finish first. Until now the only lever was the shared attribute, so cancelling one job cancelled every job on the origin, which pushes a consumer into a scraper per thread and costs it the per-origin state that sharing exists to accumulate.

- **Twelve mitigation products besides Cloudflare are recognised**, from their own headers, cookie names or block pages. A verdict from a per-session model over the whole request is layer 14, whose stance is _delegate_: DataDome, Kasada, PerimeterX, Akamai and Imperva. A WAF acting on coarser rules is layer 12, stance _satisfy_, so the ladder still tries the tier that supplies a better profile: DDoS-Guard, Sucuri, AWS WAF, F5.

A CDN is named without being blamed. CloudFront and Fastly headers are on every response those services serve, so their presence says who answered and nothing about why; a refusal there is an operator's own rule, which is layer 15. The half that failed silently was the challenge markers — a DataDome captcha iframe, a PerimeterX press-and-hold, a DDoS-Guard interstitial and an Imperva resource page all arrive with a 200, so a caller parsed one and recorded a successful scrape of nothing. A bare hCaptcha or reCAPTCHA widget is deliberately not one of these on a 2xx: login and comment forms carry it.

`scraper.edge(headers, body)` names the product publicly, and `livetest/probe.py` now calls it instead of keeping its own copy of four signatures.

- **`ScraperConfig.tiers` — rungs of your own.** `Tier` declares `cost` and `reach` alongside `name` and exposes `capability()`. Two documents already promised this and neither was true: the instruction was to edit a private method of an installed library. Reach is enforced rather than trusted — naming one of layers 2–5 names all four, and naming layer 18 or 19 raises `ConfigError`, since those read a secret and a rung claiming one would be offered for something no rung can do.

- **`SharedState.create(config, memory=…)`.** A consumer that wants state per site and persistence for the process could not have both: each store holds every origin it knows and `flush()` writes the whole file, so two stores on one path do not merge and the later write wins.

- **`Memory` and `ExitPool` enumerate what they hold** — `count`, `origins()`, `profiles()`, `export()`, `forget()`, `clear()`, and one `ExitStatus` row per configured address with what it is leased to and when a retired one returns. Both views are narrowed for a status page rather than a debugger: `profiles()` returns copies so a caller cannot edit what the loop is reading, `export()` reduces a clearance to its expiry and the User-Agent it belongs to, and `ExitStatus` names an exit by label or host because a proxy URL carries its password.

### Changed

- **The origin memory is bounded**, by age first and size second: an origin unseen for `FORGET_AFTER` (30 days) is dropped, and beyond `MAX_ORIGINS` (512) the least recently seen go. Age first because the two answer different questions — what is stored is a conclusion about a site's _current_ configuration, so a stale one is worth less than the cold start that replaces it, and a small cap must not keep a month-old binding layer alive just because the store was quiet. Eviction never drops the origin being asked for: `profile()` hands back a live object the retrieval loop mutates, so evicting the entry its own insertion created would discard everything that retrieval learns.

- **Layer 11 is reachable.** A 403 behind the edge always read as the scoring layer, whatever the client had sent. It is now named when the User-Agent is not a browser's, which names the remedy: a faithful transport profile, not a browser launch. An _absent_ User-Agent is left as scoring — silence is the caller not saying, and reading it as "not a browser" would relabel every diagnosis made from a recorded page.

- **The matrix tests as well as builds.** CI built on 3.9 through 3.14 and tested on none, so a version-specific break reached a release as long as the package still built. The two ends are where one lands: the browser extra is marked for 3.10 to 3.13, so 3.9 and 3.14 resolve without nodriver.

### Fixed

- **Nine Cloudflare codes said nothing about the visitor and were read as a block.** 1000–1004, 1013, 1016, 1018 and 1023 are a prohibited or unresolvable DNS target, direct access by IP, a Host/SNI disagreement, or a host that is not configured. They fell through to "forbidden by the origin" at layer 15, whose stance is _avoid_ — so the ladder exhausted itself over a misconfigured zone, retired a healthy exit on the way, and wrote a detection verdict to that origin's profile that outlived the misconfiguration. They now refuse with no layer.

- **Layer 15 has an observed signal for the first time.** 1101 and 1102 are the operator's own edge code throwing or exhausting its limits; every other route to that layer is by elimination. A crash is not a refusal, so they retry — no browser and no address changes what a thrown script returns. 1200 retries with no layer, and 1011 refuses, since hotlink protection reads a `Referer` this library already sends. An unmapped code now reaches the message rather than being discarded: "Cloudflare error 1024" is actionable, "forbidden by the origin" is not.

- **The solver kept its own copy of the challenge markers**, and that copy never gained the Turnstile ones — so a browser watching a Turnstile page concluded on its first poll that it had cleared, harvested no clearance cookie, and the tier reported itself unavailable on the one layer it exists for. `diagnosis` owns both questions now, and deliberately as two functions, because their costs run opposite ways: `is_challenge()` decides whether to _start_ a solve, where a false positive wastes a browser on a page that already arrived, and `is_still_challenged()` decides whether one has _finished_, where a false positive abandons it.

- **Header values were compared case-sensitively.** `_lower_headers` folded names but not values, so `Server: AkamaiGHost` and `X-CDN: Incapsula` — both written mixed-case in the wild — could never match. The pre-existing Cloudflare check worked only because Cloudflare happens to send its `Server` value lowercase.

- **A vendor refusing with a status other than 403** read as the site's answer about a path, which is a silent give-up on a page that is there. Akamai answers a failed sensor check with 400 and a WAF rule commonly answers 405, so a named vendor is consulted on 400, 405, 406 and 409. A 404 is not: a 404 behind a bot manager is still a 404.

- **The README claimed nineteen layers are diagnosable.** Thirteen are, verified by exercising every branch; `docs/layers.md` now says which six are not and why.

## [1.1.0] - 2026-07-30

### Added

- **`retry_backoff` and `max_retry_wait`.** Base seconds before a retry, doubled per attempt and capped. Only consulted when the response named no delay — a `Retry-After` header always wins.

### Changed

- **The `browser` extra is marked for 3.10 to 3.13.** nodriver cannot be imported outside that range: below 3.10 its module body evaluates a PEP 604 union, and from 3.14 its generated `cdp/network.py` fails to tokenize on a stray non-UTF-8 byte. Only the first was handled, and only in the exception clause — so on 3.14, which is what a modern Docker image and release build use, the extra installed and then raised a bare `SyntaxError` from inside a dependency. `NoDriverSolver` now raises `MissingDependency` naming the supported range for either failure, and the marker keeps the extra out of an environment that cannot use it.
- **A concurrency gate is keyed per address _and_ origin.** With no proxy configured every origin shared the literal exit id `direct`, so `max_sessions_per_exit` — clamped to the low single digits — gated the **whole process** rather than one address. A consumer crawling several sites at once was serialised across all of them, which is what forced building one `SharedState` per domain. The id is now `direct#<origin>`.
  Identity tokens and stored clearances key on the exit id too, so this narrows what they match — the safe direction, since a clearance issued for one site was never usable on another. It does mean **every clearance already in `origins.json` stops matching once, on upgrade**. Self-healing: the next solve replaces it. A first run after upgrading will look colder than it is.
  The browser profile directory is deliberately _not_ per origin. `profile_dir_for` keys on the address, because a Chrome profile is tens of megabytes and a consumer with a few hundred sources would otherwise keep one for each.
- **A transport failure through a proxy no longer claims layer 1.** `diagnose_transport` attributed `IP_REPUTATION` to any connection error through an exit. The address is still blamed and still rotated, but with no layer, because the site never answered — there is nothing to conclude about it. Attributing reputation wrote a permanent verdict onto the origin's profile that the _destination_ refuses us, from evidence that only says one address failed to carry a request. `Exhausted.layer` is `None` for this case now, and the pool is told `transport` rather than a reputation kind.

### Fixed

- **Rotation is reachable with a pool of published ranges.** The planner asked `IP_REPUTATION in exit_reach` before _every_ rotation. `ExitKind.TOR.reach` is empty and honestly so — Tor exit lists are published — so with a `TorPoolSpec` configured `Move.ROTATE` was never emitted and `ExitPool.rotate` and `ExitPool.report` were unreachable from `fetch` entirely: a dead pool instance could never be replaced. The check now applies only when the site actually attributed reputation, which is the question it answers. Whether rotating can produce a different address at all is `ExitPool.rotatable`, and a `TorPoolSpec` counts as several addresses where a single plain proxy does not.
  `ExitKind.TOR.reach` is deliberately unchanged. Widening it would make the planner recommend rotation as a cure for reputation blocks it cannot cure.
- **An unconfigured pool reports no reach.** `best_kind` falls back to `DIRECT`, whose reach includes layer 1 — correct when there is a burnt proxy to move off, meaningless with `exits=[]`. Reported anyway, it told the planner a remedy was available that it had no way to perform. `ExitKind.DIRECT.reach` itself is unchanged: moving off a datacenter proxy onto direct genuinely can clear layer 1.
- **An exit that names a kind must name an address.** `ExitSpec(kind=ExitKind.MOBILE)` with no `url` was accepted, reported mobile reach, and printed `exits: mobile` in `explain()` while every packet left from the local address. It raises `ValueError` now. `ExitSpec(kind=ExitKind.DIRECT)` is still valid — that is what a fallback-to-direct entry looks like.
- **Retries back off.** `Action.RETRY` waited `retry_after or 0.0`, and 408, 502, 504 and the 52x family never parse a `Retry-After` — so a retry on any of those was sent back-to-back, a tight loop aimed at a site already struggling.
- **`get_image` raises `MissingDependency` when Pillow is absent**, naming the `image` extra, instead of a bare `ModuleNotFoundError` from the middle of the call. Every cover and inline image goes through it.
- **A tier closes only a transport it owns.** `DirectTier.close()` closed whatever transport it held, including one handed in through `ScraperConfig.transport`. Two scrapers sharing an injected transport broke each other on the first `close()`.
- **Retired addresses no longer leak their gates and clearances.** `ExitPool._slots` held a semaphore per exit id and every rotation minted a new id, so a long-running process accumulated one per rotation forever; the gate of a proxied lease is now dropped with the lease, whose session key means the id can never be asked for again. `ClearanceTier._held` was keyed by origin and never evicted, holding cookies long past their expiry; expired entries are dropped on each solve, with a cap as a backstop.
  Browser profile directories were the third and largest of these, at tens of megabytes each. A proxied exit id carries a session key, so every rotation that reached a solve left another one behind and nothing ever removed it. `profile_dir_for` now prunes the least recently used beyond `MAX_PROFILES`, skipping anything touched in the last few minutes — two scrapers can share a data dir, and each solver only serialises against itself, so the directory being removed must not be one another process has a browser in. Keying them coarsely was not the alternative: for a pool endpoint the URL is constant while the exit IP is not, so one shared profile would hand a fresh session the accumulated history of a burnt exit.
- **Success is recorded only for a response that succeeded.** Any status without a matching diagnosis is reported as `ACCEPT` — correctly, since nothing about it says a layer is blocking — but the accept path then wrote a success unconditionally. A site answering 439 to everything set `profile.tier`, incremented `successes` and zeroed `consecutive_failures`, teaching the store that whatever tier had just been tried works. The bar is now that the site responded: a 2xx or a 3xx records the tier, and a 4xx or 5xx records nothing.
  `402`, `405`, `410` and `423` are counted against the origin as failures, because those are a site refusing this visitor rather than answering about a path. They are recorded with **no layer** — none of them identifies one, and naming one would retire a healthy exit over what may be a URL mistake. A `404` still moves the ledger in neither direction and still surfaces as a plain `HTTPError`.
- **A throttle is counted once.** The handler for `BACKOFF` and `ACCUMULATE` records the failure itself, with the widened interval, and the retrieval loop recorded it a second time. With the default `promote_after=3` that meant a third failure — and an escalation to a tier the caller may not have configured — on the **second** 429.
- **A failure with nothing to attribute no longer erases the binding layer.** `record_failure(url, None)` assigned `None` over whatever an earlier, attributed failure had learned, so a transport error or an unmatched status discarded the single most valuable thing the store holds and sent the next run back to guessing.

## [1.0.1] - 2026-07-30

### Fixed

- **A response is decoded the way it declares itself.** `PageSoup.create` read the body of a `Response` as UTF-8 with `errors="ignore"` and never looked at what the page said its charset was, so every multi-byte character on a non-UTF-8 site was silently dropped — a GBK page returned an empty title rather than a wrong one. The charset now comes from the first source that declares a usable one: an explicit `encoding=` argument, then the response's `Content-Type` header, then a `<meta charset>` near the top of the markup, then UTF-8. A charset the server names but Python cannot load falls through to the next candidate instead of raising.

Deliberately _not_ `response.encoding`: requests fills that with ISO-8859-1 for any `text/*` response that declared no charset, so preferring it would mojibake exactly the pages this fixes. `Scraper._peek` does read it, which is why diagnosis and the parsed soup could disagree about the same bytes.

- **`parser` survives a `Response`.** `PageSoup.create` recursed into its own bytes branch without passing `parser` on, so `ScraperConfig.parser` and `Scraper(parser=…)` had no effect on `get_soup`, `post_soup` or `make_soup(response)` — the only paths a caller uses — and everything was parsed with lxml.

- **A challenge is no longer written to a download target.** `stream_to` blanked the body before diagnosis ran, so a challenge interstitial — which arrives with a 200 and a body — was streamed to the caller's path and accepted. `get_file` produced a file that was really a Cloudflare page, indistinguishable from the asset once the response was gone. The opening bytes are now held back and diagnosed before the file is created, and the abort signal is honoured while they are buffered as well as while the rest is written.

## [1.0.0] - 2026-07-29

A complete rewrite. There are no compatibility shims: almost every import changes. [Migration](https://lncrawl.github.io/scraper/migration/) is the mapping, and [The model](https://lncrawl.github.io/scraper/model/) is why.

### The change

The library is now organised around a model of what it is up against, rather than around a request pipeline with anti-detection features bolted on. A mitigation engine folds many detectors into one score, and admission is close to a conjunction — so the weakest layer bounds the outcome, and effort spent on any other layer buys nothing. Detectors that read an artifact the client _emits_ are reproducible; detectors that read a property it must _possess_ are not.

Every behaviour below follows from those two statements.

### Breaking

- **`ScraperEngine` is gone**, and `Scraper` is no longer a `requests.Session` subclass. The transport is a two-method seam, so the escalation ladder can move between transports and every tier is testable without a network.
- **The in-process Cloudflare solvers are gone** (v1, v2, v3, Turnstile), along with the `exejs` dependency. They cannot keep up with the challenge format, and the layer they targeted is only reachable by a real browser. A challenged site now needs `ScraperConfig.browser`; without one it raises and says so, instead of attempting a solve that usually failed.
- **TLS cipher rotation is gone.** Reordering the cipher list per request does not produce a browser fingerprint, it produces an unstable one — and an unstable TLS fingerprint invalidates any clearance bound to it. The feature was breaking the layer above it.
- **Header randomisation is gone.** Header _order_ is read, not just header values. An impersonation profile emits a complete, correctly ordered set; `scraper.identity.OVERRIDABLE` now caps what may be written over it.
- **The User-Agent is taken from the transport, not imposed on it.** The generated-UA machinery is gone. A profile supplies the User-Agent until a real browser earns a clearance, at which point the browser is the source of truth and its exact string is reproduced — because that is what the clearance is bound to.
- **Impersonation is a core dependency**, not the `impersonate` extra. An ordinary Python client fails layers 2–5 in the first round trip, so a build without it is not a degraded scraper but one that cannot reach a protected page.
- `default_config()`, `StealthConfig`, `BrowserConfig`, `ProxyConfig`, `ProxyUrl`, `TorProxyUrl`, `apply_browser_clearance()` and the `scraper.engine` package are removed. `SharedLimiter` becomes `SharedState`. `AbortedException` becomes `Aborted`, and the `CloudflareException` hierarchy becomes `Blocked` / `Impassable` / `Exhausted`, each carrying the layer it is attributed to — or `None`, when the failure is ours rather than the site's. Code that dereferences `exc.layer` or `exc.layer_info` must handle that; a type checker will point at every such place.
- New extras: `browser` (nodriver) and `botauth` (cryptography). `impersonate` is gone.
- **`ScraperConfig.impersonate` defaults to `""`, which resolves to `firefox`** — or to `chrome` when a browser solver is configured, because the bundled solver drives Chrome and a clearance is bound to a User-Agent _and_ a TLS fingerprint together. Read it through the new `ScraperConfig.profile()`; the field is no longer the answer on its own.

### Measured against 0.2.6

The rewrite was A/B'd against 0.2.6 on 150 hosts from lightnovel-crawler's source index, three arms per host, `curl_cffi` pinned to the same version in both, one classifier for every arm, arm order shuffled per host. `livetest/compare.py` runs it.

The first run said the rewrite had made per-request retrieval slightly _worse_: 0.2.6 took 56 of 150 hosts with impersonation off, 55 with it on, and 1.0 took 51 — losing five hosts head to head and winning one. Everything in this section is what closed that gap, and each item is here because it was measured, not reasoned about:

- **A first request now carries a `Referer` and matching `Sec-Fetch-Site`.** No browser does this — a typed address has no referrer, which is exactly why the rewrite sent none. Over 85 hosts that refuse an impersonated client it recovered three and cost zero, turning a 403-with-a-challenge into a full page. The coherent header _set_ matters more than the header: a synthesised referrer alongside `Sec-Fetch-Site: none` is a contradiction, and one host only yielded once both agreed.
- **A JavaScript-only redirect is followed instead of being mistaken for content.** A family of bot checks answers with a few hundred bytes of `window.location.replace('…?token=…')` — a `200`, no challenge marker, no Cloudflare header. Both releases handed that back as a successful retrieval, so a scraper reported success and collected an empty document. It was 19% of the corpus. No browser is needed because the destination is _emitted_ in the HTML; running the script would produce the URL already sitting there in plain text. New `Action.FOLLOW`, `Diagnosis.location` and `diagnosis.js_redirect()`.
- **The default impersonation profile is Firefox.** Over a random 150-host sample: firefox 85, safari 84, edge 82, chrome 81 — and against chrome, firefox won four hosts and lost none. Chrome being the most common browser is a reason to expect it to be unremarkable, not evidence that it is the least remarkable.
- **Pool sessions are released when a scraper closes.** `ExitPool.release()` existed, was never called, and only dropped the local lease. Every lease minted a fresh key that the pool then held until `SESSION_TTL`, so a process building several scrapers in a row walked the pool out of capacity — and the symptom was the misleading part: the next lease could not connect, a transport failure through a proxy is evidence about the exit, and the model reported a reputation block on a destination that never saw the request. Needs tor-pool 0.2.1, which puts `DELETE /api/sessions/{key}` on the `proxy` scope.

After those, on the same corpus with the archive tier **off** — so nothing is borrowed that 0.2.6 cannot do — 1.0 takes 82 of 150 against 0.2.6's best of 76, and 30.8% of the hard hosts against 25.6%. Head to head it wins nine and loses none (sign test p = 0.004). Hosts serving an unrecognised stub fell from 28 to 6. With the archive tier on, total reach was 73% against 39%.

### Added

- `scraper.layers` — the model as code: nineteen layers, what each reads (`Trait`), what this library does about it (`Stance`), the bound (`weakest`) and the arithmetic that shows why fixing the wrong layer gains nothing (`marginal_gain`). Layers 2–5 are declared as one barrier, and `expand()` keeps any reach set closed over the group.
- `scraper.diagnosis` — a response becomes a binding layer plus an action, as a pure function over primitives. Three readings that a status-code table gets wrong: a `200` carrying a challenge is a failure, a `429` is a pacing problem rather than a spent address, and a `403` with error 1010 is about the automation channel rather than the address. A `407` is reported as our own proxy credential, not as the site needing a login.
- `scraper.planner` — chooses the cheapest capability whose reach covers the binding layer. Three rules that contradict the conventional table: a possessed property is never rotated away from; rotation requires somewhere better to go, so a pool of published ranges stops with an explanation instead of cycling; and escalation only goes to a tier that actually reaches the layer. Repeated failure at an already-covered emitted layer is re-attributed to the per-zone composite, because recurrence is the only evidence available from outside.
- `scraper.identity` — the emitted signals as one indivisible thing. `Clearance.usable_by()` refuses to replay a clearance under a different identity, which makes the classic rotating-proxy failure structurally impossible rather than merely documented.
- `scraper.exits` — addresses described by _kind_, and `ExitKind.reach` deciding what layer 1 can be told. Leased per origin and held; rotation happens on evidence, never on a timer. tor-pool support is retained, and a failure report now carries the kind derived from the binding layer.
- `scraper.pacing` — inter-request gaps drawn from a gamma distribution rather than set to a constant, occasional reading pauses, homepage warm-up, and a real referrer chain with fetch metadata. Throttles widen a learned per-origin interval that persists.
- `scraper.memory` — per-origin state that survives the process: the binding layer, the working tier, a clearance and the identity it belongs to, the learned interval, observed JSON endpoints, and recorded decoy URLs. On by default, because the layer it exists for cannot be satisfied by a process that forgets.
- `scraper.state.SharedState` — shares the address, identity, history, pacing, referrer chain and decoy list between scrapers pointed at one site. Two scrapers with separate state do not look like one visitor going faster; they look like two who contradict each other.
- `scraper.tiers` — `archive` (Wayback, serving the original URL so links resolve against the real site), `direct` (the baseline), `clearance` (solve once, reuse many, delegating every request to `direct` so the solve and the fetch cannot diverge), and `managed` (a provider callable; none bundled, since a wrapper that guesses a vendor format wrong fails in a way that looks like the site blocking you).
- `scraper.browser` — a two-method `BrowserSolver` protocol, a `nodriver` adapter, and `CallableSolver` for anything else. Headed and WebRTC-disabled by default, both deliberately: a headless build reports a software renderer, and a STUN request reports the host's real address past the proxy without any request failing. One browser profile directory per address.
- `scraper.links` — `safe_links` enumerates only anchors a person could click, and `TopicGuard` notices content that stopped being about the site. This is the only defence against the one layer that returns no error, so the guard runs on the way out rather than on demand.
- `scraper.botauth` — RFC 9421 Ed25519 request signing with the `web-bot-auth` tag, plus the key directory document to publish. The one layer with no bypass, and for a crawler willing to identify itself, the cheapest tier in the stack.
- `Scraper.explain(url)` and `Scraper.knows(url)` — what the library concluded is binding, which tier settled, how fast it has learned it can go, and what it has available.
- `livetest/` — a live verification harness that exercises every path against real Cloudflare deployments, using every host in lightnovel-crawler's source index as the corpus. Separate from `tests/`, which stays offline. See [livetest/README.md](https://github.com/lncrawl/scraper/blob/main/livetest/README.md); the current run is the [live report](https://lncrawl.github.io/scraper/live-report/).

### Fixed before release, found by live traffic

Most of what was fixed before release was found this way, and none of it was visible to a stubbed transport. These are not regressions from 0.2.x — they are defects in the new code, and two of them made a whole feature silently useless while every unit test passed. Each has a regression test whose docstring says it was found live.

- **Cloudflare's injected JavaScript-Detections script was read as a challenge.** The script is served from `/cdn-cgi/challenge-platform/scripts/jsd/…` on ordinary _successful_ pages, and that path was a challenge marker — so content pages were diagnosed as interstitials. Measured across two live populations before changing it: the bare prefix appeared on 9 of 10 normally-served pages, the challenge-only `/h/` orchestrate sub-path on none. 18 of 22 hosts reported as challenged were serving content fine. The same marker also stopped the browser solve loop from ever detecting "cleared", so every solve burned its full timeout.
- **The archive tier could never find a capture.** A negative Wayback CDX `limit` is documented as "the last N rows" but returns an empty body once a filter is applied. The query is now bounded server-side and the newest rows taken from the tail. Separately, an unbounded query timed out on popular URLs, and a rate-limited index was reported as "nothing archived" — all three produced the same misleading message, so a lookup failure, an empty index and an age limit now say which they are, and the index is retried once.
- **Real navigation was being dropped as decoy content.** An anchor containing only an icon-font element counted as "nothing rendered", and a URL took the verdict of whichever anchor appeared first — so a card's empty overlay anchor rejected a page its own text anchor linked to. On one real page 11 of 11 rejections were wrong.
- **A stop could advise configuring a capability that was already configured.** A browser solver that ran and produced no clearance yielded "Configure a browser solver". The message now says the tier ran and failed, and quotes what it reported.
- **Rotating with nowhere to go spent the rotation budget on one address.** Found against a host that bans this machine's ASN outright. The pool now reports whether an alternative exists, and with none the stop is immediate.
- **A proxy refusing our own credential was diagnosed as the site's IP reputation.** tor-pool 0.2 enforces authentication, and a rejected SOCKS5 handshake never becomes an HTTP response — so it reached `diagnose_transport`, which blamed the exit for every proxied transport error. Three wrong things followed: the address was rotated though nothing was wrong with it, the pool was told a healthy exit was `blocked`, and layer 1 was written to the origin's persisted profile — so a missing token left behind a permanent verdict that the _site_ refuses this address. A proxy that refuses us is now `REFUSE` with no layer, matching how HTTP 407 was already handled. The distinction is drawn on curl's wording rather than the exception class, because an unreachable destination reported through a SOCKS5 reply raises the same `ProxyError` and that one really is evidence about the exit.
- **A failure with nothing to attribute was reported as layer 15.** `Blocked` required a layer, so a layer-less stop borrowed `Layer.WORKERS` — and "L15 Operator edge code" is indistinguishable from a Cloudflare Worker refusing the request. `Blocked.layer` is now `Optional` and renders as "no detection layer".
- **The `browser` extra failed with a raw dependency error on unsupported Pythons.** nodriver raises `TypeError` before 3.10 and `SyntaxError` on 3.14; both now produce a message naming the version floor, and the extra is marked so it does not install where it cannot load.

### Fixed

- **One share-button link took out a page's whole crawl frontier.** `extract_host` read `urlparse(...).port`, which raises rather than returning `None` when the netloc's `:` is followed by something that is not a number — so an ordinary `whatsapp:send?text=…` anchor aborted `safe_links` for the entire page. The port is now optional and the host survives without it.

## [0.2.6] - 2026-07-29

### Added

- `TorPoolProxyUrl`: support for [tor-pool](https://github.com/lncrawl/tor-pool), which fronts many Tor instances with one sticky SOCKS port. The SOCKS5 username is a session key, so a scrape keeps the same exit IP until it rotates; rotation goes through the pool's API and skips Tor's ~10s NEWNYM cooldown by reassigning to an already-built instance.

Set `token` to a `proxy`-scoped token from the pool — it is required by tor-pool 0.2 and later, and is sent both as the SOCKS5 password and as a bearer token on the pool's API. Without it the pool answers `401`, and because those calls are best-effort the failure would otherwise pass as a warning while the pool quietly stopped hearing about soft blocks; that specific case is logged at `error` instead.

- `ProxyManager.report_failure()`: reports 403s, challenges, rate limits and transport errors to the pool. This is the only signal that catches a soft block — a proxy relaying bytes cannot see a 403 or a captcha inside an HTTPS tunnel — and it is what lets the pool quarantine a burnt exit. Sent automatically by the engine; call it directly when your own code detects a block.

The pool weighs a report by what it says went wrong, so each one carries a _kind_ as well as free text. A 429 that no challenge handler claimed is sent as `rate_limited` rather than as a generic failure: a throttle says the exit works and is being asked for too much, and reported as a block it would retire a working exit while the next one is throttled just the same.

- `scraper.engine.proxy_manager.FAILURE_KINDS`, the mapping from a failure reason to the kind sent alongside it. Reasons the engine raises itself are all covered, as is the pool's own vocabulary for callers passing it straight through; anything else is still reported and the pool counts it as unclassified. Sent explicitly rather than left to the pool to read out of the free text — its aliases exist for callers written before kinds did, so leaning on them means a vocabulary drift on either side quietly downgrades every report to unremarkable.
- `examples/13_tor_pool.py`.

### Fixed

- Rotating a proxy now drops pooled connections. A live keep-alive stays bound to its original exit, so without this the exit IP appeared not to change until the socket happened to be evicted.
- A pool that no longer knows a session is no longer a warning. Acting on a report, the pool takes the instance out of rotation and unpins its sessions, so the next report about that session answers `404` — routine, and the next request re-pins to a healthy instance, but it logged a warning per report for exactly the exit that was failing most.

## [0.2.5] - 2026-07-23

### Added

- `SharedLimiter`: a shareable throttle clock + concurrency semaphore. Give the same limiter to every `Scraper` that talks to one host — via the new `limiter=` constructor argument or `adopt_limiter()` — and the host-wide request rate and in-flight cap are enforced across all of them, while each scraper keeps its own cookies, headers, and abort signal.
- `Scraper` now forwards extra keyword arguments (e.g. `limiter=`) to the underlying `ScraperEngine`.

## [0.2.4] - 2026-06-28

### Fixed

- `ScraperEngine.close()` now closes the curl_cffi impersonation transport. Previously `requests.Session.close()` only disposed the standard urllib3 adapters, leaking the curl_cffi session (libcurl handle + connection pool) when impersonation was enabled, causing per-job scrapers to accumulate native handles.
- Re-mounting TLS adapter to repalce the existing `https://` adapter in `self.adapters`. Close the old one first so its `urllib3` `PoolManager` (open sockets) and `SSLContext` are released now; cipher rotation re-mounts almost every request, so relying on cyclic GC lets these native handles accumulate.

## [0.2.3] - 2026-06-16

### Changed

- When fallback to direct is not allowed and no proxies are available, raising error even before making the request.

### Fixed

- The proxy configuration was returning raw string URL. Changed it to proper proxy object format.

## [0.2.2] - 2026-06-16

### Fixed

- On proxy error, re-enable fallback to direct

## [0.2.1] - 2026-06-16

### Changed

- **`scraper._engine` → `scraper.engine`** — the engine sub-package is now a public module; all internal imports updated accordingly
- **`scraper._utils` → `scraper.utils`** — utilities sub-package promoted to public
- **`scraper.exceptions`** — `CloudflareException` and `AbortedException` are now importable directly from `scraper.engine.exceptions` (previously `scraper._engine.exceptions`)
- **`ProxyManager` rewritten** — moved from `scraper._engine.proxy_manager` to `scraper.engine.proxy_manager`; richer proxy rotation, Tor support split into a dedicated example
- **`ScraperConfig` / proxy config consolidated** — `_engine/config.py` merged into `scraper/config.py`; `EventLock` moved to `scraper/utils/event_lock.py`
- **Examples reorganised** — `09_proxies.py`, `10_tor_proxy.py` added; `09_proxies_and_tor.py` removed

## [0.1.2] - 2026-06-13

### Changed

- Replace `quickjs` with `exejs` as the JavaScript engine for solving Cloudflare IUAM (V1) challenges. `exejs` is a pure-Python JS evaluator that eliminates the compiled C extension dependency, improving cross-platform compatibility and installation reliability.

## [0.1.1] - 2026-06-12

### Fixed

- `CipherSuiteAdapter.send()`: clear `check_hostname` and set `verify_mode = CERT_NONE` before urllib3 touches the shared SSL context when `verify=False` is requested. Previously, the SSL auto-retry in `_send()` would crash with `ValueError: Cannot set verify_mode to CERT_NONE when check_hostname is enabled` instead of gracefully falling back to unverified mode.

## [0.1.0] - 2026-06-04

Initial public release of `lncrawl-scraper`, extracted from [lightnovel-crawler](https://github.com/lncrawl/lightnovel-crawler).

### Added

- `Scraper` — a `requests.Session` subclass with transparent Cloudflare challenge handling (v1, v2, v3, Turnstile) and helpers: `get_soup`, `post_soup`, `get_json`, `post_json`, `get_file`, `get_image`, `submit_form`, `ping`.
- `PageSoup` — a null-safe BeautifulSoup wrapper; selection methods never return `None` and text/HTML accessors always return `str`.
- Typed configuration: `ScraperConfig`, `StealthConfig`, `ProxyConfig`, `BrowserConfig`, plus the `default_config()` factory.
- **Browser fingerprint impersonation** (`impersonate` extra): route requests through `curl_cffi` for a real Chrome/Firefox TLS (JA3/JA4) and HTTP/2 fingerprint, with the spoofed User-Agent family aligned to the target.
- **Browser-assisted clearance**: `apply_browser_clearance()` to reuse a `cf_clearance` cookie + User-Agent solved by an external real browser.
- **Accurate Client Hints**: `sec-ch-ua` / platform / mobile derived from the chosen User-Agent (Chromium only) instead of hardcoded values.
- Stealth mode, proxy rotation with Tor identity refresh, TLS cipher rotation, rate limiting, and cooperative `abort()`.
- `py.typed` marker (PEP 561) and full type coverage.

[1.3.0]: https://github.com/lncrawl/scraper/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/lncrawl/scraper/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/lncrawl/scraper/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/lncrawl/scraper/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/lncrawl/scraper/compare/v0.2.6...v1.0.0
[0.2.6]: https://github.com/lncrawl/scraper/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/lncrawl/scraper/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/lncrawl/scraper/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/lncrawl/scraper/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/lncrawl/scraper/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/lncrawl/scraper/compare/v0.1.2...v0.2.1
[0.1.2]: https://github.com/lncrawl/scraper/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/lncrawl/scraper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/lncrawl/scraper/releases/tag/v0.1.0
