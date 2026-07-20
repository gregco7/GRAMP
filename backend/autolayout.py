"""
Auto-Layout engine.

Takes a landscape gameplay/stream clip that has a facecam embedded somewhere
(usually a corner) and reflows it into a 9:16 vertical video:

    +---------------------+
    |      FACECAM        |   top  35%  -> facecam shown in full (contain / fit)
    |   (fully visible)   |
    +---------------------+
    |                     |
    |      GAMEPLAY       |   bottom 65% -> cropped to fill (cover, never stretched)
    |                     |
    +---------------------+

The hard part is *finding* the facecam. We sample frames across the clip, run a
face detector, and cluster the detections to a stable region. That region is then
padded out to a plausible webcam rectangle. The caller may also pass an explicit
facecam rect (from the UI) to skip detection entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

_MODEL_PATH = str(Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx")


# ----------------------------------------------------------------------------- #
# Data types
# ----------------------------------------------------------------------------- #
@dataclass
class Rect:
    """A rectangle in source-video pixel coordinates."""
    x: int
    y: int
    w: int
    h: int

    def clamp(self, W: int, H: int) -> "Rect":
        x = max(0, min(self.x, W - 1))
        y = max(0, min(self.y, H - 1))
        w = max(1, min(self.w, W - x))
        h = max(1, min(self.h, H - y))
        return Rect(x, y, w, h)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float


@dataclass
class Transition:
    """A transition applied at a cut's join point (where footage resumes).

    The special visual "speedup" is not a join effect: it keeps the cut's span
    (instead of removing it) and plays it faster by `speed` — a per-region speed
    ramp. sound/visual/volume are ignored in that mode."""
    sound: str = "none"    # SFX key (see SFX_CATALOG) or "none"
    visual: str = "none"   # visual key (see TRANSITION_CATALOG) or "none"
    volume: float = 1.0    # SFX gain multiplier (0..2)
    speed: float = 2.0     # region speed-up factor (only when visual == "speedup")


# Overlap (seconds) consumed by a visual swipe / audio cross-fade at a join.
TRANSITION_DUR = 0.5
FRAME_RATE = 30  # only referenced symbolically in select/setpts expressions
_WHOOSH_SR = 48000


# --- H.264 encoder ------------------------------------------------------------
# Prefer Apple's hardware encoder (h264_videotoolbox) when available: on Apple
# Silicon it's several times faster than libx264 and offloads the CPU, at a
# generous bitrate the quality is visually indistinguishable for this content.
# Falls back to libx264 (veryfast/crf20) on machines without VideoToolbox.
_H264_BITRATE = "12M"   # target for 1080x1920; high quality, hardware-friendly


def _has_videotoolbox() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True).stdout
        return "h264_videotoolbox" in out
    except Exception:
        return False


_USE_VT = _has_videotoolbox()


def _video_encode_args() -> list[str]:
    """ffmpeg output args for the video codec (hardware if available)."""
    if _USE_VT:
        return ["-c:v", "h264_videotoolbox", "-b:v", _H264_BITRATE,
                "-profile:v", "high", "-allow_sw", "1", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]

# --- Visual transitions -------------------------------------------------------
# Eased directional pushes ("swipes") are built with an overlay whose x-offset
# follows an easing curve (see _push_chain) — this ffmpeg build can't ease
# xfade, and its custom-expr can't translate pixels, so xfade slides look linear
# and cheap. The overlay push gives the smooth CapCut-style motion instead.
# Legacy keys slideleft/slideright are aliased to the eased swipes.
_SWIPE_VISUALS = {"swipeleft", "swiperight", "slideleft", "slideright"}
_SWIPE_ALIAS = {"slideleft": "swipeleft", "slideright": "swiperight"}

# Formatter segment joins (GET /api/formatter/meta). `cut` is the default. Swipes
# are the eased overlay push (_push_chain); the rest are ffmpeg xfade built-ins,
# which look fine without easing (they aren't translations). `overlap` seconds are
# consumed from each side of the join.
JOIN_CATALOG = [
    {"key": "cut",        "label": "Hard cut",           "overlap": 0.0},
    {"key": "swipeleft",  "label": "Swipe Left",         "overlap": 0.5},
    {"key": "swiperight", "label": "Swipe Right",        "overlap": 0.5},
    {"key": "fade",       "label": "Crossfade",          "overlap": 0.5},
    {"key": "fadeblack",  "label": "Fade thru black",    "overlap": 0.5},
    {"key": "dissolve",   "label": "Dissolve",           "overlap": 0.5},
    {"key": "wipeleft",   "label": "Wipe Left",          "overlap": 0.5},
    {"key": "wiperight",  "label": "Wipe Right",         "overlap": 0.5},
    {"key": "wipeup",     "label": "Wipe Up",            "overlap": 0.5},
    {"key": "circleopen", "label": "Circle Open",        "overlap": 0.6},
    {"key": "radial",     "label": "Radial",             "overlap": 0.6},
    {"key": "pixelize",   "label": "Pixelize",           "overlap": 0.5},
    {"key": "smoothleft", "label": "Smooth Left",        "overlap": 0.5},
]
_XFADE_JOINS = {j["key"] for j in JOIN_CATALOG} - {"cut", "swipeleft", "swiperight"}

# Catalog surfaced to the UI (GET /api/transitions). `overlap` drives how much
# footage the effect consumes at the join (0 = instant cut with a sound only).
TRANSITION_CATALOG = [
    {"key": "none",       "label": "None (hard cut)", "overlap": 0.0},
    {"key": "swipeleft",  "label": "Swipe Left",      "overlap": TRANSITION_DUR},
    {"key": "swiperight", "label": "Swipe Right",     "overlap": TRANSITION_DUR},
    # Not a join effect: keeps the cut's span and plays it faster (per-region
    # speed ramp). The frontend shows a speed slider instead of sound/vol.
    {"key": "speedup",    "label": "Speed up",        "overlap": 0.0, "is_speed": True},
]

# Bundled transition SFX (see assets/sfx/README.md). `key` is the value sent by
# the UI; a missing file falls back to a synthesized whoosh. `preview` is the
# static URL the UI plays for auditioning.
_SFX_DIR = Path(__file__).resolve().parent.parent / "assets" / "sfx"
SFX_CATALOG = [
    {"key": "whoosh",      "label": "Whoosh",        "category": "Whooshes", "file": "whoosh.wav"},
    {"key": "whoosh-soft", "label": "Whoosh (soft)", "category": "Whooshes", "file": "whoosh-soft.wav"},
    {"key": "cinematic",   "label": "Cinematic",     "category": "Whooshes", "file": "cinematic.wav"},
    {"key": "pop",         "label": "Pop",           "category": "Impacts",  "file": "pop.wav"},
    {"key": "boom",        "label": "Boom",          "category": "Impacts",  "file": "boom.wav"},
    {"key": "riser",       "label": "Riser",         "category": "Risers",   "file": "riser.wav"},
]
_SFX_FILES = {s["key"]: s["file"] for s in SFX_CATALOG}

_SFX_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def _prettify_sfx(stem: str) -> str:
    """Turn a filename stem into a readable dropdown label."""
    import re
    s = re.sub(r"[_\-]+", " ", stem).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:1].upper() + s[1:]) if s else stem


def scan_sfx_folder():
    """Live list of every sound effect in assets/sfx, sorted by name. `key` ==
    filename, so the render resolves the chosen file directly. This is the
    "combined SFX" list shared by the SFX B-roll and the bonus-title entrance."""
    if not _SFX_DIR.is_dir():
        return []
    out = []
    for p in sorted(_SFX_DIR.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in _SFX_EXTS:
            out.append({"key": p.name, "label": _prettify_sfx(p.stem), "file": p.name})
    return out


def effect_sounds(effect: dict):
    """Sound choices for a B-roll effect. Folder-backed effects (SFX) are scanned
    live so newly dropped files appear; others use their static `sounds` list."""
    if effect.get("sfx_dir"):
        return scan_sfx_folder()
    return effect.get("sounds", [])


def combined_sfx_path(key: str) -> Optional[Path]:
    """Resolve a sound key from the combined SFX list (assets/sfx) to a file.
    Accepts a bundled catalog key (whoosh, boom, …) or a raw filename."""
    if not key or key == "none":
        return None
    p = _sfx_path(key)                       # bundled SFX_CATALOG key?
    if p is not None:
        return p
    p = _SFX_DIR / Path(key).name            # .name guards against traversal
    return p if p.exists() else None


# --- B-roll effects (timeline "moments") --------------------------------------
# Drag-and-drop point effects the editor drops onto the trim bar. Each fires at a
# single source timestamp for a short, eased duration: a per-effect colour+blur
# flash on the video and a one-shot SFX on the audio. This is the extensible seam
# — add an entry here (+ optionally an sfx file) and it surfaces at /api/fx and in
# the editor's B-roll tray automatically. Unlike transition SFX (which live at cut
# joins), effects are placed freely anywhere on the clip. Rendered by
# `_apply_effects()` as a second pass so the timeline maths stay simple.
#   flash_dur : DEFAULT seconds the visual flash lasts (eased in/out around the marker);
#               each placed marker can override it (dur_min..dur_max) via a slider
#   dur_min/dur_max : slider bounds for the per-marker flash duration
#   blur      : peak gaussian sigma at the flash's centre (0 = no blur)
#   tint      : (r,g,b) 0..1 multipliers for the tinted+blurred flash copy
#   peak      : peak opacity of the flash overlay (0..1)
#   sounds    : selectable one-shot SFX (first = default); each {key,label,file in assets/sfx/}
#   sound     : fallback default filename (mirrors sounds[0].file)
#   lead      : seconds the flash starts BEFORE the marker (align punch to sound attack)
EFFECTS_CATALOG = [
    {"key": "money", "label": "Money", "emoji": "💸", "category": "Hype",
     "flash_dur": 0.64, "dur_min": 0.25, "dur_max": 1.6,
     "blur": 16.0, "tint": (0.30, 1.35, 0.45), "peak": 0.52, "lead": 0.06,
     "sounds": [
         {"key": "cash_1", "label": "Ka-ching", "file": "cash_1.wav"},
         {"key": "cash",   "label": "Cash register", "file": "cash.wav"},
     ],
     "sound": "cash_1.wav",
     "hint": "Cha-ching — green blur flash + cash-register sound"},
    # Sound-only moment: no visual flash (peak 0), just a one-shot SFX. Its sound
    # dropdown is scanned LIVE from assets/sfx (see scan_sfx_folder), so any file
    # dropped there is selectable. `sfx_dir` flags the folder-backed resolution
    # path in _normalize_effects / _apply_effects (sound key == filename).
    {"key": "sfx", "label": "SFX", "emoji": "🔊", "category": "Sound",
     "flash_dur": 0.4, "dur_min": 0.15, "dur_max": 2.0,
     "blur": 0.0, "tint": (1.0, 1.0, 1.0), "peak": 0.0, "lead": 0.03,
     "sounds": [], "sound": None, "sfx_dir": _SFX_DIR,
     "hint": "Drop a sound effect anywhere on the clip — pick any sound from your SFX library"},
    # Branded animated CTA (NOT a tint flash): the "Subscribe CTA" — a mouse cursor
    # slides in, clicks a YouTube-red Subscribe button (it flips to "Subscribed" with
    # a ripple + gold sparkles + one-shot SFX). Baked as a PNG sequence in cta.py.
    # The `overlay` key routes it out of the tint-flash path (_normalize_effects skips
    # it) into _normalize_subscribes / _apply_subscribe, which bakes + overlays it.
    {"key": "subscribe", "label": "Subscribe CTA", "emoji": "🔔", "category": "Branding",
     "flash_dur": 4.0, "dur_min": 1.0, "dur_max": 10.0,
     "blur": 0.0, "tint": (1.0, 1.0, 1.0), "peak": 1.0, "lead": 0.0,
     "overlay": "subscribe",
     "styles": [{"key": "classic", "label": "Classic"}, {"key": "card", "label": "Card"}],
     "style": "classic",
     "vd_accent": "#ff0033",                  # VD mark ring + "D" colour (default red)
     "scale": 1.0, "scale_min": 0.5, "scale_max": 2.0,
     "params": [{"key": "anim", "label": "Click", "min": 0.3, "max": 2.5,
                 "step": 0.05, "default": 0.7, "fmt": "num"}],
     "sounds": [{"key": "pop", "label": "Pop", "file": "pop.wav"},
                {"key": "whoosh", "label": "Whoosh", "file": "whoosh.wav"},
                {"key": "boom", "label": "Boom", "file": "boom.wav"},
                {"key": "none", "label": "None", "file": None}],
     "sound": "pop",
     "hint": "Animated Subscribe CTA — cursor clicks the button, it flips to "
             "Subscribed with a sparkle burst + sound"},
    # Motion moment (NOT a tint flash / NOT a graphic chip): an eased "punch-in"
    # toward the facecam, while the template UI slides down (locked to the facecam's
    # new height) out of the way. The `motion` key routes it out of BOTH the tint-flash
    # path (_normalize_effects skips it) AND the overlay-chip path into
    # _normalize_face_zoom / _apply_face_zoom, which zooms the composed video and
    # slides the (deferred) overlay independently so every element stays in line.
    # `params` are the per-marker adjustable sliders (surfaced by /api/fx + the
    # editor B-roll row); each {key,label,min,max,step,default,fmt in pct|num}.
    {"key": "face_zoom", "label": "Face Zoom", "emoji": "🔎", "category": "Motion",
     "flash_dur": 0.9, "dur_min": 0.3, "dur_max": 3.0,
     "blur": 0.0, "tint": (1.0, 1.0, 1.0), "peak": 0.0, "lead": 0.0,
     "motion": "face_zoom",
     "sounds": [
         {"key": "whoosh",      "label": "Whoosh",      "file": "whoosh.wav"},
         {"key": "whoosh-soft", "label": "Whoosh soft", "file": "whoosh-soft.wav"},
         {"key": "cinematic",   "label": "Cinematic",   "file": "cinematic.wav"},
         {"key": "none",        "label": "None",        "file": None},
     ],
     "sound": "whoosh.wav",
     "params": [
         {"key": "grow", "label": "Facecam", "min": 0.36, "max": 0.66, "step": 0.02, "default": 0.50, "fmt": "pct"},
     ],
     "hint": "Grows the facecam pane to this share of the frame (gameplay gives way below), the title/list slide down to stay on the seam, then it eases back"},
]
_EFFECTS = {e["key"]: e for e in EFFECTS_CATALOG}


# ----------------------------------------------------------------------------- #
# Probing
# ----------------------------------------------------------------------------- #
# ffprobe is deterministic for a given file, but a single render calls probe() on
# the same clip many times (compose + effects + face-zoom + join, plus main.py).
# Memoize on (path, mtime, size) so a rebuilt file busts its own entry. Thread-safe
# for the parallel Formatter renders; only the dict ops hold the lock (never the
# subprocess). Returns identical data — this is pure caching, output is unchanged.
_PROBE_CACHE: "dict[tuple, VideoInfo]" = {}
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE_CAP = 256


def probe(path: str) -> VideoInfo:
    """Read basic stream info with ffprobe (memoized by path + mtime + size)."""
    ckey = None
    try:
        st = os.stat(path)
        ckey = (path, st.st_mtime_ns, st.st_size)
        with _PROBE_LOCK:
            hit = _PROBE_CACHE.get(ckey)
        if hit is not None:
            return hit
    except OSError:
        ckey = None

    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,duration",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    width = int(stream["width"])
    height = int(stream["height"])

    fr = stream.get("avg_frame_rate", "0/1")
    try:
        num, den = fr.split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    except Exception:
        fps = 30.0

    duration = 0.0
    for src in (stream.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(src)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    info = VideoInfo(width=width, height=height, duration=duration, fps=fps or 30.0)
    if ckey is not None:
        with _PROBE_LOCK:
            _PROBE_CACHE[ckey] = info
            if len(_PROBE_CACHE) > _PROBE_CACHE_CAP:
                _PROBE_CACHE.pop(next(iter(_PROBE_CACHE)))
    return info


# ----------------------------------------------------------------------------- #
# Facecam detection
# ----------------------------------------------------------------------------- #
def _make_detector(w: int, h: int) -> "cv2.FaceDetectorYN":
    return cv2.FaceDetectorYN.create(
        _MODEL_PATH, "", (w, h),
        score_threshold=0.6, nms_threshold=0.3, top_k=50,
    )


def detect_facecam(path: str, samples: int = 24) -> Optional[Rect]:
    """
    Sample frames across the clip, detect faces (YuNet), and infer the facecam
    rectangle.

    Returns a Rect in source pixels, or None if no stable face was found.
    """
    info = probe(path)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None

    total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    if not total or total <= 0:
        total = max(1, info.fps * max(info.duration, 1.0))

    detector = _make_detector(info.width, info.height)

    faces: list[tuple[int, int, int, int]] = []
    for i in range(samples):
        pos = (i + 0.5) / samples
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * pos))
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        if frame.shape[1] != info.width or frame.shape[0] != info.height:
            detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, dets = detector.detect(frame)
        if dets is None:
            continue
        min_side = min(info.width, info.height) * 0.05
        for d in dets:
            x, y, w, h = d[0], d[1], d[2], d[3]
            if w >= min_side and h >= min_side:
                faces.append((int(x), int(y), int(w), int(h)))

    cap.release()

    if not faces:
        return None

    arr = np.array(faces, dtype=float)
    # cluster by center: keep faces near the dominant location (the facecam is
    # stationary; stray game-character faces move around and get dropped)
    centers = arr[:, :2] + arr[:, 2:] / 2.0
    median_c = np.median(centers, axis=0)
    dist = np.linalg.norm(centers - median_c, axis=1)
    keep = arr[dist < max(info.width, info.height) * 0.12]
    if len(keep) == 0:
        keep = arr

    # median face box for stability
    fx, fy, fw, fh = np.median(keep, axis=0)

    # Expand the face box into a plausible webcam frame. A webcam usually shows
    # head + shoulders with margin, so pad generously (esp. below the face).
    pad_x = fw * 0.9
    pad_top = fh * 0.7
    pad_bottom = fh * 1.4
    rx = fx - pad_x
    ry = fy - pad_top
    rw = fw + pad_x * 2
    rh = fh + pad_top + pad_bottom

    return Rect(int(rx), int(ry), int(rw), int(rh)).clamp(info.width, info.height)


def detect_facecam_corner(rect: Rect, info: VideoInfo) -> str:
    """Human-readable corner label for the detected facecam (for the UI)."""
    cx = rect.x + rect.w / 2
    cy = rect.y + rect.h / 2
    vert = "top" if cy < info.height / 2 else "bottom"
    horiz = "left" if cx < info.width / 2 else "right"
    return f"{vert}-{horiz}"


# ----------------------------------------------------------------------------- #
# Composition
# ----------------------------------------------------------------------------- #
def _even(n: float) -> int:
    """ffmpeg/H.264 want even dimensions."""
    return int(round(n / 2)) * 2


@dataclass
class LayoutOpts:
    """Everything the compositor needs to build the vertical video."""
    facecam_enabled: bool = True
    facecam: Optional[Rect] = None
    facecam_ratio: float = 0.35          # only used when facecam_autofit is False
    facecam_autofit: bool = True         # size the top pane to the facecam's aspect (no bars)
    facecam_height: Optional[float] = None  # lock the top pane to this fraction of the
                                         # content block (crop-independent, still cover-fit);
                                         # overrides autofit so several clips can share one split
    blur_bg: bool = True                 # blurred fill behind facecam when NOT autofitting
    gameplay_fill: str = "crop"          # "crop" (cover) or "blur" (zoomed + blurred edges)
    blur_bar: float = 0.12               # blurred-edge thickness (fraction per bar) in blur mode
    mousecam_enabled: bool = False
    mousecam: Optional[Rect] = None
    mousecam_pos: str = "bottom-right"   # corner preset (fallback when x/y unset)
    mousecam_x: Optional[float] = None   # overlay TOP-LEFT as a fraction of output W (drag)
    mousecam_y: Optional[float] = None   # overlay TOP-LEFT as a fraction of output H (drag)
    mousecam_scale: float = 0.28         # overlay width as a fraction of output width
    trim_start: float = 0.0              # seconds into the source to start
    trim_end: Optional[float] = None     # seconds into the source to stop (None = to end)
    speed: float = 1.0                   # playback speed (>1 = faster/shorter)
    cuts: Optional[list[tuple[float, float]]] = None  # (start,end) source-second spans to REMOVE
    cut_transitions: Optional[list[Optional[Transition]]] = None  # aligned 1:1 with `cuts`
    audio_fade_out: bool = False         # fade the audio out as the clip ends
    audio_fade_dur: float = 0.8          # fade-out length in seconds
    overlay_png: Optional[str] = None    # RGBA overlay stamped on the final frame
                                         # (e.g. a ranking-template graphic). Same
                                         # size as the output; drawn spatially so
                                         # it also shows in the still preview.
    effects: Optional[list[dict]] = None  # B-roll point effects: [{"type": key, "t": source_sec}]
                                          # applied as a post-pass (see _apply_effects)
    out_w: int = 1080
    out_h: int = 1920


# -- pane builders: each takes an input pad label, emits a chain ending in [out] --
def _pane_cover(in_label: str, out: str, W: int, H: int) -> str:
    """Scale to COVER WxH and center-crop (fills, never stretches)."""
    return (f"{in_label}scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H}[{out}]")


def _pane_blurfit(in_label: str, out: str, W: int, H: int, blur: int = 22) -> str:
    """Scale to FIT (fully visible), filling the empty edges with a blurred copy."""
    a, b, bg, fg = f"{out}a", f"{out}b", f"{out}bg", f"{out}fg"
    return (
        f"{in_label}split=2[{a}][{b}];"
        f"[{a}]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},boxblur={blur}:2[{bg}];"
        f"[{b}]scale={W}:{H}:force_original_aspect_ratio=decrease[{fg}];"
        f"[{bg}][{fg}]overlay=(W-w)/2:(H-h)/2[{out}]"
    )


def _full_blur_bg(in_label: str, out: str, W: int, H: int, blur: int = 26) -> str:
    """A full-frame blurred backdrop (cover) spanning the entire WxH output."""
    return (f"{in_label}scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},boxblur={blur}:3[{out}]")


def _pane_padblack(in_label: str, out: str, W: int, H: int) -> str:
    """Scale to FIT with black bars."""
    return (f"{in_label}scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black[{out}]")


def _build_filter(info: VideoInfo, o: LayoutOpts, out_label: str = "v") -> str:
    """Assemble the spatial filter_complex ending in the [out_label] pad."""
    W, H = _even(o.out_w), _even(o.out_h)
    need_fc = o.facecam_enabled and o.facecam is not None
    need_mc = o.mousecam_enabled and o.mousecam is not None
    blur_mode = o.gameplay_fill == "blur"

    chains: list[str] = []

    # In blur mode the entire composition is inset inside blurred edges that span
    # the WHOLE output frame. The facecam + gameplay are stacked into a shorter
    # "content" block, then centered on a full-frame blurred backdrop so the
    # blurred bars show at the true top/bottom edges of the clip — not just inside
    # the gameplay pane. `bar` is the fraction of H taken by each blurred edge.
    bar = max(0.0, min(o.blur_bar, 0.4)) if blur_mode else 0.0
    content_h = _even(H * (1 - 2 * bar)) or 2

    # The source stream can only be consumed once, so split it per consumer.
    # (The blurred backdrop is derived from the composed content, not the source,
    # so it needs no source copy of its own.)
    count = 1 + (1 if need_fc else 0) + (1 if need_mc else 0)
    fc_in = mc_in = None
    if count == 1:
        gp_in = "[0:v]"
    else:
        outs = "".join(f"[src{i}]" for i in range(count))
        chains.append(f"[0:v]split={count}{outs}")
        i = 0
        gp_in = f"[src{i}]"; i += 1
        if need_fc:
            fc_in = f"[src{i}]"; i += 1
        if need_mc:
            mc_in = f"[src{i}]"; i += 1

    # --- facecam pane (top of the content block) ------------------------------
    if need_fc:
        fc = o.facecam.clamp(info.width, info.height)
        chains.append(f"{fc_in}crop={fc.w}:{fc.h}:{fc.x}:{fc.y}[fccrop]")
        if o.facecam_height:
            # explicit, crop-INDEPENDENT pane height: the facecam pane is a fixed
            # fraction of the content block, still cover-fit (edge-to-edge, no bars).
            # Lets several clips whose facecam crops differ share ONE split, so a
            # fixed overlay (the ranking list/title) lines up identically on all of
            # them. Clamped to the same sane range as autofit.
            frac = max(0.15, min(float(o.facecam_height), 0.62))
            top_h = _even(content_h * frac)
            chains.append(_pane_cover("[fccrop]", "top", W, top_h))
        elif o.facecam_autofit:
            # top pane matches the facecam's own aspect ratio -> fills edge-to-edge,
            # no blurred strips, nothing cropped (clamped to a sane height range).
            top_h = _even(W * fc.h / fc.w)
            top_h = max(_even(content_h * 0.15), min(top_h, _even(content_h * 0.62)))
            chains.append(_pane_cover("[fccrop]", "top", W, top_h))
        else:
            top_h = _even(content_h * o.facecam_ratio)
            if o.blur_bg:
                chains.append(_pane_blurfit("[fccrop]", "top", W, top_h))
            else:
                chains.append(_pane_padblack("[fccrop]", "top", W, top_h))
        gp_h = _even(content_h - top_h)
    else:
        gp_h = content_h

    # --- gameplay pane (fills width, cover) -----------------------------------
    chains.append(_pane_cover(gp_in, "gp", W, gp_h))

    # --- stack facecam + gameplay into the content block ----------------------
    if need_fc:
        chains.append("[top][gp]vstack=inputs=2[content]")
        content = "[content]"
    else:
        content = "[gp]"

    # --- inset the content on a blurred backdrop derived from itself ----------
    if blur_mode:
        # Split the composed content: one copy is scaled up to COVER the whole
        # frame and blurred (the backdrop), the other is drawn crisp on top. Because
        # the backdrop is the rendered composition (facecam on top, gameplay below),
        # the blurred top edge mirrors the facecam and the bottom mirrors the
        # gameplay — the blur is global to the *rendered clip*, not the raw source.
        chains.append(f"{content}split=2[cfg][cbg]")
        chains.append(_full_blur_bg("[cbg]", "bg", W, H))
        chains.append(f"[bg][cfg]overlay=(W-w)/2:(H-h)/2[base]")
        base = "[base]"
    else:
        base = content

    # --- mousecam overlay ------------------------------------------------------
    if need_mc:
        mc = o.mousecam.clamp(info.width, info.height)
        mw = _even(W * o.mousecam_scale)
        margin = _even(W * 0.03)
        if o.mousecam_x is not None and o.mousecam_y is not None:
            # Dragged position: place the overlay's top-left at (x,y) fractions of the
            # output, clamped so it stays fully on-screen. mh mirrors scale=…:-2.
            mh = _even(mw * mc.h / mc.w) if mc.w else mw
            px = max(0, min(int(round(o.mousecam_x * W)), W - mw))
            py = max(0, min(int(round(o.mousecam_y * H)), H - mh))
            pos = f"{px}:{py}"
        else:
            pos = {
                "top-left": f"{margin}:{margin}",
                "top-right": f"W-w-{margin}:{margin}",
                "bottom-left": f"{margin}:H-h-{margin}",
                "bottom-right": f"W-w-{margin}:H-h-{margin}",
            }.get(o.mousecam_pos, f"W-w-{margin}:H-h-{margin}")
        chains.append(f"{mc_in}crop={mc.w}:{mc.h}:{mc.x}:{mc.y},scale={mw}:-2[mc]")
        chains.append(f"{base}[mc]overlay={pos}[{out_label}]")
    else:
        chains.append(f"{base}null[{out_label}]")

    return ";".join(chains)


def _seek_args(opts: LayoutOpts) -> list[str]:
    """Fast input seek to the trim start (before -i)."""
    start = max(0.0, opts.trim_start or 0.0)
    return ["-ss", f"{start:.3f}"] if start > 0 else []


def _duration_args(opts: LayoutOpts) -> list[str]:
    """Output duration limit for the trim window (after -i)."""
    start = max(0.0, opts.trim_start or 0.0)
    if opts.trim_end is not None and opts.trim_end > start:
        return ["-t", f"{opts.trim_end - start:.3f}"]
    return []


# ----------------------------------------------------------------------------- #
# Temporal editing: speed-up, cut-out segments, audio fade
# ----------------------------------------------------------------------------- #
def _clamp_speed(speed: float) -> float:
    try:
        return max(0.25, min(float(speed or 1.0), 4.0))
    except (TypeError, ValueError):
        return 1.0


def _has_audio(path: str) -> bool:
    """True if the source has at least one audio stream."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return bool(out)
    except Exception:
        return False


