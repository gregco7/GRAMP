"""
Clip storage for storage/clips — folders + retention.

Clips live in **folders** under storage/clips/ (e.g. default/, old/, or any you
create). A clip's identity is its relative path `folder/name.mp4`. One folder is
the **download target** (where raw grabs land, default "default"); it is the only
folder that auto-cleans — the newest EPHEMERAL_LIMIT non-kept clips in it are kept
and older ones auto-clear, so search-grab spam doesn't pile up. **Every other folder
is permanent** (never auto-deleted), so anything you file away — including the "old"
archive — is safe.

State is two tiny dotfiles at CLIPS_DIR: .keep.json (pinned clips, by relative
path) and .folders.json ({"target": <folder>}). The clip listing filters by video
extension so these never show up as clips.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import config

EPHEMERAL_LIMIT = 20                      # newest N non-kept clips kept in the target folder
DEFAULT_FOLDER = "default"                # seeded download target
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
_KEEP_FILE = config.CLIPS_DIR / ".keep.json"
_FOLDERS_FILE = config.CLIPS_DIR / ".folders.json"
_META_FILE = config.CLIPS_DIR / ".clipmeta.json"      # {rel: {favorite, tags, creator}}
_CREATORS_FILE = config.CLIPS_DIR / ".creators.json"  # managed creator list


# --------------------------------------------------------------------------- #
# Paths / folders
# --------------------------------------------------------------------------- #
def _clips() -> list[Path]:
    """Every clip file across all folders."""
    return [p for p in config.CLIPS_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() in _VIDEO_EXTS]


def _rel(p: Path) -> str:
    return p.relative_to(config.CLIPS_DIR).as_posix()


def _safe_folder(name: str) -> str:
    """A single-segment, non-hidden folder name (no separators / traversal)."""
    n = Path(str(name)).name.strip()
    if not n or n.startswith("."):
        raise ValueError(f"invalid folder name: {name!r}")
    return n


def _dedupe(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, i = path.stem, path.suffix, 1
    while (p := path.with_name(f"{stem}_{i}{suf}")).exists():
        i += 1
    return p


def resolve(rel: str) -> Path:
    """Resolve a clip identity (folder/name, or a bare name / moved clip) to a real
    file under CLIPS_DIR. Raises ValueError if it can't be found safely."""
    base = config.CLIPS_DIR.resolve()
    p = (config.CLIPS_DIR / Path(rel)).resolve()
    if base == p.parent or base in p.parents:
        if p.is_file():
            return p
    # fallback: bare name or a clip that was moved between folders — match basename
    bn = Path(rel).name
    for c in _clips():
        if c.name == bn:
            return c
    raise ValueError(f"clip not found: {rel}")


def folders() -> list[str]:
    """Folder names, default first then alphabetical."""
    ds = sorted(p.name for p in config.CLIPS_DIR.iterdir() if p.is_dir())
    if DEFAULT_FOLDER in ds:
        ds = [DEFAULT_FOLDER] + [d for d in ds if d != DEFAULT_FOLDER]
    return ds


def create_folder(name: str) -> str:
    n = _safe_folder(name)
    (config.CLIPS_DIR / n).mkdir(parents=True, exist_ok=True)
    return n


def rename_folder(name: str, to: str) -> str:
    a = config.CLIPS_DIR / _safe_folder(name)
    b = config.CLIPS_DIR / _safe_folder(to)
    if not a.is_dir():
        raise ValueError(f"no such folder: {name}")
    if b.exists():
        raise ValueError(f"folder already exists: {to}")
    a.rename(b)
    # re-key the keep registry (folder/…) and the target pointer
    keep = {(f"{b.name}/{r.split('/', 1)[1]}" if r.split('/', 1)[0] == a.name else r)
            for r in load_keep()}
    _save_keep(keep)
    # re-key per-clip metadata under the renamed folder
    meta = load_meta()
    remapped = {}
    for r, v in meta.items():
        parts = r.split('/', 1)
        remapped[f"{b.name}/{parts[1]}" if parts[0] == a.name and len(parts) == 2 else r] = v
    _save_meta(remapped)
    if get_target() == a.name:
        set_target(b.name)
    return b.name


def delete_folder(name: str) -> int:
    """Delete a folder, moving any clips inside it to the default folder (no data
    loss). Returns how many clips were relocated. The default folder can't be deleted."""
    n = _safe_folder(name)
    if n == DEFAULT_FOLDER:
        raise ValueError("the default folder can't be deleted")
    d = config.CLIPS_DIR / n
    if not d.is_dir():
        raise ValueError(f"no such folder: {name}")
    moved = 0
    for p in list(d.iterdir()):
        if p.is_file() and p.suffix.lower() in _VIDEO_EXTS:
            move_clip(_rel(p), DEFAULT_FOLDER)
            moved += 1
    # remove whatever non-clip leftovers remain, then the dir
    for p in list(d.iterdir()):
        try:
            p.unlink()
        except OSError:
            pass
    d.rmdir()
    if get_target() == n:
        set_target(DEFAULT_FOLDER)
    return moved


