"""
Content Tool Manager — local FastAPI backend.

Run:  ./run.sh   (or)   .venv/bin/uvicorn backend.main:app --reload
Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (aibrain, animate, autolayout, clips, config, cta, formatter, hook,
               music, packages, reddit, search2, store, twitch, youtube)

app = FastAPI(title="Content Tool Manager")

# In-memory job registry for long-running renders.
JOBS: dict[str, dict] = {}

# Session cache of composed+baked Formatter segments, keyed by everything that
# determines a segment's pixels (clip file + layout + progressive-reveal state).
# So a rebuild after editing one rank reuses the other ranks' already-rendered
# segments instead of re-composing all N. Cleared on restart; files live in TMP.
_SEG_CACHE: "dict[str, str]" = {}
_SEG_CACHE_CAP = 80                              # evict oldest beyond this (+ unlink file)
_SEG_LOCK = threading.Lock()                     # guards _SEG_CACHE across parallel renders

# Formatter segments are independent renders (no cross-segment state once the
# progressive-reveal overlays are materialized), so they render concurrently.
# subprocess.run releases the GIL, so worker threads give real ffmpeg parallelism.
# BUT there's one hardware video encoder (VideoToolbox) + shared cores for the
# crop/scale/blur filtering, so concurrent segments contend: measured knee is ~2
# (2 workers ≈ 25% faster than serial; 3–4 give no extra wall-clock, just more
# CPU thrash — the encoder is saturated). Kept at 2 for that reason.
_FMT_WORKERS = 2


def _seg_cache_key(src: str, layout_dump: dict, bake: bool, mode: str,
                   cfg: dict | None, tov: dict, template: str, ri: int) -> str:
    """Hash of all inputs that feed one segment's render. `tov` carries the
    per-segment overlay (revealed labels/widgets, card, bonus state, style,
    offsets), so editing one rank only invalidates the segments it changes."""
    try:
        st = os.stat(src)
        sig = [st.st_mtime_ns, st.st_size]
    except OSError:
        sig = [0, 0]
    blob = json.dumps(
        {"v": 1, "src": src, "sig": sig, "layout": layout_dump, "bake": bake,
         "mode": mode, "cfg": cfg, "tov": tov, "tpl": template, "ri": ri},
        sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _seg_cache_put(key: str, path: str) -> None:
    _SEG_CACHE[key] = path
    while len(_SEG_CACHE) > _SEG_CACHE_CAP:
        old_key, old_path = next(iter(_SEG_CACHE.items()))
        _SEG_CACHE.pop(old_key, None)
        try:
            if os.path.exists(old_path):
                os.remove(old_path)
        except OSError:
            pass


# ----------------------------------------------------------------------------- #
# Request models
# ----------------------------------------------------------------------------- #
class SearchReq(BaseModel):
    query: str = ""
    creator: str = ""
    limit: int = 16
    include_twitch: bool = True


class Search2Req(BaseModel):
    """Experimental engine (search2). `query` is natural language; `filters`
    optionally pins agent/map/weapon/org/creator/recency/etc; `opts` toggles the
    AI parse + vision passes and their depth."""
    query: str = ""
    filters: dict | None = None
    opts: dict | None = None


class DownloadReq(BaseModel):
    url: str
    start: float | None = None   # source seconds — fetch only this section when set
    end: float | None = None
    keep: bool = False           # long-term (exempt from the ephemeral prune)
    folder: str | None = None    # target folder (defaults to the stored download target)


class KeepReq(BaseModel):
    name: str                    # clip identity (folder/name) inside storage/clips
    keep: bool = True


class FolderReq(BaseModel):
    name: str                    # folder name
    to: str | None = None        # new name (rename only)


class MoveReq(BaseModel):
    path: str                    # clip identity (folder/name)
    folder: str                  # destination folder


class TargetReq(BaseModel):
    folder: str                  # folder to make the download target


class ClipDeleteReq(BaseModel):
    path: str                    # clip identity (folder/name)


class FavoriteReq(BaseModel):
    path: str                    # clip identity (folder/name)
    value: bool = True


class TagsReq(BaseModel):
    path: str
    tags: list[str] = []         # freeform labels (leading '#' + case normalized)


class CreatorReq(BaseModel):
    path: str
    creator: str = ""            # "" clears; a new name auto-registers in the list


class CreatorAddReq(BaseModel):
    name: str


class DuplicateReq(BaseModel):
    path: str                    # clip identity (folder/name) to copy on disk


class RectModel(BaseModel):
    x: int
    y: int
    w: int
    h: int


class DetectReq(BaseModel):
    clip: str  # filename inside storage/clips


class TransitionModel(BaseModel):
    sound: str = "none"    # SFX key (see autolayout.SFX_CATALOG) or "none"
    visual: str = "none"   # visual key (see autolayout.TRANSITION_CATALOG) or "none"
    volume: float = 1.0    # SFX gain (0..2)
    speed: float = 2.0     # region speed-up factor (only when visual == "speedup")


class CutModel(BaseModel):
    start: float
    end: float
    transition: TransitionModel | None = None


class EffectModel(BaseModel):
    """A B-roll point effect dropped on the timeline (see autolayout.EFFECTS_CATALOG)."""
    type: str                    # effect key ("money", …)
    t: float                     # source-second timestamp the effect fires at
    dur: float | None = None     # per-marker flash duration override (secs; clamped to range)
    sound: str | None = None     # per-marker sound choice (a key from the effect's sounds)
    volume: float | None = None  # per-marker SFX gain (0..2; 1.0 = unchanged)
    scale: float | None = None   # overlay-chip size (subscribe prompt); clamped to range
    x: float | None = None       # overlay-chip centre X as a fraction of output width
    y: float | None = None       # overlay-chip centre Y as a fraction of output height
    style: str | None = None     # CTA look variant ("classic" | "card")
    vd_accent: str | None = None # CTA VD-mark ring + "D" colour (hex; default red)
    params: dict | None = None   # per-marker adjustable params (Face Zoom: zoom/pull/diag/focus; CTA: anim)


class LayoutReq(BaseModel):
    """Shared options for both preview and render."""
    clip: str
    facecam_enabled: bool = True
    rect: RectModel | None = None            # facecam region in source pixels
    facecam_ratio: float = 0.35
    facecam_autofit: bool = True
    facecam_height: float | None = None      # lock the facecam pane to this fraction
                                             # of the content block (crop-independent)
    blur_bg: bool = True
    gameplay_fill: str = "crop"              # "crop" | "blur" (blurred edges)
    blur_bar: float = 0.12
    mousecam_enabled: bool = False
    mousecam: RectModel | None = None
    mousecam_pos: str = "bottom-right"       # corner preset (fallback when x/y unset)
    mousecam_x: float | None = None          # overlay top-left as a fraction of output W (drag)
    mousecam_y: float | None = None          # overlay top-left as a fraction of output H (drag)
    mousecam_scale: float = 0.28
    trim_start: float = 0.0                  # seconds into the source to start
    trim_end: float | None = None            # seconds into the source to stop
    speed: float = 1.0                       # playback speed multiplier (>1 = faster)
    cuts: list[CutModel] | None = None       # source-second spans to remove (+ optional transition)
    audio_fade_out: bool = False             # fade the audio out at the end
    audio_fade_dur: float = 0.8              # fade-out length in seconds
    effects: list[EffectModel] | None = None  # B-roll point effects on the timeline
    template: str | None = None              # ranking-template preset key (Video Formatter)
    template_opts: dict | None = None        # {title:{...}, list:{...}} overrides for the template


def _render_template_overlay(req: "LayoutReq") -> str | None:
    """Render the ranking-template PNG (if a template is selected) and return
    its path, for use as autolayout's spatial overlay."""
    if not req.template:
        return None
    name = f"tpl_{uuid.uuid4().hex[:8]}.png"
    dst = str(config.TMP_DIR / name)
    return formatter.render(req.template, req.template_opts, dst)


