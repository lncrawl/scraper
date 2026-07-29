# Documentation

Start with [model.md](model.md). The rest of the library follows from it, and the design looks
arbitrary without it.

| Page | What it covers |
| --- | --- |
| [model.md](model.md) | Composite scoring, the bound, and the emit/possess distinction. **Read first.** |
| [layers.md](layers.md) | The nineteen layers: what each reads, what moves it. |
| [tiers.md](tiers.md) | The escalation ladder, and how to write a tier. |
| [configuration.md](configuration.md) | Every `ScraperConfig` field, grouped by what it changes. |
| [behaviour.md](behaviour.md) | Pacing, warm-up, referrer chains, persistence, shared state. |
| [decoy-content.md](decoy-content.md) | The one layer that returns no error. |
| [web-bot-auth.md](web-bot-auth.md) | Signed requests, and publishing a key directory. |
| [diagnostics.md](diagnostics.md) | `explain()`, the exception taxonomy, and common conclusions. |
| [migration.md](migration.md) | Porting from 0.2.x. |

Runnable versions of most of this are in [../examples](../examples/), which are ordered so that
reading them in sequence explains the design.

## The three-sentence version

A modern mitigation engine runs many detectors and folds them into one score, and admission
behaves as a near-conjunction — so the weakest layer bounds the outcome, and effort spent on any
other layer buys nothing. Some detectors read an artifact the client *emits*, which a faithful
imitator can reproduce; others read a property the client must *possess*, which it cannot. This
library diagnoses which layer is binding, escalates to the cheapest capability that can reach
that layer, and — where the layer reads a possessed property — holds identity still and
accumulates rather than rotating, because rotating resets the thing being measured.
