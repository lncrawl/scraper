"""One request through **scraper 0.2.6**, reported as JSON on stdout.

Run by `compare.py` under 0.2.6's own interpreter — both releases install a package
called `scraper`, so they cannot share a process. Usage:

    python livetest/arm_v026.py <url> [default|impersonate]

`default` is what a 0.2.6 user got with no configuration. `impersonate` sets the
`impersonate` target that 0.2.6 supported but did not enable, which is the arm that
keeps the comparison honest: without it, any win by 1.0 could just be a flipped flag.

This deliberately reports **raw facts only** — status, selected headers, a body prefix.
Classification happens in the driver, with one classifier for every arm. An arm that
graded its own response would be comparing two different definitions of success.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict

PEEK = 20_000
KEEP = ("server", "cf-mitigated", "cf-ray", "www-authenticate", "retry-after", "content-type")


def main() -> None:
    url = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "default"
    out: Dict[str, Any] = {"arm": f"v026-{mode}", "url": url, "ok": False}
    started = time.monotonic()
    try:
        import scraper

        config = scraper.default_config()
        if mode == "impersonate":
            config.impersonate = "chrome"
        client = scraper.Scraper(config=config)
        try:
            # 0.2.6's Scraper calls raise_for_status() inside request(), so a block
            # arrives as an exception carrying the response rather than as a value.
            response = client.get(url, timeout=(15, 45))
            body, status, headers = response.text, response.status_code, response.headers
        except Exception as exc:
            carried = getattr(exc, "response", None)
            # `status_code is None` matters: requests attaches a blank Response to
            # some connection errors, so a truthiness check on `carried` alone
            # reported 19 transport failures as successful responses with no status.
            if carried is None or carried.status_code is None:
                raise
            body, status, headers = carried.text, carried.status_code, carried.headers
        finally:
            client.close()
        out.update(
            ok=True,
            status=status,
            bytes=len(body or ""),
            peek=(body or "")[:PEEK],
            headers={k: v for k, v in dict(headers).items() if k.lower() in KEEP},
        )
    except BaseException as exc:  # noqa: BLE001 - a failure is the measurement
        out.update(error=f"{type(exc).__name__}: {exc}"[:300])
    out["elapsed"] = round(time.monotonic() - started, 2)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