def _to_opts(req: "LayoutReq") -> autolayout.LayoutOpts:
    return autolayout.LayoutOpts(
        facecam_enabled=req.facecam_enabled,
        facecam=autolayout.Rect(**req.rect.model_dump()) if req.rect else None,
        facecam_ratio=req.facecam_ratio,
        facecam_autofit=req.facecam_autofit,
        facecam_height=req.facecam_height,
        blur_bg=req.blur_bg,
        gameplay_fill=req.gameplay_fill,
        blur_bar=req.blur_bar,
        mousecam_enabled=req.mousecam_enabled,
        mousecam=autolayout.Rect(**req.mousecam.model_dump()) if req.mousecam else None,
        mousecam_pos=req.mousecam_pos,
        mousecam_x=req.mousecam_x,
        mousecam_y=req.mousecam_y,
        mousecam_scale=req.mousecam_scale,
        trim_start=req.trim_start,
        trim_end=req.trim_end,
        speed=req.speed,
        cuts=[(c.start, c.end) for c in req.cuts] if req.cuts else None,
        cut_transitions=[
            autolayout.Transition(sound=c.transition.sound, visual=c.transition.visual,
                                  volume=c.transition.volume, speed=c.transition.speed)
            if c.transition else None
            for c in req.cuts
        ] if req.cuts else None,
        audio_fade_out=req.audio_fade_out,
        audio_fade_dur=req.audio_fade_dur,
        effects=[e.model_dump() for e in req.effects] if req.effects else None,
        overlay_png=_render_template_overlay(req),
    )


# ----------------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------------- #
class FormatterRank(BaseModel):
    """One rank slot: its list label, the clip + per-clip layout config, and the
    transition used when leaving this rank's segment (in play order)."""
    label: str = ""
    layout: LayoutReq            # full per-clip config (clip + facecam/trim/cuts/speed…)
    join: TransitionModel | None = None


class AnimModeCfg(BaseModel):
    """Reveal settings for one mode (Intro or New item)."""
    motion: str = "pop"                        # "pop" | "slide" | "stagger"
    speed: float = 1.0                         # reveal-speed multiplier (>1 = faster)
    sound: str = "none"                        # SFX key (see autolayout.SFX_CATALOG) or "none"
    volume: float = 1.0


class FormatterAnimCfg(BaseModel):
    """Whether to bake the list reveals into the build, and each mode's settings.
    Intro plays on the first segment; New item on each later segment's reveal."""
    enabled: bool = False
    intro: AnimModeCfg = AnimModeCfg()
    new_item: AnimModeCfg = AnimModeCfg()


class MusicCfg(BaseModel):
    """Background-music bed laid under the finished Top-N render (see music.py)."""
    track: str                                 # catalog track id (must be downloaded)
    volume: float = 0.25                       # music bed level (0..1+)
    keep_clip_audio: bool = True               # play clip audio (game + reactions) over the bed
    clip_volume: float = 1.0                   # level of the clip audio when kept
    start: float = 0.0                         # seconds into the track to begin (pick the drop)
    fade_in: float = 0.4
    fade_out: float = 2.0


class SubscribeCfg(BaseModel):
    """The animated Subscribe CTA pinned to the END of the finished Top-N render —
    a cursor clicks the button, it flips to Subscribed (+ SFX). Baked in cta.py so
    it FINISHES right as the video ends. Replaces the old static chip."""
    on: bool = False
    style: str = "classic"                      # "classic" (mark + pill) or "card"
    dur: float = 4.0                            # persist secs (the click lands, then it holds)
    scale: float = 1.0                          # widget size multiplier
    anim: float = 0.7                           # cursor click speed (secs)
    sound: str = "pop"                          # click SFX key ("none" = silent)
    volume: float = 1.0                         # click SFX gain
    x: float = 0.5                              # centre X as a fraction of output width
    y: float = 0.5                              # centre Y as a fraction of output height
    accent: str = "#ff0033"                     # VD mark ring + "D" colour (default red;
                                                # frontend sends the title accent to match)


class HookCfg(BaseModel):
    """A Clip Hook cold-open: play a MOMENT from any clip in a framed box over the
    OPENING of the finished Top-N, then shrink it away to reveal the ranking (the
    first segment's reveal is held back until then). No added length. See hook.py."""
    on: bool = False
    clip: str | None = None                     # clip identity to hook from (any rank's clip);
                                                # None → the first-played rank's clip
    moment: float = 0.0                         # start second within the clip
    length: float = 1.5                         # hold secs before the shrink-away
    in_speed: float = 1.0                       # pop-IN speed (higher = snappier)
    out_speed: float = 1.0                      # shrink-OUT speed (higher = snappier)
    style: str = "glow"                         # frame look: glow | shadow | border | brackets
    accent: str = "#2ea6ff"                     # frame accent colour
    sound: str = "whoosh"                       # cold-open SFX ("none" = silent)
    volume: float = 1.0
    freeze_bg: bool = False                     # pause the background on frame 0 until the
                                                # hook is over (adds length) vs play it under


class FormatterReq(BaseModel):
    template: str                              # ranking-template preset key
    template_opts: dict | None = None          # base title/list styling (font, accent, pill, title)
    order: str = "desc"                        # play order: "desc" (N→1), "asc" (1→N), or "custom"
    play_order: list[int] | None = None        # custom play sequence: rank indices (0-based) in play order
    ranks: list[FormatterRank]                 # index 0 == rank 1
    anim: FormatterAnimCfg | None = None        # bake animated reveals into the video (else static overlay)
    music: MusicCfg | None = None               # optional background-music bed on the final render
    subscribe: SubscribeCfg | None = None       # optional subscribe chip on the last X seconds
    hook: HookCfg | None = None                 # optional Clip Hook cold-open on the opening
    hook_preview: bool = False                  # render ONLY the hook + start of the reveal (fast preview)
    only: int | None = None                     # render ONLY this rank's segment (index into
                                                # the filled ranks) as a fast single-clip preview
                                                # — skips joins + music


class FormatterPreviewReq(BaseModel):
    """Preview the selected template overlay (optionally over a real clip frame)."""
    template: str
    template_opts: dict | None = None
    items: list[str] = []
    highlight: int = -1
    clip: str | None = None              # first assigned rank's clip (for a realistic bg)
    layout: LayoutReq | None = None      # that clip's layout, so the preview matches the build
    bare: bool = False                   # composed clip frame only — the in-browser canvas
                                         # draws the template itself on top


class FormatterAnimateReq(BaseModel):
    """Render a short animated preview of a list template's reveal (preview-only)."""
    template: str
    template_opts: dict | None = None
    items: list[str] = []
    highlight: int = -1                  # target rank (new_item); also highlighted in intro
    mode: str = "intro"                  # "intro" | "new_item"
    motion: str = "pop"                  # "pop" | "slide" | "stagger"
    speed: float = 1.0                   # reveal-speed multiplier (>1 = faster)
    sound: str = "pop"                   # SFX key (see autolayout.SFX_CATALOG) or "none"
    volume: float = 1.0
    clip: str | None = None              # optional clip to show behind the animation
    layout: LayoutReq | None = None


_JOIN_OVERLAP = {j["key"]: j["overlap"] for j in autolayout.JOIN_CATALOG}


