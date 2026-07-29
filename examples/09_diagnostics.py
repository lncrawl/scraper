"""Finding out why, which is the point of the whole design.

Two habits make this library usable in anger: read `explain()` after a run, and read
the exception's `layer` rather than its status code. "403 after 3 retries" is the
message that sends people to rewrite the part that was already working.

    uv run python examples/09_diagnostics.py
"""

import logging

from scraper import Layer, Scraper, ScraperConfig, diagnose
from scraper.exceptions import Blocked, Exhausted, Impassable, Poisoned

# DEBUG prints one line per decision, with the reasoning attached.
logging.basicConfig(level=logging.DEBUG, format="%(levelname)-7s %(message)s")

# Diagnosis is a pure function, so a saved page can be classified with no network.
# This is the fastest way to check what the library thinks of a response you captured.
saved = "<html><head><title>Just a moment...</title></head><body>__cf_chl_</body></html>"
print(diagnose(status=200, body=saved))
print(diagnose(status=429, headers={"retry-after": "30"}))
print(diagnose(status=403, body="<p>Error 1020</p>"))
print(diagnose(status=403, body="<p>Error 1010</p>"))
print(diagnose(status=404))

URL = "https://example.com/deep/page"
with Scraper(config=ScraperConfig(remember=True)) as scraper:
    try:
        scraper.get(URL)
    except Impassable as exc:
        # No bypass exists. The message names the only legitimate route.
        print("stop:", exc.layer, exc.detail)
    except Poisoned as exc:
        print("decoy content:", exc.detail)
    except Exhausted as exc:
        # A bypass may exist; this configuration does not reach it.
        print("out of reach:", exc.layer)
        print("  ", exc.detail)
        if exc.layer is Layer.IP_REPUTATION:
            print("   -> configure a residential or mobile exit")
        elif exc.layer in (Layer.MANAGED_CHALLENGE, Layer.TURNSTILE, Layer.CDP):
            print("   -> configure a browser solver")
    except Blocked as exc:
        print("blocked at", exc.layer, "which reads a", exc.layer_info.trait.value, "property")

    print()
    print(scraper.explain(URL))

    # Everything learned is inspectable, and persists between runs.
    profile = scraper.knows(URL)
    print()
    print("binding layer :", profile.binding)
    print("working tier  :", profile.tier)
    print("interval      :", round(profile.interval, 2))
    print("ledger        :", profile.successes, "ok /", profile.failures, "failed")
    print("known decoys  :", len(profile.decoys))