def _keep_intervals(window: float, cuts, trim_start: float):
    """
    Given cut spans in SOURCE seconds, return (keep_intervals, removed_seconds)
    where keep_intervals are window-relative (t=0 at trim_start) spans to KEEP.
    """
    segs = []
    for c in cuts or []:
        s, e = float(c[0]) - trim_start, float(c[1]) - trim_start
        s = max(0.0, min(s, window))
        e = max(0.0, min(e, window))
        if e - s > 1e-3:
            segs.append((s, e))
    segs.sort()

    merged: list[tuple[float, float]] = []
    for s, e in segs:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    keep: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in merged:
        if s > cur:
            keep.append((cur, s))
        cur = max(cur, e)
    if window - cur > 1e-3:
        keep.append((cur, window))

    removed = sum(e - s for s, e in merged)
    return keep, removed


def _atempo_factors(speed: float) -> list[float]:
    """Decompose a speed multiplier into atempo factors within [0.5, 2.0]."""
    factors: list[float] = []
    s = speed
    while s > 2.0 + 1e-9:
        factors.append(2.0)
        s /= 2.0
    while s < 0.5 - 1e-9:
        factors.append(0.5)
        s /= 0.5
    factors.append(round(s, 6))
    return factors


# ----------------------------------------------------------------------------- #
# Transitions at cut joins (slide + whoosh)
# ----------------------------------------------------------------------------- #
def _transition_for_gap(gap_s: float, gap_e: float, opts: "LayoutOpts",
                        start: float) -> Optional[Transition]:
    """
    Find the transition attached to the cut that produced this (window-relative)
    gap between two keep intervals. Cuts arrive in source seconds, so convert to
    window-relative before testing overlap. First overlapping cut wins.
    """
    cuts = opts.cuts or []
    trs = opts.cut_transitions or []
    for i, (cs, ce) in enumerate(cuts):
        tr = trs[i] if i < len(trs) else None
        if tr is None:
            continue
        ws, we = float(cs) - start, float(ce) - start
        if ws <= gap_e + 1e-3 and we >= gap_s - 1e-3:   # overlaps the gap
            return tr
    return None


