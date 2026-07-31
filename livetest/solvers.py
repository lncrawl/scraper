"""Two solvers, same hosts, same session, alternating. The merge gate for a new one.

A solver quietly worse at clearing is the failure mode that matters, and it is invisible
from a green unit run — the stubs in `tests/test_cdp.py` prove a module drives a browser
correctly and can say nothing about whether a site believes it. This is what says that.

The pairing is the measurement, so the two arms have to run against the same host within
seconds of each other: challenge deployments change during a day, and a host that
switched from scoring to Turnstile between two runs an hour apart makes the earlier arm
look better for a reason that has nothing to do with the solver.

Only `cdp` is bundled today, so this is a one-armed run until a second backend lands
— which is the point of keeping it: whatever comes next gets compared against what
already works, on the same hosts, before it replaces anything.

    uv run python livetest/solvers.py
    uv run python livetest/solvers.py --report    # offline

Writes livetest/solvers.json.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

from headless import CORPUS, verify  # noqa: E402 - a sibling file, not an installed module

from scraper.browser import BrowserSolver  # noqa: E402
from scraper.cdp import CdpSolver  # noqa: E402

OUT = HERE / "solvers.json"

BUILDERS: Dict[str, Callable[[bool], BrowserSolver]] = {
    "cdp": lambda headless: CdpSolver(headless=headless, settle=3.0),
}


def one(name: str, solver: BrowserSolver, url: str, timeout: float) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = solver.solve(url, timeout=timeout)
        row = verify(url, result.cookies, result.user_agent)
    except Exception as exc:  # noqa: BLE001 - which arm fails is the measurement
        row = {"cleared": False, "why": f"{type(exc).__name__}: {exc}"[:100]}
    row.update(solver=name, url=url, seconds=round(time.monotonic() - started, 1))
    return row


def report(rows: List[Dict[str, Any]]) -> None:
    names = sorted({r["solver"] for r in rows})
    by: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        by[r["url"]][r["solver"]] = r

    width = max((len(u) for u in by), default=20)
    print(f"\n{'host':<{width}}  " + "  ".join(f"{n:>16}" for n in names))
    print("-" * (width + 2 + 18 * len(names)))
    disagreements = []
    for url in sorted(by):
        cells = []
        for name in names:
            r = by[url].get(name)
            cells.append(
                "-"
                if r is None
                else f"{'CLEARED' if r.get('cleared') else 'no'} {r['seconds']:.0f}s"
            )
        print(f"{url:<{width}}  " + "  ".join(f"{c:>16}" for c in cells))
        verdicts = {n: bool(by[url].get(n, {}).get("cleared")) for n in names}
        if len(set(verdicts.values())) > 1:
            disagreements.append((url, verdicts))

    print()
    for name in names:
        got = [r for r in rows if r["solver"] == name]
        ok = [r for r in got if r.get("cleared")]
        times = sorted(r["seconds"] for r in ok)
        median = times[len(times) // 2] if times else 0.0
        print(f"  {name:<10} cleared {len(ok)}/{len(got)}   median {median:.1f}s")

    if disagreements:
        # The only rows worth reading twice. Everything else is the two agreeing,
        # which is what a replacement solver has to earn before it is one.
        print("\n  they disagree on:")
        for url, verdicts in disagreements:
            says = ", ".join(f"{n}={'yes' if v else 'no'}" for n, v in verdicts.items())
            print(f"    {url}  ({says})")
    else:
        print("\n  no disagreements")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="re-read the JSON, no network")
    ap.add_argument("--solvers", default="cdp")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--hosts", default="", help="comma-separated, overrides the corpus")
    ap.add_argument("--limit", type=int, default=16)
    args = ap.parse_args()

    if args.report:
        report(json.loads(OUT.read_text())["rows"])
        return 0

    hosts = [h for h in args.hosts.split(",") if h] or CORPUS[: args.limit]
    solvers = {n: BUILDERS[n](args.headless) for n in args.solvers.split(",") if n}
    rows: List[Dict[str, Any]] = []

    for url in hosts:
        # Alternating per host rather than per run: the two arms have to meet the same
        # deployment, and a whole pass takes long enough for that to stop being true.
        for name, solver in solvers.items():
            row = one(name, solver, url, args.timeout)
            rows.append(row)
            mark = "CLEARED" if row.get("cleared") else "no     "
            print(
                f"{mark} {name:<9} {url:<40} {row['seconds']:5.1f}s "
                f"{row.get('why') or row.get('layer') or ''}",
                flush=True,
            )
            OUT.write_text(
                json.dumps({"headless": args.headless, "rows": rows}, indent=1),
                encoding="utf-8",
            )

    report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
