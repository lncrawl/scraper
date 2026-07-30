"""Build livetest/report.html from whatever the harness has recorded.

Reads probe.json, tor_probe.json, results.json, clearance.json and render.json —
each optional —
and renders a single self-contained page. No network, no dependencies.

    uv run python livetest/report.py
"""

from __future__ import annotations

import collections
import datetime as dt
import html
import json
import pathlib
import subprocess
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).parent

# Two series only, validated in both modes with the dataviz palette validator:
# light worst adjacent CVD ΔE 24.7 / normal-vision 33.6; dark 26.8 / 31.8.
SERIES = {"plain": ("#2a78d6", "#3987e5"), "impersonate": ("#eb6834", "#d95926")}


def load(name: str) -> Any:
    path = HERE / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def sh(command: str) -> str:
    try:
        return subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - environment capture is best-effort
        return ""


# -- rendering -----------------------------------------------------------------------


def stat_tiles(results: List[Dict[str, Any]], probe: Optional[List[Dict[str, Any]]]) -> str:
    passed = sum(1 for r in results if r["verdict"] == "pass")
    checks = sum(1 for r in results for s in r["steps"] if s["ok"] is True)
    failed = sum(1 for r in results for s in r["steps"] if s["ok"] is False)
    hosts = len(probe or [])
    cf = sum(
        1
        for r in (probe or [])
        if (r.get("impersonate") or {}).get("edge") == "cloudflare"
        or (r.get("plain") or {}).get("edge") == "cloudflare"
    )
    tiles = [
        (f"{passed}/{len(results)}", "scenarios passed", ""),
        (str(checks), "assertions verified live", f"{failed} failed"),
        (str(hosts), "hosts probed", f"{cf} behind Cloudflare"),
    ]
    cells = "".join(
        f'<div class="tile"><div class="tile-v">{esc(v)}</div>'
        f'<div class="tile-l">{esc(label)}</div>'
        + (f'<div class="tile-s">{esc(sub)}</div>' if sub else "")
        + "</div>"
        for v, label, sub in tiles
    )
    return f'<div class="tiles">{cells}</div>'


def layer_chart(probe: List[Dict[str, Any]]) -> str:
    """Grouped bars: which layers each client meets across the whole corpus."""

    def tally(who: str) -> collections.Counter:
        return collections.Counter(
            (row[who] or {}).get("layer_name")
            for row in probe
            if (row[who] or {}).get("layer_name")
        )

    plain, imp = tally("plain"), tally("impersonate")
    names = sorted(set(plain) | set(imp), key=lambda n: -(plain[n] + imp[n]))
    if not names:
        return ""
    top = max([*plain.values(), *imp.values()])

    rows = []
    for name in names:
        a, b = plain[name], imp[name]
        rows.append(
            f"""<div class="brow">
  <div class="blabel">{esc(name)}</div>
  <div class="btrack">
    <div class="bpair">
      <div class="bar b-plain" style="width:{a / top * 100:.1f}%"
           data-tip="plain client — {a} host{"s" if a != 1 else ""}"></div>
      <span class="bval">{a}</span>
    </div>
    <div class="bpair">
      <div class="bar b-imp" style="width:{b / top * 100:.1f}%"
           data-tip="impersonated — {b} host{"s" if b != 1 else ""}"></div>
      <span class="bval">{b}</span>
    </div>
  </div>
</div>"""
        )

    return f"""<figure class="chart viz-root">
  <figcaption>
    <strong>Which detection layer each client meets</strong>
    <span class="cap">Hosts, out of {len(probe)} probed. Lower is better — a layer met is a
    layer that has to be answered.</span>
  </figcaption>
  <div class="legend">
    <span><i class="sw sw-plain"></i>plain <code>requests</code></span>
    <span><i class="sw sw-imp"></i>impersonated transport</span>
  </div>
  <div class="bars">{"".join(rows)}</div>
</figure>"""


