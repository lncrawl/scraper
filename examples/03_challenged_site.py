"""A site that serves a JavaScript challenge.

The pattern is solve-once-and-reuse, and the reason it works is that a clearance is
bound to the address, User-Agent and TLS fingerprint that earned it. So the browser
runs once, its exact User-Agent is adopted, and everything after that is an ordinary
cheap request on the same identity.

Needs the `browser` extra:  pip install lncrawl-scraper[browser]

    uv run python examples/03_challenged_site.py
"""

import logging

from scraper import Scraper, ScraperConfig
from scraper.browser import NoDriverSolver
from scraper.exceptions import Exhausted

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

config = ScraperConfig(
    # headless=False is the default and it is deliberate: a headless build reports a
    # software renderer for WebGL, which is a clear indicator on its own. On a
    # server, run this under a virtual display rather than turning headless on.
    browser=NoDriverSolver(),
)

TARGET = "https://nowsecure.nl/"

with Scraper(config=config) as scraper:
    try:
        for page in range(3):
            response = scraper.get(TARGET)
            print(page, response.status_code, len(response.content))
    except Exhausted as exc:
        # The message names the layer that ended the attempt and what would move it.
        print("stopped at", exc.layer, "-", exc.detail)

    print()
    print(scraper.explain(TARGET))