def _clip_path(name: str) -> Path:
    # Clip identity is a relative path `folder/name`; store.resolve also falls back
    # to a basename match so pre-folder references (saved projects) still resolve.
    try:
        return store.resolve(name)
    except ValueError:
        raise HTTPException(404, f"clip not found: {name}")


# ----------------------------------------------------------------------------- #
# API
# ----------------------------------------------------------------------------- #
@app.get("/api/niches")
def get_niches():
    return config.NICHES


@app.get("/api/fx")
def get_fx():
    """Sound effects + visual transitions for the FX picker (with preview URLs)."""
    sounds = [
        {**s, "preview": f"/assets/sfx/{s['file']}"}
        for s in autolayout.SFX_CATALOG
    ]
    effects = []
    for e in autolayout.EFFECTS_CATALOG:
        # drop non-JSON-safe keys (tint tuple, sfx_dir Path)
        ed = {k: v for k, v in e.items() if k not in ("tint", "sfx_dir")}
        # folder-backed effects (SFX) are scanned live so newly dropped files
        # appear; all sounds live in assets/sfx, served at /assets/sfx. quote()
        # handles spaces / # / parens in the merged filenames.
        snds = autolayout.effect_sounds(e)
        ed["sounds"] = [{**s, "preview": (f"/assets/sfx/{quote(s['file'])}" if s.get("file") else None)}
                        for s in snds]
        default_file = e.get("sound") or (snds[0]["file"] if snds else None)
        ed["preview"] = f"/assets/sfx/{quote(default_file)}" if default_file else None
        effects.append(ed)
    return {"sounds": sounds, "visuals": autolayout.TRANSITION_CATALOG,
            "effects": effects, "subscribe": cta.META}


@app.get("/api/templates")
def get_templates():
    """Ranking-template presets + font choices for the Video Formatter."""
    templates = [
        {"key": k, "label": v["label"],
         "title": v.get("title") or {}, "list": v.get("list") or {}}
        for k, v in formatter.TEMPLATES.items()
    ]
    return {"templates": templates, "fonts": formatter.FONT_CHOICES}


@app.get("/api/clips")
def list_clips():
    """Clips grouped by folder (newest first within each), plus the current download
    target. `path` (folder/name) is a clip's identity; `name` is just for display."""
    exts = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
    kept = store.load_keep()
    target = store.get_target()
    meta = store.load_meta()
    out = []
    for folder in store.folders():
        d = config.CLIPS_DIR / folder
        clips = []
        for p in sorted((q for q in d.iterdir()
                         if q.is_file() and q.suffix.lower() in exts),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            rel = f"{folder}/{p.name}"
            m = store._norm_meta(meta.get(rel))
            clips.append({
                "name": p.name, "path": rel, "folder": folder,
                "size": p.stat().st_size,
                "url": f"/storage/clips/{rel}",
                "thumb": f"/api/clips/thumb?path={rel}",
                "mtime": int(p.stat().st_mtime),
                "kept": rel in kept,
                "favorite": m["favorite"], "tags": m["tags"], "creator": m["creator"],
            })
        out.append({"name": folder, "clips": clips, "target": folder == target})
    return {"target": target, "folders": out,
            "creator_roster": store.creator_roster(), "tags": store.all_tags()}


@app.post("/api/clips/search")
def clips_search(req: SearchReq):
    try:
        return clips.search(req.query, req.creator, req.limit, req.include_twitch)
    except Exception as e:
        raise HTTPException(500, f"search failed: {e}")


# ----------------------------------------------------------------------------- #
# Experimental engine (search2) — AI-parsed, multi-source, vision-verified.
# Runs as a background job (LLM + frame sampling take ~30-90s); poll /api/jobs/{id}.
# ----------------------------------------------------------------------------- #
@app.get("/api/search2/meta")
def search2_meta():
    """Capabilities + vocab for the Experimental UI (what's authed, filter chips)."""
    return {
        "ai_available": aibrain.available(),
        "twitch_available": twitch.available(),
        "reddit_available": reddit.available(),
        "agents": search2.AGENTS, "maps": search2.MAPS,
        "weapons": search2.WEAPONS, "orgs": search2.ORGS, "plays": search2.PLAYS,
        "sources": ["youtube", "twitch", "reddit"],
    }


def _run_search2(job_id: str, req: "Search2Req"):
    JOBS[job_id]["status"] = "running"

    def progress(pct, msg):
        JOBS[job_id].update(progress=pct, stage=msg)

    try:
        out = search2.deep_search(req.query, filters=req.filters, opts=req.opts,
                                  progress=progress)
        JOBS[job_id].update(status="done", finished=time.time(), **out)
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e), finished=time.time())