def _ff_escape(path: str) -> str:
    """Escape a path for use as an ffmpeg filtergraph option value (amovie=...)."""
    out = path.replace("\\", "\\\\")
    for ch in (":", "'", ",", ";", "[", "]"):
        out = out.replace(ch, "\\" + ch)
    return out


def _sfx_path(sound: str) -> Optional[Path]:
    """Resolve a UI sound key to a bundled asset file, if present."""
    fn = _SFX_FILES.get(sound)
    if not fn:
        return None
    p = _SFX_DIR / fn
    return p if p.exists() else None


def _sfx_dur(path: Path) -> float:
    """Duration of a bundled SFX (best-effort; used only for placement)."""
    try:
        return max(0.05, probe(str(path)).duration or 1.0)
    except Exception:
        return 1.0


def _whoosh_source(label: str, delay_ms: int, sound: str = "whoosh",
                   vol: float = 1.0) -> str:
    """
    A transition SFX chain ending in [label], delayed so it lands on the cut.
    Prefers the bundled asset (`amovie`); falls back to a synthesized whoosh
    (enveloped, band-limited pink noise) if the file is missing.
    """
    p = _sfx_path(sound)
    if p is not None:
        return (
            f"amovie={_ff_escape(str(p))},"
            f"aformat=sample_rates={_WHOOSH_SR}:channel_layouts=stereo,"
            f"volume={vol:.2f},adelay={delay_ms}|{delay_ms}[{label}]"
        )
    d = TRANSITION_DUR
    return (
        f"anoisesrc=color=pink:duration={d:.3f}:amplitude=0.9:sample_rate={_WHOOSH_SR},"
        f"highpass=f=250,lowpass=f=5500,"
        f"afade=t=in:st=0:d={d * 0.36:.3f}:curve=exp,"
        f"afade=t=out:st={d * 0.44:.3f}:d={d * 0.56:.3f},"
        f"volume={max(vol, 2.2):.2f},adelay={delay_ms}|{delay_ms}[{label}]"
    )


def _join_plan(keep, opts: "LayoutOpts", start: float):
    """
    Decide, per internal junction between consecutive keep intervals, the visual
    slide + overlap + whoosh. `plan[i]` describes the join that precedes
    keep-interval i+1. Also returns the joined (pre-speed) duration so the caller
    can place whooshes and compute the final length. All arithmetic — no filters.
    """
    plan: list[dict] = []
    if not keep:
        return plan, 0.0
    acc = keep[0][1] - keep[0][0]
    for i in range(1, len(keep)):
        seg = keep[i][1] - keep[i][0]
        tr = _transition_for_gap(keep[i - 1][1], keep[i][0], opts, start)
        visual = _SWIPE_ALIAS.get(tr.visual, tr.visual) if (tr and tr.visual in _SWIPE_VISUALS) else "none"
        sound = tr.sound if (tr and tr.sound and tr.sound != "none") else "none"
        vol = max(0.0, min(float(tr.volume), 2.0)) if tr else 1.0
        # a swipe needs headroom in BOTH adjacent segments; else fall back to a cut
        D = min(TRANSITION_DUR, acc - 0.05, seg - 0.05)
        use_swipe = visual != "none" and D > 0.05
        if use_swipe:
            center = acc - D / 2.0          # midpoint of the swipe, joined timeline
            acc = acc + seg - D
        else:
            visual, D = "none", 0.0
            center = acc                    # the plain concat boundary
            acc = acc + seg
        plan.append({"visual": visual, "D": D, "sound": sound, "center": center, "vol": vol})
    return plan, acc


def _ease_out_cubic(p_expr: str) -> str:
    """easeOutCubic applied to a progress expression in [0,1] (snappy decel)."""
    return f"(1-pow(1-({p_expr}),3))"


def _push_chain(o_in: str, i_in: str, out: str, W: int, H: int, D: float,
                direction: str, fps: float) -> list[str]:
    """
    An eased directional push ("swipe") between two D-second, full-frame clips.
    Both clips translate together on an ease-out curve so the incoming footage
    decelerates smoothly into place — the CapCut-style motion. `o_in`/`i_in` are
    bracketed labels for the outgoing tail and incoming head; the returned chain
    ends in [out] and is exactly D seconds long. The two clips tile the frame at
    every instant, so the black base only guards sub-pixel seams.
    """
    e = _ease_out_cubic(f"t/{D:.4f}")          # 0 -> 1 across the transition
    if direction == "swiperight":              # content moves right, new from left
        xo, xi = f"({e})*{W}", f"({e})*{W}-{W}"
    else:                                      # swipeleft: content moves left, new from right
        xo, xi = f"-({e})*{W}", f"{W}-({e})*{W}"
    bg, t1 = f"[{out[1:-1]}bg]", f"[{out[1:-1]}t1]"
    return [
        f"color=c=black:size={W}x{H}:duration={D:.3f}:rate={fps:.4f}{bg}",
        f"{bg}{o_in}overlay=x='{xo}':y=0:eval=frame{t1}",
        f"{t1}{i_in}overlay=x='{xi}':y=0:eval=frame{out}",
    ]


