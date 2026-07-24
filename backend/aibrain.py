"""
The Experimental engine's "brain" — a thin wrapper over the local `claude` CLI
(Claude Code headless), so parsing / curation / vision draw on the user's Claude
**Max subscription** instead of per-token API billing.

Three jobs:
  1. parse_query(text)      natural language  -> structured search intent
  2. curate(intent, cands)  metadata pool     -> ranked picks + a "why" per clip
  3. analyze_clip(frames)   sampled JPEG frame(s) -> vision verdict
                            (facecam? agent/map/weapon? center clutter? edited?)

Everything here is BEST-EFFORT and degrades gracefully: if the CLI is missing,
un-authenticated, times out, or returns junk, each function returns None (or a
neutral verdict) and the orchestrator falls back to its rule-based path. The
engine must never hard-depend on the LLM.

Why the CLI and not the SDK: the `claude` binary is already authenticated with
the user's Max plan on this machine; shelling out to it (`claude -p --json-schema
…`, `--allowedTools Read` for local frames) reuses that auth with zero API cost.
Calls are slow-ish (~10-30s cold), so the orchestrator runs them inside a
background job with progress and fans vision out concurrently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# Fast + cheap for parse/vision; override per-call. Sonnet is available for the
# heavier curation pass if desired.
MODEL_FAST = os.environ.get("CTM_AI_MODEL", "claude-haiku-4-5-20251001")
MODEL_CURATE = os.environ.get("CTM_AI_CURATE_MODEL", MODEL_FAST)

_PROJECT = Path(__file__).resolve().parent.parent


def _claude_bin() -> Optional[str]:
    return shutil.which("claude") or next(
        (p for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude") if Path(p).exists()),
        None,
    )


def available() -> bool:
    return _claude_bin() is not None


def _subprocess_env() -> dict:
    """Env for the `claude` subprocess: inherit the parent, then layer in the user's
    saved ANTHROPIC_API_KEY (Settings screen) when the CLI isn't already
    subscription-authed via the environment. Lets a cloner "insert their Claude key"
    without an interactive Claude Code login."""
    env = os.environ.copy()
    if not env.get("ANTHROPIC_API_KEY"):
        try:
            from . import config
            key = (config.load_settings().get("anthropic_api_key") or "").strip()
            if key:
                env["ANTHROPIC_API_KEY"] = key
        except Exception:
            pass
    return env


def _extract_json(text: str):
    """Parse the CLI stdout into JSON — tolerant of stray prose / code fences."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # strip ```json fences
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # first {...} or [...] block
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _run(prompt: str, schema: Optional[dict] = None, model: str = MODEL_FAST,
         system: str = "", allow_read: bool = False, add_dirs: Optional[list[str]] = None,
         timeout: int = 90):
    """Invoke `claude -p` once. Returns parsed JSON (if `schema`) / raw text, or
    None on any failure. Never raises."""
    binp = _claude_bin()
    if not binp:
        return None
    cmd = [binp, "-p", prompt, "--model", model]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    if system:
        cmd += ["--append-system-prompt", system]
    if allow_read:
        cmd += ["--allowedTools", "Read"]
        for d in (add_dirs or []):
            cmd += ["--add-dir", d]
    try:
        proc = subprocess.run(
            cmd, cwd=str(_PROJECT), capture_output=True, text=True,
            timeout=timeout, env=_subprocess_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if schema is not None:
        return _extract_json(out)
    return out or None


# --------------------------------------------------------------------------- #
# 1) Query parsing
# --------------------------------------------------------------------------- #
_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "creator": {"type": "string", "description": "streamer/pro handle, '' if none"},
        "agents": {"type": "array", "items": {"type": "string"}},
        "maps": {"type": "array", "items": {"type": "string"}},
        "weapons": {"type": "array", "items": {"type": "string"}},
        "plays": {"type": "array", "items": {"type": "string"},
                  "description": "e.g. ace, clutch, 1v5, flick, wallbang"},
        "orgs": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "extra free-text search terms"},
        "recency_days": {"type": "integer", "description": "0 = any time"},
        "min_views": {"type": "integer"},
        "duration_max": {"type": "integer", "description": "seconds, 0 = any"},
        "sources": {"type": "array", "items": {"type": "string"},
                    "description": "subset of youtube/twitch/reddit, [] = all"},
        "want_facecam": {"type": "boolean"},
        "pro_play": {"type": "boolean", "description": "true if VCT/pro-match specific"},
        "search_terms": {"type": "string",
                         "description": "one concise YouTube search string for this intent"},
    },
    "required": ["creator", "search_terms", "plays", "keywords"],
}

