"""Does a headless browser clear a challenge that a headed one clears?

The answer decides whether a solver can run where nobody is sitting — a server, a
container, the remote interactive-solve design — and it used to be assumed to be no.
Measured over the corpus below it is **yes, once the browser stops announcing itself**,
and the two reasons previously given for headed-only both turned out to be wrong. The
recorded arms in ``headless.json`` are what retired them; keep them, because "we tried
that and it did not work" is only worth anything with the run attached.

Needs a real Chrome. Runs on any Python this package supports:

    uv run python livetest/headless.py             # the A/B, ~25 min
    uv run python livetest/headless.py --report    # re-read the JSON, no network

Two things this has to get right or the numbers mean nothing:

1. **A returned ``SolveResult`` is not a cleared challenge.** The solve loop polls
   until ``is_still_challenged`` goes false *or the deadline passes*, and returns
   either way. Clearance is therefore verified separately, by replaying the harvested
   cookies and User-Agent over an impersonating transport and diagnosing what comes
   back — which is the thing a clearance exists to do.
2. **Every solve gets its own profile directory.** Share one and the headless arm
   inherits a clearance the headed arm earned, reporting headless as working when it
   did nothing at all.

And one thing the *analysis* has to get right: a host neither arm clears is a control,
not a loss. It says the solver cannot do that host, not that headless is why, so those
are counted apart and kept out of the verdict.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from scraper.cdp import CdpSolver
from scraper.diagnosis import Action, diagnose
from scraper.transport import ImpersonateTransport

HERE = pathlib.Path(__file__).parent
OUT = HERE / "headless.json"

# Hosts that actually put a challenge in the way — the only ones this can learn from.
# Two thirds came from probe.json (everything the direct tier could not get past, one
# per registrable domain); the rest from source requests on lncrawl's tracker, since a
# corpus drawn only from sources we already support is a corpus of sites we already
# beat. Re-derive rather than curate by hand: sites move between layers, and a host
# that stopped challenging silently stops being evidence.
CORPUS = [
    "https://centralnovel.com/",
    "https://www.chereads.com/",
    "https://ranobes.net/",
    "https://ranobes.top/",
    "https://www.novelupdates.com/",
    "https://novelgo.id/",
    "https://dragontea.ink/",
    "https://foxaholic.com/",
    "https://arcanetranslations.com/",
    "https://www.f-w-o.com/",
    "https://www.scribblehub.com/",
    "https://kissmanga.in/",
    "https://syosetu.org/",
    "https://daotranslate.com/",
    "https://aquareader.org/",
    "https://www.foxteller.com/",
    "https://skydemonorder.com/",
    "https://katreadingcafe.com/",
    "https://novelasligeras.net/",
    "https://ln.hako.vn/",
    "https://docln.net/",
    "https://novelmania.com.br/",
    "https://novelfrance.fr/",
    "https://smnovels.com/",
    "https://librarynovel.com/",
    "https://novelnext.com/",
    "http://readlightnovel.online/",
    "https://world-novel.fr/",
    "https://massnovel.fr/",
    "https://archiveofourown.org/",
    "https://manga-tr.com/",
    "https://housesaikai.net/",
    "https://readnovelmtl.com/",
    "https://tunovelaligera.com/",
    "https://uukanshu.cc/",
    "https://lnkuro.top/",
    "https://noveldex.io/",
    "https://dumahstranslations.wordpress.com/",
    "https://maplesantl.com/",
    "https://karistudio.com/",
    "https://sakuranovel.id/",
    "https://mmxianxia.com/",
    "https://kinkytranslations.com/",
    "https://98novels.com/",
    "https://www.novelfull.in/",
    "https://cyborg-tl.com/",
]

CLEARANCE_COOKIES = ("cf_clearance", "__ddg1_", "__ddg2_", "__ddgid_", "datadome", "_px3")


def verify(url: str, cookies: Dict[str, str], user_agent: str) -> Dict[str, Any]:
    """Replay the clearance on the cheap transport and report whether the site takes it."""
    if not user_agent:
        return {"cleared": False, "why": "no user-agent"}
    transport = ImpersonateTransport("chrome")
    headers = {"User-Agent": user_agent}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        response = transport.send("GET", url, headers=headers, timeout=(15, 45))
    except Exception as exc:  # noqa: BLE001 - a transport failure is a datum, not a crash
        return {"cleared": False, "why": f"{type(exc).__name__}: {exc}"[:90]}
    finally:
        try:
            transport.close()
        except Exception:  # noqa: BLE001
            pass
    verdict = diagnose(
        status=response.status_code,
        headers=response.headers,
        body=response.text[:65536],
        url=url,
        user_agent=user_agent,
    )
    return {
        "cleared": verdict.action is Action.ACCEPT,
        "status": response.status_code,
        "action": verdict.action.value,
        "layer": str(verdict.layer) if verdict.layer else None,
        "bytes": len(response.content or b""),
    }


def one(url: str, headless: bool, timeout: float, extra: List[str]) -> Dict[str, Any]:
    profile = pathlib.Path(tempfile.mkdtemp(prefix="headless-ab-"))
    solver = CdpSolver(headless=headless, args=list(extra))
    started = time.monotonic()
    row: Dict[str, Any] = {"url": url, "headless": headless}
    try:
        result = solver.solve(url, profile_dir=profile, timeout=timeout)
        row["user_agent"] = result.user_agent
        row["clearance_cookie"] = sorted(
            name for name in result.cookies if any(c in name for c in CLEARANCE_COOKIES)
        )
        row.update(verify(url, result.cookies, result.user_agent))
    except Exception as exc:  # noqa: BLE001 - which arms fail is the measurement
        row["cleared"] = False
        row["why"] = f"{type(exc).__name__}: {exc}"[:120]
    finally:
        row["solve_seconds"] = round(time.monotonic() - started, 1)
        try:
            solver.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(profile, ignore_errors=True)
    return row


def report(arms: List[Dict[str, Any]]) -> None:
    for arm in arms:
        rows = arm["rows"]
        by: Dict[Tuple[str, bool], List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by[(r["url"], bool(r["headless"]))].append(r)

        print(f"\n=== {arm['arm']} ===")
        print(f"{arm['note']}\n")
        print(f"{'host':<44} {'headed':>10} {'headless':>10}   verdict")
        print("-" * 92)

        decisive = both = only_headless = controls = 0
        for host in sorted({r["url"] for r in rows}):
            hd = by.get((host, False), [])
            hl = by.get((host, True), [])
            hd_ok = sum(1 for r in hd if r.get("cleared"))
            hl_ok = sum(1 for r in hl if r.get("cleared"))
            if hd_ok and hl_ok:
                verdict, decisive, both = "both clear", decisive + 1, both + 1
            elif hd_ok:
                verdict, decisive = "HEADLESS LOSES", decisive + 1
            elif hl_ok:
                # Checked before the control case: a host only headless clears is
                # evidence, and folding it into the controls understates the result.
                verdict, only_headless = "headless only", only_headless + 1
            else:
                verdict, controls = "control - neither arm clears it", controls + 1
            hd_s, hl_s = f"{hd_ok}/{len(hd)}", f"{hl_ok}/{len(hl)}"
            print(f"{host:<44} {hd_s:>10} {hl_s:>10}   {verdict}")

        print()
        print(f"  hosts the headed arm clears:  {decisive}")
        print(f"  ...headless also clears:      {both}")
        print(f"  cleared only headless:        {only_headless}")
        print(f"  controls (neither):           {controls}")
        if decisive:
            print(f"  VERDICT: headless clears {both} of {decisive}.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="re-read the JSON, no network")
    ap.add_argument("--arm", default="corrected", help="name this run in the JSON")
    ap.add_argument("--note", default="", help="what this arm changes, for the record")
    ap.add_argument("--modes", default="headed,headless")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--hosts", default="", help="comma-separated, overrides the corpus")
    ap.add_argument(
        "--extra-args",
        default="",
        help="comma-separated Chrome flags. Note that --no-sandbox is itself an "
        "automation tell, so using it in one arm and not another confounds the A/B.",
    )
    args = ap.parse_args()

    recorded: Dict[str, Any] = json.loads(OUT.read_text()) if OUT.exists() else {"arms": []}
    if args.report:
        report(recorded["arms"])
        return 0

    hosts: List[str] = [h for h in args.hosts.split(",") if h] or CORPUS
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    extra = [a for a in args.extra_args.split(",") if a]
    rows: List[Dict[str, Any]] = []

    arm: Optional[Dict[str, Any]] = None
    for existing in recorded["arms"]:
        if existing["arm"] == args.arm:
            arm = existing
    if arm is None:
        arm = {"arm": args.arm, "note": args.note, "rows": rows}
        recorded["arms"].append(arm)
    arm["note"] = args.note or arm.get("note", "")
    arm["rows"] = rows

    for run in range(args.repeats):
        for url in hosts:
            for mode in modes:
                row = one(url, mode == "headless", args.timeout, extra)
                row["run"] = run
                rows.append(row)
                mark = "CLEARED" if row.get("cleared") else "no     "
                print(
                    f"[{run}] {mode:<8} {mark} {url:<40} {row['solve_seconds']}s "
                    f"{row.get('why') or row.get('layer') or ''}",
                    flush=True,
                )
                OUT.write_text(json.dumps(recorded, indent=1), encoding="utf-8")

    report([arm])
    return 0


if __name__ == "__main__":
    sys.exit(main())