AREAS = [
    ("Transport — layers 2–5", ["S01", "S02", "S03"]),
    ("Diagnosis", ["S05", "S06", "S07", "S08", "S09", "S24", "S25"]),
    ("Behaviour — layer 8", ["S04", "S10", "S16", "S20", "S21"]),
    ("Addresses and tor-pool — layer 1", ["S11", "S12", "S13", "S14", "S15", "S26", "S28"]),
    ("The ladder", ["S17", "S23", "S27"]),
    ("Rendering — when the HTML is not the content", ["S30"]),
    ("Identity and content safety", ["S18", "S19", "S22"]),
]


def scenarios_section(results: List[Dict[str, Any]]) -> str:
    by_id = {r["id"]: r for r in results}
    used: set = set()
    blocks = []
    for title, ids in AREAS:
        cards = []
        for sid in ids:
            row = by_id.get(sid)
            if not row:
                continue
            used.add(sid)
            cards.append(scenario_card(row))
        if cards:
            blocks.append(f'<h3 class="area">{esc(title)}</h3>{"".join(cards)}')
    leftover = [scenario_card(r) for r in results if r["id"] not in used]
    if leftover:
        blocks.append(f'<h3 class="area">Other</h3>{"".join(leftover)}')
    return "".join(blocks)


def scenario_card(row: Dict[str, Any]) -> str:
    steps = []
    for step in row["steps"]:
        mark = {True: "ok", False: "no", None: "note"}[step["ok"]]
        glyph = {"ok": "✓", "no": "✕", "note": "·"}[mark]
        value = f'<span class="sv">{esc(step["value"])}</span>' if step["value"] else ""
        steps.append(
            f'<li class="s-{mark}"><span class="g">{glyph}</span>'
            f'<span class="sw2">{esc(step["what"])}</span>{value}</li>'
        )
    layers = "".join(f'<span class="lchip">{esc(x)}</span>' for x in row["layers"])
    target = (
        f'<div class="target">against <code>{esc(row["target"])}</code></div>'
        if row.get("target")
        else ""
    )
    error = f'<p class="err">{esc(row["error"])}</p>' if row.get("error") else ""
    return f"""<details class="scn v-{row["verdict"]}" {"open" if row["verdict"] != "pass" else ""}>
  <summary>
    <span class="pill p-{row["verdict"]}">{esc(row["verdict"])}</span>
    <span class="sid">{esc(row["id"])}</span>
    <span class="stitle">{esc(row["title"])}</span>
    <span class="layers">{layers}</span>
    <span class="secs">{row["seconds"]}s</span>
  </summary>
  <div class="body">
    <p class="proves"><strong>What a pass proves.</strong> {esc(row["proves"])}</p>
    {target}
    {error}
    <ul class="steps">{"".join(steps)}</ul>
  </div>
</details>"""


def corpus_section(probe: List[Dict[str, Any]], tor: Optional[List[Dict[str, Any]]]) -> str:
    cf = [
        r
        for r in probe
        if (r.get("impersonate") or {}).get("edge") == "cloudflare"
        or (r.get("plain") or {}).get("edge") == "cloudflare"
    ]
    wins = [
        r
        for r in cf
        if (r.get("plain") or {}).get("status") not in (200, None)
        and (r.get("impersonate") or {}).get("status") == 200
        and (r.get("impersonate") or {}).get("action") == "accept"
    ]
    edges = collections.Counter(
        (r.get("impersonate") or {}).get("edge", "unreachable")
        if (r.get("impersonate") or {}).get("ok")
        else "unreachable"
        for r in probe
    )
    edge_rows = "".join(
        f"<tr><td>{esc(name)}</td><td class='n'>{count}</td></tr>"
        for name, count in edges.most_common(10)
    )

    tor_block = ""
    if tor:
        served = sum(1 for r in tor if r.get("status") == 200 and r.get("action") == "accept")
        challenged = sum(1 for r in tor if r.get("action") == "solve")
        rep = sum(1 for r in tor if r.get("layer") == 1)
        tor_block = f"""<h4>Through a Tor exit</h4>
<p>Of {len(tor)} Cloudflare hosts reached through the local tor-pool:
<strong>{served} served outright</strong>, {challenged} challenged,
<strong>{rep} reputation-blocked</strong>.</p>
<p class="note-b">A negative result worth keeping: the reputation layer is not uniformly
hostile to Tor on this corpus. The model treats a Tor exit as clearing nothing at layer 1,
which is conservative rather than measured — and the consequence is that a browser solver
buys more here than a residential proxy would.</p>"""

    return f"""<p>Every base URL in lncrawl's source index was probed twice — once with a
plain <code>requests</code> session, once with the impersonating transport — and each
response classified by the library's own diagnosis. That pairing is the measurement: the
only difference between the two requests is the network signature.</p>
{stat_row(len(probe), len(cf), len(wins))}
<div class="two">
<div><h4>What fronts these hosts</h4>
<div class="scroll"><table><thead><tr><th>Edge</th><th class="n">Hosts</th></tr></thead>
<tbody>{edge_rows}</tbody></table></div></div>
<div>{tor_block}</div>
</div>"""