_PARSE_SYS = (
    "You parse VALORANT clip-search requests for a YouTube Shorts tool. Extract "
    "structured filters. Agents/maps/weapons/orgs must be real VALORANT names, "
    "canonicalized (Title Case: Jett, Ascent, Operator, Sentinels). Interpret "
    "vague recency ('recent'~30, 'lately'~14, 'this year'~365; else 0). Keep "
    "search_terms tight and game-relevant. Omit anything not implied by the query."
)


def parse_query(text: str, model: str = MODEL_FAST) -> Optional[dict]:
    """Natural-language query -> structured intent dict (see _PARSE_SCHEMA)."""
    text = (text or "").strip()
    if not text:
        return None
    res = _run(f"Parse this VALORANT clip search request:\n\n{text}",
               schema=_PARSE_SCHEMA, model=model, system=_PARSE_SYS, timeout=45)
    if not isinstance(res, dict):
        return None
    # normalize shape
    for k in ("agents", "maps", "weapons", "plays", "orgs", "keywords", "sources"):
        v = res.get(k)
        res[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
    res["creator"] = str(res.get("creator") or "").strip()
    res["search_terms"] = str(res.get("search_terms") or text).strip()
    for k in ("recency_days", "min_views", "duration_max"):
        try:
            res[k] = int(res.get(k) or 0)
        except Exception:
            res[k] = 0
    res["want_facecam"] = bool(res.get("want_facecam"))
    res["pro_play"] = bool(res.get("pro_play"))
    return res


# --------------------------------------------------------------------------- #
# 2) Vision — analyze sampled frames of one clip
# --------------------------------------------------------------------------- #
_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_valorant_gameplay": {"type": "boolean",
            "description": "true only if these are real VALORANT match frames (HUD, first-person)"},
        "facecam": {"type": "boolean", "description": "a streamer webcam/facecam is overlaid"},
        "facecam_corner": {"type": "string",
            "description": "top-left/top-right/bottom-left/bottom-right/none"},
        "agent": {"type": "string"},
        "map": {"type": "string"},
        "weapon": {"type": "string"},
        "center_clutter": {"type": "boolean",
            "description": "large edited text/graphics/sponsor overlay covering the CENTER (not normal HUD or small corner banners)"},
        "edited": {"type": "boolean",
            "description": "looks like an edited montage: added captions, zoom/shake effects, meme overlays"},
        "clippable": {"type": "boolean",
            "description": "clean landscape gameplay a creator could crop as-is (minimal center clutter)"},
        "confidence": {"type": "number", "description": "0..1"},
        "notes": {"type": "string", "description": "one short phrase"},
    },
    "required": ["is_valorant_gameplay", "facecam", "clippable", "center_clutter", "edited"],
}

_VISION_SYS = (
    "You are a VALORANT footage QA checker for a Shorts editor. You are shown 1-3 "
    "frames sampled from one clip. Judge the CLIP as a whole. 'Clippable' means "
    "clean 16:9 landscape gameplay with the play readable and no big edited "
    "graphics/text over the center — normal game HUD and a small corner facecam are "
    "FINE and do not count as clutter. Identify agent/map/weapon from the HUD only "
    "if clearly visible; otherwise leave blank. Be strict about 'edited'."
)

