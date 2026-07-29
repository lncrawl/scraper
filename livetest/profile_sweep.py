"""Which impersonation profile should be the default? Measured, not assumed.

`ScraperConfig.impersonate` defaults to `"chrome"` because Chrome is the most common
browser, which is a reason to *expect* it to be unremarkable rather than evidence that
it is. The comparison against 0.2.6 turned up nine hosts that release retrieved and 1.0
did not, and on those nine a Firefox profile got seven. That is a biased sample — they
were selected for Chrome failing — so it says nothing about the population, which is
what this file measures.

One request per host per profile, on the same transport class, with only the profile
differing. The corpus excludes hosts lncrawl has rejected as dead.

    uv run python livetest/profile_sweep.py [--hosts N] [profile ...]

Writes livetest/profile_sweep.json.
"""

from __future__ import annotations

import json
import pathlib
import random
import sys
import time
from typing import Any, Dict, List

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parents[0] / "src"))

from compare import stub_reason  # noqa: E402

from scraper.diagnosis import diagnose  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402

PROFILES = ("chrome", "firefox", "safari", "edge")
SEED = 20260729
GAP = 1.2


def grade(response: Any, url: str) -> Dict[str, Any]:
    body = response.text or ""
    found = diagnose(
        status=response.status_code, headers=dict(response.headers), body=body[:20_000], url=url
    )
    stub = ""
    if response.status_code == 200 and found.ok:
        stub = stub_reason(body[:20_000], len(response.content))
    content = response.status_code == 200 and found.ok and not stub
    return {
        "status": response.status_code,
        "bytes": len(response.content),
        "layer": int(found.layer) if found.layer else None,
        "verdict": "content" if content else (stub and "stub" or found.action.value),
        "content": content,
    }


def corpus(limit: int) -> List[str]:
    """A seeded sample of the live target list — not the hard hosts only.

    Choosing a default from the subset where the current default fails would pick
    whichever profile is best at exactly the cases the incumbent loses, which is how
    a regression-to-the-mean artifact becomes a config change.
    """
    rows = json.loads((HERE / "targets.json").read_text())
    urls = [str(r["url"]) for r in rows]
    random.Random(SEED).shuffle(urls)
    return urls[:limit]


def main() -> None:
    argv = sys.argv[1:]
    limit = 150
    if "--hosts" in argv:
        at = argv.index("--hosts")
        limit = int(argv[at + 1])
        argv = argv[:at] + argv[at + 2 :]
    profiles = tuple(argv) or PROFILES

    hosts = corpus(limit)
    transports = {name: ImpersonateTransport(name) for name in profiles}
    print(f"{len(hosts)} hosts x {len(profiles)} profiles: {', '.join(profiles)}\n")

    rows: List[Dict[str, Any]] = []
    rng = random.Random(SEED)
    try:
        for index, url in enumerate(hosts, 1):
            record: Dict[str, Any] = {"url": url, "profiles": {}}
            order = list(profiles)
            rng.shuffle(order)
            for name in order:
                try:
                    response = transports[name].send("GET", url, timeout=25)
                    record["profiles"][name] = grade(response, url)
                except Exception as exc:  # noqa: BLE001 - a failure is data
                    record["profiles"][name] = {
                        "verdict": "error",
                        "content": False,
                        "error": type(exc).__name__,
                    }
                time.sleep(GAP)
            rows.append(record)
            marks = "".join("+" if record["profiles"][p]["content"] else "." for p in profiles)
            print(f"{index:>4}/{len(hosts)}  {marks}  {url}", flush=True)
    finally:
        for transport in transports.values():
            transport.close()

    out = {"profiles": list(profiles), "seed": SEED, "hosts": rows}
    (HERE / "profile_sweep.json").write_text(json.dumps(out, indent=1))
    summarise(out)


def summarise(out: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = out["hosts"]
    profiles: List[str] = out["profiles"]
    print(f"\n{'profile':<12}{'content':>12}{'rate':>8}")
    for name in profiles:
        wins = sum(1 for r in rows if r["profiles"][name]["content"])
        print(f"{name:<12}{wins:>7}/{len(rows):<4}{wins / len(rows):>7.1%}")

    print("\nhead to head against chrome (the current default)")
    for name in profiles:
        if name == "chrome":
            continue
        won = [
            r
            for r in rows
            if r["profiles"][name]["content"] and not r["profiles"]["chrome"]["content"]
        ]
        lost = [
            r
            for r in rows
            if r["profiles"]["chrome"]["content"] and not r["profiles"][name]["content"]
        ]
        print(f"  {name:<10} +{len(won):<3} -{len(lost):<3} net {len(won) - len(lost):+d}")
        for row in won[:6]:
            print(f"      + {row['url']}")
        for row in lost[:6]:
            print(f"      - {row['url']}")


if __name__ == "__main__":
    main()
