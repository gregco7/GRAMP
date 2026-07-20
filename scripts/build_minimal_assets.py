"""Build assets/minimal/ — a ~1/4-size subset of orgs, plrs, and weapons for
plugging into the designer for mock-ups. The full folders stay the source of truth;
re-run this after re-scraping to refresh the subset.

  .venv/bin/python scripts/build_minimal_assets.py
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
MIN = ASSETS / "minimal"

# Well-known orgs to prefer for mock-ups (vlr slugs); topped up alphabetically to 12.
PREFERRED_ORGS = [
    "sentinels", "100-thieves", "g2-esports", "nrg", "cloud9",
    "fnatic", "team-liquid", "team-heretics",
    "paper-rex", "t1", "gen-g", "kiwoom-drx",
    "edward-gaming",
]

# Iconic skin collections to always include when a weapon has them.
PREFERRED_SKINS = [
    "reaver", "prime", "elderflame", "oni", "glitchpop", "araxys", "kuronami",
    "champions", "ion", "singularity", "gaia", "sentinels-of-light", "rgx",
]


def fresh_dir(p: Path) -> Path:
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
    return p


def pick_quarter(names: list[str], preferred_prefixes: list[str]) -> list[str]:
    target = max(1, math.ceil(len(names) / 4))
    chosen = [n for n in names if any(n.startswith(p) for p in preferred_prefixes)]
    chosen = chosen[:target]
    for i, n in enumerate(names):
        if len(chosen) >= target:
            break
        if n not in chosen and i % 4 == 0:
            chosen.append(n)
    return sorted(chosen[:target])


def main() -> None:
    fresh_dir(MIN)

    # --- orgs: 12 of 48 ---
    orgs_index = json.loads((ASSETS / "orgs" / "index.json").read_text())
    have = [s for s in PREFERRED_ORGS if orgs_index.get(s, {}).get("file")]
    for slug in sorted(orgs_index):
        if len(have) >= 12:
            break
        if slug not in have and orgs_index[slug].get("file"):
            have.append(slug)
    have = have[:12]
    dest = fresh_dir(MIN / "orgs")
    for slug in have:
        shutil.copy2(ASSETS / "orgs" / orgs_index[slug]["file"], dest)
    (dest / "index.json").write_text(
        json.dumps({s: orgs_index[s] for s in have}, indent=2, ensure_ascii=False))
    print(f"orgs: {len(have)} / {len(orgs_index)}")

    # --- plrs: rosters of the chosen orgs (~1/4 of the pool) ---
    plrs_index = json.loads((ASSETS / "plrs" / "index.json").read_text())
    chosen = {k: v for k, v in plrs_index.items()
              if v["team_slug"] in have and v.get("file")}
    dest = fresh_dir(MIN / "plrs")
    for v in chosen.values():
        shutil.copy2(ASSETS / "plrs" / v["file"], dest)
    (dest / "index.json").write_text(json.dumps(chosen, indent=2, ensure_ascii=False))
    print(f"plrs: {len(chosen)} / {len(plrs_index)}")

    # --- weapons: ~1/4 of each weapon folder, iconic collections first ---
    weapons_index = json.loads((ASSETS / "weapons" / "index.json").read_text())
    mini_windex = {}
    for weapon, skins in weapons_index.items():
        picks = pick_quarter(skins, PREFERRED_SKINS)
        wdir = fresh_dir(MIN / "weapons" / weapon)
        for stem in picks:
            src = next((ASSETS / "weapons" / weapon).glob(f"{stem}.*"))
            shutil.copy2(src, wdir)
        mini_windex[weapon] = picks
        print(f"weapons/{weapon}: {len(picks)} / {len(skins)}")
    (MIN / "weapons" / "index.json").write_text(json.dumps(mini_windex, indent=2))

    total = sum(1 for p in MIN.rglob("*") if p.is_file() and p.suffix != ".json")
    print(f"\nminimal/: {total} images")


if __name__ == "__main__":
    main()
