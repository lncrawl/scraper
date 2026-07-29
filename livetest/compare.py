"""Did the 1.0 rewrite actually improve retrieval? A live A/B against 0.2.6.

Three arms per host, because two would not be honest:

    v026-default      0.2.6 as a user got it — `default_config()`, no impersonation
    v026-impersonate  0.2.6 with `config.impersonate = "chrome"`
    v1-single         1.0 capped at one request, no tier above `direct`

The middle arm is the one that stops this being a victory lap. 0.2.6 *supported*
impersonation as an extra and simply did not switch it on, so a comparison against the
default alone would credit 1.0 for a flipped flag. If 1.0 does not beat the middle arm,
the finding is that a 0.2.6 user needed one line of config, not a rewrite.

`v1-single` is capped deliberately. 0.2.6 has no escalation ladder, so leaving 1.0's
retries and archive tier on would measure a feature 0.2.6 cannot have. What the ladder
adds is measured separately, by `--ladder`, on the hosts nothing else could retrieve.

Fairness rules, all of which cost something:

- **One classifier for every arm.** Arms return raw facts; this file labels them with
  1.0's `diagnose()`. An arm that graded itself would compare two definitions of
  success. `diagnose` is a pure function of (status, headers, body) — it reads what the
  server said, which is not a property of the client that asked.
- **Identical `curl_cffi` in both venvs.** Otherwise the measurement includes an
  upstream change. Verified at startup, and the run refuses to start if they differ.
- **Arm order is shuffled per host**, with a gap between arms, and the order is
  recorded so an ordering effect can be tested for afterwards rather than assumed away.
- **Dead hosts are excluded** using the last probe: 131 of the 501 source hosts do not
  resolve, and including them would dilute every rate toward parity.

    uv run python livetest/compare.py [--hosts N] [--ladder]

Writes livetest/compare.json.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parents[0] / "src"))

from scraper.diagnosis import diagnose  # noqa: E402

V026 = pathlib.Path(
    "/private/tmp/claude-501/-Users-dipu-projects-lncrawl/"
    "4e7852cb-709e-43cf-a932-92d28690d752/scratchpad/v026"
)
"""Checkout of the v0.2.6 tag with its own venv. Recreate with:

    git worktree add <path> v0.2.6
    uv venv --python 3.12 <path>/.venv
    uv pip install --python <path>/.venv/bin/python -e "<path>[all]"
    uv pip install --python <path>/.venv/bin/python "curl_cffi==<same as main>"