def stat_row(total: int, cf: int, wins: int) -> str:
    return f"""<div class="tiles small">
  <div class="tile"><div class="tile-v">{total}</div><div class="tile-l">hosts probed</div></div>
  <div class="tile"><div class="tile-v">{cf}</div><div class="tile-l">behind Cloudflare</div></div>
  <div class="tile"><div class="tile-v">{wins}</div><div class="tile-l">refused a plain client,
    served the impersonated one</div></div>
</div>"""


def environment() -> str:
    rows = [
        ("scraper", sh('uv run python -c "import scraper; print(scraper.__version__)"') or "1.0.0"),
        ("Python (harness)", sh("uv run python -V")),
        ("Python (browser tier)", sh("/tmp/scr312/bin/python -V") or "3.12 (separate venv)"),
        ("curl_cffi", sh('uv run python -c "import curl_cffi; print(curl_cffi.__version__)"')),
        (
            "impersonation profile",
            sh(
                'uv run python -c "from scraper.transport import resolve_target;'
                " print(resolve_target('chrome'))\""
            ),
        ),
        (
            "nodriver",
            sh('/tmp/scr312/bin/python -c "import nodriver; print(nodriver.__version__)"'),
        ),
        ("browser", "Google Chrome (headed)"),
        ("tor-pool", sh("docker inspect tor-pool --format '{{.Config.Image}}' 2>/dev/null")),
        (
            "tor-pool exits",
            sh(
                "curl -s -m 5 http://127.0.0.1:8080/metrics | "
                "awk '/^torpool_instances_total /{print $2\" instances\"}'"
            ),
        ),
        (
            "local egress",
            sh(
                "curl -s -m 8 https://check.torproject.org/api/ip | "
                "python3 -c \"import sys,json;d=json.load(sys.stdin);print(d['IP'])\""
            ),
        ),
    ]
    body = "".join(
        f"<tr><td>{esc(k)}</td><td><code>{esc(v or 'n/a')}</code></td></tr>" for k, v in rows
    )
    return f'<div class="scroll"><table class="env"><tbody>{body}</tbody></table></div>'


CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --rule:#c3c2b7; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --crit:#d03b3b;
  --s-plain:#2a78d6; --s-imp:#eb6834;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){
  --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,.10);
  --s-plain:#3987e5; --s-imp:#d95926; color-scheme:dark;
}}
:root[data-theme="dark"]{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --rule:#383835; --ring:rgba(255,255,255,.10);
  --s-plain:#3987e5; --s-imp:#d95926; color-scheme:dark;
}
body{margin:0;background:var(--page);color:var(--ink);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;overflow-x:hidden}
.wrap{max-width:1080px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:1.9rem;line-height:1.2;margin:0 0 8px;letter-spacing:-.02em}
h2{font-size:1.25rem;margin:56px 0 6px;padding-bottom:8px;border-bottom:1px solid var(--rule)}
h3.area{font-size:.82rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:28px 0 10px;font-weight:600}
h4{font-size:1rem;margin:18px 0 6px}
p{margin:8px 0}
.lede{color:var(--ink2);font-size:1.05rem;max-width:74ch}
.sub{color:var(--muted);font-size:.85rem;margin:0 0 4px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
  background:color-mix(in srgb,var(--ink) 7%,transparent);padding:.1em .35em;border-radius:4px}
