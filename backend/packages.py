"""
Clip packages — the "clip-package agent" delivery system.

A **package** is a curated set of clip candidates the agent assembles for a prompt
(e.g. "TenZ clips"): each candidate carries a title, source (YouTube / Twitch),
duration, thumbnail and a short "why this made the cut" reason. You browse the
package, pick the ones you want (or all of them), and they download into a folder
ready to drop into the Video Formatter as ranks.

Two engines produce packages, sharing one on-disk format:

1. **Offline heuristic builder** (`build_heuristic`) — always available, no LLM.
   Turns the prompt into search terms, pulls a ranked candidate pool across
   YouTube + a creator's Twitch clips (reusing `clips.search`), and writes the
   package. This is what `request_package` runs in a background thread so the app
   delivers packages on its own — even headless.

2. **The base agent** (Claude sub-agent, see `.claude/agents/clip-package-agent.md`
   + `scripts/package_agent.py`) — an optional richer curation pass that writes a
   package directly with hand-picked reasons. It reads the agent's evolving prompt
   + learnings so it improves over time.

**Learning from critiques:** the main package view has a critiques box. Every
critique you write is appended to the package *and* to the agent's learnings
(`_agent.json`), so the next build — heuristic or agent — is steered by everything
you've told it before. `apply_learnings_to_terms()` folds simple prefer/avoid
signals straight into the heuristic search; the agent reads the full list.

Storage (all under storage/packages/, gitignored):
    _agent.json           the base agent's state (prompt, default_count, learnings)
    <id>/package.json     one package (metadata + candidate clips + critiques)

Downloaded clips land in the normal clip library (store.py) — a package only holds
metadata until you select from it.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import clips, config, store

# --------------------------------------------------------------------------- #
# Base agent state
# --------------------------------------------------------------------------- #
DEFAULT_COUNT = 20

# Seed prompt for the base agent — a neutral Valorant clip curator. The user can
# retune this (e.g. bias it toward their favourite pro); critiques accumulate into
# `learnings`. The prompt is generic so a fresh clone isn't tied to one creator.
_SEED_AGENT = {
    "name": "Clip Package Agent",
    "prompt": (
        "You are a Valorant Shorts clip curator. Given a prompt, assemble a package "
        "of short, self-contained highlight clips that crop cleanly into a 9:16 "
        "vertical Short (one ace / clutch / flick / insane play each, ~10-60s, "
        "facecam present when possible). Search across YouTube and the creator's "
        "Twitch clips. Prefer recent, high-view, visually clean moments; avoid full "
        "VODs, montages, podcasts and watch-parties. For each clip give a one-line "
        "reason it earns its spot. Follow whatever creator / theme the prompt asks "
        "for; when the prompt is vague, favour well-known pros and viral plays."
    ),
    "default_count": DEFAULT_COUNT,
    "sources": ["youtube", "twitch"],
    "learnings": [],          # [{"text": ..., "ts": ..., "package": <id|None>}]
}


def _agent_path() -> Path:
    return config.PACKAGES_DIR / "_agent.json"


def agent_state() -> dict:
    """The base agent's persisted state (seeded on first read)."""
    p = _agent_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            # backfill any keys added since the file was written
            for k, v in _SEED_AGENT.items():
                data.setdefault(k, v if not isinstance(v, list) else list(v))
            return data
        except Exception:
            pass
    data = json.loads(json.dumps(_SEED_AGENT))   # deep copy
    _agent_path().write_text(json.dumps(data, indent=2))
    return data


def save_agent_state(data: dict) -> dict:
    _agent_path().write_text(json.dumps(data, indent=2))
    return data


def update_agent(prompt: Optional[str] = None,
                 default_count: Optional[int] = None) -> dict:
    st = agent_state()
    if prompt is not None:
        st["prompt"] = prompt.strip()
    if default_count is not None:
        st["default_count"] = max(1, min(int(default_count), 50))
    return save_agent_state(st)


def add_learning(text: str, package_id: Optional[str] = None) -> dict:
    """Record a critique/learning so future builds improve. Deduped on text."""
    text = (text or "").strip()
    st = agent_state()
    if text and not any(l.get("text") == text for l in st["learnings"]):
        st["learnings"].append({"text": text, "ts": time.time(), "package": package_id})
        save_agent_state(st)
    return st


# --------------------------------------------------------------------------- #
# Package storage
# --------------------------------------------------------------------------- #
def _pkg_dir(pid: str) -> Path:
    # guard against traversal: only a bare id maps to a directory
    safe = re.sub(r"[^a-z0-9]", "", (pid or "").lower())
    d = (config.PACKAGES_DIR / safe).resolve()
    if d.parent != config.PACKAGES_DIR.resolve() or not safe:
        raise ValueError("bad package id")
    return d