"""

ARMS: Tuple[Tuple[str, ...], ...] = (
    ("v026-default", "v026", "default"),
    ("v026-impersonate", "v026", "impersonate"),
    ("v1-single", "v1", "single"),
    ("v1-noarchive", "v1", "noarchive"),
)

SEED = 20260729
GAP = 2.5


def venv_python(root: pathlib.Path) -> pathlib.Path:
    return root / ".venv" / "bin" / "python"


def curl_version(python: pathlib.Path) -> str:
    out = subprocess.run(
        [str(python), "-c", "import curl_cffi;print(curl_cffi.__version__)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.stdout.strip()


def corpus(limit: int) -> List[Dict[str, Any]]:
    """Reachable hosts, weighted toward the ones where arms can differ.

    Every host the last probe found presenting something other than plain content is
    included, plus a seeded sample of ordinary ones to catch a regression on the easy
    majority. Sampling only the hard hosts would report a difference that does not
    generalise; sampling uniformly would put almost all of the budget on hosts where
    every arm trivially succeeds.
    """
    rows = json.loads((HERE / "probe.json").read_text())
    # The probe predates lncrawl's rejected list being honoured, and a third of its
    # hosts are on it — expired domains and dead projects. A host that cannot answer
    # is not evidence about a client; it just drags every arm toward the same rate.
    live = {str(r["url"]) for r in json.loads((HERE / "targets.json").read_text())}
    rows = [r for r in rows if str(r["url"]) in live]
    reachable = [r for r in rows if (r.get("impersonate") or {}).get("ok")]
    hard = [r for r in reachable if (r["impersonate"].get("action") or "accept") != "accept"]
    easy = [r for r in reachable if (r["impersonate"].get("action") or "accept") == "accept"]
    random.Random(SEED).shuffle(easy)
    picked = hard + easy[: max(0, limit - len(hard))]
    return [
        {
            "url": r["url"],
            "lang": r.get("lang", ""),
            "was": r["impersonate"].get("layer_name") or r["impersonate"].get("action"),
            "group": "hard" if r in hard else "easy",
        }
        for r in picked[:limit]
    ]


def run_arm(name: str, which: str, mode: str, url: str) -> Dict[str, Any]:
    python = venv_python(V026) if which == "v026" else pathlib.Path(sys.executable)
    script = HERE / (f"arm_{which}.py")
    try:
        out = subprocess.run(
            [str(python), str(script), url, mode],
            capture_output=True,
            text=True,
            timeout=150,
        )
    except subprocess.TimeoutExpired:
        return {"arm": name, "url": url, "ok": False, "error": "arm process timed out"}
    line = ""
    for candidate in reversed(out.stdout.strip().splitlines()):
        if candidate.startswith("{"):
            line = candidate
            break
    if not line:
        return {
            "arm": name,
            "url": url,
            "ok": False,
            "error": f"no JSON from arm (rc={out.returncode}): {out.stderr.strip()[-200:]}",
        }
    return json.loads(line)


_JS_REDIRECT = re.compile(
    r"window\.location\s*(?:\.\s*(?:replace|assign)\s*\(|(?:\.\s*href\s*)?=)", re.IGNORECASE
)
_META_REFRESH = re.compile(r'<meta[^>]+http-equiv=["\']?refresh', re.IGNORECASE)
_TAGS = re.compile(r"<(script|style|noscript)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)

STUB_BYTES = 4_000
TEXT_FLOOR = 400


def stub_reason(peek: str, full: int) -> str:
    """Why this response is not the site's page, or `""` if it looks like one.

    Needed because a status code and Cloudflare's markers do not cover everything
    that is served instead of content. The case that forced this: a host in the
    corpus answers `200` with a 483-byte page whose entire body is
    `window.location.replace(...)` carrying a signed token — a JS bot-check from a
    non-Cloudflare stack. Both releases hand that to the caller as a successful
    retrieval, so scoring on status alone had *both* 0.2.6 arms "winning" a host
    where nobody got the content.

    `full` is the response's real length; `peek` is only its first 20 KB. Every test
    here is gated on `full`, which is not pedantry — gating the rendered-text floor on
    the peek instead called a 109 KB WordPress page a stub, because the first 20 KB of
    it is `<head>` full of meta and inline script. That misgrading favoured whichever
    arm happened to receive a *smaller* page, in a comparison whose entire purpose was
    to be even-handed.
    """
    if full >= STUB_BYTES:
        return ""
    if _JS_REDIRECT.search(peek):
        return "javascript redirect stub"
    if _META_REFRESH.search(peek):
        return "meta-refresh stub"
    text = " ".join(_TAGS.sub(" ", peek).split())
    if len(text) < TEXT_FLOOR:
        return f"only {len(text)} chars of rendered text"
    return ""


def classify(record: Dict[str, Any]) -> Dict[str, Any]:
    """Label an arm's raw result. One classifier, every arm, no exceptions."""
    if not record.get("ok"):
        return {**record, "verdict": "error", "layer": None, "content": False}
    if record.get("stopped"):
        return {**record, "verdict": "stopped", "layer": record["stopped"], "content": False}
    if not record.get("status"):
        # No status is not a response, and `diagnose(status=0)` falls through to
        # ACCEPT — so without this, a connection failure that carried an empty
        # Response object was reported as the site answering fine.
        return {**record, "verdict": "error", "layer": None, "content": False}
    found = diagnose(
        status=int(record.get("status") or 0),
        headers=record.get("headers") or {},
        body=record.get("peek") or "",
        url=record["url"],
    )
    status = int(record.get("status") or 0)
    # "Content" means the site's own page arrived. Three things can make a 200 not
    # that: a challenge interstitial (which `diagnose` knows), a redirect stub, and a
    # page with no rendered text. Scoring on status alone is the single most
    # misleading thing this file could do, and scoring on status plus `diagnose`
    # alone was still wrong — see stub_reason.
    peek = record.get("peek") or ""
    full = int(record.get("bytes") or len(peek))
    stub = stub_reason(peek, full) if status == 200 and found.ok else ""
    content = status == 200 and found.ok and not stub
    if content:
        verdict = "content"
    elif stub:
        verdict = "stub"
    else:
        verdict = found.action.value if found else "other"
    return {
        **record,
        "verdict": verdict,
        "layer": f"{found.layer}" if found.layer else None,
        "detail": stub or found.detail,
        "content": content,
    }


