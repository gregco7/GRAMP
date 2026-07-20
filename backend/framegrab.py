"""
Cheap frame sampling for the Experimental engine's vision pass.

Given a clip URL, grab a few representative JPEG frames WITHOUT committing to a
full-quality download — just enough for `aibrain.analyze_clip` to read the HUD,
spot a facecam, and judge whether the footage is clean/croppable.

Strategy (crop-ready clips are short, so this stays cheap):
  - duration <= ~120s or unknown  -> yt-dlp a single low-res (<=360p) copy, then
    ffmpeg out N frames spread across the middle 80%.
  - longer                        -> yt-dlp two short low-res sections (~33% /
    ~66%) and pull a frame or two from each.

Frames land in a per-clip temp dir under storage/tmp; the caller deletes it after
the verdict. Everything is best-effort — any failure returns [] and the clip just
skips vision (its rule-based score still stands).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func

from . import config

_TMP = Path(config.TMP_DIR) / "_framegrab"


def _ydl_opts(out_tmpl: str, max_h: int, ranges=None) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True,
        "format": (f"bestvideo[height<=?{max_h}]+bestaudio/"
                   f"best[height<=?{max_h}]/best"),
        "merge_output_format": "mp4",
        "outtmpl": out_tmpl,
        "retries": 1, "fragment_retries": 1,
    }
    if ranges:
        opts["download_ranges"] = download_range_func(None, ranges)
        opts["force_keyframes_at_cuts"] = True
    return opts


def _dl(url: str, out_tmpl: str, max_h: int, ranges=None) -> Optional[Path]:
    try:
        with YoutubeDL(_ydl_opts(out_tmpl, max_h, ranges)) as y:
            info = y.extract_info(url, download=True)
            p = Path(y.prepare_filename(info))
        if not p.exists():
            mp4 = p.with_suffix(".mp4")
            if mp4.exists():
                p = mp4
        return p if p.exists() else None
    except Exception:
        return None


def _extract(video: Path, ts: float, dest: Path, width: int = 854) -> Optional[str]:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{max(0.0, ts):.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest),
             "-loglevel", "error"],
            timeout=30, check=True,
        )
        return str(dest) if dest.exists() and dest.stat().st_size > 0 else None
    except Exception:
        return None


def sample_frames(url: str, duration: Optional[float], n: int = 3,
                  max_h: int = 360) -> tuple[list[str], Optional[str]]:
    """Return (frame_paths, workdir). Caller must `cleanup(workdir)` when done.
    `frame_paths` is [] on failure."""
    if not url:
        return [], None
    work = _TMP / uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=True)
    dur = float(duration) if duration and duration > 0 else 0.0
    frames: list[str] = []

    try:
        if dur == 0.0 or dur <= 120.0:
            vid = _dl(url, str(work / "v.%(ext)s"), max_h)
            if not vid:
                return [], str(work)
            span = dur if dur > 0 else 30.0
            lo, hi = span * 0.12, span * 0.88
            if hi <= lo:
                lo, hi = 0.0, span
            for i in range(max(1, n)):
                t = lo + (hi - lo) * (i / max(1, n - 1)) if n > 1 else (lo + hi) / 2
                f = _extract(vid, t, work / f"f{i}.jpg")
                if f:
                    frames.append(f)
        else:
            # long clip: two short sections at ~1/3 and ~2/3
            marks = [dur * 0.33, dur * 0.66]
            for j, m in enumerate(marks):
                sec = _dl(url, str(work / f"s{j}.%(ext)s"), max_h,
                          ranges=[(m, m + 3.0)])
                if sec:
                    f = _extract(sec, 1.0, work / f"f{j}.jpg")
                    if f:
                        frames.append(f)
    except Exception:
        pass
    return frames, str(work)


def cleanup(workdir: Optional[str]) -> None:
    if not workdir:
        return
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass
