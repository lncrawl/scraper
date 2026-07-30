"""Rendering, live: a real browser on a page whose HTML is not its content.

Run under a Python that can import nodriver (3.10-3.13), with a real Chrome installed
and a display available — same requirement as `clearance.py`, and for the same reason:

    /tmp/scr312/bin/python livetest/render.py

Writes livetest/render.json, which the report merges with the main results.

This is the one capability no amount of HTTP fidelity substitutes for, and it is also
the one that is easiest to fake passing. A render that returns the shell it started
with looks like a success to anything that only checks for an exception — the caller
parses it, finds nothing, and reports an empty page. So the check here is comparative:
the same URL through the impersonated transport and through the browser, and the
browser has to produce substantially more readable text than the transport did.

Candidates are single-page applications found by surveying lncrawl's source index:
each answers 200 with a few dozen characters of visible text and fills the page in
from JavaScript. Nothing is blocking on any of them, which is the point — no layer is
binding, so no diagnosis would ever lead here.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = pathlib.Path(__file__).parent

from scraper import PacingPolicy, Scraper, ScraperConfig  # noqa: E402
from scraper.browser import NoDriverSolver, RenderError  # noqa: E402
from scraper.exceptions import ScraperError  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402

# (url, a selector the *content* is behind, or None if one has not been verified for
# that host). Measured HTTP vs rendered visible text: wuxiaworld 10 -> 9902,
# wordexcerpt 29 -> 5451, webnovelonline 49 -> 3151.
#
# The selector has to name content, not chrome. `a[href]` on wuxiaworld is satisfied by
# the navigation bar and returns 457 characters of a 9902-character page — the API
# behaving exactly as documented, and a reminder that a loose selector ends the wait
# early. An unverified guess here would be worse than none, so the fallbacks carry None
# and the selector check reports skipped rather than passing on a host nobody checked.
CANDIDATES: List[Tuple[str, Optional[str]]] = [
    ("https://www.wuxiaworld.com/", "a.line-clamp-2"),
    ("https://wordexcerpt.com/", None),
    ("https://webnovelonline.com/", None),
]

SETTLE = 6.0
"""Seconds to let a page run when no selector was given.

The only stand-in for "the page has finished" when there is nothing to poll for."""

MIN_GAIN = 5.0
"""How much more text the browser must produce to call the page rendered.