@app.post("/api/search2")
def search2_start(req: Search2Req):
    if not (req.query or (req.filters or {})):
        raise HTTPException(400, "empty query")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "started": time.time(), "kind": "search2",
                    "progress": 0, "stage": "queued", "query": req.query}
    threading.Thread(target=_run_search2, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/clips/download")
def clips_download(req: DownloadReq):
    try:
        folder = store.create_folder(req.folder) if req.folder else store.get_target()
        res = clips.download(req.url, str(config.CLIPS_DIR / folder),
                             start=req.start, end=req.end)
        name = Path(res["path"]).name
        rel = f"{folder}/{name}"
        store.record(rel, keep=req.keep)      # ephemeral by default; only the target prunes
        return {"name": name, "path": rel, "folder": folder, "title": res["title"],
                "url": f"/storage/clips/{rel}", "kept": req.keep}
    except Exception as e:
        raise HTTPException(500, f"download failed: {e}")


@app.post("/api/clips/import")
async def clips_import(file: UploadFile = File(...), keep: bool = True, folder: str | None = None):
    folder = store.create_folder(folder) if folder else store.get_target()
    name = Path(file.filename).name
    dest = config.CLIPS_DIR / folder / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    rel = f"{folder}/{name}"
    # your own uploads can't be re-fetched — keep them out of the ephemeral prune
    store.record(rel, keep=keep)
    return {"name": name, "path": rel, "folder": folder,
            "url": f"/storage/clips/{rel}", "kept": keep}


@app.post("/api/clips/keep")
def clips_keep(req: KeepReq):
    """Pin a clip in the download-target folder so it survives auto-cleanup (clips
    in named folders are permanent regardless)."""
    p = _clip_path(req.name)                    # 404 if it isn't a real clip
    store.set_keep(store._rel(p), req.keep)
    return {"name": req.name, "kept": req.keep}


# --------------------------------------------------------------------------- #
# Clip folders — organize storage/clips (create/rename/delete/move/target)
# --------------------------------------------------------------------------- #
@app.post("/api/folders/create")
def folders_create(req: FolderReq):
    try:
        return {"folder": store.create_folder(req.name)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/folders/rename")
def folders_rename(req: FolderReq):
    if not req.to:
        raise HTTPException(400, "new name required")
    try:
        return {"folder": store.rename_folder(req.name, req.to)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/folders/delete")
def folders_delete(req: FolderReq):
    try:
        moved = store.delete_folder(req.name)
        return {"deleted": req.name, "moved_to_default": moved}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/folders/target")
def folders_target(req: TargetReq):
    try:
        return {"target": store.set_target(req.folder)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/clips/move")
def clips_move(req: MoveReq):
    try:
        return {"path": store.move_clip(req.path, req.folder)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/clips/delete")
def clips_delete(req: ClipDeleteReq):
    try:
        store.delete_clip(req.path)
        return {"deleted": req.path}
    except ValueError as e:
        raise HTTPException(404, str(e))


# --------------------------------------------------------------------------- #
# Clip metadata — favorite / tags / creator / duplicate + creators registry
# --------------------------------------------------------------------------- #
@app.post("/api/clips/favorite")
def clips_favorite(req: FavoriteReq):
    _clip_path(req.path)                        # 404 if it isn't a real clip
    rec = store.set_meta(req.path, favorite=req.value)
    return {"path": req.path, **rec}


@app.post("/api/clips/tags")
def clips_tags(req: TagsReq):
    _clip_path(req.path)
    rec = store.set_meta(req.path, tags=req.tags)
    return {"path": req.path, **rec, "all_tags": store.all_tags()}


@app.post("/api/clips/creator")
def clips_creator(req: CreatorReq):
    _clip_path(req.path)
    rec = store.set_meta(req.path, creator=req.creator)
    return {"path": req.path, **rec, "creator_roster": store.creator_roster()}


@app.post("/api/clips/duplicate")
def clips_duplicate(req: DuplicateReq):
    try:
        new_rel = store.duplicate_clip(req.path)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"path": new_rel}


@app.get("/api/creators")
def get_creators():
    return {"roster": store.creator_roster()}


@app.post("/api/creators")
def add_creator(req: CreatorAddReq):
    return {"creators": store.add_creator(req.name)}


@app.get("/api/clips/thumb")
def clip_thumb(path: str):
    """Cached poster frame (jpg) for a clip, for the Library thumbnail grid.
    Regenerated when the source is newer than the cache."""
    import hashlib
    src = _clip_path(path)
    key = hashlib.sha1(f"{src}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    dst = config.THUMBS_DIR / f"{key}.jpg"
    if not dst.exists():
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1",
                 "-vf", "scale=360:-2", "-q:v", "4", str(dst)],
                check=True, capture_output=True, timeout=30)
        except Exception:
            # very short clips: grab the first frame instead
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(src), "-frames:v", "1",
                     "-vf", "scale=360:-2", "-q:v", "4", str(dst)],
                    check=True, capture_output=True, timeout=30)
            except Exception:
                raise HTTPException(500, "thumbnail generation failed")
    return FileResponse(str(dst), media_type="image/jpeg")


# --------------------------------------------------------------------------- #
# Channel overview — live YouTube stats (Valorant tab). NOT vidiq.
# --------------------------------------------------------------------------- #
@app.get("/api/youtube/status")
def youtube_status():
    return youtube.status()


@app.get("/api/youtube/overview")
def youtube_overview(niche: str = "valorant", days: int = 28, refresh: bool = False):
    return youtube.overview(niche, days=days, force=refresh)


# --------------------------------------------------------------------------- #
# Nb1 grabber — browse a compilation channel + resolve a scrub-preview stream.
# Grabbing itself reuses /api/clips/download (it already does section download).
# --------------------------------------------------------------------------- #
class Nb1StreamReq(BaseModel):
    url: str
    max_height: int = 720


@app.get("/api/nb1/videos")
def nb1_videos(limit: int = 30, offset: int = 0, sort: str = "recent",
               query: str = "", force: bool = False):
    """Page over the Nb1 channel's uploads (cached). See clips.list_channel."""
    try:
        return clips.list_channel(limit=limit, offset=offset, sort=sort,
                                  query=query, force=force)
    except Exception as e:
        raise HTTPException(500, f"channel listing failed: {e}")


@app.post("/api/nb1/stream")
def nb1_stream(req: Nb1StreamReq):
    """Resolve a directly-playable progressive URL for in-browser scrubbing."""
    try:
        return clips.resolve_stream(req.url, req.max_height)
    except Exception as e:
        raise HTTPException(500, f"stream resolve failed: {e}")


# --------------------------------------------------------------------------- #
# Music beds — catalog + grab-to-local for the Video Formatter
# --------------------------------------------------------------------------- #
class MusicGrabReq(BaseModel):
    track: str


@app.get("/api/music/catalog")
def music_catalog():
    """The license-checked track catalog grouped by genre, with per-track
    downloaded/file status. See music.py."""
    return music.catalog()


@app.post("/api/music/grab")
def music_grab(req: MusicGrabReq):
    """Download a track's audio into local storage/music/ (via yt-dlp)."""
    try:
        return music.grab(req.track)
    except Exception as e:
        raise HTTPException(500, f"grab failed: {e}")


@app.post("/api/autolayout/detect")
def autolayout_detect(req: DetectReq):
    path = str(_clip_path(req.clip))
    info = autolayout.probe(path)
    rect = autolayout.detect_facecam(path)
    detected = rect is not None
    if rect is None:
        # sensible default: top-left corner box, ~28% width, 16:9-ish
        w = int(info.width * 0.28)
        h = int(w * 9 / 16)
        rect = autolayout.Rect(0, 0, w, h)

    # render a preview of the composed frame (default facecam layout)
    preview_name = f"prev_{uuid.uuid4().hex[:8]}.jpg"
    preview_path = config.TMP_DIR / preview_name
    try:
        opts = autolayout.LayoutOpts(facecam=rect)
        autolayout.make_preview_frame(path, opts, str(preview_path))
        preview_url = f"/storage/tmp/{preview_name}"
    except Exception:
        preview_url = None

    return {
        "video": {"width": info.width, "height": info.height,
                  "duration": info.duration, "fps": info.fps},
        "rect": rect.to_dict(),
        "detected": detected,
        "corner": autolayout.detect_facecam_corner(rect, info) if detected else None,
        "preview": preview_url,
    }


@app.post("/api/autolayout/preview")
def autolayout_preview(req: LayoutReq):
    path = str(_clip_path(req.clip))
    preview_name = f"prev_{uuid.uuid4().hex[:8]}.jpg"
    preview_path = config.TMP_DIR / preview_name
    autolayout.make_preview_frame(path, _to_opts(req), str(preview_path))
    return {"preview": f"/storage/tmp/{preview_name}"}


def _run_render(job_id: str, src: str, dst: str, opts: autolayout.LayoutOpts):
    JOBS[job_id]["status"] = "running"
    try:
        autolayout.compose(src, dst, opts)
        JOBS[job_id].update(status="done",
                            output=f"/storage/output/{Path(dst).name}",
                            finished=time.time())
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e), finished=time.time())


class MomentReq(LayoutReq):
    """A single-clip render scoped to one moment on the timeline — for eyeballing
    a B-roll punch without rendering the whole clip. The window is source seconds
    (same clock as trim / cuts / effects). All the layout + effects still apply."""
    moment_start: float = 0.0
    moment_end: float = 0.0


def _run_moment(job_id: str, src: str, dst: str, opts: autolayout.LayoutOpts):
    """Render a moment window to TMP (played back in the browser modal)."""
    JOBS[job_id]["status"] = "running"
    try:
        autolayout.compose(src, dst, opts)
        JOBS[job_id].update(status="done",
                            output=f"/storage/tmp/{Path(dst).name}",
                            finished=time.time())
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e), finished=time.time())


