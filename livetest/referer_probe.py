"""How many hosts are lost by not sending a `Referer` on a first navigation?

Written to size one finding from the 0.2.6 comparison. 1.0 sends `Sec-Fetch-Site:
none` and no `Referer` when it has no trail for an origin, which is exactly what a
browser does on a typed-in navigation. 0.2.6 unconditionally set a *self-referential*
`Referer` — a request to `https://x/` carrying `Referer: https://x/` — which no browser
emits. On at least one host the technically-wrong header is the one that gets the page,
so the question is whether that host is an outlier or a population.

Two requests per host, same transport, same profile, differing only in that header, so
the header is the only thing the difference can be attributed to.

    uv run python livetest/referer_probe.py [limit]

Writes livetest/referer_probe.json.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any, Dict, List

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parents[0] / "src"))

from scraper.diagnosis import diagnose  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402
from scraper.utils.url_tools import extract_base  # noqa: E402


def verdict(response: Any, url: str) -> Dict[str, Any]:
    found = diagnose(
        status=response.status_code,
        headers=dict(response.headers),
        body=response.text[:20_000],
        url=url,
    )
    return {
        "status": response.status_code,
        "bytes": len(response.content),
        "action": found.action.value,
        "layer": int(found.layer) if found.layer else None,
        "content": response.status_code == 200 and found.ok,
    }


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rows = json.loads((HERE / "probe.json").read_text())
    # Only hosts that answered at all, and only the ones where the impersonated
    # client did *not* already get content — a host that works without a Referer
    # cannot be rescued by one, so asking it again is a wasted request.
    hosts = [
        r["url"]
        for r in rows
        if (r.get("impersonate") or {}).get("ok")
        and (r["impersonate"].get("action") or "accept") != "accept"
    ][:limit]

    out: List[Dict[str, Any]] = []
    transport = ImpersonateTransport()
    try:
        for index, url in enumerate(hosts, 1):
            record: Dict[str, Any] = {"url": url}
            for label, headers in (
                ("without", None),
                ("with", {"Referer": extract_base(url) + "/"}),
            ):
                try:
                    response = transport.send("GET", url, headers=headers, timeout=30)
                    record[label] = verdict(response, url)
                except Exception as exc:  # noqa: BLE001 - a failure is data
                    record[label] = {"error": f"{type(exc).__name__}"}
                time.sleep(1.5)
            rescued = bool(not record["without"].get("content") and record["with"].get("content"))
            lost = bool(record["without"].get("content") and not record["with"].get("content"))
            record["rescued"] = rescued
            record["lost"] = lost
            out.append(record)
            mark = "RESCUED" if rescued else ("LOST" if lost else "")
            print(
                f"{index:>4}/{len(hosts)}  "
                f"{record['without'].get('status', '-'):>4} -> "
                f"{record['with'].get('status', '-'):<4} {mark:<8} {url}",
                flush=True,
            )
    finally:
        transport.close()

    (HERE / "referer_probe.json").write_text(json.dumps(out, indent=1))
    rescued = [r for r in out if r["rescued"]]
    lost = [r for r in out if r["lost"]]
    print(f"\nof {len(out)} hosts that refused an impersonated client with no Referer:")
    print(f"  rescued by a self-referential Referer : {len(rescued)}")
    print(f"  broken by one                         : {len(lost)}")
    for row in rescued:
        print(f"    + {row['url']}")
    for row in lost:
        print(f"    - {row['url']}")


if __name__ == "__main__":
    main()
