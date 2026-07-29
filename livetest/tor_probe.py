"""Which Cloudflare hosts actually refuse a Tor exit?

Needed to pick an honest target for the reputation-layer scenario. Also a result in
its own right: the reputation layer is not uniformly hostile to Tor, and assuming it
is would make the scenario a fiction.

One sticky session for the whole run — a fresh session key per request forces a new
pin every time, which is both rude to the pool and a different thing to measure.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any, Dict, List

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parents[0] / "src"))

import pool  # noqa: E402

from scraper.diagnosis import diagnose  # noqa: E402
from scraper.exits import with_credentials  # noqa: E402
from scraper.transport import ImpersonateTransport  # noqa: E402

SESSION = "livetest-repprobe"


def main() -> None:
    # Before any measuring: without a working credential the pool refuses the SOCKS5
    # handshake, every host then looks like it refuses Tor, and the finding this
    # script exists to produce comes out exactly inverted.
    blocked = pool.ready()
    if blocked:
        print(blocked)
        raise SystemExit(1)
    socks = with_credentials(pool.SOCKS, SESSION, pool.token())
    proxies = {"http": socks, "https": socks}

    rows = json.loads((HERE / "probe.json").read_text())
    hosts = [
        r["url"]
        for r in rows
        if (r.get("impersonate") or {}).get("edge") == "cloudflare"
        and (r.get("impersonate") or {}).get("ok")
    ]
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    hosts = hosts[:limit]

    out: List[Dict[str, Any]] = []
    transport = ImpersonateTransport(verify=False)
    try:
        for url in hosts:
            record: Dict[str, Any] = {"url": url}
            try:
                response = transport.send(
                    "GET",
                    url,
                    proxies=proxies,
                    timeout=45,
                    verify=False,
                )
                verdict = diagnose(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=response.text,
                    url=url,
                )
                record.update(
                    status=response.status_code,
                    action=verdict.action.value,
                    layer=int(verdict.layer) if verdict.layer else None,
                    layer_name=str(verdict.layer) if verdict.layer else None,
                    detail=verdict.detail,
                )
            except Exception as exc:  # noqa: BLE001 - a probe records failures
                record.update(status=None, error=f"{type(exc).__name__}: {str(exc)[:90]}")
            out.append(record)
            flag = ""
            if record.get("layer") == 1:
                flag = "   <-- REPUTATION BLOCK"
            print(
                f"  {str(record.get('status')):>5}  {record.get('layer_name') or record.get('error', '-'):45.45}"
                f"  {url}{flag}",
                flush=True,
            )
            time.sleep(1.2)
    finally:
        transport.close()

    (HERE / "tor_probe.json").write_text(json.dumps(out, indent=1))

    served = [r for r in out if r.get("status") == 200 and r.get("action") == "accept"]
    reputation = [r for r in out if r.get("layer") == 1]
    challenge = [r for r in out if r.get("action") == "solve"]
    print(f"\nthrough a Tor exit, of {len(out)} Cloudflare hosts:")
    print(f"  served outright     : {len(served)}")
    print(f"  challenged          : {len(challenge)}")
    print(f"  reputation-blocked  : {len(reputation)}")
    print(f"  unreachable/other   : {len(out) - len(served) - len(challenge) - len(reputation)}")
    if reputation:
        print("\nreputation-block targets:")
        for r in reputation:
            print(f"  {r['url']}  {r['detail']}")


if __name__ == "__main__":
    main()