def _temporal_segmented(src: str, info: VideoInfo, opts: "LayoutOpts",
                        spatial_out: str, keep, plan, joined_dur: float):
    """
    Cut path WITH transitions: slice the spatial output into per-keep-interval
    segments and re-join them. Swipe joins use an eased overlay push (CapCut-
    style, see `_push_chain`); plain joins concat (hard cut). Whooshes are
    synthesized/mixed on top at each join. Speed + audio fade are applied last.

    Video uses a slice-and-concat model: each segment contributes an optional
    leading `head` (consumed as a swipe's incoming footage), a `body` that plays
    alone, and an optional trailing `tail` (a swipe's outgoing footage). The
    output is  body0, T0, body1, T1, ... , body(n-1)  where Tk is the push
    between segment k's tail and segment k+1's head. Audio keeps the simpler
    acrossfade/concat accumulator below — its per-join overlap of D seconds lines
    up 1:1 with the video, so A/V stay in sync.
    """
    W, H = _even(opts.out_w), _even(opts.out_h)
    fps = info.fps or 30.0
    speed = _clamp_speed(opts.speed)
    has_speed = abs(speed - 1.0) > 1e-3
    want_fade = bool(opts.audio_fade_out) and opts.audio_fade_dur > 0
    n = len(keep)
    final_dur = (joined_dur / speed) if speed else joined_dur
    min_slice = 1.0 / max(fps, 1.0)

    # per-segment overlap consumed at its start (D of the preceding swipe) and
    # end (D of the following swipe); D is 0 for plain/no-transition joins.
    dprev = [plan[i - 1]["D"] if i >= 1 else 0.0 for i in range(n)]
    dnext = [plan[i]["D"] if i <= n - 2 else 0.0 for i in range(n)]

    # --- video: split spatial -> full segments -> head/body/tail slices -------
    vchain: list[str] = [f"[{spatial_out}]setsar=1[vspn]"]
    vchain.append(f"[vspn]split={n}" + "".join(f"[vseg{i}]" for i in range(n)))
    body_lbl: list[Optional[str]] = [None] * n
    head_lbl: list[Optional[str]] = [None] * n
    tail_lbl: list[Optional[str]] = [None] * n
    for i, (s, e) in enumerate(keep):
        seg = e - s
        vchain.append(f"[vseg{i}]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[vfull{i}]")
        b0 = min(dprev[i], seg)
        b1 = max(b0, seg - dnext[i])
        slices = []                                  # (kind, start, end)
        if dprev[i] > 0:
            slices.append(("head", 0.0, dprev[i]))
        if b1 - b0 >= min_slice or (dprev[i] == 0 and dnext[i] == 0):
            slices.append(("body", b0, b1))
        if dnext[i] > 0:
            slices.append(("tail", seg - dnext[i], seg))
        outs = "".join(f"[vsl{i}_{k}]" for k in range(len(slices)))
        if len(slices) > 1:
            vchain.append(f"[vfull{i}]split={len(slices)}{outs}")
        for k, (kind, a, b) in enumerate(slices):
            src_lbl = f"[vfull{i}]" if len(slices) == 1 else f"[vsl{i}_{k}]"
            lbl = f"[v{kind}{i}]"
            vchain.append(f"{src_lbl}trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS{lbl}")
            {"head": head_lbl, "body": body_lbl, "tail": tail_lbl}[kind][i] = lbl

    # assemble: body0, push0, body1, push1, ... , body(n-1)
    pieces: list[str] = []
    for i in range(n):
        if body_lbl[i]:
            pieces.append(body_lbl[i])
        p = plan[i] if i < n - 1 else None
        if p and p["D"] > 0 and tail_lbl[i] and head_lbl[i + 1]:
            out = f"[vpush{i}]"
            vchain += _push_chain(tail_lbl[i], head_lbl[i + 1], out,
                                  W, H, p["D"], p["visual"], fps)
            pieces.append(out)
    if len(pieces) > 1:
        vchain.append("".join(pieces) + f"concat=n={len(pieces)}:v=1:a=0[vcat]")
        acc = "[vcat]"
    else:
        acc = pieces[0]
    if has_speed:
        vchain.append(f"{acc}setpts=PTS/{speed:.4f}[vsp]")
        acc = "[vsp]"
    vchain.append(f"{acc}null[v]")

    # transition SFX: (center in the FINAL post-speed timeline, sound key, gain)
    whoosh_specs = [(p["center"] / speed, p["sound"], p["vol"])
                    for p in plan if p["sound"] != "none"]

    # --- audio ----------------------------------------------------------------
    has_audio = _has_audio(src)
    achain: list[str] = []
    base: Optional[str] = None
    if has_audio:
        achain.append(f"[0:a]asplit={n}" + "".join(f"[aseg{i}]" for i in range(n)))
        for i, (s, e) in enumerate(keep):
            achain.append(f"[aseg{i}]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")
        acc = "[a0]"
        for i in range(1, n):
            p, out = plan[i - 1], f"[aj{i}]"
            if p["visual"] != "none":   # match the video overlap so A/V stay in sync
                achain.append(f"{acc}[a{i}]acrossfade=d={p['D']:.3f}{out}")
            else:
                achain.append(f"{acc}[a{i}]concat=n=2:v=0:a=1{out}")
            acc = out
        if has_speed:
            tempo = ",".join(f"atempo={f:.4f}" for f in _atempo_factors(speed))
            achain.append(f"{acc}{tempo}[asp]")
            acc = "[asp]"
        base = acc
    elif whoosh_specs:
        # no source audio — lay the whooshes over generated silence
        achain.append(f"anullsrc=r={_WHOOSH_SR}:cl=stereo,atrim=0:{final_dur:.3f}[asil]")
        base = "[asil]"

    if base is not None:
        if whoosh_specs:
            achain.append(f"{base}aresample={_WHOOSH_SR}[abase]")
            labels = ["[abase]"]
            for k, (c, snd, vol) in enumerate(whoosh_specs):
                # place the SFX so its body lands on the cut (a chunk leads in)
                p = _sfx_path(snd)
                lead = 0.45 * _sfx_dur(p) if p is not None else TRANSITION_DUR * 0.4
                ms = int(max(0.0, c - lead) * 1000)
                achain.append(_whoosh_source(f"w{k}", ms, snd, vol))
                labels.append(f"[w{k}]")
            achain.append("".join(labels) +
                          f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[amix]")
            base = "[amix]"
        if want_fade:
            fade_dur = min(float(opts.audio_fade_dur), final_dur)
            st = max(0.0, final_dur - fade_dur)
            achain.append(f"{base}afade=t=out:st={st:.3f}:d={fade_dur:.3f}[afd]")
            base = "[afd]"
        achain.append(f"{base}anull[a]")

    return vchain, achain, (has_audio or bool(whoosh_specs))


def _clamp_region_speed(v) -> float:
    """A speed-up region only ever speeds up; cap it at 8× to keep atempo sane."""
    try:
        return max(1.0, min(float(v or 2.0), 8.0))
    except (TypeError, ValueError):
        return 2.0


def _has_speedup(opts: "LayoutOpts", window: float, start: float) -> bool:
    for i, (cs, ce) in enumerate(opts.cuts or []):
        tr = (opts.cut_transitions or [None] * len(opts.cuts))[i] if opts.cut_transitions else None
        if tr is not None and getattr(tr, "visual", "none") == "speedup":
            s = max(0.0, min(float(cs) - start, window))
            e = max(0.0, min(float(ce) - start, window))
            if e - s > 1e-3:
                return True
    return False


def _speed_segment_plan(window: float, opts: "LayoutOpts", start: float):
    """
    Classify cuts into *removes* (spans deleted, like a normal cut) and
    *speed-ups* (spans kept but played faster). Return (segments, junctions):
      segments  = [(s, e, factor)] window-relative kept spans, in play order,
                  each tagged with its region speed factor (1.0 = normal).
      junctions = one per gap between consecutive segments:
                  {"remove", "visual", "sound", "vol"} — a "remove" junction is
                  where a deleted span sat (eligible for a swipe/SFX from that
                  cut's transition); a non-remove junction is a plain speed
                  boundary inside kept footage (always a hard concat).
    """
    cuts = opts.cuts or []
    trs = opts.cut_transitions or []
    removes: list[list] = []     # [s, e, tr]
    speeds: list[tuple] = []     # (s, e, factor)
    for i, (cs, ce) in enumerate(cuts):
        tr = trs[i] if i < len(trs) else None
        s = max(0.0, min(float(cs) - start, window))
        e = max(0.0, min(float(ce) - start, window))
        if e - s <= 1e-3:
            continue
        if tr is not None and getattr(tr, "visual", "none") == "speedup":
            speeds.append((s, e, _clamp_region_speed(getattr(tr, "speed", 2.0))))
        else:
            removes.append([s, e, tr])

    removes.sort(key=lambda r: r[0])
    merged: list[list] = []
    for s, e, tr in removes:
        if merged and s <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, tr])

    # keep intervals (complement of removes) + the removed-gap transition before each
    keep: list[tuple] = []
    gap_tr: list = []
    cur, pending = 0.0, None
    for s, e, tr in merged:
        if s > cur + 1e-9:
            keep.append((cur, s)); gap_tr.append(pending)
        pending = tr
        cur = max(cur, e)
    if window - cur > 1e-3:
        keep.append((cur, window)); gap_tr.append(pending)

    # subdivide each keep interval by the speed spans it overlaps
    segments: list[tuple] = []
    seg_keep: list[int] = []
    for ki, (ks, ke) in enumerate(keep):
        pts = {ks, ke}
        for ss, se, _f in speeds:
            a, b = max(ks, ss), min(ke, se)
            if b - a > 1e-3:
                pts.add(a); pts.add(b)
        ordered = sorted(p for p in pts if ks - 1e-9 <= p <= ke + 1e-9)
        for a, b in zip(ordered, ordered[1:]):
            if b - a <= 1e-3:
                continue
            mid = (a + b) / 2.0
            fac = 1.0
            for ss, se, f in speeds:
                if ss - 1e-6 <= mid <= se + 1e-6:
                    fac = f; break
            segments.append((a, b, fac)); seg_keep.append(ki)

    junctions: list[dict] = []
    for i in range(len(segments) - 1):
        if seg_keep[i] == seg_keep[i + 1]:
            junctions.append({"remove": False, "visual": "none", "sound": "none", "vol": 1.0})
        else:
            tr = gap_tr[seg_keep[i + 1]] if seg_keep[i + 1] < len(gap_tr) else None
            vis = _SWIPE_ALIAS.get(tr.visual, tr.visual) if (tr and tr.visual in _SWIPE_VISUALS) else "none"
            snd = tr.sound if (tr and tr.sound and tr.sound != "none") else "none"
            vol = max(0.0, min(float(tr.volume), 2.0)) if tr else 1.0
            junctions.append({"remove": True, "visual": vis, "sound": snd, "vol": vol})
    return segments, junctions


