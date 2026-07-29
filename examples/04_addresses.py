"""Where the packets come from, which is layer 1.

The difficulty here is economic, not technical. The address is chosen freely, but
its reputation accrued over time and can be rented, never fabricated. Declare the
kind honestly: claiming MOBILE for a datacenter range does not change what the
reputation database thinks, it only stops this library from telling you that layer 1
is the reason nothing works.

    uv run python examples/04_addresses.py
"""

from scraper import ExitKind, ExitSpec, Scraper, ScraperConfig, TorPoolSpec
from scraper.exceptions import Exhausted

config = ScraperConfig(
    exits=[
        # Best kind first is not required — the pool sorts by kind.
        ExitSpec(
            url="http://user:pass@mobile.provider.test:8000",
            kind=ExitKind.MOBILE,
            label="carrier",
        ),
        ExitSpec(
            url="http://user:pass@residential.provider.test:8000",
            kind=ExitKind.RESIDENTIAL,
        ),
        # A tor-pool endpoint: many Tor instances behind one sticky port. Reported as
        # TOR because exit lists are published, so it clears none of layer 1. Right
        # for a site that does not score addresses, wrong for one that does.
        TorPoolSpec(api_url="http://127.0.0.1:8080", token="tp_...."),
    ],
    # Concurrent sessions per address is itself a behavioural signal, so this stays
    # in the low single digits. Values above 3 are clamped.
    max_sessions_per_exit=2,
)

with Scraper(config=config) as scraper:
    print("best kind on offer:", scraper.exits.best_kind.value)
    print("clears layer 1:", scraper.exits.reach())

    # An address is leased per origin and held. Rotation happens on evidence, never
    # on a timer, because a clearance and the accumulated history are both bound to
    # the address.
    lease = scraper.exits.lease("example.com")
    print("leased:", lease.exit_id, "->", lease.proxies)

    try:
        scraper.get("https://example.com/")
    except Exhausted as exc:
        # With only Tor configured this is the message you get, and it is the useful
        # one: rotating between published ranges cannot help.
        print(exc.detail)
