"""Scrape all weapon-skin renders from valorant.fandom.com into assets/weapons/.

Writes assets/weapons/<weapon>/<skin-slug>.png (one folder per weapon, knife = melee)
plus assets/weapons/index.json mapping weapon -> [skin names].

Uses the MediaWiki API (the HTML site is behind a Cloudflare challenge; api.php and
the static.wikia CDN are not). Resume-safe: already-downloaded files are skipped.

  .venv/bin/python scripts/scrape_valorant_skins.py
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "weapons"
API = "https://valorant.fandom.com/api.php"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# folder name -> wiki category
WEAPONS = {
    "classic": "Classic Skins", "shorty": "Shorty Skins", "frenzy": "Frenzy Skins",
    "ghost": "Ghost Skins", "sheriff": "Sheriff Skins",
    "stinger": "Stinger Skins", "spectre": "Spectre Skins",
    "bucky": "Bucky Skins", "judge": "Judge Skins",
    "bulldog": "Bulldog Skins", "guardian": "Guardian Skins",
    "phantom": "Phantom Skins", "vandal": "Vandal Skins",
    "marshal": "Marshal Skins", "outlaw": "Outlaw Skins", "operator": "Operator Skins",
    "ares": "Ares Skins", "odin": "Odin Skins",
    "knife": "Melee Skins",
}


def api_get(params: dict) -> dict:
    params = {**params, "format": "json"}
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def category_files(category: str) -> list[str]:
    titles, cont = [], {}
    while True:
        d = api_get({"action": "query", "list": "categorymembers",
                     "cmtitle": f"Category:{category}", "cmtype": "file",
                     "cmlimit": "500", **cont})
        titles += [m["title"] for m in d["query"]["categorymembers"]]
        cont = d.get("continue")
        if not cont:
            return titles
        cont = {"cmcontinue": cont["cmcontinue"]}
        time.sleep(0.3)


def image_urls(titles: list[str]) -> dict[str, str]:
    """File title -> full-size CDN url, batched 50 per API call."""
    out = {}
    for i in range(0, len(titles), 50):
        d = api_get({"action": "query", "titles": "|".join(titles[i:i + 50]),
                     "prop": "imageinfo", "iiprop": "url"})
        for page in d["query"]["pages"].values():
            info = page.get("imageinfo")
            if info:
                out[page["title"]] = info[0]["url"]
        time.sleep(0.3)
    return out


def slugify(title: str) -> str:
    name = re.sub(r"^File:|\.(png|jpe?g|webp)$", "", title, flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def download(url: str, dest: Path) -> bool:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        dest.write_bytes(r.read())
    return True


def main() -> None:
    index = {}
    for folder, category in WEAPONS.items():
        wdir = OUT / folder
        wdir.mkdir(parents=True, exist_ok=True)
        titles = category_files(category)
        urls = image_urls(titles)
        jobs = []
        for title, url in urls.items():
            ext = re.search(r"\.(png|jpe?g|webp)", url, re.I)
            dest = wdir / f"{slugify(title)}.{ext.group(1).lower() if ext else 'png'}"
            if not dest.exists():
                jobs.append((url, dest))
        done = fail = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(download, u, d): d for u, d in jobs}
            for f in as_completed(futs):
                try:
                    f.result()
                    done += 1
                except Exception as e:
                    fail += 1
                    print(f"  !! {futs[f].name}: {e}", flush=True)
        index[folder] = sorted(p.stem for p in wdir.glob("*.*") if p.suffix != ".json")
        print(f"[{folder}] {len(titles)} files: {done} downloaded, "
              f"{len(urls) - len(jobs)} already present, {fail} failed", flush=True)
        time.sleep(0.5)

    (OUT / "index.json").write_text(json.dumps(index, indent=2))
    total = sum(len(v) for v in index.values())
    print(f"\nDone: {total} skin images across {len(index)} weapons.")


if __name__ == "__main__":
    main()