def _temporal_speed_segmented(src: str, info: VideoInfo, opts: "LayoutOpts", spatial_out: str):
    """
    Temporal path for clips that use a **speed-up region**. Generalises
    `_temporal_segmented`: the timeline is a list of kept sub-segments each with
    its own speed factor (global speed × the region factor), applied per segment
    *before* the head/body/tail swipe slicing so pushes land in final time.
    Remove-junctions may still carry a swipe (eased push) + SFX; speed boundaries
    are hard concats. Audio is time-compressed per segment with atempo.
    """
    W, H = _even(opts.out_w), _even(opts.out_h)
    fps = info.fps or 30.0
    gspeed = _clamp_speed(opts.speed)
    want_fade = bool(opts.audio_fade_out) and opts.audio_fade_dur > 0

    start = max(0.0, opts.trim_start or 0.0)
    end = opts.trim_end if (opts.trim_end is not None and opts.trim_end > start) else info.duration
    window = max(0.0, end - start)
    segments, junctions = _speed_segment_plan(window, opts, start)
    n = len(segments)

    fspeed = [gspeed * seg[2] for seg in segments]                 # final per-seg speed
    fdur = [(seg[1] - seg[0]) / f if f else (seg[1] - seg[0]) for seg, f in zip(segments, fspeed)]
    min_slice = 1.0 / max(fps, 1.0)

    # swipe overlap D per junction (final time; both sides need headroom)
    for j, jc in enumerate(junctions):
        D = 0.0
        if jc["remove"] and jc["visual"] in _SWIPE_VISUALS:
            D = min(TRANSITION_DUR, fdur[j] - 0.05, fdur[j + 1] - 0.05)
        jc["D"] = D if D > 0.05 else 0.0
    dprev = [junctions[i - 1]["D"] if i >= 1 else 0.0 for i in range(n)]
    dnext = [junctions[i]["D"] if i <= n - 2 else 0.0 for i in range(n)]

    # whoosh centers + final duration (final timeline, accounting for overlaps)
    acc = fdur[0] if n else 0.0
    for j in range(1, n):
        D = junctions[j - 1]["D"]
        junctions[j - 1]["center"] = (acc - D / 2.0) if D > 0 else acc
        acc = acc + fdur[j] - (D if D > 0 else 0.0)
    final_dur = acc
    whoosh_specs = [(jc["center"], jc["sound"], jc["vol"])
                    for jc in junctions if jc.get("sound", "none") != "none" and "center" in jc]

    # --- video: per-segment speed, then head/body/tail slice + push -----------
    vchain: list[str] = [f"[{spatial_out}]setsar=1[vspn]"]
    vchain.append(f"[vspn]split={n}" + "".join(f"[vseg{i}]" for i in range(n)))
    body_lbl: list[Optional[str]] = [None] * n
    head_lbl: list[Optional[str]] = [None] * n
    tail_lbl: list[Optional[str]] = [None] * n
    for i, (s, e, _f) in enumerate(segments):
        # trim source span, reset PTS and apply the region's final speed at once
        vchain.append(f"[vseg{i}]trim={s:.3f}:{e:.3f},setpts=(PTS-STARTPTS)/{fspeed[i]:.6f}[vfull{i}]")
        seg = fdur[i]
        b0 = min(dprev[i], seg)
        b1 = max(b0, seg - dnext[i])
        slices = []
        if dprev[i] > 0:
            slices.append(("head", 0.0, dprev[i]))
        if b1 - b0 >= min_slice or (dprev[i] == 0 and dnext[i] == 0):
            slices.append(("body", b0, b1))
        if dnext[i] > 0:
            slices.append(("tail", seg - dnext[i], seg))
        outs = "".join(f"[vsl{i}_{k}]" for k in range(len(slices)))
        if len(slices) > 1:
            vchain.append(f"[vfull{i}]split={len(slices)}{outs}")
        for k, (kind, a, b) in enumerate(slices):
            src_lbl = f"[vfull{i}]" if len(slices) == 1 else f"[vsl{i}_{k}]"
            lbl = f"[v{kind}{i}]"
            vchain.append(f"{src_lbl}trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS{lbl}")
            {"head": head_lbl, "body": body_lbl, "tail": tail_lbl}[kind][i] = lbl

    pieces: list[str] = []
    for i in range(n):
        if body_lbl[i]:
            pieces.append(body_lbl[i])
        jc = junctions[i] if i < n - 1 else None
        if jc and jc["D"] > 0 and tail_lbl[i] and head_lbl[i + 1]:
            out = f"[vpush{i}]"
            vchain += _push_chain(tail_lbl[i], head_lbl[i + 1], out, W, H, jc["D"], jc["visual"], fps)
            pieces.append(out)
    if len(pieces) > 1:
        vchain.append("".join(pieces) + f"concat=n={len(pieces)}:v=1:a=0[vcat]")
        acc_lbl = "[vcat]"
    else:
        acc_lbl = pieces[0]
    vchain.append(f"{acc_lbl}null[v]")

    # --- audio: per-segment atempo, then acrossfade/concat --------------------
    has_audio = _has_audio(src)
    achain: list[str] = []
    base: Optional[str] = None
    if has_audio:
        achain.append(f"[0:a]asplit={n}" + "".join(f"[aseg{i}]" for i in range(n)))
        for i, (s, e, _f) in enumerate(segments):
            tempo = ",".join(f"atempo={fac:.4f}" for fac in _atempo_factors(fspeed[i]))
            achain.append(f"[aseg{i}]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS,{tempo}[a{i}]")
        acc_lbl = "[a0]"
        for i in range(1, n):
            jc, out = junctions[i - 1], f"[aj{i}]"
            if jc["D"] > 0:
                achain.append(f"{acc_lbl}[a{i}]acrossfade=d={jc['D']:.3f}{out}")
            else:
                achain.append(f"{acc_lbl}[a{i}]concat=n=2:v=0:a=1{out}")
            acc_lbl = out
        base = acc_lbl
    elif whoosh_specs:
        achain.append(f"anullsrc=r={_WHOOSH_SR}:cl=stereo,atrim=0:{final_dur:.3f}[asil]")
        base = "[asil]"

    if base is not None:
        if whoosh_specs:
            achain.append(f"{base}aresample={_WHOOSH_SR}[abase]")
            labels = ["[abase]"]
            for k, (c, snd, vol) in enumerate(whoosh_specs):
                p = _sfx_path(snd)
                lead = 0.45 * _sfx_dur(p) if p is not None else TRANSITION_DUR * 0.4
                ms = int(max(0.0, c - lead) * 1000)
                achain.append(_whoosh_source(f"w{k}", ms, snd, vol))
                labels.append(f"[w{k}]")
            achain.append("".join(labels) +
                          f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[amix]")
            base = "[amix]"
        if want_fade:
            fade_dur = min(float(opts.audio_fade_dur), final_dur)
            st = max(0.0, final_dur - fade_dur)
            achain.append(f"{base}afade=t=out:st={st:.3f}:d={fade_dur:.3f}[afd]")
            base = "[afd]"
        achain.append(f"{base}anull[a]")

    return vchain, achain, (has_audio or bool(whoosh_specs))


def _temporal_filters(src: str, info: VideoInfo, opts: LayoutOpts, spatial_out: str):
    """
    Build the video + audio temporal chains applied AFTER spatial composition.

    Returns (video_chain, audio_chain, has_audio_out). Video chain runs from
    [spatial_out] to [v]; audio chain (empty if no processing/no stream) from
    [0:a] to [a]. Applies cut-outs, speed, and audio fade-out.
    """
    start = max(0.0, opts.trim_start or 0.0)
    end = opts.trim_end if (opts.trim_end is not None and opts.trim_end > start) else info.duration
    window = max(0.0, end - start)

    # A speed-up region keeps its span (rather than removing it) — handled by the
    # generalised per-segment-speed renderer, which also covers any remove-cuts
    # and swipes present alongside it.
    if _has_speedup(opts, window, start):
        segments, _j = _speed_segment_plan(window, opts, start)
        if segments:
            return _temporal_speed_segmented(src, info, opts, spatial_out)

    speed = _clamp_speed(opts.speed)
    keep, removed = _keep_intervals(window, opts.cuts, start)
    has_cuts = bool(opts.cuts) and removed > 1e-3 and len(keep) > 0

    # If a surviving join carries a real transition, take the segment/xfade path.
    if has_cuts and len(keep) > 1 and opts.cut_transitions:
        plan, joined_dur = _join_plan(keep, opts, start)
        if any(p["visual"] != "none" or p["sound"] != "none" for p in plan):
            return _temporal_segmented(src, info, opts, spatial_out, keep, plan, joined_dur)

    has_speed = abs(speed - 1.0) > 1e-3
    want_fade = bool(opts.audio_fade_out) and opts.audio_fade_dur > 0

    keep_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep) if has_cuts else ""

    # --- video ---------------------------------------------------------------
    vchain: list[str] = []
    prev = f"[{spatial_out}]"
    if has_cuts:
        # drop unwanted frames, then re-timestamp to collapse the gaps
        vchain.append(f"{prev}select='{keep_expr}',setpts=N/FRAME_RATE/TB[vcut]")
        prev = "[vcut]"
    if has_speed:
        vchain.append(f"{prev}setpts=PTS/{speed:.4f}[vsp]")
        prev = "[vsp]"
    vchain.append(f"{prev}null[v]")

    # --- audio ---------------------------------------------------------------
    has_audio = _has_audio(src)
    achain: list[str] = []
    if has_audio:
        prev = "[0:a]"
        if has_cuts:
            achain.append(f"{prev}aselect='{keep_expr}',asetpts=N/SR/TB[acut]")
            prev = "[acut]"
        if has_speed:
            tempo = ",".join(f"atempo={f:.4f}" for f in _atempo_factors(speed))
            achain.append(f"{prev}{tempo}[asp]")
            prev = "[asp]"
        if want_fade:
            final_dur = max(0.0, (window - removed) / speed)
            fade_dur = min(float(opts.audio_fade_dur), final_dur)
            st = max(0.0, final_dur - fade_dur)
            achain.append(f"{prev}afade=t=out:st={st:.3f}:d={fade_dur:.3f}[a]")
            prev = "[a]"
        achain.append(f"{prev}anull[a]")

    return vchain, achain, has_audio


# ----------------------------------------------------------------------------- #
# B-roll effects — point "moments" (colour+blur flash + one-shot SFX)
# ----------------------------------------------------------------------------- #
def _effect_final_time(t_src: float, opts: LayoutOpts, info: VideoInfo) -> float:
    """
    Map a source-second marker to its time in the FINAL render. This must land the
    marker exactly where compose() actually places that footage, so it REPLICATES
    whichever temporal path the render uses:

      • speed-up region(s)  → walk _speed_segment_plan (per-segment speed × global
        speed, swipe overlaps subtracted) — mirrors _temporal_speed_segmented.
      • remove-cuts + a real transition → walk the keep intervals with _join_plan's
        swipe overlaps, then divide by global speed — mirrors _temporal_segmented.
      • plain cuts / global speed only → kept-time before the marker ÷ speed.

    Getting this wrong is what made B-roll drift (~0.5s) between the moment preview
    (short window, upstream edits excluded) and the full render. A marker inside a
    removed span snaps forward to that gap's boundary.
    """
    start = max(0.0, opts.trim_start or 0.0)
    end = opts.trim_end if (opts.trim_end is not None and opts.trim_end > start) else info.duration
    window = max(0.0, end - start)
    tw = max(0.0, min(float(t_src) - start, window))
    gspeed = _clamp_speed(opts.speed)

    # --- speed-up region(s): mirror _temporal_speed_segmented's final timeline ---
    if _has_speedup(opts, window, start):
        segments, junctions = _speed_segment_plan(window, opts, start)
        if segments:
            fspeed = [gspeed * seg[2] for seg in segments]
            fdur = [(e - s) / f if f else (e - s)
                    for (s, e, _), f in zip(segments, fspeed)]
            Ds = []
            for j, jc in enumerate(junctions):
                D = 0.0
                if jc["remove"] and jc["visual"] in _SWIPE_VISUALS:
                    D = min(TRANSITION_DUR, fdur[j] - 0.05, fdur[j + 1] - 0.05)
                Ds.append(D if D > 0.05 else 0.0)
            seg_start = [0.0] * len(segments)
            for i in range(1, len(segments)):
                seg_start[i] = seg_start[i - 1] + fdur[i - 1] - Ds[i - 1]
            for i, (s, e, _f) in enumerate(segments):
                if tw <= e + 1e-6:
                    return seg_start[i] + (max(s, tw) - s) / fspeed[i]
            i = len(segments) - 1
            return seg_start[i] + fdur[i]

    keep, removed = _keep_intervals(window, opts.cuts, start)
    has_cuts = bool(opts.cuts) and removed > 1e-3 and len(keep) > 0

    # --- remove-cuts carrying a transition: mirror _temporal_segmented's overlaps -
    if has_cuts and len(keep) > 1 and opts.cut_transitions:
        plan, _joined = _join_plan(keep, opts, start)
        if any(p["visual"] != "none" or p["sound"] != "none" for p in plan):
            seg_start = [0.0] * len(keep)
            for i in range(1, len(keep)):
                D = plan[i - 1]["D"] if i - 1 < len(plan) else 0.0
                seg_start[i] = seg_start[i - 1] + (keep[i - 1][1] - keep[i - 1][0]) - D
            for i, (s, e) in enumerate(keep):
                if tw <= e + 1e-6:
                    return (seg_start[i] + (max(s, tw) - s)) / gspeed
            i = len(keep) - 1
            return (seg_start[i] + (keep[i][1] - keep[i][0])) / gspeed

    # --- plain cuts / global speed (select path): kept time before ÷ speed --------
    if not keep:                                   # everything cut — clamp to 0
        kept_before = tw
    else:
        kept_before = 0.0
        for s, e in keep:
            if tw <= s:
                break
            kept_before += min(e, tw) - s
    return kept_before / gspeed if gspeed else kept_before