@app.post("/api/autolayout/moment")
def autolayout_moment(req: MomentReq):
    path = str(_clip_path(req.clip))
    out_name = f"moment_{Path(req.clip).stem}_{uuid.uuid4().hex[:6]}.mp4"
    dst = str(config.TMP_DIR / out_name)

    opts = _to_opts(req)
    # Scope the render to the moment: seek to its start, stop at its end. Cuts,
    # speed and B-roll effects keep their source-second times and are re-clocked
    # to this window by the compose pipeline (a marker inside the window fires).
    lo, hi = sorted((float(req.moment_start), float(req.moment_end)))
    lo = max(0.0, lo)
    if req.trim_end is not None:                 # keep the moment inside the trim
        hi = min(hi, float(req.trim_end))
    lo = max(lo, float(req.trim_start or 0.0))
    if hi - lo < 0.2:                            # guarantee a watchable minimum
        hi = lo + 0.2
    opts.trim_start = lo
    opts.trim_end = hi
    opts.audio_fade_out = False                  # the whole-clip fade would land wrong here

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "started": time.time(), "clip": req.clip}
    threading.Thread(
        target=_run_moment,
        args=(job_id, path, dst, opts),
        daemon=True,
    ).start()
    return {"job": job_id}


@app.post("/api/autolayout/render")
def autolayout_render(req: LayoutReq):
    path = str(_clip_path(req.clip))
    out_name = f"vertical_{Path(req.clip).stem}_{uuid.uuid4().hex[:6]}.mp4"
    dst = str(config.OUTPUT_DIR / out_name)

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "started": time.time(), "clip": req.clip}
    threading.Thread(
        target=_run_render,
        args=(job_id, path, dst, _to_opts(req)),
        daemon=True,
    ).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    return JOBS[job_id]


# ----------------------------------------------------------------------------- #
# Video Formatter — assemble many clips into one ranking video
# ----------------------------------------------------------------------------- #
@app.get("/api/formatter/meta")
def formatter_meta():
    """Everything the Formatter tab needs: the template, its styles (with the
    design's drag-offset defaults), fonts, join effects, sounds, picker data."""
    templates = [
        {"key": k, "label": v["label"], "title": v.get("title") or {},
         "list": v.get("list") or {}}
        for k, v in formatter.TEMPLATES.items()
    ]
    styles = [
        {"key": k, "label": v["label"], "offsets": v["offsets"],
         "accent": v["list"].get("accent")}
        for k, v in formatter.STYLE_SPECS.items()
    ]
    sounds = [{**s, "preview": f"/assets/sfx/{s['file']}"} for s in autolayout.SFX_CATALOG]
    return {"templates": templates, "styles": styles,
            "bonus_styles": formatter.BONUS_STYLES, "streamers": formatter.STREAMERS,
            "picker": "/assets/catalog.json", "vct_icon": "/assets/vct-icon.png",
            "fonts": formatter.FONT_CHOICES,
            "joins": autolayout.JOIN_CATALOG, "sounds": sounds,
            "anim_modes": animate.ANIM_MODES, "anim_motions": animate.ANIM_MOTIONS,
            "hook": hook.META, "subscribe": cta.META}


@app.post("/api/formatter/preview")
def formatter_preview(req: FormatterPreviewReq):
    """Render the selected template as a still — over a real clip frame if one is
    assigned, else on a neutral backdrop — so the Formatter shows the look live."""
    import copy
    W, H = 1080, 1920
    out_name = f"tprev_{uuid.uuid4().hex[:8]}.jpg"
    dst = str(config.TMP_DIR / out_name)

    overlay = None
    if not req.bare:
        tov = copy.deepcopy(req.template_opts or {})
        tov.setdefault("title", {})
        tov.setdefault("list", {})
        items = req.items or []
        tov["list"]["items"] = items
        tov["list"]["count"] = len(items) or tov["list"].get("count", 5)
        tov["list"]["highlight"] = req.highlight
        overlay = formatter.render(req.template, tov, str(config.TMP_DIR / f"tpv_{uuid.uuid4().hex[:8]}.png"))

    if req.clip:
        try:
            src = str(_clip_path(req.clip))
            opts = _to_opts(req.layout or LayoutReq(clip=req.clip))
            opts.overlay_png = overlay
            autolayout.make_preview_frame(src, opts, dst)
            return {"preview": f"/storage/tmp/{out_name}"}
        except Exception:
            pass  # fall back to the neutral backdrop
    # neutral vertical-gradient backdrop (+ overlay unless bare)
    from PIL import Image
    grad = Image.new("RGB", (1, 2)); grad.putpixel((0, 0), (30, 36, 48)); grad.putpixel((0, 1), (11, 13, 18))
    bg = grad.resize((W, H))
    if overlay:
        ov = Image.open(overlay).convert("RGBA")
        bg.paste(ov, (0, 0), ov)
    bg.save(dst, quality=88)
    return {"preview": f"/storage/tmp/{out_name}"}


@app.post("/api/formatter/animate")
def formatter_animate(req: FormatterAnimateReq):
    """Render a short animated preview mp4 (Intro / New item reveal + SFX) over
    the assigned clip's frame if one is given, else a neutral backdrop."""
    import copy
    from PIL import Image
    W, H = 1080, 1920
    tov = copy.deepcopy(req.template_opts or {})
    tov.setdefault("title", {})
    tov.setdefault("list", {})
    items = req.items or []
    tov["list"]["items"] = items
    tov["list"]["count"] = len(items) or tov["list"].get("count", 5)
    tov["list"]["highlight"] = req.highlight
    cards = tov.pop("clip_infos", None) or []
    if "clip_info" not in tov:
        tov["clip_info"] = cards[req.highlight] if 0 <= req.highlight < len(cards) else None

    # background still: the clip's composed frame (no template) or a neutral gradient
    bg_path = str(config.TMP_DIR / f"animbg_{uuid.uuid4().hex[:8]}.jpg")
    made_bg = False
    if req.clip:
        try:
            src = str(_clip_path(req.clip))
            opts = _to_opts(req.layout or LayoutReq(clip=req.clip))
            opts.overlay_png = None                 # animation draws the template itself
            autolayout.make_preview_frame(src, opts, bg_path)
            made_bg = True
        except Exception:
            made_bg = False
    if not made_bg:
        grad = Image.new("RGB", (1, 2)); grad.putpixel((0, 0), (30, 36, 48)); grad.putpixel((0, 1), (11, 13, 18))
        grad.resize((W, H)).save(bg_path, quality=90)

    out_name = f"anim_{uuid.uuid4().hex[:8]}.mp4"
    dst = str(config.TMP_DIR / out_name)
    try:
        info = animate.render_preview(
            req.template, tov, req.mode, req.motion, req.sound, req.volume,
            bg_path, dst, target_index=req.highlight, speed=req.speed)
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "")[-400:] if isinstance(e.stderr, str) else str(e)
        raise HTTPException(500, f"animation render failed: {detail}")
    return {"preview": f"/storage/tmp/{out_name}", "duration": info["duration"]}


