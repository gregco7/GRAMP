"""Build assets/catalog.json — the widget/logo picker metadata the frontend
fetches at /assets/catalog.json.

Sources the **full** packs (the real lists), NOT the minimal mock-up subset:
  - players  <- assets/plrs/       (+ plrs/index.json for real name / team)
  - weapons  <- assets/weapons/<type>/  (+ weapons/index.json for the per-type skin lists)
  - orgs     <- assets/orgs/        (+ orgs/index.json for display name / tag)

Only entries whose image actually exists on disk are included. Re-run after
re-scraping / changing the packs:

  .venv/bin/python scripts/build_catalog.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Acronyms that stay fully upper-cased in generated weapon names (the rest of a
# slug is word-capitalised). Extend if new collections need it.
ACRONYMS = {"rgx": "RGX", "vct": "VCT", "vct25": "VCT25", "mk": "MK"}


def _weapon_name(slug: str) -> str:
    return " ".join(ACRONYMS.get(w, w.capitalize()) for w in slug.split("-"))


def _norm(*parts: str) -> str:
    return " ".join(p for p in (s.strip().lower() for s in parts if s) if p)


def build_players() -> list[dict]:
    idx = json.loads((ASSETS / "plrs" / "index.json").read_text())
    out = []
    for v in idx.values():
        f = v.get("file")
        if not f or not (ASSETS / "plrs" / f).exists():
            continue
        name = v.get("alias") or Path(f).stem
        real, team = v.get("real_name", ""), v.get("team", "")
        sub = f"{real} · {team}".strip(" ·") if (real or team) else ""
        out.append({
            "src": f"/assets/plrs/{f}",
            "name": name,
            "sub": sub,
            "q": _norm(name, Path(f).stem, real, team),
        })
    return sorted(out, key=lambda e: e["name"].lower())


def build_weapons() -> list[dict]:
    idx = json.loads((ASSETS / "weapons" / "index.json").read_text())
    out = []
    for weapon, skins in idx.items():
        for slug in skins:
            if not (ASSETS / "weapons" / weapon / f"{slug}.png").exists():
                continue
            out.append({
                "src": f"/assets/weapons/{weapon}/{slug}.png",
                "name": _weapon_name(slug),
                "sub": weapon.capitalize(),
                "type": weapon,
                "q": f"{slug} {weapon}",
            })
    return out


def build_orgs() -> list[dict]:
    idx = json.loads((ASSETS / "orgs" / "index.json").read_text())
    out = []
    for v in idx.values():
        f = v.get("file")
        if not f or not (ASSETS / "orgs" / f).exists():
            continue
        name, tag = v.get("name") or Path(f).stem, v.get("tag", "")
        out.append({
            "src": f"/assets/orgs/{f}",
            "name": name,
            "tag": tag,
            "q": _norm(name, tag, Path(f).stem),
        })
    return sorted(out, key=lambda e: e["name"].lower())


def main() -> None:
    catalog = {
        "players": build_players(),
        "weapons": build_weapons(),
        "orgs": build_orgs(),
    }
    dst = ASSETS / "catalog.json"
    dst.write_text(json.dumps(catalog, ensure_ascii=False))
    print(f"catalog.json: players={len(catalog['players'])} "
          f"weapons={len(catalog['weapons'])} orgs={len(catalog['orgs'])} -> {dst}")


if __name__ == "__main__":
    main()