def move_clip(rel: str, folder: str) -> str:
    """Move a clip into `folder`. Returns the new identity (folder/name)."""
    src = resolve(rel)
    dstfolder = config.CLIPS_DIR / _safe_folder(folder)
    dstfolder.mkdir(parents=True, exist_ok=True)
    dst = _dedupe(dstfolder / src.name)
    src.rename(dst)
    new_rel = _rel(dst)
    old_rel = _rel(src)
    keep = load_keep()
    if old_rel in keep or rel in keep:
        keep.discard(old_rel); keep.discard(rel); keep.add(new_rel)
        _save_keep(keep)
    _migrate_meta(old_rel, new_rel) or _migrate_meta(rel, new_rel)
    return new_rel


def delete_clip(rel: str) -> None:
    p = resolve(rel)
    real = _rel(p)
    p.unlink()
    keep = load_keep(); keep.discard(real); keep.discard(rel); _save_keep(keep)
    meta = load_meta()
    if meta.pop(real, None) is not None or meta.pop(rel, None) is not None:
        _save_meta(meta)


def duplicate_clip(rel: str) -> str:
    """Copy a clip on disk into its own folder (deduped name) and clone its
    metadata (tags/creator; the copy starts un-favorited). Returns the new identity."""
    src = resolve(rel)
    dst = _dedupe(src)
    shutil.copy2(src, dst)
    new_rel = _rel(dst)
    m = get_meta(_rel(src)) or get_meta(rel)
    if m:
        set_meta(new_rel, favorite=False, tags=list(m.get("tags", [])),
                 creator=m.get("creator", ""))
    return new_rel


# --------------------------------------------------------------------------- #
# Download target
# --------------------------------------------------------------------------- #
def _meta() -> dict:
    try:
        return json.loads(_FOLDERS_FILE.read_text())
    except Exception:
        return {}


def get_target() -> str:
    t = _meta().get("target", DEFAULT_FOLDER)
    return t if (config.CLIPS_DIR / t).is_dir() else DEFAULT_FOLDER


def set_target(name: str) -> str:
    n = _safe_folder(name)
    (config.CLIPS_DIR / n).mkdir(parents=True, exist_ok=True)
    m = _meta(); m["target"] = n
    _FOLDERS_FILE.write_text(json.dumps(m))
    return n


# --------------------------------------------------------------------------- #
# Keep / prune
# --------------------------------------------------------------------------- #
def load_keep() -> set[str]:
    try:
        return set(json.loads(_KEEP_FILE.read_text()))
    except Exception:
        return set()


def _save_keep(names: set[str]) -> None:
    live = {_rel(p) for p in _clips()}
    _KEEP_FILE.write_text(json.dumps(sorted(names & live)))


def is_kept(rel: str) -> bool:
    return rel in load_keep()


def set_keep(rel: str, keep: bool) -> None:
    names = load_keep()
    if keep:
        names.add(rel)
    else:
        names.discard(rel)
    _save_keep(names)


