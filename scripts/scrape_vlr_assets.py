"""Scrape VCT league team logos + player photos from vlr.gg into assets/.

Writes:
  assets/orgs/<team-slug>.<ext>   one logo per franchised team
  assets/plrs/<player-slug>.<ext> one photo per active roster player
  assets/orgs/index.json, assets/plrs/index.json  metadata (name, team, league, vlr id)

Re-run any time to refresh (existing files are overwritten). Uses only stdlib.

  .venv/bin/python scripts/scrape_vlr_assets.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORGS_DIR = ROOT / "assets" / "orgs"
PLRS_DIR = ROOT / "assets" / "plrs"

# VCT 2026 Stage 2 league events on vlr.gg — update these ids each season.
LEAGUE_EVENTS = {
    "americas": 2977,
    "emea": 2976,
    "pacific": 2776,
    "china": 2978,
}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
DELAY = 0.6  # be polite to vlr.gg / owcdn


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch_html(url: str) -> str:
    time.sleep(DELAY)
    return fetch(url).decode("utf-8", errors="replace")


def save_image(cdn_src: str, dest_dir: Path, stem: str) -> str | None:
    """cdn_src is a protocol-relative //owcdn.net/img/... path. Returns filename or None."""
    if not cdn_src or "owcdn.net" not in cdn_src:
        return None  # placeholder silhouette (/img/base/ph/sil.png) → no image
    url = "https:" + cdn_src if cdn_src.startswith("//") else cdn_src
    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    dest = dest_dir / f"{stem}.{ext}"
    time.sleep(DELAY)
    dest.write_bytes(fetch(url))
    return dest.name


def team_links(event_id: int) -> list[tuple[int, str]]:
    html = fetch_html(f"https://www.vlr.gg/event/{event_id}")
    seen, out = set(), []
    for tid, slug in re.findall(r'href="/team/(\d+)/([^/"]+)/?"', html):
        if tid not in seen:
            seen.add(tid)
            out.append((int(tid), slug))
    return out


def parse_team(team_id: int, slug: str) -> dict:
    html = fetch_html(f"https://www.vlr.gg/team/{team_id}/{slug}")
    team = {"id": team_id, "slug": slug, "logo_src": None, "name": None, "tag": None, "players": []}

    m = re.search(r'team-header-logo.*?<img src="([^"]+)"', html, re.S)
    if m:
        team["logo_src"] = m.group(1)
    m = re.search(r'<h1 class="wf-title"[^>]*>([^<]+)</h1>', html)
    if m:
        team["name"] = m.group(1).strip()
    m = re.search(r'team-header-tag">([^<]+)</h2>', html)
    if m:
        team["tag"] = m.group(1).strip()

    # Roster card: sections labelled "players" / "inactive" / "staff". Players only.
    roster = html.split("Current\tRoster", 1)[-1]
    sections = re.split(r'wf-module-label"[^>]*>\s*([a-z]+)\s*<', roster)
    for label, body in zip(sections[1::2], sections[2::2]):
        if label.strip() != "players":
            continue
        for item in re.findall(r'<div class="team-roster-item">(.*?)</a>\s*</div>', body, re.S):
            pm = re.search(r'href="/player/(\d+)/([^/"]+)/?"', item)
            if not pm:
                continue
            img = re.search(r'<img src="([^"]+)"', item)
            alias = re.search(r'team-roster-item-name-alias">(.*?)</div>', item, re.S)
            real = re.search(r'team-roster-item-name-real">\s*([^<]+?)\s*</div>', item)
            alias_txt = re.sub(r"<[^>]+>", "", alias.group(1)).strip() if alias else pm.group(2)
            team["players"].append({
                "id": int(pm.group(1)),
                "slug": pm.group(2),
                "alias": alias_txt,
                "real_name": real.group(1).strip() if real else None,
                "img_src": img.group(1) if img else None,
            })
        break
    return team


def main() -> None:
    ORGS_DIR.mkdir(parents=True, exist_ok=True)
    PLRS_DIR.mkdir(parents=True, exist_ok=True)
    orgs_index, plrs_index = {}, {}
    used_player_stems: set[str] = set()
    missing = []

    for league, event_id in LEAGUE_EVENTS.items():
        teams = team_links(event_id)
        print(f"[{league}] {len(teams)} teams", flush=True)
        for tid, slug in teams:
            try:
                team = parse_team(tid, slug)
            except Exception as e:
                print(f"  !! {slug}: {e}", flush=True)
                continue
            logo_file = save_image(team["logo_src"], ORGS_DIR, slug)
            if not logo_file:
                missing.append(f"org logo: {slug}")
            orgs_index[slug] = {
                "name": team["name"], "tag": team["tag"], "league": league,
                "vlr_id": tid, "file": logo_file,
            }
            for p in team["players"]:
                stem = p["slug"]
                if stem in used_player_stems:
                    stem = f"{p['slug']}-{p['id']}"
                used_player_stems.add(stem)
                img_file = save_image(p["img_src"], PLRS_DIR, stem)
                if not img_file:
                    missing.append(f"player photo: {p['alias']} ({team['name']})")
                plrs_index[stem] = {
                    "alias": p["alias"], "real_name": p["real_name"], "team": team["name"],
                    "team_slug": slug, "league": league, "vlr_id": p["id"], "file": img_file,
                }
            print(f"  {team['name']}: logo={'ok' if logo_file else 'MISSING'}, "
                  f"{len(team['players'])} players", flush=True)

    (ORGS_DIR / "index.json").write_text(json.dumps(orgs_index, indent=2, ensure_ascii=False))
    (PLRS_DIR / "index.json").write_text(json.dumps(plrs_index, indent=2, ensure_ascii=False))
    print(f"\nDone: {len(orgs_index)} orgs, {len(plrs_index)} players.")
    if missing:
        print(f"No image on vlr for {len(missing)}:")
        for m in missing:
            print(f"  - {m}")


if __name__ == "__main__":
    sys.exit(main())
