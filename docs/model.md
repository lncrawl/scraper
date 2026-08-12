# The model

Everything in this library is organised by one model of what it is up against. Reading
this page first will make every other decision in the codebase look inevitable rather
than arbitrary.

## Composite scoring

A modern mitigation engine does not make one decision from one signal. It runs many
largely independent detectors on each request and folds their outputs into a single
trust score, then bands the score into three outcomes: return the content, serve an
interactive challenge, or block.

The score is close to **non-compensatory**: a strong result on most detectors does not
offset a single strongly anomalous one. A datacenter address or a non-browser TLS
fingerprint tends to dominate the outcome regardless of what else is correct.

## The bound

Treat admission as a conjunction. If `p_i` is the probability that some strategy passes
detector `i`, then

```
P(evade) ≲ min(p_1, p_2, … p_n)
```

A conjunction is no more likely than its least likely conjunct. The arithmetic is
trivial; the consequences are not.

**Improving a layer you already pass buys nothing.** If a strategy fails on address
reputation, perfecting its TLS profile does not raise `P(evade)` at all — not a little,
zero — until reputation stops being the minimum. `scraper.layers.marginal_gain` computes
this, and `scraper.planner` refuses remedies whose gain is zero.

**The two sides are not symmetric in effort.** An attacker must raise the minimum across
every active layer. A defender need only add one layer that is close to impassable. That
asymmetry is why this library treats two layers as stops rather than obstacles.

```python
from scraper import Layer, weakest
from scraper.layers import marginal_gain

odds = {Layer.IP_REPUTATION: 0.05, Layer.TLS_FINGERPRINT: 0.99}
weakest(odds)  # (L1 IP reputation, 0.05)
marginal_gain(odds, Layer.TLS_FINGERPRINT, 1.0)  # 0.0  — nothing to gain
marginal_gain(odds, Layer.IP_REPUTATION, 0.9)  # 0.85 — this is the one
```

## Emit versus possess

Detection is usually split into network-level and browser-level checks. That says where a
check sits; it does not predict how hard it is to satisfy. The useful question is what
each detector *reads*.

An **emitted artifact** is something the client transmits at connection time: the bytes of
a TLS `ClientHello`, the ordering of HTTP/2 frames, the sequence of request headers. It
carries no secret and records no history, so anything a real browser emits can in
principle be reproduced by a client built to do so.

A **possessed property** is something the client must hold continuously and cannot
fabricate on demand: accumulated per-zone behavioural history, or a private signing key.
These resist forgery not through obscurity but because reproducing them amounts to
actually being what the check tests for.

`scraper.layers.Trait` records this per layer, along with `HYBRID` for checks that read an
emitted artifact bound to something possessed — an address whose reputation accrued over
time, a cookie valid only from the context that earned it, an automation channel rather
than a byte string.

### Why the distinction is the load-bearing one

Three concrete behaviours fall straight out of it.

**Layers 2–5 are one barrier, not four.** TLS fingerprint, post-quantum key share, HTTP
frame fingerprinting and header order all read emitted artifacts, and they are correlated:
a client built to reproduce one browser's network stack passes all of them together. In
the bound they behave as a single term, so a defender adding a fifth emit-reading check
barely moves the result. `scraper.layers.expand` closes any reach set over the group so
the two can never drift apart.

**A possessed property is never rotated away from.** If the binding layer reads accumulated
history, discarding identity resets exactly what is being measured. The conventional
reflex — new address, try again — guarantees the history never gets long enough to pass.
`scraper.planner` vetoes rotation there and slows down instead.

**Two layers raise instead of retrying.** A mandated request signature and an
identity-provider gate both read a secret. `scraper.layers.IMPASSABLE` names them, and
reaching one produces `scraper.Impassable` with the single legitimate route in the message.

## What this replaces

The conventional shape is a table from status code to remedy: 403 rotates the proxy, 429
sleeps, a challenge re-solves. Two of those entries are actively harmful.

- A 403 can mean the address is blocklisted, the transport profile is wrong, or a challenge
  is being served with an error status. Rotating helps in one of the three cases and
  discards a working identity in the other two.
- A 429 says the address works and is being asked for too much. Treated as a block it
  retires a working exit, and the replacement is throttled just the same.

So nothing in this library reacts to a status code. `scraper.diagnosis.diagnose` turns a
response into a statement about which layer is binding, and `scraper.planner` decides what
to do about it. Both are pure and both are testable without a network.

## Reading order in the source

| Module | What it holds |
| --- | --- |
| `scraper/layers.py` | The model: the layer table, traits, the bound. |
| `scraper/diagnosis.py` | Response → binding layer. Pure. |
| `scraper/planner.py` | Binding layer → what to change. Pure. |
| `scraper/identity.py` | The emitted half, treated as one indivisible thing. |
| `scraper/pacing.py` | The possessed half: timing and navigation. |
| `scraper/memory.py` | The possessed half, persisted. |
| `scraper/session.py` | The loop that wires them together. Small on purpose. |

See also [layers.md](layers.md) for the per-layer reference and [tiers.md](tiers.md) for
the escalation ladder.