def prune(limit: int = EPHEMERAL_LIMIT) -> list[str]:
    """Auto-clean only the download-target folder: delete its non-kept clips beyond
    the newest `limit`. Named folders are permanent and untouched."""
    kept = load_keep()
    target = config.CLIPS_DIR / get_target()
    if not target.is_dir():
        return []
    ephemeral = sorted(
        (p for p in target.iterdir()
         if p.is_file() and p.suffix.lower() in _VIDEO_EXTS and _rel(p) not in kept),
        key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for p in ephemeral[limit:]:
        try:
            p.unlink()
            removed.append(_rel(p))
        except OSError:
            pass
    _save_keep(kept)
    return removed


def record(rel: str, keep: bool) -> None:
    """Register a freshly-added clip (keep or ephemeral), then prune the target overflow."""
    if keep:
        set_keep(rel, True)
    prune()


# --------------------------------------------------------------------------- #
# Per-clip metadata (favorite / tags / creator) + managed creators list
# --------------------------------------------------------------------------- #
def load_meta() -> dict:
    try:
        m = json.loads(_META_FILE.read_text())
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _save_meta(meta: dict) -> None:
    """Persist metadata, dropping entries for clips that no longer exist and any
    empty/default records so the file stays lean (mirrors _save_keep's live-set prune)."""
    live = {_rel(p) for p in _clips()}
    clean = {}
    for rel, v in meta.items():
        if rel not in live:
            continue
        rec = _norm_meta(v)
        if rec["favorite"] or rec["tags"] or rec["creator"]:
            clean[rel] = rec
    _META_FILE.write_text(json.dumps(clean, indent=0))


def _norm_meta(v: dict | None) -> dict:
    v = v or {}
    tags = v.get("tags") or []
    tags = [str(t).lstrip("#").strip().lower() for t in tags if str(t).strip()]
    # de-dup, preserve order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t); out.append(t)
    return {"favorite": bool(v.get("favorite")),
            "tags": out,
            "creator": str(v.get("creator") or "").strip()}


def get_meta(rel: str) -> dict:
    """Metadata for a clip identity (empty defaults if none). Falls back to a
    basename match so pre-existing references still resolve their metadata."""
    meta = load_meta()
    if rel in meta:
        return _norm_meta(meta[rel])
    bn = Path(rel).name
    for r, v in meta.items():
        if Path(r).name == bn:
            return _norm_meta(v)
    return _norm_meta(None)


def set_meta(rel: str, favorite=None, tags=None, creator=None) -> dict:
    """Partial update of a clip's metadata. Only the passed fields change.
    New creators are auto-registered in the managed creators list."""
    meta = load_meta()
    rec = _norm_meta(meta.get(rel))
    if favorite is not None:
        rec["favorite"] = bool(favorite)
    if tags is not None:
        rec = _norm_meta({**rec, "tags": tags})
    if creator is not None:
        rec["creator"] = str(creator).strip()
        if rec["creator"]:
            add_creator(rec["creator"])
    meta[rel] = rec
    _save_meta(meta)
    return rec


def _migrate_meta(old_rel: str, new_rel: str) -> bool:
    meta = load_meta()
    if old_rel in meta and old_rel != new_rel:
        meta[new_rel] = meta.pop(old_rel)
        _save_meta(meta)
        return True
    return False


def all_tags() -> list[str]:
    tags: set[str] = set()
    for v in load_meta().values():
        tags.update(_norm_meta(v)["tags"])
    return sorted(tags)


# Creators = anyone taggable on a clip. The **roster** is every Valorant player in
# the picker catalog + every Formatter STREAMER + any custom name the user typed.
# `.creators.json` holds ONLY the custom (non-roster) names, so the roster stays a
# live view of the catalog without baking 200+ names into a file.
def _load_custom_creators() -> list[str]:
    try:
        data = json.loads(_CREATORS_FILE.read_text())
        names = data.get("creators") if isinstance(data, dict) else data
        if isinstance(names, list):
            return [str(n).strip() for n in names if str(n).strip()]
    except Exception:
        pass
    return []


def _save_custom_creators(names) -> None:
    uniq = sorted({str(n).strip() for n in names if str(n).strip()}, key=str.lower)
    _CREATORS_FILE.write_text(json.dumps({"creators": uniq}, indent=0))


def _catalog_players() -> list[dict]:
    """(name, src) for every player in the picker catalog — all are taggable creators."""
    try:
        d = json.loads((config.ASSETS / "catalog.json").read_text())
        return [{"name": p.get("name", ""), "src": p.get("src", "")}
                for p in d.get("players", []) if p.get("name")]
    except Exception:
        return []


def _streamer_entries() -> list[dict]:
    try:
        from . import formatter
        return [{"name": s["name"], "src": s.get("src", "")} for s in formatter.STREAMERS]
    except Exception:
        return []


def creator_roster() -> list[dict]:
    """Everyone taggable as a clip creator: all players + streamers + custom names.
    Returns [{name, src}] sorted case-insensitively (first entry per name keeps its
    avatar; streamers override a same-named player's headshot)."""
    out: dict[str, tuple[str, str]] = {}   # lower-name → (display name, src)
    for e in _streamer_entries() + _catalog_players():   # streamers first → win name+avatar
        k = e["name"].lower()
        if e["name"] and k not in out:
            out[k] = (e["name"], e["src"])
    for n in _load_custom_creators():
        out.setdefault(n.lower(), (n, ""))
    return [{"name": nm, "src": sr}
            for nm, sr in sorted(out.values(), key=lambda t: t[0].lower())]


def _roster_names_lower() -> set[str]:
    return ({p["name"].lower() for p in _catalog_players()} |
            {s["name"].lower() for s in _streamer_entries()})


def creators() -> list[str]:
    """The custom (non-roster) creator names the user added by hand."""
    return sorted(set(_load_custom_creators()), key=str.lower)


def add_creator(name: str) -> list[str]:
    """Register a *custom* creator. Players/streamers are already in the roster, so
    tagging one of them doesn't grow the custom file."""
    name = str(name).strip()
    if name and name.lower() not in _roster_names_lower():
        cur = _load_custom_creators()
        if name.lower() not in {c.lower() for c in cur}:
            cur.append(name)
            _save_custom_creators(cur)
    return creators()


def clip_meta_all() -> dict:
    """Fresh, normalized metadata for every live clip (rel -> record)."""
    meta = load_meta()
    return {rel: get_meta(rel) for rel in {_rel(p) for p in _clips()}
            if rel in meta or Path(rel).name in {Path(r).name for r in meta}}
