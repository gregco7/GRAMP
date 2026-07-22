"""Live end-to-end check of the redesigned deep_search on the two problem queries.
Writes a readable before/after-style report to outputs/search_redesign/."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import search2  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "outputs" / "search_redesign"
OUT.mkdir(parents=True, exist_ok=True)

QUERIES = ["aspas ace", "TenZ highlights"]
OPTS = {"vision_count": 6, "limit": 12, "per_source": 18, "sources": ["youtube", "twitch"]}


def line(e):
    tag = f"[{e.get('source','?')[:2]}]"
    dur = f"{int(e.get('duration') or 0)}s"
    m = e.get("media") or {}
    cps = f"cps{m.get('cuts_per_sec')}" if m.get("analyzed") else ""
    return f"  {e.get('score'):+.3f} {tag} {dur:>4} {cps:>7}  {(e.get('title') or '')[:70]}"


report = []
for q in QUERIES:
    print(f"running: {q} …", flush=True)
    out = search2.deep_search(q, opts=OPTS, progress=lambda p, m: print(f"   {p}% {m}", flush=True))
    meta = out["meta"]
    report.append(f"\n{'='*78}\nQUERY: {q}")
    report.append(f"intent.subjects: {meta.get('subjects')}   gated: {meta.get('gated')}")
    report.append(f"pool={meta.get('pool')} ai_parse={meta.get('ai_parse')} "
                  f"vision_verified={meta.get('vision_verified')}")
    report.append("results (best first):")
    for e in out["results"]:
        report.append(line(e))
    (OUT / f"{q.replace(' ', '_')}.json").write_text(json.dumps(out, indent=2, default=str))

text = "\n".join(report)
print(text)
(OUT / "report.txt").write_text(text)
print(f"\nwrote {OUT}/report.txt")