def _marker_in_window(t_src: float, opts: LayoutOpts, info: VideoInfo,
                      tol: float = 1e-3) -> bool:
    """True if a source-seconds marker lies inside the active trim window.

    Markers OUTSIDE the trim are dropped rather than snapped to a window edge.
    This matters for a scoped MOMENT / wand preview (`/api/autolayout/moment`
    narrows trim_start/trim_end to the previewed span): `_effect_final_time`
    clamps `tw` to `[0, window]`, so an adjacent B-roll just before the span
    would fire at t=0 and one just after would fire at the span's end — reading
    as "out of sync". Filtering by the true source window means only the B-roll
    actually inside the previewed span fires, at its real relative time. In a
    full render trim_end is the user's real trim, so in-trim markers are a no-op
    (and a genuinely out-of-trim marker correctly no longer flashes at the edge).
    """
    start = max(0.0, opts.trim_start or 0.0)
    end = (opts.trim_end if (opts.trim_end is not None and opts.trim_end > start)
           else info.duration)
    return (start - tol) <= float(t_src) <= (end + tol)


def _normalize_effects(opts: LayoutOpts, info: VideoInfo):
    """
    Resolve opts.effects into (final_time, spec) pairs, sorted, valid only. Each
    marker may override the catalog default flash duration (`dur`, clamped to the
    effect's slider range) and pick one of its `sounds` (`sound` = a sound key);
    both are merged onto a per-marker copy of the catalog spec.
    """
    out = []
    for ef in opts.effects or []:
        base = _EFFECTS.get((ef or {}).get("type"))
        if not base or base.get("overlay") or base.get("motion"):
            continue                                   # overlay chips / motion moments: handled separately
        try:
            t_src = float(ef.get("t"))
        except (TypeError, ValueError):
            continue
        if not _marker_in_window(t_src, opts, info):
            continue                                   # outside the (possibly scoped) trim → don't snap to an edge
        spec = dict(base)
        # per-marker flash duration (clamped to the effect's slider bounds)
        if ef.get("dur") is not None:
            try:
                lo, hi = base.get("dur_min", 0.1), base.get("dur_max", 3.0)
                spec["flash_dur"] = max(lo, min(float(ef["dur"]), hi))
            except (TypeError, ValueError):
                pass
        # per-marker sound choice (falls back to the default file)
        snd_key = ef.get("sound")
        if base.get("sfx_dir"):
            # folder-backed (SFX): the sound key IS the filename in that folder
            fname = Path(snd_key).name if snd_key else None
            if not fname:
                first = scan_sfx_folder()
                fname = first[0]["file"] if first else None
            spec["sound"] = fname
        else:
            sounds = base.get("sounds") or []
            chosen = None
            if snd_key:
                chosen = next((s["file"] for s in sounds if s.get("key") == snd_key), None)
            spec["sound"] = chosen or base.get("sound") or (sounds[0]["file"] if sounds else None)
        # per-marker SFX gain (0..2; default 1.0)
        try:
            spec["volume"] = max(0.0, min(float(ef.get("volume", 1.0)), 2.0))
        except (TypeError, ValueError):
            spec["volume"] = 1.0
        tf = _effect_final_time(t_src, opts, info)
        out.append((tf, spec))
    out.sort(key=lambda x: x[0])
    return out