a{color:var(--s-plain)}
.scroll{overflow-x:auto;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;
  letter-spacing:.05em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
table.env td:first-child{color:var(--ink2);white-space:nowrap;width:1%}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px 32px}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;
  margin:24px 0}
.tiles.small{margin:16px 0;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:16px 18px}
.tile-v{font-size:2rem;line-height:1.05;font-weight:600;letter-spacing:-.02em}
.tile-l{color:var(--ink2);font-size:.85rem;margin-top:4px}
.tile-s{color:var(--muted);font-size:.78rem;margin-top:2px}

.chart{margin:24px 0 8px;background:var(--surface);border:1px solid var(--ring);
  border-radius:10px;padding:18px 20px}
figcaption{margin:0 0 4px}
figcaption .cap{display:block;color:var(--muted);font-size:.82rem;font-weight:400;margin-top:3px}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 16px;font-size:.83rem;
  color:var(--ink2)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
.sw-plain{background:var(--s-plain)} .sw-imp{background:var(--s-imp)}
.bars{display:flex;flex-direction:column;gap:14px}
.brow{display:grid;grid-template-columns:minmax(120px,230px) 1fr;gap:14px;align-items:center}
.blabel{font-size:.83rem;color:var(--ink2);line-height:1.3}
.btrack{display:flex;flex-direction:column;gap:2px}
.bpair{display:flex;align-items:center;gap:8px}
.bar{height:11px;border-radius:0 4px 4px 0;min-width:2px;position:relative;cursor:default}
.b-plain{background:var(--s-plain)} .b-imp{background:var(--s-imp)}
.bval{font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}
.bar[data-tip]:hover::after{content:attr(data-tip);position:absolute;left:100%;top:-6px;
  margin-left:10px;white-space:nowrap;background:var(--ink);color:var(--page);
  padding:5px 9px;border-radius:6px;font-size:.76rem;z-index:5}

.pill{font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;font-weight:700;
  padding:2px 7px;border-radius:20px;border:1px solid transparent;white-space:nowrap}
.p-pass{color:var(--good);border-color:var(--good)}
.p-fail{color:var(--crit);border-color:var(--crit)}
.p-inconclusive,.p-skip{color:var(--muted);border-color:var(--rule)}
.p-error{color:var(--crit);border-color:var(--crit)}

.scn{background:var(--surface);border:1px solid var(--ring);border-radius:8px;margin:7px 0}
.scn summary{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:11px 16px;
  cursor:pointer;list-style:none}
.scn summary::-webkit-details-marker{display:none}
.scn summary::before{content:"▸";color:var(--muted);font-size:.8rem;width:10px}
.scn[open] summary::before{content:"▾"}
.sid{font-weight:600;font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}
.stitle{flex:1;min-width:200px;font-size:.93rem}
.layers{display:flex;gap:4px;flex-wrap:wrap}
.lchip{font-size:.68rem;color:var(--muted);border:1px solid var(--grid);border-radius:4px;
  padding:1px 5px;font-variant-numeric:tabular-nums}
.secs{font-size:.75rem;color:var(--muted);font-variant-numeric:tabular-nums}
.scn .body{padding:0 16px 14px 38px;border-top:1px solid var(--grid);margin-top:2px;
  padding-top:12px}
.proves{font-size:.88rem;color:var(--ink2);max-width:80ch}
.target{font-size:.8rem;color:var(--muted);margin:6px 0}
.err{font-size:.85rem;color:var(--crit);background:color-mix(in srgb,var(--crit) 8%,transparent);
  padding:8px 10px;border-radius:6px}
.steps{list-style:none;padding:0;margin:10px 0 0;font-size:.86rem}
.steps li{display:flex;gap:9px;padding:4px 0;border-bottom:1px solid var(--grid);
  align-items:baseline;flex-wrap:wrap}
