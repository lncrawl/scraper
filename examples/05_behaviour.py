"""The hardest layer, addressed by not trying to defeat it.

A per-zone behavioural model reads accumulated, non-portable history: timing
regularity, navigation chains, session age and depth, concurrent sessions per
address. None of that can be presented on demand, so the only thing that works is to
behave the way the model expects and let the history accrue.

    uv run python examples/05_behaviour.py
"""

import statistics

from scraper import Pacer, PacingPolicy, Scraper, ScraperConfig, SharedState

policy = PacingPolicy(
    interval=4.0,  # target mean, not a floor
    shape=2.5,  # low shape = long right tail, which is what browsing looks like
    pause_chance=0.06,  # occasional reading pauses; a pure stream has none
    warmup=True,  # a visitor does not land on a deep page first
)

# The gaps are drawn, not set. A fixed interval produces perfectly regular arrivals,
# which is a stronger signal than being fast.
pacer = Pacer(policy, seed=1)
gaps = [pacer.gap("example.com") for _ in range(500)]
print(f"mean {statistics.mean(gaps):.2f}s  median {statistics.median(gaps):.2f}s")
print(f"min {min(gaps):.2f}s  max {max(gaps):.2f}s  sd {statistics.pstdev(gaps):.2f}s")

config = ScraperConfig(pacing=policy)

# Two scrapers on one host must not look like two contradictory visitors: separate
# addresses, separate clocks, one of them always arriving cold. Sharing the site
# state is what keeps them one visitor.
state = SharedState.create(config)
first = Scraper(origin="https://example.com", config=config, state=state)
second = Scraper(origin="https://example.com", config=config, state=state)

try:
    first.get("https://example.com/")
    print("what the other scraper knows:", second.knows("https://example.com/").successes)
    print(second.explain("https://example.com/"))
finally:
    first.close()
    second.close()
    state.close()