def _apply_effects(inp: str, outp: str, opts: LayoutOpts, info: VideoInfo) -> str:
    """
    Second pass: stamp B-roll point effects onto an already-composed clip. The
    clip's own PTS is the final timeline, so effect times come straight from
    `_effect_final_time`. Each effect eases a tinted+blurred copy of the frame in
    and out over `flash_dur` (peak at its centre) and mixes its one-shot SFX so
    the punch lands on the marker. Non-destructive to the source geometry.
    """
    fx = _normalize_effects(opts, info)
    if not fx:
        return inp
    dur = max(0.05, probe(inp).duration)

    # Each effect gets its OWN tinted+blurred copy + overlay pass, so its eased
    # alpha window (fade in→hold→out) only affects itself — chaining every fade
    # onto one shared copy fails, because a later effect's fade-in zeroes the
    # earlier flash (its factor is 0 before its own window).
    n = len(fx)
    parts = [f"[0:v]split={n + 1}[base]" + "".join(f"[fxsrc{k}]" for k in range(n))]
    cur = "[base]"
    for k, (tf, spec) in enumerate(fx):
        D = float(spec["flash_dur"])
        r = min(D * 0.5, 0.18)                        # eased ramp in/out
        st = max(0.0, tf - float(spec.get("lead", 0.0)))
        tr, tg, tb = spec["tint"]
        peak = max(0.0, min(float(spec["peak"]), 1.0))
        sig = float(spec.get("blur", 0.0))
        blur_step = f"gblur=sigma={sig:.2f}," if sig > 0.1 else ""
        parts.append(
            f"[fxsrc{k}]{blur_step}"
            f"colorchannelmixer=rr={tr:.3f}:gg={tg:.3f}:bb={tb:.3f},"
            f"eq=brightness=0.03:saturation=1.15,format=yuva420p,"
            f"fade=t=in:st={st:.3f}:d={r:.3f}:alpha=1,"
            f"fade=t=out:st={max(st, st + D - r):.3f}:d={r:.3f}:alpha=1,"
            f"colorchannelmixer=aa={peak:.3f}[fx{k}]"
        )
        out = "[v]" if k == n - 1 else f"[ov{k}]"
        parts.append(f"{cur}[fx{k}]overlay=0:0:eof_action=repeat{out}")
        cur = out
    vchain = ";".join(parts)

    # audio: base track (source audio if present, else silence) + one-shot SFX mix
    has_audio = _has_audio(inp)
    achain = []
    if has_audio:
        achain.append(f"[0:a]aresample={_WHOOSH_SR}[abase]")
    else:
        achain.append(f"anullsrc=r={_WHOOSH_SR}:cl=stereo,atrim=0:{dur:.3f}[abase]")
    labels = ["[abase]"]
    for k, (tf, spec) in enumerate(fx):
        snd = spec.get("sound")
        if not snd:
            continue
        base_dir = spec.get("sfx_dir") or _SFX_DIR
        p = Path(base_dir) / Path(snd).name        # .name guards against traversal
        if not p.exists():
            continue
        st = max(0.0, tf - float(spec.get("lead", 0.0)))
        ms = int(st * 1000)
        vol = float(spec.get("volume", 1.0))
        labels.append(f"[efx{k}]")
        achain.append(
            f"amovie={_ff_escape(str(p))},"
            f"aformat=sample_rates={_WHOOSH_SR}:channel_layouts=stereo,"
            f"volume={vol:.2f},adelay={ms}|{ms}[efx{k}]"
        )
    if len(labels) > 1:
        achain.append("".join(labels) +
                      f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                      f"atrim=0:{dur:.3f}[a]")
    else:
        achain.append(f"[abase]atrim=0:{dur:.3f}[a]")

    filter_complex = ";".join([vchain, *achain])
    cmd = [
        "ffmpeg", "-y", "-i", inp,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *_video_encode_args(),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", outp,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return outp


# ----------------------------------------------------------------------------- #
# Subscribe prompt — a branded, audioless graphic chip eased in/out over the clip
# ----------------------------------------------------------------------------- #
def _normalize_subscribes(opts: LayoutOpts, info: VideoInfo):
    """Resolve opts.effects entries whose catalog spec has an `overlay` (currently
    the subscribe chip) into (final_time, spec) pairs. Same source→final-time
    mapping as B-roll flashes; each marker may override duration + chip scale."""
    out = []
    for ef in opts.effects or []:
        base = _EFFECTS.get((ef or {}).get("type"))
        if not base or not base.get("overlay"):
            continue
        try:
            t_src = float(ef.get("t"))
        except (TypeError, ValueError):
            continue
        if not _marker_in_window(t_src, opts, info):
            continue                                   # outside the (possibly scoped) trim → drop, don't snap
        spec = dict(base)
        if ef.get("dur") is not None:
            try:
                lo, hi = base.get("dur_min", 0.5), base.get("dur_max", 8.0)
                spec["flash_dur"] = max(lo, min(float(ef["dur"]), hi))
            except (TypeError, ValueError):
                pass
        if ef.get("scale") is not None:
            try:
                lo, hi = base.get("scale_min", 0.5), base.get("scale_max", 2.0)
                spec["scale"] = max(lo, min(float(ef["scale"]), hi))
            except (TypeError, ValueError):
                pass
        for k in ("x", "y"):
            if ef.get(k) is not None:
                try:
                    spec[k] = max(0.0, min(float(ef[k]), 1.0))
                except (TypeError, ValueError):
                    pass
        # CTA extras: animated Subscribe widget carries style / click-speed / SFX.
        if ef.get("style"):
            spec["style"] = ef["style"]
        if ef.get("vd_accent"):
            spec["vd_accent"] = ef["vd_accent"]
        params = ef.get("params") or {}
        if params.get("anim") is not None:
            try:
                spec["anim"] = max(0.3, min(float(params["anim"]), 2.5))
            except (TypeError, ValueError):
                pass
        if ef.get("sound") is not None:
            spec["cta_sound"] = ef["sound"]
        if ef.get("volume") is not None:
            try:
                spec["cta_volume"] = max(0.0, min(float(ef["volume"]), 2.0))
            except (TypeError, ValueError):
                pass
        out.append((_effect_final_time(t_src, opts, info), spec))
    out.sort(key=lambda x: x[0])
    return out


def _overlay_chips(inp: str, outp: str, chips: list[dict]) -> str:
    """Stamp one or more pre-rendered RGBA chip PNGs onto a finished clip, each
    over its own time window with an eased alpha fade (in→hold→out) and a subtle
    ease-out slide-up. Audio is stream-copied — these overlays are silent and must
    NOT interrupt the clip. `chips` = [{png, start, dur, x, y, scale}] (start/dur
    in the FINAL clip's own seconds; x/y are centre position as W/H fractions)."""
    chips = [c for c in chips if c.get("png") and Path(c["png"]).exists()]
    if not chips:
        return inp
    base_dur = max(0.1, probe(inp).duration)          # bound: -loop image is infinite
    inputs: list[str] = ["-i", inp]
    parts: list[str] = []
    cur = "[0:v]"
    for i, c in enumerate(chips):
        idx = i + 1
        inputs += ["-loop", "1", "-i", c["png"]]
        st = max(0.0, float(c.get("start", 0.0)))
        D = max(0.4, float(c.get("dur", 3.0)))
        r = min(0.4, D * 0.4)                         # eased ramp in/out
        rise = 46.0 * float(c.get("scale", 1.0))      # slide-up distance (px)
        X = max(0.0, min(float(c.get("x", 0.5)), 1.0))
        Y = max(0.0, min(float(c.get("y", 0.5)), 1.0))
        parts.append(
            f"[{idx}:v]format=rgba,"
            f"fade=t=in:st={st:.3f}:d={r:.3f}:alpha=1,"
            f"fade=t=out:st={max(st, st + D - r):.3f}:d={r:.3f}:alpha=1[sub{i}]"
        )
        out = "[v]" if i == len(chips) - 1 else f"[oc{i}]"
        # ease-out-cubic slide: y = target + rise*(1-p)^3, p=entry progress 0..1
        p = f"clip((t-{st:.3f})/{r:.3f}\\,0\\,1)"
        yexpr = (f"(main_h*{Y:.4f}-overlay_h/2)+{rise:.1f}*pow(1-{p}\\,3)")
        parts.append(
            f"{cur}[sub{i}]overlay=x=(main_w*{X:.4f}-overlay_w/2):"
            f"y='{yexpr}':eval=frame{out}"
        )
        cur = out
    filter_complex = ";".join(parts)
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a?",
        *_video_encode_args(),
        "-c:a", "copy",
        "-t", f"{base_dur:.3f}",                       # stop at the base clip's end
        "-movflags", "+faststart", outp,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return outp


def _apply_subscribe(inp: str, outp: str, opts: LayoutOpts, info: VideoInfo) -> str:
    """Editor path: bake the animated Subscribe CTA(s) for placed markers and
    overlay each over its final-time window (from `_normalize_subscribes`)."""
    import shutil
    import tempfile
    from . import cta
    subs = _normalize_subscribes(opts, info)
    if not subs:
        return inp
    cur = inp
    tmpdirs: list[str] = []
    try:
        for i, (tf, spec) in enumerate(subs):
            fdir = tempfile.mkdtemp(prefix="ctaframes_")
            tmpdirs.append(fdir)
            meta = cta.render_frames(
                fdir,
                style=spec.get("style", "classic"),
                size=float(spec.get("scale", 1.0)),
                anim=float(spec.get("anim", 0.7)),
                dur=float(spec.get("flash_dur", 4.0)),
                cx=1080 * float(spec.get("x", 0.5)),
                cy=1920 * float(spec.get("y", 0.5)),
                accent=spec.get("vd_accent", "#ff0033"))
            nxt = str(Path(outp).with_name(f"_cta_{i}_{Path(outp).name}"))
            cta.overlay(cur, nxt, fdir, start=tf, click_t=meta["click_t"],
                        sound=spec.get("cta_sound", "none"),
                        volume=float(spec.get("cta_volume", 1.0)))
            cur = nxt
        if cur != outp:
            shutil.move(cur, outp)
    finally:
        for d in tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        # clean any intermediate _cta_* files that weren't the final move
        for i in range(len(subs)):
            f = Path(outp).with_name(f"_cta_{i}_{Path(outp).name}")
            if f.exists() and str(f) != outp:
                try:
                    f.unlink()
                except OSError:
                    pass
    return outp


# ----------------------------------------------------------------------------- #
# Face Zoom — an eased "facecam grows" moment (bigger facecam pane + UI slide)
# ----------------------------------------------------------------------------- #
# Unlike the tint-flash effects, Face Zoom transforms LAYOUT: it eases the facecam
# pane taller (from its base share of the frame up to the marker's `grow` target and
# back), so the streamer cam becomes more prominent while the gameplay gives way
# below — then slides the template overlay (title/list) DOWN by exactly how far the
# seam moved, so the UI keeps sitting on the facecam/gameplay boundary. It is NOT a
# uniform magnification (that can't change the facecam:gameplay ratio); the two panes
# are RE-COMPOSITED per frame at a moving seam S(t): the facecam region is uniformly
# up-scaled (face bigger, sides cropped — no stretch) and the gameplay region is
# centre-cropped shorter. Idle (S=base seam) → ×1 scale + full crops = the original
# frame, clean. The overlay is DEFERRED out of the main compose render and handed to
# this pass so it can slide independently (see compose() + _face_zoom_exprs). The
# slide is DERIVED from the seam, not free — the title stays glued to the boundary.
# Self-closing (grow→hold→back within the marker window), so it doesn't bleed into
# the next Formatter segment.

def _normalize_face_zoom(opts: "LayoutOpts", info: VideoInfo):
    """Resolve opts.effects entries whose catalog spec has motion=="face_zoom" into
    normalized marker dicts (final-clip time + clamped per-marker params + sound).
    Same source→final-time mapping as the flash effects."""
    out = []
    for ef in opts.effects or []:
        base = _EFFECTS.get((ef or {}).get("type"))
        if not base or base.get("motion") != "face_zoom":
            continue
        try:
            t_src = float(ef.get("t"))
        except (TypeError, ValueError):
            continue
        if not _marker_in_window(t_src, opts, info):
            continue                                   # outside the (possibly scoped) trim → drop, don't snap
        # per-marker duration (clamped to the effect's slider bounds)
        D = float(base.get("flash_dur", 0.9))
        if ef.get("dur") is not None:
            try:
                lo, hi = base.get("dur_min", 0.3), base.get("dur_max", 3.0)
                D = max(lo, min(float(ef["dur"]), hi))
            except (TypeError, ValueError):
                pass
        # per-marker params merged onto catalog defaults + clamped to each range
        vals = {}
        marker_params = ef.get("params") or {}
        for p in base.get("params", []):
            k = p["key"]
            v = marker_params.get(k, p.get("default"))
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = float(p.get("default", 0.0))
            vals[k] = max(p["min"], min(v, p["max"]))
        # per-marker sound (a key from `sounds`; "none"/None = silent)
        snd_key = ef.get("sound")
        sounds = base.get("sounds") or []
        if snd_key == "none":
            snd = None
        elif snd_key:
            snd = next((s["file"] for s in sounds if s.get("key") == snd_key),
                       base.get("sound"))
        else:
            snd = base.get("sound")
        try:
            vol = max(0.0, min(float(ef.get("volume", 1.0)), 2.0))
        except (TypeError, ValueError):
            vol = 1.0
        out.append({
            "tf": _effect_final_time(t_src, opts, info),
            "dur": D, "sound": snd, "lead": float(base.get("lead", 0.0)),
            "grow": vals.get("grow", 0.50),
            "volume": vol,
        })
    out.sort(key=lambda x: x["tf"])
    return out


def _facecam_boundary_frac(info: VideoInfo, opts: "LayoutOpts") -> float:
    """Fraction of output H at the facecam/gameplay seam (0 if no facecam). Mirrors
    the top-pane height _build_filter computes, so Face Zoom can slide the UI by the
    exact amount that seam moves under the zoom."""
    if not (opts.facecam_enabled and opts.facecam is not None):
        return 0.0
    W, H = _even(opts.out_w), _even(opts.out_h)
    blur_mode = opts.gameplay_fill == "blur"
    bar = max(0.0, min(opts.blur_bar, 0.4)) if blur_mode else 0.0
    content_h = _even(H * (1 - 2 * bar)) or 2
    fc = opts.facecam.clamp(info.width, info.height)
    if opts.facecam_height:
        frac = max(0.15, min(float(opts.facecam_height), 0.62))
        top_h = _even(content_h * frac)
    elif opts.facecam_autofit:
        top_h = _even(W * fc.h / fc.w) if fc.w else _even(content_h * 0.35)
        top_h = max(_even(content_h * 0.15), min(top_h, _even(content_h * 0.62)))
    else:
        top_h = _even(content_h * opts.facecam_ratio)
    return (bar * H + top_h) / H


def _face_zoom_exprs(fz: list, H: int, B: float):
    """Build the per-frame ffmpeg expressions shared by the grow pass and the
    Formatter's baked-reveal slide. Returns (S, PULL):
      S    — the facecam/gameplay SEAM as a fraction of H, eased from the base seam
             `B` up to each marker's target `grow` and back (0→1→0 bump). Windows
             don't overlap, so the bumps just sum.
      PULL — overlay Y offset in px = (S-B)*H, so the title/list slide DOWN by exactly
             how far the seam moved and stay sitting on it. No facecam (B≤0) → no move.
    """
    if B <= 0:
        return f"({B:.5f})", "0"
    s_terms = []
    for m in fz:
        _st, _r, bump = _fz_bump(m["tf"], m["dur"], m["lead"])
        amp = max(0.0, float(m["grow"]) - B)     # only ever grows (never below base)
        s_terms.append(f"({amp:.5f})*{bump}")
    S = f"({B:.5f}+" + "+".join(s_terms) + ")" if s_terms else f"({B:.5f})"
    PULL = f"(({S}-{B:.5f})*{H})"
    return S, PULL


def _fz_bump(tf: float, D: float, lead: float):
    """One face-zoom marker's eased envelope: a raised-cosine trapezoid that eases
    0→1→0 over [st, st+D] with ramp `r` at each edge. Returns (st, r, bump_expr).
    Windows don't overlap in practice, so the pass just SUMS the amplitude-weighted
    bumps for zoom / focal / UI-slide."""
    st = max(0.0, tf - lead)
    r = max(0.05, min(D * 0.5, 0.28))
    se = st + D
    p = (f"min(clip((t-{st:.3f})/{r:.3f}\\,0\\,1)\\,"
         f"clip(({se:.3f}-t)/{r:.3f}\\,0\\,1))")
    return st, r, f"(0.5-0.5*cos(PI*{p}))"


def face_zoom_slides(opts: "LayoutOpts", info: VideoInfo, H: int):
    """UI-slide expression for a pre-composed segment, for callers that composite the
    overlay themselves (the Formatter's baked-reveal path). Returns a y-offset
    expression (pixels, +down) driven by the final-clip PTS `t` that tracks the
    facecam seam, or "0" if there are no markers / no facecam."""
    fz = _normalize_face_zoom(opts, info)
    if not fz:
        return "0"
    B = _facecam_boundary_frac(info, opts)
    _S, PULL = _face_zoom_exprs(fz, _even(H), B)
    return PULL


def _apply_face_zoom(inp: str, outp: str, fz: list, overlay_png, opts: "LayoutOpts",
                     info: VideoInfo) -> str:
    """Facecam-grow pass: eases the facecam pane taller (up to each marker's `grow`
    share of the frame) and back, the gameplay giving way below, while the (deferred)
    template overlay slides DOWN by the same amount so the title/list keep sitting on
    the facecam seam — plus an optional one-shot whoosh per marker. `fz` =
    _normalize_face_zoom() output."""
    if not fz:
        return inp
    W, H = _even(opts.out_w), _even(opts.out_h)
    dur = max(0.05, probe(inp).duration)

    # animated seam fraction S(t) + seam-locked UI-slide PULL(t) (shared builder)
    B = _facecam_boundary_frac(info, opts)
    S, PULL = _face_zoom_exprs(fz, H, B)

    inputs = ["-i", inp]
    if B <= 0:
        # No facecam pane to grow → keep the video as-is (the effect is a no-op
        # visually), but still slide/hold the overlay and mix the whoosh.
        parts = ["[0:v]null[vpanes]"]
    else:
        # Re-composite the two panes at the animated seam. This ffmpeg build's `crop`
        # can't animate its w/h, so we lean on the two filters that CAN take t-driven
        # expressions: `scale` (grow the facecam) + `overlay` (slide the gameplay).
        #   facecam — isolate the top pane, UNIFORM up-scale by f=S/B (face gets
        #             bigger, no stretch), overlay centred at the top; the extra width
        #             is clipped by the frame. Its height becomes S·H.
        #   gameplay — isolate the bottom pane (unchanged) and slide it DOWN so its top
        #             sits on the moving seam S·H; its bottom runs off-frame (clipped).
        # Idle (S=B → f=1) rebuilds the original frame exactly (verified passthrough).
        Bpx = _even(B * H)
        GHpx = H - Bpx                                    # base gameplay pane height
        f = f"({S}/{B:.5f})"                              # facecam up-scale factor ≥ 1
        parts = [
            f"[0:v]split=3[base][fcsrc][gpsrc]",
            f"[fcsrc]crop={W}:{Bpx}:0:0,"
            f"scale=w='ceil({W}*{f}/2)*2':h='ceil({Bpx}*{f}/2)*2':eval=frame[fcpane]",
            f"[gpsrc]crop={W}:{GHpx}:0:{Bpx}[gppane]",
            f"[base][fcpane]overlay=x='(W-w)/2':y=0:eval=frame[b1]",
            f"[b1][gppane]overlay=x=0:y='{S}*{H}':eval=frame[vpanes]",
        ]
    if overlay_png and Path(overlay_png).exists():
        inputs += ["-i", overlay_png]
        parts.append(f"[1:v]scale={W}:{H}[ovl]")
        parts.append(f"[vpanes][ovl]overlay=x=0:y='{PULL}':eval=frame:"
                     f"eof_action=repeat:format=auto,format=yuv420p[v]")
    else:
        parts.append("[vpanes]format=yuv420p[v]")
    vfc = ";".join(parts)

    # audio: source (or silence) base + one-shot whoosh mixed at each marker
    has_audio = _has_audio(inp)
    achain = []
    if has_audio:
        achain.append(f"[0:a]aresample={_WHOOSH_SR}[abase]")
    else:
        achain.append(f"anullsrc=r={_WHOOSH_SR}:cl=stereo,atrim=0:{dur:.3f}[abase]")
    labels = ["[abase]"]
    for k, m in enumerate(fz):
        snd = m.get("sound")
        if not snd:
            continue
        p = _SFX_DIR / Path(snd).name                  # .name guards against traversal
        if not p.exists():
            continue
        st = max(0.0, m["tf"] - m["lead"])
        ms = int(st * 1000)
        vol = float(m.get("volume", 1.0))
        labels.append(f"[fzs{k}]")
        achain.append(
            f"amovie={_ff_escape(str(p))},"
            f"aformat=sample_rates={_WHOOSH_SR}:channel_layouts=stereo,"
            f"volume={vol:.2f},adelay={ms}|{ms}[fzs{k}]")
    if len(labels) > 1:
        achain.append("".join(labels) +
                      f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                      f"atrim=0:{dur:.3f}[a]")
    else:
        achain.append(f"[abase]atrim=0:{dur:.3f}[a]")

    filter_complex = ";".join([vfc, *achain])
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *_video_encode_args(),
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{dur:.3f}",
        "-movflags", "+faststart", outp,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return outp


def _try_unlink(path: str) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _needs_temporal(info: VideoInfo, opts: LayoutOpts) -> bool:
    start = max(0.0, opts.trim_start or 0.0)
    end = opts.trim_end if (opts.trim_end is not None and opts.trim_end > start) else info.duration
    window = max(0.0, end - start)
    _, removed = _keep_intervals(window, opts.cuts, start)
    return (
        abs(_clamp_speed(opts.speed) - 1.0) > 1e-3
        or (bool(opts.cuts) and removed > 1e-3)
        or (bool(opts.audio_fade_out) and opts.audio_fade_dur > 0)
    )


def _apply_overlay(fc: str, opts: LayoutOpts, in_label: str = "v"):
    """
    Stamp opts.overlay_png (RGBA template graphic) on top of the composed video.
    Returns (filter_complex, video_label, extra_input_args). The PNG is scaled to
    the output size and drawn as the last spatial layer, so it appears in both
    the render and the still preview. `eof_action=repeat` holds the single image
    frame across the whole clip.
    """
    if opts.overlay_png and Path(opts.overlay_png).exists():
        W, H = _even(opts.out_w), _even(opts.out_h)
        fc = (f"{fc};[1:v]scale={W}:{H}[ovl];"
              f"[{in_label}][ovl]overlay=0:0:eof_action=repeat[vout]")
        return fc, "vout", ["-i", opts.overlay_png]
    return fc, in_label, []


def compose(src: str, dst: str, opts: LayoutOpts, skip_subscribe: bool = False) -> str:
    """Render the full vertical video with ffmpeg.

    `skip_subscribe`: don't bake the Subscribe CTA pass here — the caller will apply
    it later (via `_apply_subscribe`) so it lands ON TOP of a layer stamped after
    compose. The Formatter's baked-reveal path uses this: the ranking list reveal is
    composited over the segment AFTER compose, so the CTA must be deferred past it or
    the list items would cover the Subscribe widget."""
    info = probe(src)

    # Face Zoom (geometry punch-in), B-roll point effects, and the subscribe chip
    # all run as extra passes on the composed clip (their timing is the FINAL clip's
    # PTS), so render the layout to a temp first, then apply them in order:
    #   base → face_zoom (zoom + slide the deferred overlay) → flashes → subscribe.
    # The Subscribe CTA is ALWAYS the last pass so it renders above every other
    # element (list overlay, flashes, zoom) — it's the topmost call-to-action.
    fz = _normalize_face_zoom(opts, info)
    passes = []
    if fz:                                    passes.append("face")
    if _normalize_effects(opts, info):        passes.append("fx")
    if not skip_subscribe and _normalize_subscribes(opts, info):
        passes.append("sub")

    # Face Zoom needs the template overlay as its OWN layer (so it can slide down
    # independently of the video punch), so hold it back from the main render and
    # hand it to the zoom pass. Restored afterwards so the caller's opts is intact.
    deferred_overlay = None
    saved_overlay = opts.overlay_png
    if fz and opts.overlay_png and Path(opts.overlay_png).exists():
        deferred_overlay = opts.overlay_png
        opts.overlay_png = None

    render_dst = dst if not passes else str(Path(dst).with_name("_fxbase_" + Path(dst).name))
    try:
        if not _needs_temporal(info, opts):
            filter_complex = _build_filter(info, opts, out_label="v")
            filter_complex, vlbl, ov_in = _apply_overlay(filter_complex, opts)
            maps = ["-map", f"[{vlbl}]", "-map", "0:a?"]
        else:
            spatial = _build_filter(info, opts, out_label="vspatial")
            vchain, achain, has_audio = _temporal_filters(src, info, opts, "vspatial")
            parts = [spatial, *vchain, *achain]
            filter_complex = ";".join(parts)
            filter_complex, vlbl, ov_in = _apply_overlay(filter_complex, opts)
            maps = ["-map", f"[{vlbl}]"] + (["-map", "[a]"] if has_audio else [])

        cmd = [
            "ffmpeg", "-y",
            *_seek_args(opts),
            "-i", src,
            *ov_in,
            *_duration_args(opts),
            "-filter_complex", filter_complex,
            *maps,
            *_video_encode_args(),
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            render_dst,
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    finally:
        opts.overlay_png = saved_overlay

    # Chained post-passes on the composed base (each temp is cleaned as consumed).
    cur = render_dst
    for i, kind in enumerate(passes):
        nxt = dst if i == len(passes) - 1 else str(
            Path(dst).with_name(f"_fx{i}_" + Path(dst).name))
        try:
            if kind == "face":
                _apply_face_zoom(cur, nxt, fz, deferred_overlay, opts, info)
            elif kind == "fx":
                _apply_effects(cur, nxt, opts, info)
            else:
                _apply_subscribe(cur, nxt, opts, info)
        finally:
            _try_unlink(cur)
        cur = nxt
    return dst


def make_preview_frame(src: str, opts: LayoutOpts, dst: str, at: float = 0.4) -> str:
    """Render a single composed frame as a JPEG for quick UI preview."""
    info = probe(src)
    start = max(0.0, opts.trim_start or 0.0)
    end = opts.trim_end if (opts.trim_end is not None and opts.trim_end > start) else info.duration
    span = max(end - start, 0.0)
    t = start + span * at
    t = max(0.0, min(t, max(end - 0.1, 0.0)))
    filter_complex = _build_filter(info, opts)
    filter_complex, vlbl, ov_in = _apply_overlay(filter_complex, opts)
    cmd = [
        "ffmpeg", "-y", "-ss", str(t), "-i", src, *ov_in,
        "-filter_complex", filter_complex, "-map", f"[{vlbl}]",
        "-frames:v", "1", "-q:v", "3", dst,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return dst


# ----------------------------------------------------------------------------- #
# Formatter: join several pre-rendered segment files into one video.
# ----------------------------------------------------------------------------- #
def join_segments(seg_files: list[str], joins: list[dict], dst: str,
                  W: int = 1080, H: int = 1920, fps: float = 30.0) -> str:
    """
    Concatenate already-composed segment clips into one video, with a per-join
    transition + optional SFX. `joins[i]` describes the join AFTER segment i
    (len == len(seg_files) - 1): {"visual": key, "sound": key, "volume": float}.

    Same slice-and-concat model as `_temporal_segmented`, lifted to whole files:
    each segment is normalized (fps/scale/sar/audio), split into head/body/tail,
    and joined with an eased push (swipe), an xfade built-in, or a plain concat
    (cut, D=0). Audio uses the acrossfade/concat accumulator so A/V stay in sync;
    join SFX are mixed on top at each transition centre.
    """
    n = len(seg_files)
    if n == 0:
        raise ValueError("no segments to join")
    W, H = _even(W), _even(H)
    durs = [max(0.05, probe(f).duration) for f in seg_files]
    joins = list(joins or []) + [{}] * max(0, (n - 1) - len(joins or []))

    # resolve each join's effect + overlap (clamped to the shorter neighbour)
    D = [0.0] * (n - 1)
    vis = ["cut"] * (n - 1)
    for j in range(n - 1):
        v = _SWIPE_ALIAS.get(joins[j].get("visual", "cut"), joins[j].get("visual", "cut"))
        vis[j] = v
        if v != "cut" and (v in _SWIPE_VISUALS or v in _XFADE_JOINS):
            over = float(joins[j].get("overlap") or TRANSITION_DUR)
            D[j] = max(0.0, min(over, durs[j] - 0.1, durs[j + 1] - 0.1))
            if D[j] <= 0.05:
                D[j], vis[j] = 0.0, "cut"
    dprev = [D[i - 1] if i >= 1 else 0.0 for i in range(n)]
    dnext = [D[i] if i <= n - 2 else 0.0 for i in range(n)]

    chains: list[str] = []
    # --- normalize every input to a common format, then slice ------------------
    body_lbl: list[Optional[str]] = [None] * n
    head_lbl: list[Optional[str]] = [None] * n
    tail_lbl: list[Optional[str]] = [None] * n
    min_slice = 1.0 / max(fps, 1.0)
    for i in range(n):
        chains.append(
            f"[{i}:v]fps={fps:.4f},scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[nv{i}]"
        )
        b0 = min(dprev[i], durs[i])
        b1 = max(b0, durs[i] - dnext[i])
        slices = []
        if dprev[i] > 0:
            slices.append(("head", 0.0, dprev[i]))
        if b1 - b0 >= min_slice or (dprev[i] == 0 and dnext[i] == 0):
            slices.append(("body", b0, b1))
        if dnext[i] > 0:
            slices.append(("tail", durs[i] - dnext[i], durs[i]))
        outs = "".join(f"[nvs{i}_{k}]" for k in range(len(slices)))
        if len(slices) > 1:
            chains.append(f"[nv{i}]split={len(slices)}{outs}")
        for k, (kind, a, b) in enumerate(slices):
            src_lbl = f"[nv{i}]" if len(slices) == 1 else f"[nvs{i}_{k}]"
            lbl = f"[j{kind}{i}]"
            chains.append(f"{src_lbl}trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS{lbl}")
            {"head": head_lbl, "body": body_lbl, "tail": tail_lbl}[kind][i] = lbl

    # --- assemble video: body0, T0, body1, ... , body(n-1) --------------------
    pieces: list[str] = []
    acc_t = 0.0
    sfx_specs: list[tuple[float, str, float]] = []   # (centre, sound, volume)
    for i in range(n):
        if body_lbl[i]:
            pieces.append(body_lbl[i])
            b0 = min(dprev[i], durs[i])
            acc_t += max(b0, durs[i] - dnext[i]) - b0     # this body's duration
        if i < n - 1 and D[i] > 0 and tail_lbl[i] and head_lbl[i + 1]:
            out = f"[jt{i}]"
            if vis[i] in _SWIPE_VISUALS:
                chains += _push_chain(tail_lbl[i], head_lbl[i + 1], out, W, H, D[i], vis[i], fps)
            else:  # xfade built-in on the two D-length slices
                chains.append(f"{tail_lbl[i]}{head_lbl[i + 1]}"
                              f"xfade=transition={vis[i]}:duration={D[i]:.3f}:offset=0{out}")
            centre = acc_t + D[i] / 2.0
            snd = joins[i].get("sound", "none")
            if snd and snd != "none":
                sfx_specs.append((centre, snd, float(joins[i].get("volume") or 1.0)))
            pieces.append(out)
            acc_t += D[i]
    if len(pieces) > 1:
        chains.append("".join(pieces) + f"concat=n={len(pieces)}:v=1:a=0[vout]")
        vlabel = "[vout]"
    else:
        vlabel = pieces[0]

    # --- audio: normalize each seg (silence if missing), accumulate -----------
    for i in range(n):
        if _has_audio(seg_files[i]):
            chains.append(f"[{i}:a]aresample={_WHOOSH_SR},"
                          f"aformat=sample_rates={_WHOOSH_SR}:channel_layouts=stereo,"
                          f"asetpts=PTS-STARTPTS[na{i}]")
        else:
            chains.append(f"anullsrc=r={_WHOOSH_SR}:cl=stereo,"
                          f"atrim=0:{durs[i]:.3f}[na{i}]")
    acc = "[na0]"
    for j in range(1, n):
        out = f"[aj{j}]"
        if D[j - 1] > 0:
            chains.append(f"{acc}[na{j}]acrossfade=d={D[j - 1]:.3f}{out}")
        else:
            chains.append(f"{acc}[na{j}]concat=n=2:v=0:a=1{out}")
        acc = out

    # --- mix join SFX on top ---------------------------------------------------
    if sfx_specs:
        chains.append(f"{acc}aresample={_WHOOSH_SR}[abase]")
        labels = ["[abase]"]
        for k, (c, snd, vol) in enumerate(sfx_specs):
            p = _sfx_path(snd)
            lead = 0.45 * _sfx_dur(p) if p is not None else TRANSITION_DUR * 0.4
            ms = int(max(0.0, c - lead) * 1000)
            chains.append(_whoosh_source(f"jw{k}", ms, snd, vol))
            labels.append(f"[jw{k}]")
        chains.append("".join(labels) +
                      f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[aout]")
        alabel = "[aout]"
    else:
        alabel = acc

    filter_complex = ";".join(chains)
    cmd = ["ffmpeg", "-y"]
    for f in seg_files:
        cmd += ["-i", f]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", vlabel, "-map", alabel,
        *_video_encode_args(),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", dst,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return dst
