"""One retrieval through **scraper 1.0**, reported as JSON on stdout.

The counterpart to `arm_v026.py`; see that file for why the arms report raw facts and
let the driver classify. Usage:

    python livetest/arm_v1.py <url> [single|ladder]

`single` caps the run at one request with no tier above `direct`, which is the only
setting that compares like with like: 0.2.6 has no escalation ladder, so letting 1.0
retry and fall back to the archive would be measuring a feature 0.2.6 cannot have
rather than the quality of a request.

`ladder` is the opposite arm on purpose — 1.0 with its real defaults, run only on the
hosts `single` could not retrieve, to measure what the ladder is worth on top.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any, Dict

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

PEEK = 20_000
KEEP = ("server", "cf-mitigated", "cf-ray", "www-authenticate", "retry-after", "content-type")


def main() -> None:
    url = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "single"
    out: Dict[str, Any] = {"arm": f"v1-{mode}", "url": url, "ok": False}
    started = time.monotonic()
    try:
        from scraper import PacingPolicy, Scraper, ScraperConfig
        from scraper.exceptions import Blocked

        settings: Dict[str, Any] = {
            "raise_for_status": False,
            "remember": False,
            "guard_topic": False,
            "timeout": (15, 45),
            "data_dir": pathlib.Path(__file__).parent / "state" / "compare",
            # Warm-up off in both modes: it spends an extra request on the origin,
            # and 0.2.6 has no equivalent, so leaving it on would make the request
            # count differ between arms.
            "pacing": PacingPolicy(interval=0.1, warmup=False, pause_chance=0.0),
        }
        if mode == "single":
            settings.update(max_attempts=1, archive=False)
        elif mode == "noarchive":
            # 1.0's real defaults minus the archive: retries and escalation on, but
            # nothing that answers with a stale copy instead of the live site. The bar
            # this has to clear is 0.2.6, without borrowing the one tier 0.2.6 lacks.
            settings.update(max_attempts=3, archive=False)
        else:
            settings.update(max_attempts=3, archive=True)

        with Scraper(config=ScraperConfig(**settings)) as client:
            try:
                response = client.get(url)
                body, status, headers = response.text, response.status_code, response.headers
                out.update(tier=client.knows(url).tier or "direct")
            except Blocked as exc:
                # A stop is a result, not an error: it carries the layer the model
                # blames and, in `ladder` mode, the trail of what was tried.
                out.update(
                    ok=True,
                    status=0,
                    bytes=0,
                    peek="",
                    headers={},
                    stopped=f"{exc.layer}" if exc.layer else "unattributed",
                    detail=str(exc.detail)[:400],
                )
                out["elapsed"] = round(time.monotonic() - started, 2)
                print(json.dumps(out))
                return
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