def main() -> None:
    argv = sys.argv[1:]
    limit = 150
    if "--hosts" in argv:
        limit = int(argv[argv.index("--hosts") + 1])
    ladder_only = "--ladder" in argv

    ours = curl_version(pathlib.Path(sys.executable))
    theirs = curl_version(venv_python(V026))
    if not ours or ours != theirs:
        print(f"curl_cffi differs: 1.0 has {ours!r}, 0.2.6 has {theirs!r}")
        print("Pin them to the same version, or the comparison includes an upstream change.")
        raise SystemExit(1)
    print(f"curl_cffi {ours} in both venvs")

    hosts = corpus(limit)
    print(f"{len(hosts)} hosts ({sum(1 for h in hosts if h['group'] == 'hard')} hard)\n")

    rows: List[Dict[str, Any]] = []
    rng = random.Random(SEED)
    for index, host in enumerate(hosts, 1):
        order = list(ARMS)
        rng.shuffle(order)
        record: Dict[str, Any] = {**host, "order": [a[0] for a in order], "arms": {}}
        for position, (name, which, mode) in enumerate(order):
            result = classify(run_arm(name, which, mode, host["url"]))
            result["position"] = position
            record["arms"][name] = result
            time.sleep(GAP)
        rows.append(record)
        marks = "".join("+" if record["arms"][a[0]]["content"] else "." for a in ARMS)
        print(f"{index:>4}/{len(hosts)}  {marks}  {host['url']}", flush=True)

    out: Dict[str, Any] = {
        "curl_cffi": ours,
        "seed": SEED,
        "arms": [a[0] for a in ARMS],
        "hosts": rows,
    }

    if ladder_only:
        # The ladder arm runs only where the capped arm failed. Anywhere it already
        # succeeded, the ladder has nothing to add and the request would be waste.
        stuck = [r for r in rows if not r["arms"]["v1-single"]["content"]]
        print(f"\nladder arm on {len(stuck)} hosts nothing retrieved")
        for index, record in enumerate(stuck, 1):
            result = classify(run_arm("v1-ladder", "v1", "ladder", record["url"]))
            record["arms"]["v1-ladder"] = result
            print(
                f"{index:>4}/{len(stuck)}  {'+' if result['content'] else '.'}  "
                f"{record['url']}  [{result.get('tier', '-')}]",
                flush=True,
            )
            time.sleep(GAP)
        out["arms"].append("v1-ladder")

    (HERE / "compare.json").write_text(json.dumps(out, indent=1))
    summarise(out)


def summarise(out: Dict[str, Any]) -> None:
    rows: List[Dict[str, Any]] = out["hosts"]
    print(f"\n{'arm':<20} {'content':>9} {'rate':>7} {'hard':>10} {'easy':>10} {'median s':>9}")
    for arm in out["arms"]:
        got = [r["arms"][arm] for r in rows if arm in r["arms"]]
        if not got:
            continue
        wins = sum(1 for g in got if g["content"])
        hard = [r for r in rows if arm in r["arms"] and r["group"] == "hard"]
        easy = [r for r in rows if arm in r["arms"] and r["group"] == "easy"]
        hw = sum(1 for r in hard if r["arms"][arm]["content"])
        ew = sum(1 for r in easy if r["arms"][arm]["content"])
        times = sorted(g.get("elapsed", 0) for g in got)
        median = times[len(times) // 2] if times else 0
        print(
            f"{arm:<20} {wins:>4}/{len(got):<4} {wins / len(got):>6.1%} "
            f"{hw:>4}/{len(hard):<5} {ew:>4}/{len(easy):<5} {median:>9.2f}"
        )


if __name__ == "__main__":
    main()