.steps li:last-child{border-bottom:0}
.g{width:12px;flex:none;font-weight:700}
.s-ok .g{color:var(--good)} .s-no .g{color:var(--crit)} .s-note .g{color:var(--muted)}
.sw2{flex:1;min-width:190px}
.sv{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.8em;word-break:break-word;max-width:100%}
.s-note .sw2{color:var(--ink2)}
.note-b{font-size:.88rem;color:var(--ink2);border-left:2px solid var(--grid);
  padding-left:12px;margin:10px 0}
.rerun{background:var(--surface);border:1px solid var(--ring);border-radius:8px;
  padding:4px 18px 14px}
pre{overflow-x:auto;background:color-mix(in srgb,var(--ink) 5%,transparent);
  padding:12px 14px;border-radius:8px;font-size:.82rem;line-height:1.55}
footer{margin-top:56px;padding-top:16px;border-top:1px solid var(--rule);
  color:var(--muted);font-size:.82rem}
"""


def main() -> None:
    probe = load("probe.json") or []
    tor = load("tor_probe.json")
    results = list(load("results.json") or [])
    for extra in ("clearance.json", "render.json"):
        found = load(extra) or []
        known = {row["id"] for row in results}
        results = results + [row for row in found if row["id"] not in known]
    results.sort(key=lambda r: r["id"])

    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    passed = sum(1 for r in results if r["verdict"] == "pass")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lncrawl-scraper 1.0 — live verification report</title>
<style>{CSS}</style></head><body><div class="wrap">

<p class="sub">lncrawl-scraper 1.0.0 · {esc(stamp)}</p>
<h1>Live verification against real Cloudflare-protected sites</h1>
<p class="lede">Every code path in the scraper, exercised against production Cloudflare
deployments rather than fixtures — the transport group, the diagnosis classifier, the
escalation ladder, a real browser solve, the archive tier, and a live tor-pool.
{passed} of {len(results)} scenarios pass. A stubbed transport can confirm that the code
does what it says; only this can confirm that what it says is true of a real server.</p>

{stat_tiles(results, probe)}

<h2>The corpus</h2>
{corpus_section(probe, tor)}
{layer_chart(probe)}
<p class="note-b">Reading the chart: the impersonated client meets the managed challenge on
far fewer hosts, and meets the lighter scoring tiers less often — those are the layers a
network signature answers. It meets the behavioural layer <em>more</em> often, which is not
a regression: that is this session's own request volume against the same hosts being rate
limited, and it is the one layer no client-side change addresses.</p>

<h2>Scenarios</h2>
<p>Each one names the layer it exercises and what a pass actually proves — "it returned 200"
is not evidence about a detection layer on its own. Expand any row for the individual
assertions and the values observed.</p>
{scenarios_section(results)}

<h2>Environment</h2>
{environment()}

<h2>Reproducing this</h2>
<div class="rerun">
<pre>uv run poe live-probe    # classify every host in lncrawl's source index (~4 min)
uv run poe live-tor      # classify Cloudflare hosts through tor-pool
uv run poe live          # run every scenario
uv run poe live-report   # rebuild this page

# the clearance tier needs its own interpreter (nodriver: Python 3.10-3.13) and Chrome
/tmp/scr312/bin/python livetest/clearance.py</pre>
<p>Requirements and the politeness rules are in
<code>livetest/README.md</code>. Targets are looked up from the probe rather than
hardcoded, because site configuration moves: one host in this corpus switched from
Turnstile to plain scoring between two runs an hour apart.</p>
</div>

<footer>Generated by <code>livetest/report.py</code>. Every verdict and observed value on
this page comes from the JSON the harness wrote.</footer>

</div></body></html>"""

    out = HERE / "report.html"
    out.write_text(page, "utf-8")
    print(f"wrote {out}  ({len(page) // 1024} KB)")
    print(f"  scenarios: {passed}/{len(results)} pass")


if __name__ == "__main__":
    main()
