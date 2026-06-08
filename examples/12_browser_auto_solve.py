"""Auto-solving Cloudflare challenges with a pluggable solver.

Modern CF challenges (managed / Turnstile / captcha) can't be solved in pure
Python — they need a real browser. Set `cloudflare.solver` and the engine will,
on a detected challenge, drive the solver to obtain a `cf_clearance` cookie,
apply it, and transparently retry the request. With no solver configured the
engine raises a clear exception instead (see 11_error_handling.py).

Two backends ship in the box (both implement the `ClearanceSolver` protocol, so
you can plug in your own — Camoufox, SeleniumBase, a captcha service, etc):

* BrowserSolver — drives Chrome in-process via nodriver
  (`pip install "lncrawl-scraper[browser]"`). Use `xvfb=True` on a GUI-less
  Linux server to run headful under a virtual display (true headless is detectable).

Run:
    uv run python examples/13_browser_auto_solve.py
"""

import json

from scraper import BrowserSolver, Scraper, default_config

TARGET = "https://novelfire.net/book/marvel-starting-with-the-ice-ice-fruit"


def main() -> None:
    config = default_config()
    # xvfb=True → headful under a virtual display (GUI-less Linux server).
    config.cloudflare.solver = BrowserSolver(timeout=60)
    s = Scraper(config=config)
    print("Solver configured:", type(config.cloudflare.solver).__name__)

    # A challenge on this request is now solved and retried automatically; you
    # just get the final page back.
    resp = s.get("https://novelfire.net/book/marvel-starting-with-the-ice-ice-fruit")
    print("headers:", json.dumps(dict(resp.headers), indent=2))

    soup = s.make_soup(resp)
    print("title:", soup.select_one("title").text)

    # Tip: Cloudflare binds cf_clearance to UA + IP/TLS. Run the solver behind the
    # same egress IP/proxy as the scraper; the solver's UA is adopted for you.


if __name__ == "__main__":
    main()
