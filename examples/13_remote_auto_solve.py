"""Auto-solving Cloudflare challenges with a pluggable solver.

Modern CF challenges (managed / Turnstile / captcha) can't be solved in pure
Python — they need a real browser. Set `cloudflare.solver` and the engine will,
on a detected challenge, drive the solver to obtain a `cf_clearance` cookie,
apply it, and transparently retry the request. With no solver configured the
engine raises a clear exception instead (see 11_error_handling.py).

Two backends ship in the box (both implement the `ClearanceSolver` protocol, so
you can plug in your own — Camoufox, SeleniumBase, a captcha service, etc):

* RemoteSolver  — talks to a FlareSolverr/Byparr container over HTTP. Keeps the
  scraper lightweight (no browser in its image). Recommended for servers.

Run:
    uv run python examples/13_remote_auto_solve.py
"""

import json

from scraper import RemoteSolver, Scraper, default_config

TARGET = "https://novelfire.net/book/marvel-starting-with-the-ice-ice-fruit"


def main() -> None:
    config = default_config()
    config.cloudflare.solver = RemoteSolver("http://localhost:8192", timeout=60)
    s = Scraper(config=config)
    print("Solver configured:", type(s.engine.cf_solver).__name__)

    # A challenge on this request is now solved and retried automatically; you
    # just get the final page back.
    resp = s.get(TARGET)
    print("headers:", json.dumps(dict(resp.headers), indent=2))

    soup = s.make_soup(resp)
    print("title:", soup.select_one("title").text)

    # Tip: Cloudflare binds cf_clearance to UA + IP/TLS. Run the solver behind the
    # same egress IP/proxy as the scraper; the solver's UA is adopted for you.


if __name__ == "__main__":
    main()
