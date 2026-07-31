"""The clearance tier, live: a real browser solving a real challenge.

Needs a real Chrome, and runs on any Python this package supports:

    uv run python livetest/clearance.py

Writes livetest/clearance.json, which the report merges with the main results.

Headless, matching the shipped default — matching it is the point of a live harness,
and `headless.py` measured that it costs nothing.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict, List

HERE = pathlib.Path(__file__).parent

from scraper import PacingPolicy, Scraper, ScraperConfig  # noqa: E402
from scraper.cdp import CdpSolver  # noqa: E402
from scraper.diagnosis import diagnose  # noqa: E402
from scraper.exceptions import Exhausted, Impassable  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402

# Hosts the direct tier could not get past in the main run, so a pass here is the
# browser doing work the cheap tier could not.
CANDIDATES = [
    # Ordered by what actually exercises the tier: the host must challenge the direct
    # tier *and* be solvable in a browser. webnovel does both (verified: clears in ~3s).
    "https://www.webnovel.com/",
    "https://m.webnovel.com/",
    "https://centralnovel.com/",
    "https://www.chereads.com/",
    "https://www.fanfiction.net/",
    "https://ranobes.top/",
    "https://www.novelhall.com/",
    "https://lightnovelfr.com/",
]

WORKDIR = HERE / "state" / "clearance"


def steps() -> List[Dict[str, Any]]:
    return []


def check(out: List[Dict[str, Any]], what: str, ok: bool, value: Any = "") -> bool:
    out.append({"what": what, "value": str(value)[:400], "ok": bool(ok)})
    return bool(ok)


def note(out: List[Dict[str, Any]], what: str, value: Any = "") -> None:
    out.append({"what": what, "value": str(value)[:400], "ok": None})


def confirm_direct_is_blocked(url: str) -> Dict[str, Any]:
    """Establish that the cheap tier genuinely cannot serve *url*."""
    transport = ImpersonateTransport()
    try:
        response = transport.send("GET", url, timeout=30)
    finally:
        transport.close()
    verdict = diagnose(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.text,
        url=url,
    )
    return {
        "status": response.status_code,
        "action": verdict.action.value,
        "layer": str(verdict.layer) if verdict.layer else None,
    }


def main() -> None:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    out: List[Dict[str, Any]] = []
    started = time.monotonic()
    verdict = "pass"
    error = ""

    target = ""
    for url in CANDIDATES:
        state = confirm_direct_is_blocked(url)
        note(out, f"direct tier against {url}", state)
        if state["action"] == "solve":
            target = url
            break
        time.sleep(1.0)

    if not target:
        result = {
            "id": "S27",
            "title": "A real browser solves a real challenge, then the cheap tier reuses it",
            "layers": ["L6", "L7", "L9", "L10"],
            "proves": "solve-once-and-reuse: the expensive tier runs once and the clearance "
            "is replayed on the identity that earned it.",
            "target": ", ".join(CANDIDATES),
            "verdict": "inconclusive",
            "seconds": round(time.monotonic() - started, 2),
            "steps": out,
            "error": "no candidate host was challenging the direct tier on this run",
        }
        (HERE / "clearance.json").write_text(json.dumps([result], indent=1))
        print("inconclusive: nothing was challenging right now")
        return

    solver = CdpSolver(settle=4.0)
    solves: List[str] = []

    config = ScraperConfig(
        browser=solver,
        pacing=PacingPolicy(interval=1.5, warmup=False, pause_chance=0.0),
        data_dir=WORKDIR,
        raise_for_status=False,
        guard_topic=False,
        max_attempts=4,
        solve_timeout=120.0,
        timeout=(15, 60),
    )

    original_solve = solver.solve

    def watched(url: str, **kwargs: Any):
        solves.append(url)
        return original_solve(url, **kwargs)

    solver.solve = watched  # type: ignore[method-assign]

    with Scraper(config=config) as scraper:
        note(out, "target chosen", target)
        note(out, "ladder", " -> ".join(f"{c.name}({c.cost})" for c in scraper.planner.ladder()))
        try:
            first = scraper.get(target)
            check(
                out,
                "the challenged host was retrieved",
                first.status_code == 200,
                f"HTTP {first.status_code}, {len(first.content)} bytes",
            )
            check(out, "the browser ran exactly once", len(solves) == 1, f"{len(solves)} solve(s)")
            check(
                out,
                "the clearance tier is what worked",
                scraper.knows(target).tier == "clearance",
                scraper.knows(target).tier,
            )

            profile = scraper.knows(target)
            clearance = profile.clearance_for(target)
            if check(out, "a clearance was stored", clearance is not None):
                assert clearance is not None
                note(out, "clearance cookies", sorted(clearance.cookies))
                note(out, "bound to User-Agent", clearance.user_agent)
                note(out, "valid for", f"{clearance.expires_at - clearance.issued_at:.0f}s")

            # The whole point: subsequent pages must not launch a browser again.
            for index in range(3):
                again = scraper.get(target)
                check(
                    out,
                    f"reuse #{index + 1} served without a new solve",
                    again.status_code == 200 and len(solves) == 1,
                    f"HTTP {again.status_code}, {len(solves)} solve(s) total",
                )

            note(out, "explain()", scraper.explain(target))
        except (Exhausted, Impassable) as exc:
            verdict = "fail"
            error = f"{type(exc).__name__}: {exc}"
            note(out, "stopped", str(exc))
        except Exception as exc:  # noqa: BLE001 - a live browser failure is data
            verdict = "error"
            error = f"{type(exc).__name__}: {exc}"
            note(out, "error", str(exc)[:400])

    if verdict == "pass" and any(s["ok"] is False for s in out):
        verdict = "fail"

    result = {
        "id": "S27",
        "title": "A real browser solves a real challenge, then the cheap tier reuses it",
        "layers": ["L6", "L7", "L9", "L10"],
        "proves": "solve-once-and-reuse: the expensive tier runs once and the clearance is "
        "replayed on the identity that earned it, so later pages cost one HTTP request.",
        "target": target,
        "verdict": verdict,
        "seconds": round(time.monotonic() - started, 2),
        "steps": out,
        "error": error,
    }
    (HERE / "clearance.json").write_text(json.dumps([result], indent=1))
    print(
        f"{verdict.upper()}  {len([s for s in out if s['ok']])} ok, "
        f"{len([s for s in out if s['ok'] is False])} failed"
    )
    for step in out:
        if step["ok"] is False:
            print("  x", step["what"], "|", step["value"])


if __name__ == "__main__":
    main()