def _apply_hook(inp: str, outp: str, cfg: "HookCfg", clip_path: str,
                layout=None) -> str:
    """Bake the Clip Hook (framed clip-moment) and overlay it onto the front of
    `inp`. `clip_path` is the resolved source file; `layout` is the rank's layout so
    the hook shows the EDITED clip (facecam + everything), not the raw source."""
    import os
    import shutil
    import tempfile
    fdir = tempfile.mkdtemp(prefix="hookframes_")
    edited = None
    try:
        tm = hook.timings(cfg.length, cfg.in_speed, cfg.out_speed)
        # Compose the EDITED vertical clip (facecam pane, fill, mousecam, …) for the
        # moment window, so the hook shows the clip exactly as the ranking does — not
        # the raw landscape source. The hook is a short framed cold-open, so global
        # speed / mid-clip cuts / whole-clip fade and the ranking overlay are dropped —
        # but the B-ROLL point effects are KEPT so the cold-open looks like the real
        # video (minus the UI): a flash/punch set to fire inside the moment window
        # plays in the box at its true time. `_marker_in_window` drops any marker that
        # falls outside the window (so nothing snaps to the box's first/last frame),
        # and `_effect_final_time` re-clocks the survivors to `t - moment` (speed=1,
        # no cuts), landing each punch exactly where its footage plays.
        src_for_hook, hook_moment = clip_path, cfg.moment
        if layout is not None:
            try:
                opts = _to_opts(layout)
                opts.trim_start = max(0.0, cfg.moment)
                opts.trim_end = max(0.0, cfg.moment) + tm["total"] + 1.0
                opts.speed = 1.0
                opts.cuts = None
                opts.cut_transitions = None
                opts.audio_fade_out = False
                opts.overlay_png = None
                edited = str(config.TMP_DIR / f"hookedit_{uuid.uuid4().hex[:8]}.mp4")
                autolayout.compose(clip_path, edited, opts)
                src_for_hook, hook_moment = edited, 0.0
            except Exception:
                src_for_hook, hook_moment = clip_path, cfg.moment   # fall back to raw
        meta = hook.render_frames(fdir, src_for_hook, moment=hook_moment,
                                  length=cfg.length, style=cfg.style, accent=cfg.accent,
                                  in_speed=cfg.in_speed, out_speed=cfg.out_speed)
        # the hook clip's OWN audio fades in with the pop-in (nothing before the box
        # appears), holds, then crossfades into the ranking audio as the box shrinks —
        # unless freeze_bg, where the background pauses on frame 0 for the whole hook
        hook.overlay(inp, outp, fdir, sound=cfg.sound, volume=cfg.volume,
                     duck_until=tm["hold"], duck_fade=meta["shrink"],
                     pop_in=meta["entrance"],
                     hook_clip=src_for_hook, hook_moment=hook_moment,
                     freeze=bool(cfg.freeze_bg), freeze_dur=tm["total"])
    finally:
        shutil.rmtree(fdir, ignore_errors=True)
        if edited and os.path.exists(edited):
            try:
                os.unlink(edited)
            except OSError:
                pass
    return outp