A generous multiple on purpose. A small gain is what a cookie banner or a lazily
inserted advert produces, and treating that as "rendered" would put a source on the
expensive path for nothing."""

WORKDIR = HERE / "state" / "render"


def visible_text(html: str) -> str:
    """Roughly what a person would read. Enough to compare two versions of one page."""
    without_scripts = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", without_scripts).split())


def check(out: List[Dict[str, Any]], what: str, ok: bool, value: Any = "") -> bool:
    out.append({"what": what, "value": str(value)[:400], "ok": bool(ok)})
    return bool(ok)


def note(out: List[Dict[str, Any]], what: str, value: Any = "") -> None:
    out.append({"what": what, "value": str(value)[:400], "ok": None})


def http_text(url: str) -> str:
    """The page as the cheap transport sees it, with no browser involved."""
    transport = ImpersonateTransport()
    try:
        response = transport.send("GET", url, timeout=30)
        return visible_text(response.text)
    finally:
        transport.close()


def main() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    out: List[Dict[str, Any]] = []
    started = time.monotonic()
    verdict = "pass"
    error = ""

    target = ""
    selector: Optional[str] = None
    baseline = ""
    for url, wait_for in CANDIDATES:
        try:
            text = http_text(url)
        except Exception as exc:  # noqa: BLE001 - an unreachable candidate is not a finding
            note(out, f"{url} is unreachable", type(exc).__name__)
            continue
        note(out, f"HTTP visible text from {url}", f"{len(text)} chars")
        if len(text) < 600:
            target, selector, baseline = url, wait_for, text
            break
        time.sleep(1.0)

    if not target:
        result = _result(
            "inconclusive",
            "no candidate was serving an empty shell on this run",
            ", ".join(url for url, _ in CANDIDATES),
            out,
            started,
        )
        (HERE / "render.json").write_text(json.dumps([result], indent=1))
        print("inconclusive: every candidate served content without a browser")
        return

    config = ScraperConfig(
        browser=NoDriverSolver(settle=SETTLE),
        pacing=PacingPolicy(interval=1.5, warmup=False, pause_chance=0.0),
        data_dir=WORKDIR,
        raise_for_status=False,
        guard_topic=False,
        solve_timeout=90.0,
        timeout=(15, 60),
    )

    with Scraper(config=config) as scraper:
        note(out, "target chosen", f"{target} (content selector {selector or 'none known'})")
        try:
            html = scraper.render(target)
            rendered = visible_text(html)
            note(out, "rendered visible text", f"{len(rendered)} chars")
            check(
                out,
                f"the browser produced at least {MIN_GAIN:.0f}x the transport's text",
                len(rendered) >= max(600, len(baseline) * MIN_GAIN),
                f"{len(baseline)} -> {len(rendered)} chars",
            )

            soup = scraper.render_soup(target)
            links = soup.select("a[href]")
            check(
                out, "the rendered page parses into a usable document", len(links) > 5, len(links)
            )

            if selector is None:
                note(out, "no verified content selector for this host", "selector check skipped")
            else:
                # What a selector buys: the wait ends on evidence rather than on a
                # settle interval chosen for pages that gave no selector at all.
                began = time.monotonic()
                waited = visible_text(scraper.render(target, wait_for=selector))
                elapsed = time.monotonic() - began
                check(
                    out,
                    "a satisfied selector ends the wait early, on evidence",
                    elapsed < SETTLE and len(waited) > 0,
                    f"{elapsed:.1f}s against a {SETTLE:.0f}s settle, {len(waited)} chars",
                )
                # Measured, and the lesson for anyone choosing one: this host hydrates
                # its cards as empty skeletons and fills them in afterwards, so the
                # anchors exist long before their text does — 457 chars against 8463
                # settled. A selector has to name an element that cannot exist before
                # the data does; where no such element exists, no selector is the
                # honest answer and the settle interval is what you have.
                note(
                    out,
                    "text present when the selector appeared",
                    f"{len(rendered)} settled -> {len(waited)} with {selector}",
                )

            # The two halves of "this is not a tier". A render must not be recorded as
            # a working tier — it is no evidence the HTTP ladder works — and it must
            # still take its turn on the origin's clock like any other request.
            profile = scraper.knows(target)
            check(
                out,
                "a render is not recorded as a tier that worked",
                profile.tier == "",
                f"tier={profile.tier!r} successes={profile.successes}",
            )
            check(
                out,
                "the render joined the referrer chain",
                scraper.last_url == target,
                scraper.last_url,
            )

            missing = _selector_that_will_not_appear(scraper, target)
            check(
                out,
                "a selector that never appears is an error, not an empty page",
                missing is None,
                missing or "RenderError",
            )
            note(out, "explain()", scraper.explain(target))
        except RenderError as exc:
            verdict = "fail"
            error = f"RenderError: {exc}"
            note(out, "the page never rendered", str(exc))
        except ScraperError as exc:
            verdict = "fail"
            error = f"{type(exc).__name__}: {exc}"
            note(out, "stopped", str(exc))
        except Exception as exc:  # noqa: BLE001 - a live browser failure is data
            verdict = "error"
            error = f"{type(exc).__name__}: {exc}"
            note(out, "error", str(exc)[:400])

    if verdict == "pass" and any(step["ok"] is False for step in out):
        verdict = "fail"

    result = _result(verdict, error, target, out, started)
    (HERE / "render.json").write_text(json.dumps([result], indent=1))
    print(
        f"{verdict.upper()}  {len([s for s in out if s['ok']])} ok, "
        f"{len([s for s in out if s['ok'] is False])} failed"
    )
    for step in out:
        if step["ok"] is False:
            print("  x", step["what"], "|", step["value"])


def _selector_that_will_not_appear(scraper: Scraper, url: str) -> Optional[str]:
    """``None`` when the miss raised, else what came back instead."""
    try:
        html = scraper.render(url, wait_for="#definitely-not-here", timeout=15.0)
    except RenderError:
        return None
    return f"{len(html)} chars returned"


def _result(
    verdict: str,
    error: str,
    target: str,
    steps: List[Dict[str, Any]],
    started: float,
) -> Dict[str, Any]:
    return {
        "id": "S30",
        "title": "A browser renders a page whose HTML is not its content",
        "layers": [],
        "proves": "the one capability HTTP fidelity cannot substitute for, and the one "
        "failure that is silent: a shell parses to nothing and raises nothing.",
        "target": target,
        "verdict": verdict,
        "seconds": round(time.monotonic() - started, 2),
        "steps": steps,
        "error": error,
    }


if __name__ == "__main__":
    main()
