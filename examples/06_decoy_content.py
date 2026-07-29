"""The one layer that returns no error.

A honeypot inserts hidden nofollow links into a page, leading into a maze of
generated decoy pages. Following them poisons the store *and* flags the session
network-wide, and nothing about the responses says so — the scrape looks like it is
working.

    uv run python examples/06_decoy_content.py
"""

from scraper import Scraper, ScraperConfig, TopicGuard, safe_links
from scraper.links import looks_like_maze

PAGE = """
<html><body>
  <a href="/chapter/1">Chapter 1</a>
  <a href="/trap/a" rel="nofollow">bait</a>
  <a href="/trap/b" style="display:none">bait</a>
  <div style="visibility:hidden"><a href="/trap/c">bait</a></div>
  <a href="/trap/d"></a>
</body></html>
"""

# include_rejected shows the reasoning, which is otherwise indistinguishable from a
# page that simply had no links.
for link in safe_links(PAGE, "https://example.com/index.html", include_rejected=True):
    verdict = "follow" if link.followable else f"skip ({link.rejected})"
    print(f"{verdict:32} {link.url}")

print()
# The backstop for having got it wrong anyway. Decoy pages are generated to read as
# plausible prose, so structure will not give them away — but they are not *about*
# what the site is about.
guard = TopicGuard(min_samples=3)
for _ in range(4):
    guard.learn("chapter translation novel protagonist cultivation sect elder sword")
print("on topic :", guard.suspect("the protagonist drew his sword and left the sect"))
print("off topic:", guard.suspect("amortisation schedules against municipal bond covenants"))

print()
# A maze is produced, not authored, so its paths share a shape.
print("generated:", looks_like_maze([f"https://example.com/g/{i:04d}/p" for i in range(20)]))

# on_decoy="raise" turns a suspicion into an exception. Right for anything that
# trains on or republishes what it collects; "warn" is the default because the check
# is a heuristic and a false positive should not fail a job.
config = ScraperConfig(guard_topic=True, on_decoy="raise")
with Scraper(config=config) as scraper:
    print("guard configured:", scraper.config.on_decoy)