_NEUTRAL_VERDICT = {
    "is_valorant_gameplay": True, "facecam": False, "facecam_corner": "none",
    "agent": "", "map": "", "weapon": "", "center_clutter": False,
    "edited": False, "clippable": True, "confidence": 0.0, "notes": "not analyzed",
}


def analyze_clip(frame_paths: list[str], title: str = "", model: str = MODEL_FAST,
                 timeout: int = 90) -> dict:
    """Vision verdict for one clip from its sampled frames. Returns a dict with the
    _VISION_SCHEMA fields; on any failure returns a NEUTRAL verdict (never blocks
    a clip just because the LLM was unavailable). `analyzed` flags real results."""
    frames = [f for f in (frame_paths or []) if f and Path(f).exists()]
    if not frames:
        return {**_NEUTRAL_VERDICT, "analyzed": False}
    dirs = sorted({str(Path(f).resolve().parent) for f in frames})
    listing = "\n".join(f"- {Path(f).resolve()}" for f in frames)
    prompt = (
        f"Read these {len(frames)} frame image(s) from one VALORANT clip"
        + (f' titled \"{title}\"' if title else "")
        + f" and judge it:\n{listing}"
    )
    res = _run(prompt, schema=_VISION_SCHEMA, model=model, system=_VISION_SYS,
               allow_read=True, add_dirs=dirs, timeout=timeout)
    if not isinstance(res, dict):
        return {**_NEUTRAL_VERDICT, "analyzed": False}
    out = {**_NEUTRAL_VERDICT, **res, "analyzed": True}
    for k in ("is_valorant_gameplay", "facecam", "center_clutter", "edited", "clippable"):
        out[k] = bool(out.get(k))
    try:
        out["confidence"] = float(out.get("confidence") or 0.0)
    except Exception:
        out["confidence"] = 0.0
    return out


# --------------------------------------------------------------------------- #
# 3) Curation — rank + reason over a metadata pool
# --------------------------------------------------------------------------- #
_CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer", "description": "candidate index"},
                    "score": {"type": "number", "description": "0..1 clippability/fit"},
                    "reason": {"type": "string", "description": "short why-picked"},
                },
                "required": ["i", "score", "reason"],
            },
        },
    },
    "required": ["picks"],
}

_CURATE_SYS = (
    "You curate VALORANT clips for a YouTube Shorts maker. Prefer SHORT (10-90s), "
    "self-contained single plays (an ace/clutch/flick) from raw sources over long "
    "montages/VODs. Reward on-topic match to the user's intent. Penalize obvious "
    "compilations, reactions, tutorials. Give each kept candidate a 0..1 score and "
    "a terse reason. Return only candidates worth keeping, best first."
)


def curate(intent_text: str, candidates: list[dict], model: str = MODEL_CURATE,
           timeout: int = 90) -> Optional[dict]:
    """Rank candidates by fit to `intent_text`. Returns {index: {score, reason}}
    or None on failure. Candidates are passed as compact metadata (no downloads)."""
    if not candidates:
        return None
    rows = []
    for i, c in enumerate(candidates):
        rows.append({
            "i": i,
            "title": (c.get("title") or "")[:120],
            "dur": c.get("duration"),
            "views": c.get("view_count"),
            "src": c.get("source"),
            "chan": (c.get("channel") or "")[:40],
        })
    prompt = (
        f"User wants: {intent_text}\n\n"
        f"Candidates (JSON):\n{json.dumps(rows, ensure_ascii=False)}\n\n"
        "Return the picks worth keeping, best first."
    )
    res = _run(prompt, schema=_CURATE_SCHEMA, model=model, system=_CURATE_SYS, timeout=timeout)
    if not isinstance(res, dict):
        return None
    out: dict[int, dict] = {}
    for p in res.get("picks") or []:
        try:
            idx = int(p["i"])
        except Exception:
            continue
        if 0 <= idx < len(candidates):
            try:
                sc = float(p.get("score") or 0.0)
            except Exception:
                sc = 0.0
            out[idx] = {"score": sc, "reason": str(p.get("reason") or "").strip()}
    return out or None
