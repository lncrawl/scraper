"""Read `compare.json` and answer whether 1.0 is actually better. No network.

The questions this has to answer honestly, including where the answer is unflattering:

1. Content rate per arm, split by whether the host was hard or easy.
2. Head-to-head. A rate difference hides its own composition: 1.0 can win ten hosts
   and lose four and still look like "+6". The lost ones are the interesting ones.
3. Whether arm order mattered. Arms hit a host back to back, so a rate-limited third
   request would show up as a fake loss for whichever arm ran last.
4. What the escalation ladder adds on top, measured only where nothing else worked.

    uv run python livetest/compare_analyze.py
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter
from typing import Any, Dict, List

HERE = pathlib.Path(__file__).parent

BASE = "v026-impersonate"
"""The arm 1.0 has to beat to justify itself.

Not `v026-default`: 0.2.6 shipped impersonation as an extra and simply left it off, so
measuring against the default would credit the rewrite for a one-line config change.
"""


def rate(rows: List[Dict[str, Any]], arm: str, group: str = "") -> str:
    got = [r for r in rows if arm in r["arms"] and (not group or r["group"] == group)]
    if not got:
        return "     -"
    wins = sum(1 for r in got if r["arms"][arm]["content"])
    return f"{wins:>3}/{len(got):<3} {wins / len(got):>5.1%}"


def main() -> None:
    data = json.loads((HERE / "compare.json").read_text())
    rows: List[Dict[str, Any]] = data["hosts"]
    arms: List[str] = data["arms"]
    print(f"corpus: {len(rows)} hosts | curl_cffi {data['curl_cffi']} in every arm\n")

    print(f"{'arm':<20}{'overall':>14}{'hard':>14}{'easy':>14}{'median s':>10}")
    for arm in arms:
        got = [r["arms"][arm] for r in rows if arm in r["arms"]]
        times = sorted(g.get("elapsed", 0) for g in got)
        med = times[len(times) // 2] if times else 0
        print(
            f"{arm:<20}{rate(rows, arm):>14}{rate(rows, arm, 'hard'):>14}"
            f"{rate(rows, arm, 'easy'):>14}{med:>10.2f}"
        )

    # -- head to head ---------------------------------------------------------------
    both = [r for r in rows if BASE in r["arms"] and "v1-single" in r["arms"]]
    won = [r for r in both if r["arms"]["v1-single"]["content"] and not r["arms"][BASE]["content"]]
    lost = [r for r in both if r["arms"][BASE]["content"] and not r["arms"]["v1-single"]["content"]]
    tie_ok = [r for r in both if r["arms"][BASE]["content"] and r["arms"]["v1-single"]["content"]]
    tie_no = [
        r for r in both if not r["arms"][BASE]["content"] and not r["arms"]["v1-single"]["content"]
    ]
    print(f"\nv1-single vs {BASE}, host by host ({len(both)} hosts)")
    print(f"  both got content   : {len(tie_ok)}")
    print(f"  1.0 won            : {len(won)}")
    print(f"  1.0 LOST           : {len(lost)}")
    print(f"  neither            : {len(tie_no)}")

    for label, group in (("1.0 won", won), ("1.0 LOST", lost)):
        if not group:
            continue
        print(f"\n  {label}:")
        for r in group:
            mine = r["arms"]["v1-single"]
            theirs = r["arms"][BASE]
            print(
                f"    {r['url']:<45} "
                f"v1={mine['verdict']}/{mine.get('status')} "
                f"026={theirs['verdict']}/{theirs.get('status')}"
            )
            if mine.get("detail"):
                print(f"        v1 says: {str(mine['detail'])[:120]}")

    # -- did order matter? ----------------------------------------------------------
    print("\ncontent rate by the position an arm ran in (a check, not a result)")
    for arm in arms:
        if arm == "v1-ladder":
            continue
        by_pos: Dict[int, List[bool]] = {}
        for r in rows:
            got = r["arms"].get(arm)
            if got and "position" in got:
                by_pos.setdefault(got["position"], []).append(got["content"])
        parts = [f"p{pos}: {sum(v) / len(v):.0%} (n={len(v)})" for pos, v in sorted(by_pos.items())]
        print(f"  {arm:<20} {'   '.join(parts)}")

    # -- what the ladder adds -------------------------------------------------------
    if "v1-ladder" in arms:
        laddered = [r for r in rows if "v1-ladder" in r["arms"]]
        gained = [r for r in laddered if r["arms"]["v1-ladder"]["content"]]
        print(f"\nladder ran on {len(laddered)} hosts the capped arm could not retrieve")
        print(f"  retrieved anyway : {len(gained)}")
        tiers = Counter(r["arms"]["v1-ladder"].get("tier", "-") for r in gained)
        for tier, count in tiers.most_common():
            print(f"    via {tier:<10} {count}")
        rescued_over_026 = [r for r in gained if not r["arms"][BASE]["content"]]
        print(f"  of those, {len(rescued_over_026)} were also refused by {BASE}")
        times = sorted(r["arms"]["v1-ladder"].get("elapsed", 0) for r in laddered)
        if times:
            print(f"  median seconds   : {times[len(times) // 2]:.1f}")

    # -- what each arm ran into -----------------------------------------------------
    print("\nverdict mix per arm")
    for arm in arms:
        mix = Counter(r["arms"][arm]["verdict"] for r in rows if arm in r["arms"])
        print(f"  {arm:<20} " + "  ".join(f"{k}={v}" for k, v in mix.most_common()))

    stubs = Counter()
    for r in rows:
        for arm in arms:
            got = r["arms"].get(arm)
            if got and got["verdict"] == "stub":
                stubs[r["url"]] += 1
    if stubs:
        print(f"\n{len(stubs)} hosts served a stub instead of content to at least one arm")
        print("  (neither release recognises these — a 200 with a JS redirect and no page)")
        for url, count in stubs.most_common(8):
            print(f"    {url}  ({count} arms)")


if __name__ == "__main__":
    main()