def _trim_head(inp: str, outp: str, dur: float) -> str:
    """Copy the first `dur` seconds of `inp` to `outp` (re-encoded, faststart)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", inp, "-t", f"{max(0.3, dur):.3f}",
         *autolayout._video_encode_args(), "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", outp],
        capture_output=True, text=True, check=True)
    return outp


def _run_formatter_build(job_id: str, req: "FormatterReq", dst: str):
    import copy
    JOBS[job_id]["status"] = "running"
    try:
        n = len(req.ranks)
        labels = [r.label for r in req.ranks]
        order = list(range(n))
        if req.order == "desc":
            order = order[::-1]
        # A custom play sequence (per-rank play numbers) wins over asc/desc, but
        # only when it's a valid permutation of every rank — otherwise fall back.
        if req.play_order and sorted(req.play_order) == list(range(n)):
            order = list(req.play_order)

        seg_files: list[str] = []
        joins: list[dict] = []
        revealed: set[int] = set()
        bake_anim = bool(req.anim and req.anim.enabled)
        only = req.only

        # Clip Hook cold-open: resolve its clip + total on-screen time. It overlays
        # the OPENING of the final video, and the first segment's reveal is held back
        # (`reveal_delay`) until it shrinks away — so it needs baked reveals to defer.
        hook_active = bool(req.hook and req.hook.on)
        hook_total = 0.0
        hook_clip = None
        hook_layout = None        # the rank's layout → hook shows the EDITED clip
        if hook_active:
            hook_total = hook.timings(req.hook.length, req.hook.in_speed,
                                      req.hook.out_speed)["total"]
            # source-clip identity: explicit, else the first-played rank's clip
            want = req.hook.clip or (req.ranks[order[0]].layout.clip if order else None)
            try:
                hook_clip = str(_clip_path(want)) if want else None
            except Exception:
                hook_clip = None
            # the layout that clip is edited with (facecam etc.), so the hook matches
            for r in req.ranks:
                if r.layout and r.layout.clip == want:
                    hook_layout = r.layout
                    break
            if hook_layout is None and order:
                hook_layout = req.ranks[order[0]].layout
            if not hook_clip:
                hook_active = False
        # Freeze mode PREPENDS a frozen intro (video restarts after), so the reveal
        # must NOT be pre-delayed; play mode delays the reveal until the hook shrinks.
        hook_delay = (hook_total if (hook_active and bake_anim
                                     and not (req.hook and req.hook.freeze_bg)) else 0.0)
        # Fast "preview the hook" mode: render ONLY the first-played segment (with the
        # reveal delayed) and trim to the hook + the first beat of the reveal.
        if req.hook_preview and hook_active and order:
            only = order[0]

        # ---- Phase 1: plan every segment (cheap; cumulative reveal state) --------
        # Walk the play order once to materialize each segment's progressive-reveal
        # overlay + cache key — the only work that depends on the running `revealed`
        # set. No rendering here, so the heavy renders (Phase 2) share no mutable
        # state and can run in parallel. The overlay/key produced per segment is
        # byte-for-byte what the old serial loop built.
        plans: list[dict] = []
        for pos, ri in enumerate(order):
            revealed.add(ri)
            # progressive-reveal template overlay for this segment
            tov = copy.deepcopy(req.template_opts or {})
            tov.setdefault("title", {})
            tov.setdefault("list", {})
            tov["list"]["count"] = n
            tov["list"]["items"] = [labels[k] if k in revealed else "" for k in range(n)]
            # full label set so the icon backdrop centres on a stable list box
            # (the pill list shrink-wraps; without this it would drift per segment)
            tov["list"]["full_items"] = list(labels)
            tov["list"]["highlight"] = -1          # motion is the emphasis, not a recolour
            # widgets follow the progressive reveal; the clip-info card is this rank's
            widgets = tov.get("widgets") or []
            tov["widgets"] = [widgets[k] if k in revealed and k < len(widgets) else None
                              for k in range(n)]
            cards = tov.pop("clip_infos", None) or []
            tov["clip_info"] = cards[ri] if ri < len(cards) else None
            # bonus call-out: pops on the intro segment; on later segments it is
            # gone if auto-hide is on, held static otherwise
            bonus = dict(tov.get("bonus") or {})
            if bonus.get("on"):
                bonus["show"] = "anim" if pos == 0 else (
                    "omit" if bonus.get("hide_on", True) else "static")
                tov["bonus"] = bonus

            layout = req.ranks[ri].layout
            src = str(_clip_path(layout.clip))
            mode = "intro" if pos == 0 else "new_item"
            cfg = ((req.anim.intro if pos == 0 else req.anim.new_item).model_dump()
                   if bake_anim else None)
            # A Clip Hook delays THIS segment's reveal (only pos 0) — fold it into the
            # cache key so a delayed segment never reuses an un-delayed cached render.
            seg_delay = hook_delay if pos == 0 else 0.0
            # Reuse an identical already-rendered segment (unchanged clip + layout +
            # reveal state) so editing one rank doesn't re-render the whole Top-N.
            key = _seg_cache_key(src, layout.model_dump(), bake_anim, mode, cfg,
                                 tov, req.template, ri)
            if seg_delay:
                key += f"_hd{seg_delay:.2f}"
            plans.append({"pos": pos, "ri": ri, "src": src, "layout": layout,
                          "tov": tov, "mode": mode, "cfg": cfg, "seg_delay": seg_delay,
                          "key": key})

            if pos < n - 1:                         # join leaving this segment
                j = req.ranks[ri].join
                v = (j.visual if j else "cut") or "cut"
                joins.append({
                    "visual": v,
                    "sound": (j.sound if j else "none") or "none",
                    "volume": (j.volume if j else 1.0),
                    "overlap": _JOIN_OVERLAP.get(v, autolayout.TRANSITION_DUR),
                })

        # Render one planned segment (reusing an identical cached render when present).
        # A pure function of its plan → safe to run on a worker thread; it issues the
        # exact same ffmpeg calls the old serial loop did, only the scheduling changes.
        def _render_segment(plan: dict) -> str:
            key = plan["key"]
            cached = _SEG_CACHE.get(key)
            if cached and os.path.exists(cached):
                return cached
            pos, src, tov = plan["pos"], plan["src"], plan["tov"]
            seg_dst = str(config.TMP_DIR / f"segcache_{key[:24]}.mp4")
            opts = _to_opts(plan["layout"])
            if bake_anim:
                # Compose without the overlay, then bake the reveal animation + its
                # SFX on top: Intro on the first segment, New item on the rest.
                # compose() still applies any Face Zoom punch-in to the VIDEO here
                # (overlay is None); the matching UI slide is handed to bake_reveal
                # so the reveal moves in lock-step with the zoom.
                # Defer the Subscribe CTA: the reveal (list items) is composited on
                # top of the segment by bake_reveal, so a CTA baked in compose would
                # be hidden under the list. Bake it AFTER the reveal instead, so the
                # Subscribe widget always renders above every other element.
                opts.overlay_png = None
                info = autolayout.probe(src)
                has_cta = bool(autolayout._normalize_subscribes(opts, info))
                raw = str(config.TMP_DIR / f"segraw_{job_id[:6]}_{pos}.mp4")
                autolayout.compose(src, raw, opts, skip_subscribe=True)
                fz_slide = autolayout.face_zoom_slides(opts, info, 1920)
                revealed_dst = (str(config.TMP_DIR / f"segrev_{job_id[:6]}_{pos}.mp4")
                                if has_cta else seg_dst)
                animate.bake_reveal(raw, revealed_dst, req.template, tov,
                                    plan["mode"], plan["cfg"], target_index=plan["ri"],
                                    face_zoom_y=fz_slide, reveal_delay=plan["seg_delay"])
                if has_cta:                       # CTA on top of the revealed list
                    autolayout._apply_subscribe(revealed_dst, seg_dst, opts, info)
                    autolayout._try_unlink(revealed_dst)
            else:
                opts.overlay_png = formatter.render(   # per-segment template PNG
                    req.template, tov,
                    str(config.TMP_DIR / f"fmt_{job_id[:6]}_{pos}.png"))
                autolayout.compose(src, seg_dst, opts)
            with _SEG_LOCK:
                _seg_cache_put(key, seg_dst)
            return seg_dst

        # ---- Single-clip preview: render ONLY the target rank's segment ----------
        # (still planned in play order above so its progressive-reveal state is right)
        if only is not None:
            import shutil
            JOBS[job_id]["stage"] = f"rendering rank {only + 1}"
            plan = next(p for p in plans if p["ri"] == only)
            seg_dst = _render_segment(plan)
            if req.hook_preview and hook_active and only == order[0]:
                # Stamp the hook onto the intro segment and trim to the cold-open
                # + the first beat of the reveal, so the preview is short + fast.
                JOBS[job_id]["stage"] = "hook cold-open"
                _apply_hook(seg_dst, dst, req.hook, hook_clip, layout=hook_layout)
                trimmed = str(config.TMP_DIR / f"hookprev_{job_id[:6]}.mp4")
                tail = min(autolayout.probe(dst).duration, hook_total + 1.6)
                _trim_head(dst, trimmed, tail)
                shutil.move(trimmed, dst)
            else:
                shutil.copyfile(seg_dst, dst)
            JOBS[job_id].update(status="done",
                                output=f"/storage/output/{Path(dst).name}",
                                only=only, finished=time.time())
            return

        # ---- Phase 2: render all segments (uncached ones concurrently) -----------
        # Independent renders → run on a small thread pool. Progress reflects the
        # fraction of segments finished (segments own 0–75% of the bar; the join +
        # hook + CTA + music tail owns the rest).
        results: dict[int, str] = {}
        done = 0
        JOBS[job_id].update(progress=0, stage=f"rendering segments (0/{n})")
        workers = min(_FMT_WORKERS, max(1, len(plans)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_render_segment, p): p for p in plans}
            for fut in as_completed(futs):
                p = futs[fut]
                results[p["pos"]] = fut.result()
                done += 1
                JOBS[job_id].update(
                    progress=int(done / len(plans) * 75),
                    stage=f"rendering segments ({done}/{n})")
        seg_files = [results[p["pos"]] for p in plans]

        JOBS[job_id].update(progress=78, stage="joining segments")
        if len(seg_files) == 1:
            import shutil
            shutil.copyfile(seg_files[0], dst)
        else:
            autolayout.join_segments(seg_files, joins, dst)

        # Clip Hook cold-open: stamp the framed clip-moment onto the opening (the
        # first segment's reveal is already delayed to start after it shrinks away).
        if hook_active:
            JOBS[job_id].update(progress=88, stage="hook cold-open")
            import shutil
            hooked = str(config.TMP_DIR / f"hooked_{job_id[:6]}.mp4")
            _apply_hook(dst, hooked, req.hook, hook_clip, layout=hook_layout)
            shutil.move(hooked, dst)

        # Subscribe CTA: the animated cursor-click widget, timed to FINISH as the
        # video ends (last X seconds). Baked as a PNG sequence in cta.py.
        if req.subscribe and req.subscribe.on:
            JOBS[job_id].update(progress=93, stage="subscribe CTA")
            import shutil
            import tempfile
            total = autolayout.probe(dst).duration
            fdir = tempfile.mkdtemp(prefix="ctaframes_")
            try:
                meta = cta.render_frames(
                    fdir, style=req.subscribe.style, size=req.subscribe.scale,
                    anim=req.subscribe.anim, dur=req.subscribe.dur,
                    cx=1080 * req.subscribe.x, cy=1920 * req.subscribe.y,
                    accent=req.subscribe.accent)
                start = max(0.0, total - meta["total"])   # end the CTA at the video end
                subbed = str(config.TMP_DIR / f"sub_{job_id[:6]}.mp4")
                cta.overlay(dst, subbed, fdir, start=start, click_t=meta["click_t"],
                            sound=req.subscribe.sound, volume=req.subscribe.volume)
                shutil.move(subbed, dst)
            finally:
                shutil.rmtree(fdir, ignore_errors=True)

        # Background-music bed: lay the chosen track under the finished render.
        credit = None
        if req.music and req.music.track:
            mpath = music.local_path(req.music.track)
            if mpath:
                JOBS[job_id].update(progress=97, stage="adding music")
                mixed = str(config.TMP_DIR / f"mix_{job_id[:6]}.mp4")
                music.mix_bed(dst, str(mpath), mixed,
                              music_volume=req.music.volume,
                              keep_clip_audio=req.music.keep_clip_audio,
                              clip_volume=req.music.clip_volume,
                              start=req.music.start,
                              fade_in=req.music.fade_in,
                              fade_out=req.music.fade_out)
                import shutil
                shutil.move(mixed, dst)
                credit = music.credit_for(req.music.track)

        JOBS[job_id].update(status="done", progress=100,
                            output=f"/storage/output/{Path(dst).name}",
                            credit=credit,               # paste-ready line (NCS) or None
                            finished=time.time())
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e), finished=time.time())


@app.post("/api/formatter/build")
def formatter_build(req: FormatterReq):
    ranks = [r for r in req.ranks if r.layout and r.layout.clip]
    if not ranks:
        raise HTTPException(400, "assign a clip to at least one rank")
    req = req.model_copy(update={"ranks": ranks})

    out_name = f"ranking_{req.template}_{uuid.uuid4().hex[:6]}.mp4"
    dst = str(config.OUTPUT_DIR / out_name)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "started": time.time(),
                    "kind": "formatter", "ranks": len(ranks)}
    threading.Thread(target=_run_formatter_build, args=(job_id, req, dst), daemon=True).start()
    return {"job": job_id}


# ----------------------------------------------------------------------------- #
# Clip packages — the clip-package agent's curated deliveries (see packages.py)
# ----------------------------------------------------------------------------- #
class PackageReq(BaseModel):
    prompt: str = ""
    count: int | None = None


class CritiqueReq(BaseModel):
    text: str


class SeenReq(BaseModel):
    seen: bool = True


class SelectReq(BaseModel):
    clip_ids: list[str] = []             # candidate ids to grab ([] = all)
    folder: str | None = None            # destination folder (defaults to package slug)


class AgentReq(BaseModel):
    prompt: str | None = None
    default_count: int | None = None


@app.get("/api/packages")
def packages_list():
    """All packages (newest first) + unseen count (drives the notification badge)."""
    return packages.list_packages()


@app.get("/api/packages/{pid}")
def packages_get(pid: str):
    pkg = packages.get_package(pid)
    if not pkg:
        raise HTTPException(404, "package not found")
    return pkg


@app.post("/api/packages/request")
def packages_request(req: PackageReq):
    """Ask the agent for a new package. Returns immediately; the offline builder
    fills it in on a background thread (status building → ready)."""
    return packages.request_package(req.prompt, req.count)


@app.post("/api/packages/{pid}/seen")
def packages_seen(pid: str, req: SeenReq):
    pkg = packages.mark_seen(pid, req.seen)
    if not pkg:
        raise HTTPException(404, "package not found")
    return {"seen": pkg["seen"]}


@app.post("/api/packages/{pid}/critique")
def packages_critique(pid: str, req: CritiqueReq):
    """Add a critique — appended to the package AND the agent's learnings."""
    pkg = packages.add_critique(pid, req.text)
    if not pkg:
        raise HTTPException(404, "package not found")
    return {"critiques": pkg.get("critiques", [])}


