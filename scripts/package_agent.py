#!/usr/bin/env python
"""
Clip Package Agent — CLI.

The engine behind the app's "clip packages" feature. Given a prompt it searches
YouTube + Twitch (via backend.clips), curates a set of short, crop-ready highlight
candidates, and writes a package the app's Packages panel can show. It reads the
agent's evolving prompt + learnings (storage/packages/_agent.json), so critiques
you leave on past packages steer future builds.

Two ways to run it:

  # offline heuristic build (no LLM) — what the app itself runs in the background
  .venv/bin/python scripts/package_agent.py build --prompt "TenZ clips" --count 20

  # inspect the agent's current brain (prompt + accumulated learnings)
  .venv/bin/python scripts/package_agent.py state

A Claude sub-agent (see .claude/agents/clip-package-agent.md) can call `build` to
seed candidates, then refine reasons / drop weak picks and re-write the package
with `write` from a JSON file for a hand-curated delivery.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import packages   # noqa: E402


def cmd_state(_args):
    st = packages.agent_state()
    print(f"Agent: {st['name']}")
    print(f"Default count: {st['default_count']}")
    print(f"Sources: {', '.join(st.get('sources', []))}")
    print("\nPrompt:\n" + st["prompt"])
    learn = st.get("learnings", [])
    print(f"\nLearnings ({len(learn)}):")
    for l in learn:
        print(f"  • {l['text']}")


def cmd_build(args):
    prompt = args.prompt or "TenZ clips"
    count = args.count or packages.agent_state()["default_count"]
    print(f"Building package for: {prompt!r}  (count={count})", file=sys.stderr)
    found = packages.build_heuristic(prompt, count)
    pkg = packages.create_package(
        prompt, found, count=count,
        source="agent" if args.agent else "heuristic",
        status="ready",
        note=f"{len(found)} candidates curated across YouTube + Twitch.")
    print(f"Wrote package {pkg['id']} — {pkg['title']} ({len(pkg['clips'])} clips)")
    for c in pkg["clips"]:
        d = f"{int(c['duration'])}s" if c.get("duration") else "?"
        print(f"  [{c['source'][:2]}] {d:>4}  {c['title'][:70]}  — {c['reason']}")
    return pkg


def cmd_write(args):
    """Write a hand-curated package from a JSON file: {prompt, title, note, clips:[...]}."""
    data = json.loads(Path(args.file).read_text())
    pkg = packages.create_package(
        data["prompt"], data["clips"], count=data.get("count"),
        title=data.get("title"), source="agent", status="ready",
        note=data.get("note", ""), pid=data.get("id"))
    print(f"Wrote package {pkg['id']} — {pkg['title']} ({len(pkg['clips'])} clips)")


def main():
    ap = argparse.ArgumentParser(description="Clip Package Agent CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("state", help="show the agent's prompt + learnings")
    s.set_defaults(func=cmd_state)

    b = sub.add_parser("build", help="build a package for a prompt (heuristic search)")
    b.add_argument("--prompt", default="TenZ clips")
    b.add_argument("--count", type=int, default=None)
    b.add_argument("--agent", action="store_true", help="tag source as agent-curated")
    b.set_defaults(func=cmd_build)

    w = sub.add_parser("write", help="write a hand-curated package from a JSON file")
    w.add_argument("file")
    w.set_defaults(func=cmd_write)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
