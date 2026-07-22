"""
Deterministic media gates for the Experimental searcher — the signals a static
frame + a title keyword can't carry.

Two things decide whether a Valorant clip is usable that live in the TIME domain
and are therefore invisible to `aibrain.analyze_clip` (which sees 1-3 frozen
frames) and to `clips._score` (which reads the title):

  * MONTAGE  — a compilation is many hard cuts stitched together. One continuous
               play has ~0 cuts. Measured as scene-change density (cuts / second)
               via ffmpeg's `select='gt(scene,…)'`.
  * MUSIC    — a music bed replaces the game audio the editor needs. NOTE: after
               calibrating against the shipped keepers, silence-ratio + crest
               factor were found NOT to separate a music bed from raw Valorant
               audio — raw gameplay is ALSO near-continuous (constant gunfire) and
               ALSO compressed by Twitch's pipeline (crest 4-8dB), so the audio
               heuristic false-dropped 4 keepers. The audio metrics are still
               computed (informational / future use) but are NOT a hard gate.
               Real single-clip music detection needs beat/tempo analysis (a new
               dep) — a Phase-2 decision. In practice music arrives WITH a montage,
               which the (validated) cut-density gate already catches.

The montage pass runs on the low-res clip `framegrab` ALREADY downloads for the vision pass —
no extra network, no new deps (ffmpeg is required by the app). Everything is
best-effort: any ffmpeg failure returns `analyzed=False` and the clip is NOT
gated on missing data (a gate never fires on absence of evidence).

Thresholds are CALIBRATED against the user's shipped keeper clips (see
scripts/calibrate_gates.py) so a known-good raw clip never trips a gate.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# Calibrated thresholds (see scripts/calibrate_gates.py — set from the keeper
# distribution so every shipped raw clip passes with margin).
# --------------------------------------------------------------------------- #
SCENE_THRESH = 0.40          # ffmpeg scene score for a "hard cut"
# Keeper ceiling was 0.25 cuts/sec (a multikill ace with in-game killcam cuts);
# a real beat-synced montage runs 0.4-2.0. 0.35 clears every keeper with margin.
MONTAGE_CUTS_PER_SEC = 0.35  # above this AND >= MONTAGE_MIN_CUTS -> compilation
MONTAGE_MIN_CUTS = 6         # in-game replays give a legit clip a few cuts

# Audio metrics for the EXPERIMENTAL, opt-in music-bed gate. These do NOT cleanly
# separate a music bed from raw Valorant audio (both are near-continuous + Twitch-
# compressed), so on the keeper set this gate false-drops ~4/21 (~19%). It is OFF by
# default and only fires when `gate(..., music_gate=True)`; use when a montage-heavy
# query is worth trading some good clips to strip the obvious music edits.
SILENCE_NOISE_DB = -30       # what counts as "silence"
SILENCE_MIN_DUR = 0.30       # seconds
MUSIC_MAX_SILENCE = 0.045    # music bed ~= no real silence gaps
MUSIC_MAX_CREST = 9.0        # dB; mastered music has a low peak-vs-RMS crest


def _run_ff(args: list[str], timeout: int = 40) -> str:
    """Run ffmpeg and return combined stderr (where the filters print). '' on error."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostats", "-hide_banner", *args, "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout,
        )
        return (proc.stderr or "") + (proc.stdout or "")
    except Exception:
        return ""


def _probe_dur(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _scene_cuts(path: str) -> Optional[int]:
    """Number of hard cuts (scene changes) in the clip, or None on failure."""
    txt = _run_ff(["-i", path, "-an",
                   "-vf", f"select='gt(scene,{SCENE_THRESH})',showinfo"])
    if not txt:
        return None
    # showinfo prints one line per selected (scene-change) frame.
    return len(re.findall(r"Parsed_showinfo.*pts_time", txt))


def _audio_stats(path: str) -> tuple[Optional[float], Optional[float]]:
    """Return (total_silence_seconds, crest_factor_dB) — either None on failure."""
    txt = _run_ff(["-i", path, "-vn",
                   "-af", (f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_DUR},"
                           "astats=metadata=0")])
    if not txt:
        return None, None
    sil = sum(float(m) for m in re.findall(r"silence_duration:\s*([0-9.]+)", txt))
    crest_vals = [float(m) for m in re.findall(r"Crest factor:\s*([0-9.]+)", txt)]
    crest = crest_vals[-1] if crest_vals else None  # last = Overall section
    return sil, crest


def analyze_media(path: str, duration: Optional[float] = None) -> dict:
    """Run the deterministic media analysis on a local video file.
    Returns raw metrics + verdicts. `analyzed=False` if ffmpeg couldn't read it."""
    if not path or not Path(path).exists():
        return {"analyzed": False}
    dur = float(duration) if duration and duration > 0 else _probe_dur(path)
    if dur <= 0:
        dur = 0.0

    cuts = _scene_cuts(path)
    silence, crest = _audio_stats(path)
    if cuts is None and silence is None:
        return {"analyzed": False}

    out: dict = {"analyzed": True, "dur": round(dur, 2)}

    # --- montage verdict ---------------------------------------------------- #
    if cuts is not None and dur > 0:
        cps = cuts / dur
        out["cut_count"] = cuts
        out["cuts_per_sec"] = round(cps, 4)
        out["is_montage"] = bool(cuts >= MONTAGE_MIN_CUTS and cps >= MONTAGE_CUTS_PER_SEC)
    else:
        out["is_montage"] = False

    # --- audio metrics + (experimental, opt-in) music-bed verdict ----------- #
    if silence is not None and dur > 0:
        ratio = min(1.0, silence / dur)
        out["silence_ratio"] = round(ratio, 4)
        out["crest_db"] = round(crest, 2) if crest is not None else None
        low_silence = ratio <= MUSIC_MAX_SILENCE
        low_crest = crest is not None and crest <= MUSIC_MAX_CREST
        # music bed = continuous audio (no real silence gaps) AND compressed dynamics
        out["has_music_bed"] = bool(low_silence and low_crest)
    else:
        out["has_music_bed"] = False

    return out


def gate(media: dict, music_gate: bool = False) -> Optional[str]:
    """Given an analyze_media() result, return a rejection reason string if the clip
    fails a hard gate, else None. Never gates on un-analyzed media.

    The montage (cut-density) gate is always on. The music-bed gate is EXPERIMENTAL
    and only applied when `music_gate=True` — the audio metrics don't reliably
    separate a music bed from raw game audio, so it costs ~19% of good clips."""
    if not media or not media.get("analyzed"):
        return None
    if media.get("is_montage"):
        return "montage"
    if music_gate and media.get("has_music_bed"):
        return "music bed"
    return None