def _pkg_file(pid: str) -> Path:
    return _pkg_dir(pid) / "package.json"


def _read(pid: str) -> Optional[dict]:
    f = _pkg_file(pid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _write(pkg: dict) -> dict:
    d = _pkg_dir(pkg["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps(pkg, indent=2))
    return pkg


def _title_for(prompt: str) -> str:
    p = (prompt or "").strip()
    return (p[:1].upper() + p[1:]) if p else "Clip package"


def create_package(prompt: str, clips_list: list[dict], *, count: Optional[int] = None,
                   title: Optional[str] = None, source: str = "agent",
                   note: str = "", status: str = "ready",
                   pid: Optional[str] = None) -> dict:
    """
    Persist a package. Used by BOTH the heuristic builder and the base agent —
    the agent passes source="agent" and richer per-clip `reason`s.
    """
    pid = pid or uuid.uuid4().hex[:12]
    pkg = _read(pid) or {}
    pkg.update({
        "id": pid,
        "prompt": prompt,
        "title": title or pkg.get("title") or _title_for(prompt),
        "count": count if count is not None else len(clips_list),
        "created": pkg.get("created") or time.time(),
        "updated": time.time(),
        "status": status,
        "seen": pkg.get("seen", False),
        "source": source,
        "note": note or pkg.get("note", ""),
        "clips": [_norm_clip(c, i) for i, c in enumerate(clips_list)],
        "critiques": pkg.get("critiques", []),
    })
    return _write(pkg)


def _norm_clip(c: dict, i: int) -> dict:
    """Normalize one candidate clip into the package schema."""
    return {
        "id": c.get("id") or f"c{i}",
        "title": c.get("title") or "(untitled)",
        "url": c.get("url"),
        "source": c.get("source") or "youtube",
        "channel": c.get("channel"),
        "duration": c.get("duration"),
        "thumbnail": c.get("thumbnail"),
        "view_count": c.get("view_count"),
        "reason": c.get("reason") or "",
        "start": c.get("start"),
        "end": c.get("end"),
        # filled once the user selects + downloads it into the library:
        "downloaded": bool(c.get("downloaded")),
        "clip_path": c.get("clip_path"),
    }


# --------------------------------------------------------------------------- #
# Listing / mutation
# --------------------------------------------------------------------------- #
def _summary(pkg: dict) -> dict:
    return {
        "id": pkg["id"], "title": pkg.get("title"), "prompt": pkg.get("prompt"),
        "count": len(pkg.get("clips", [])), "created": pkg.get("created"),
        "updated": pkg.get("updated"), "status": pkg.get("status", "ready"),
        "seen": pkg.get("seen", False), "source": pkg.get("source", "agent"),
        "thumb": next((c.get("thumbnail") for c in pkg.get("clips", [])
                       if c.get("thumbnail")), None),
        "critiques": len(pkg.get("critiques", [])),
    }


def list_packages() -> dict:
    out = []
    for d in config.PACKAGES_DIR.iterdir():
        if not d.is_dir():
            continue
        pkg = _read(d.name)
        if pkg:
            out.append(_summary(pkg))
    out.sort(key=lambda s: s.get("created") or 0, reverse=True)
    unseen = sum(1 for s in out if not s["seen"] and s["status"] == "ready")
    return {"packages": out, "unseen": unseen,
            "agent": {"prompt": agent_state()["prompt"],
                      "default_count": agent_state()["default_count"],
                      "learnings": agent_state()["learnings"]}}


def get_package(pid: str) -> Optional[dict]:
    return _read(pid)


def mark_seen(pid: str, seen: bool = True) -> Optional[dict]:
    pkg = _read(pid)
    if not pkg:
        return None
    pkg["seen"] = seen
    return _write(pkg)


def add_critique(pid: str, text: str) -> Optional[dict]:
    """Append a critique to the package and feed it into the agent's learnings."""
    text = (text or "").strip()
    pkg = _read(pid)
    if not pkg or not text:
        return pkg
    pkg.setdefault("critiques", []).append({"text": text, "ts": time.time()})
    _write(pkg)
    add_learning(text, package_id=pid)
    return pkg


def delete_package(pid: str) -> bool:
    d = _pkg_dir(pid)
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


# --------------------------------------------------------------------------- #
# Heuristic (offline) builder
# --------------------------------------------------------------------------- #
_CREATOR_HINT = re.compile(r"\b(tenz|aspas|demon1|zekken|yay|shroud|s0m|shahzam|"
                           r"wardell|subroza|sinatraa|zellsis|jawgemo|ethan|boostio|"
                           r"leo|derke|alfajer|chronicle|less|saadhak|aspas)\b", re.I)


def _guess_creator(prompt: str) -> str:
    """Pull a likely creator/player name out of the prompt (best effort)."""
    m = _CREATOR_HINT.search(prompt or "")
    if m:
        return m.group(1)
    # else: first capitalized single token that isn't a stop word
    for tok in re.findall(r"[A-Za-z0-9]+", prompt or ""):
        if tok.lower() not in {"clips", "clip", "valorant", "package", "top",
                               "best", "insane", "highlights", "of", "the"}:
            return tok
    return ""


# Known "this clip wasn't usable" complaints → title keywords to filter/deprioritize.
# The critiques are free text, so map problem PHRASES the user actually writes onto
# title tokens that signal the same non-editable content. (A regex can't infer intent
# the way the sub-agent can — this is the best the offline path can do; the agent reads
# the full critique list verbatim.)
_PROBLEM_SIGNALS = {
    "pre-edit": ["edit", "edited", "edits"], "predit": ["edit", "edited", "edits"],
    "pre edit": ["edit", "edited", "edits"], "already edited": ["edit", "edited"],
    "edited": ["edit", "edited", "edits"], "montage": ["montage", "compilation"],
    "compilation": ["montage", "compilation"], "music": ["remix", "amv", "edit", "song"],
    "background music": ["remix", "amv", "edit", "song"], "song": ["remix", "amv", "song"],
    "overlay": ["overlay"], "subtitle": ["subtitle", "subtitles"], "caption": ["caption", "captions"],
    "not valorant": ["cs2", "csgo", "apex", "fortnite", "cod", "minecraft"],
    "reaction": ["reacts", "reaction"], "watermark": [],
}


def learning_signals() -> tuple[list[str], list[str], list[str]]:
    """
    Distil the agent's learnings into (prefer_terms, avoid_words, applied) where
    `applied` is the human-readable learnings that produced a signal (so the build
    can show what it honoured). Parses explicit prefer/avoid phrasing AND maps known
    'unusable clip' complaints onto title keywords to filter out.
    """
    prefer: list[str] = []
    avoid: list[str] = []
    applied: list[str] = []
    for l in agent_state().get("learnings", []):
        t = (l.get("text") or "").lower()
        used = False
        for m in re.finditer(r"(?:prefer|more|want|focus on|centered? on|add|only)\s+([a-z0-9 ]{2,30})", t):
            prefer.append(m.group(1).strip()); used = True
        for m in re.finditer(r"(?:avoid|less|no |not |fewer|without|remove|drop|don'?t|too many)\s+([a-z0-9 ]{2,30})", t):
            avoid.append(m.group(1).strip()); used = True
        for phrase, words in _PROBLEM_SIGNALS.items():
            if phrase in t:
                avoid.extend(words); used = True
        if used:
            applied.append(l.get("text") or "")
    # split multiword avoids into single tokens for title matching; dedup
    avoid_words = list(dict.fromkeys(w for a in avoid for w in re.findall(r"[a-z0-9]+", a) if len(w) > 2))
    prefer = list(dict.fromkeys(prefer))
    return prefer, avoid_words, applied


# kept for backwards-compat (older callers) — now just the prefer half
def apply_learnings_to_terms(prompt: str) -> str:
    prefer, _avoid, _applied = learning_signals()
    return (prompt + " " + " ".join(prefer)).strip() if prefer else prompt.strip()


def build_heuristic(prompt: str, count: int = DEFAULT_COUNT) -> list[dict]:
    """
    Assemble `count` candidate clips for a prompt with no LLM: creator-aware,
    learning-steered `clips.search` across YouTube + Twitch, ranked crop-ready
    first. Critiques steer it two ways: `prefer` terms join the query; `avoid`
    words (incl. those mapped from 'this clip was pre-edited / had music' style
    complaints) drop matching titles and push raw Twitch clips (single-play, no
    edits/music) to the front. Each clip gets a short auto-reason.
    """
    creator = _guess_creator(prompt)
    prefer, avoid, applied = learning_signals()
    terms = (prompt + " " + " ".join(prefer)).strip()
    query = terms
    if creator:
        query = re.sub(re.escape(creator), "", terms, flags=re.I).strip()

    # over-fetch so avoid-filtering + raw-bias still leaves a full package
    results = clips.search(query or terms, creator=creator, limit=count * 2,
                           include_twitch=True)

    avoid_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in avoid) + r")\b") if avoid else None

    def _blocked(r):
        return bool(avoid_re and avoid_re.search((r.get("title") or "").lower()))

    kept = [r for r in results if not _blocked(r)]
    dropped = [r for r in results if _blocked(r)]
    # if avoid was too aggressive, backfill from the dropped tail so count is met
    pool = kept + dropped
    # when critiques flagged edited/music/montage, raw Twitch clips are the fix:
    # stable-sort them first (twitch = raw source, facecam, one play, no music)
    if avoid:
        pool.sort(key=lambda r: 0 if r.get("source") == "twitch" else 1)

    out = []
    for r in pool[:count]:
        out.append({**r, "reason": _auto_reason(r, avoid)})
    return out


def _auto_reason(r: dict, avoid: list[str] | None = None) -> str:
    d = r.get("duration") or 0
    src = "Twitch clip" if r.get("source") == "twitch" else "YouTube"
    tier = r.get("tier")
    bits = []
    if tier == "short" or (0 < d <= 90):
        bits.append("crop-ready length")
    elif tier == "medium":
        bits.append("trim a moment out")
    else:
        bits.append("long — grab one play")
    if (r.get("view_count") or 0) >= 100_000:
        bits.append("high views")
    if r.get("source") == "twitch":
        bits.append("raw facecam, no edits/music" if avoid else "facecam present")
    return f"{src} · " + ", ".join(bits)


# --------------------------------------------------------------------------- #
# Requesting a package (background heuristic build)
# --------------------------------------------------------------------------- #
def request_package(prompt: str, count: Optional[int] = None) -> dict:
    """
    Kick off a package build. Creates a `building` placeholder immediately and
    runs the offline heuristic builder in a background thread, so the caller (the
    API) returns at once and the package fills in + flips to `ready` (unseen →
    notification badge). A Claude agent can later enrich/replace the same id.
    """
    prompt = (prompt or "").strip() or "TenZ clips"
    count = max(1, min(int(count or agent_state()["default_count"]), 50))
    pid = uuid.uuid4().hex[:12]
    create_package(prompt, [], count=count, source="heuristic",
                   status="building", pid=pid,
                   note="Assembling candidates…")

    def _work():
        try:
            found = build_heuristic(prompt, count)
            _prefer, _avoid, applied = learning_signals()
            note = f"{len(found)} candidates from YouTube + Twitch search."
            if applied:
                note += (f" Honoured {len(applied)} critique(s): filtered pre-edited/"
                         f"music/montage titles and pushed raw Twitch clips first.")
            create_package(prompt, found, count=count, source="heuristic",
                           status="ready", pid=pid, note=note)
        except Exception as e:
            pkg = _read(pid) or {"id": pid, "prompt": prompt}
            pkg.update(status="error", note=f"build failed: {e}", updated=time.time())
            _write(pkg)

    threading.Thread(target=_work, daemon=True).start()
    return _read(pid)


# --------------------------------------------------------------------------- #
# Selecting clips → download into the library (for the Formatter)
# --------------------------------------------------------------------------- #
def select_clips(pid: str, clip_ids: list[str], folder: Optional[str] = None) -> dict:
    """
    Download the chosen candidates into a library folder and mark them downloaded.
    Returns {folder, clips:[{path, title, id}], failed:[...]} so the frontend can
    drop the paths into the Formatter's ranks. `folder` defaults to a slug of the
    package title so a package lands in its own folder.
    """
    pkg = _read(pid)
    if not pkg:
        raise ValueError("package not found")
    want = set(clip_ids or [])
    picks = [c for c in pkg.get("clips", []) if c["id"] in want] if want else pkg.get("clips", [])
    if not picks:
        raise ValueError("no clips selected")

    folder = folder or _folder_slug(pkg.get("title") or pkg.get("prompt") or "package")
    folder = store.create_folder(folder)
    dest = str(config.CLIPS_DIR / folder)

    got, failed = [], []
    for c in picks:
        if c.get("downloaded") and c.get("clip_path"):
            got.append({"path": c["clip_path"], "title": c["title"], "id": c["id"]})
            continue
        if not c.get("url"):
            failed.append({"id": c["id"], "title": c["title"], "error": "no url"})
            continue
        try:
            res = clips.download(c["url"], dest, start=c.get("start"), end=c.get("end"))
            name = Path(res["path"]).name
            rel = f"{folder}/{name}"
            store.record(rel, keep=True)      # curated picks shouldn't auto-prune
            c["downloaded"] = True
            c["clip_path"] = rel
            got.append({"path": rel, "title": c["title"], "id": c["id"]})
        except Exception as e:
            failed.append({"id": c["id"], "title": c["title"], "error": str(e)})

    _write(pkg)
    return {"folder": folder, "clips": got, "failed": failed}


def _folder_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return (slug[:40] or "package")
