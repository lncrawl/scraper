"""The cheapest rung and the most expensive one.

The cheapest way past a protected site is not to touch it: an archived snapshot is
served from a host with no mitigation stack. It trades freshness for cost, which only
you can weigh, so it is off by default.

The last rung is delegation, and it is last for an honest reason: against a per-zone
composite model that is actively tuned, maintaining a bypass is a standing engineering
cost rather than a piece of work with an end. No provider is bundled — their formats
differ and change, and a wrapper that guesses wrong fails in a way that looks like the
site blocking you.

    uv run python examples/08_archive_and_managed.py
"""

import requests

from scraper import Scraper, ScraperConfig
from scraper.tiers import http_provider
from scraper.tiers.archive import SOURCE_HEADER, ArchiveTier
from scraper.transport import ImpersonateTransport

URL = "https://example.com/"

# What is in the archive, and how stale.
transport = ImpersonateTransport()
tier = ArchiveTier(transport)
for timestamp, original in tier.captures(URL, limit=5):
    print(timestamp, original)
transport.close()

config = ScraperConfig(
    archive=True,
    archive_max_age=90 * 86400,  # refuse anything older than 90 days
)
with Scraper(config=config) as scraper:
    response = scraper.get(URL)
    # The response carries the *original* URL, so relative links resolve against the
    # real site rather than redirecting the crawl into the snapshot.
    print(response.url, "captured", response.headers.get(SOURCE_HEADER))


def my_provider(method: str, url: str, **options) -> requests.Response:
    """Anything satisfying this signature is a managed tier.

    It must return the *origin's* status and body. Returning the provider's own
    status instead breaks diagnosis: a 200 wrapping a 403 reads as a successful
    scrape of a block page.
    """
    return requests.request(method, url, timeout=options.get("timeout") or 60)


with Scraper(config=ScraperConfig(managed=my_provider)) as scraper:
    print(scraper.planner.ladder())

# For the several services shaped as "GET the endpoint with the target as a parameter".
provider = http_provider("https://api.provider.test/v1", token="k3y", extra={"render_js": "true"})
print(provider)