@app.delete("/api/packages/{pid}")
def packages_delete(pid: str):
    return {"deleted": packages.delete_package(pid)}


@app.post("/api/packages/agent")
def packages_agent(req: AgentReq):
    """Update the base agent's prompt / default clip count."""
    return packages.update_agent(prompt=req.prompt, default_count=req.default_count)


def _run_package_select(job_id: str, pid: str, req: "SelectReq"):
    JOBS[job_id]["status"] = "running"
    try:
        res = packages.select_clips(pid, req.clip_ids, req.folder)
        JOBS[job_id].update(status="done", finished=time.time(), **res)
    except Exception as e:
        JOBS[job_id].update(status="error", error=str(e), finished=time.time())


@app.post("/api/packages/{pid}/select")
def packages_select(pid: str, req: SelectReq):
    """Download the chosen candidates into a folder (background job). On done the
    job carries {folder, clips:[{path,title,id}], failed}, ready for the Formatter."""
    if not packages.get_package(pid):
        raise HTTPException(404, "package not found")
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "started": time.time(), "kind": "package_select"}
    threading.Thread(target=_run_package_select, args=(job_id, pid, req), daemon=True).start()
    return {"job": job_id}


# ----------------------------------------------------------------------------- #
# Saved projects — persist a Formatter edit so it can be resumed + re-rendered
# ----------------------------------------------------------------------------- #
# A project is the full front-end Formatter state (template styling, ranks with
# their per-clip layouts, play order, and animation settings) written as one
# JSON file under storage/projects/. The display name maps to a safe slug used
# as the filename; saving the same name overwrites.
class ProjectSaveReq(BaseModel):
    name: str
    state: dict                                # opaque AL snapshot: {tpl, fmt, anim}


def _project_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:60] or uuid.uuid4().hex[:8]


def _project_path(slug: str) -> Path:
    # guard against path traversal: only a bare slug maps to a file here
    p = (config.PROJECTS_DIR / f"{_project_slug(slug)}.json").resolve()
    if p.parent != config.PROJECTS_DIR.resolve():
        raise HTTPException(400, "bad project id")
    return p


@app.get("/api/projects")
def list_projects():
    """Saved projects, newest first (for the Resume list)."""
    out = []
    for p in sorted(config.PROJECTS_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        out.append({"slug": p.stem,
                    "name": data.get("name", p.stem),
                    "saved": data.get("saved", p.stat().st_mtime)})
    return out


@app.post("/api/projects")
def save_project(req: ProjectSaveReq):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    slug = _project_slug(name)
    payload = {"name": name, "slug": slug, "saved": time.time(), "state": req.state}
    _project_path(slug).write_text(json.dumps(payload, indent=2))
    return {"slug": slug, "name": name, "saved": payload["saved"]}


@app.get("/api/projects/{slug}")
def get_project(slug: str):
    p = _project_path(slug)
    if not p.exists():
        raise HTTPException(404, f"project not found: {slug}")
    return json.loads(p.read_text())


@app.delete("/api/projects/{slug}")
def delete_project(slug: str):
    p = _project_path(slug)
    if p.exists():
        p.unlink()
    return {"ok": True}


# ----------------------------------------------------------------------------- #
# Video Library — browse niche-named folders under the project root
# ----------------------------------------------------------------------------- #
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def _niche_folder(niche_id: str) -> Path | None:
    """Find a niche's library folder (matches its id or label, case-insensitive)."""
    names = {niche_id.lower()}
    for n in config.NICHES:
        if n["id"] == niche_id:
            names.add(n["label"].lower())
    for p in config.LIBRARY_DIR.iterdir():
        if p.is_dir() and p.name.lower() in names:
            return p
    return None


@app.get("/api/library")
def library():
    out = []
    for n in config.NICHES:
        folder = _niche_folder(n["id"])
        vids = []
        if folder:
            for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.suffix.lower() in _VIDEO_EXTS:
                    vids.append({
                        "name": p.name,
                        "size": p.stat().st_size,
                        "url": f"/api/library/file/{n['id']}/{p.name}",
                    })
        out.append({"id": n["id"], "label": n["label"], "videos": vids})
    return out


@app.get("/api/library/file/{niche}/{name}")
def library_file(niche: str, name: str):
    folder = _niche_folder(niche)
    if folder is None:
        raise HTTPException(404, f"niche folder not found: {niche}")
    p = (folder / Path(name).name).resolve()
    if folder.resolve() not in p.parents or not p.exists():
        raise HTTPException(404, f"video not found: {name}")
    return FileResponse(str(p))


@app.get("/api/output")
def list_output():
    items = []
    for p in sorted(config.OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix.lower() == ".mp4":
            items.append({"name": p.name, "url": f"/storage/output/{p.name}"})
    return items


# ----------------------------------------------------------------------------- #
# Static: storage files + frontend
# ----------------------------------------------------------------------------- #
app.mount("/storage", StaticFiles(directory=str(config.STORAGE)), name="storage")
app.mount("/assets", StaticFiles(directory=str(config.ASSETS)), name="assets")


@app.get("/")
def index():
    return FileResponse(str(config.FRONTEND / "index.html"))


app.mount("/", StaticFiles(directory=str(config.FRONTEND)), name="frontend")
