"""Classify every lncrawl source host by what actually defends it today.

Two clients hit each host: a plain `requests` session and the new impersonating
transport. The pair is the measurement — the difference between them *is* the
transport-layer group, and running only one of them would not show it.

Writes livetest/probe.json.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import pathlib
import sys
import time
from typing import Any, Dict, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402
import urllib3  # noqa: E402

from scraper.diagnosis import diagnose, edge  # noqa: E402
from scraper.pacing import Trail  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402

urllib3.disable_warnings()

HERE = pathlib.Path(__file__).parent
TIMEOUT = 20.0
WORKERS = 24
PEEK = 48 * 1024


def classify(headers: Dict[str, str], body: str = "") -> str:
    """What is in front of this host, as the library itself reads it.

    This used to be the probe's own small copy of four signatures, which is how a
    harness ends up measuring its own opinion instead of the library's. `edge()` knows
    the whole vendor set, so the tally in the report is now the same classification the
    pipeline acts on.
    """
    return edge(headers, body) or "unknown"


def one(client: Any, url: str, *, plain: bool) -> Dict[str, Any]:
    started = time.monotonic()
    # The same first-contact headers a `Scraper` sends. Without them this probe
    # measures a bare transport, and its layer labels drift pessimistic against what
    # the library actually does — which is worse than useless, because scenarios pick
    # their targets from here. Three scenarios chose hosts as "challenged" that the
    # real pipeline retrieves, and failed for it.
    nav = Trail().headers(url)
    try:
        if plain:
            response = client.request(
                "GET", url, timeout=TIMEOUT, allow_redirects=True, verify=False, headers=nav
            )
        else:
            response = client.send("GET", url, timeout=TIMEOUT, verify=False, headers=nav)
        body = (response.content or b"")[:PEEK].decode(response.encoding or "utf-8", "ignore")
        headers = dict(response.headers)
        verdict = diagnose(
            status=response.status_code,
            headers=headers,
            body=body,
            url=url,
            user_agent=str((response.request.headers or {}).get("user-agent", ""))
            if response.request
            else "",
        )
        return {
            "ok": True,
            "status": response.status_code,
            "edge": classify(headers, body),
            "action": verdict.action.value,
            "layer": int(verdict.layer) if verdict.layer else None,
            "layer_name": str(verdict.layer) if verdict.layer else None,
            "detail": verdict.detail,
            "bytes": len(response.content or b""),
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - a probe records failures, never raises
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
            "seconds": round(time.monotonic() - started, 2),
        }


def probe(entry: Dict[str, Any]) -> Dict[str, Any]:
    url = entry["url"]
    plain_client = requests.Session()
    imp: Optional[ImpersonateTransport] = None
    try:
        imp = ImpersonateTransport(verify=False)
        return {
            **entry,
            "plain": one(plain_client, url, plain=True),
            "impersonate": one(imp, url, plain=False),
        }
    finally:
        plain_client.close()
        if imp is not None:
            imp.close()


def main() -> None:
    targets = json.loads((HERE / "targets.json").read_text())
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(targets)
    targets = targets[:limit]
    results = []
    done = 0
    with futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(probe, targets):
            results.append(result)
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(targets)}", flush=True)
    (HERE / "probe.json").write_text(json.dumps(results, indent=1))

    edges: Dict[str, int] = {}
    for row in results:
        imp = row.get("impersonate") or {}
        key = imp.get("edge", "unreachable") if imp.get("ok") else "unreachable"
        edges[key] = edges.get(key, 0) + 1
    print("\nedge distribution (impersonated client):")
    for name, count in sorted(edges.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4}  {name}")


if __name__ == "__main__":
    main()
