"""
Formatter reveal animations — server-rendered PNG-sequence overlays.

Two jobs:

  - `render_preview` — a short mp4 that auditions the reveal + SFX.
  - `bake_reveal`    — composite the reveal onto an already-composed segment
                       (used by the Formatter build).

Each element (title, rows, widgets, bonus, sparkles, clip-info card) comes from
`formatter.animation_layers` as a Pillow RGBA sprite. We composite one
transparent RGBA frame per tick, easing scale / offset / alpha / rotation in
Pillow (this ffmpeg build has no `drawtext` and can't ease `xfade`), then hand
the PNG sequence to ffmpeg.

Two clocks (mirrors the design editor exactly):
  - reveal starts/durations live in a 1× timebase and are divided by the
    reveal `speed` multiplier;
  - idle motion (widget bob, sparkle float), the bonus auto-hide, and the
    card's 0.5s entrance run in real output seconds, unscaled — like the CSS
    animations/transitions they port.

If any element idles (widgets bob, sparkles float), the overlay is animated for
the segment's **entire duration** ("baked idle motion"); otherwise frames stop
once everything has settled and the last frame is held — much cheaper.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from PIL import Image

from . import autolayout, formatter

FPS = 30

# UI-facing catalogs (surfaced via /api/formatter/meta).
ANIM_MODES = [
    {"key": "intro",    "label": "Intro (all text pops in)"},
    {"key": "new_item", "label": "New item reveal"},
]
ANIM_MOTIONS = [
    {"key": "pop",     "label": "Pop (scale + fade)"},
    {"key": "slide",   "label": "Slide up + fade"},
    {"key": "stagger", "label": "Stagger (per word / item)"},
]

# Timing (seconds, 1× timebase) — mirrors the design editor's constants.
_ANIM = 0.55           # per-element reveal duration
_ROW_STEP = 0.14       # gap between successive list rows (intro cascade)
_TITLE_TO_ROWS = 0.22  # delay before the first row after the title (intro)
_PART_STEP = 0.10      # gap between sub-parts (stagger: words / number+label)
_NEW_ITEM_LEAD = 0.15  # let the backdrop show before the new item lands
_TAIL = 0.40           # hold after the last element is fully in

_CARD_DUR = 0.5        # clip-info card entrance (real seconds)
_HIDE_DUR = 0.55       # bonus auto-hide fade (real seconds)


def _ease_out_cubic(p: float) -> float:
    return 1 - (1 - p) ** 3


def _ease_out_back(p: float) -> float:
    """Overshoot-and-settle easing (the CapCut-style 'pop')."""
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _clamp01(p: float) -> float:
    return 0.0 if p < 0 else (1.0 if p > 1 else p)


def _transform(motion: str, p: float, slide_px: float) -> tuple[float, float, float]:
    """(scale, dy, alpha) for reveal progress p in the given motion."""
    p = _clamp01(p)
    alpha = _ease_out_cubic(min(1.0, p * 1.35))     # fade a touch ahead of the move
    if motion == "slide":
        return 1.0, (1 - _ease_out_cubic(p)) * slide_px, alpha
    return 0.60 + 0.40 * _ease_out_back(p), 0.0, alpha


def _idle_transform(el: dict, t_real: float, u: float) -> tuple[float, float, float, float]:
    """(scale_mult, dy, rot_deg, alpha_mult) of the perpetual float, in real
    seconds. Ports the design's `iconIdle` (widgets) / `sparkleFloat` CSS."""
    idle = el.get("idle")
    if not idle:
        return 1.0, 0.0, 0.0, 1.0
    period = float(idle.get("period", 2.6))
    delay = float(idle.get("delay", 0.0))
    t = t_real - delay
    if t <= 0 or period <= 0:
        wave = 0.0
        cosw = 1.0
    else:
        ph = (t % period) / period
        wave = math.sin(math.pi * ph) ** 2          # 0 → 1 → 0, eased
        cosw = math.cos(2 * math.pi * ph)           # 1 → -1 → 1
    amp = float(idle.get("amp", 0.0)) * u
    if el.get("kind") == "sparkle":
        scale = 1.0 + float(idle.get("scale", 0.0)) * wave
        rot = float(idle.get("rot", 0.0)) * wave
        a_lo, a_hi = idle.get("alpha") or (1.0, 1.0)
        alpha = a_lo + (a_hi - a_lo) * wave
    else:                                            # widget bob
        scale = 1.0
        rot = -float(idle.get("rot", 0.0)) * cosw
        alpha = 1.0
    return scale, -amp * wave, rot, alpha


def _hide_progress(el: dict, t_real: float) -> float:
    """0 → visible, 1 → fully hidden (bonus auto-hide, real seconds)."""
    hide_at = el.get("hide_at")
    if hide_at is None:
        return 0.0
    return _clamp01((t_real - float(hide_at)) / _HIDE_DUR)


def _paste(canvas: Image.Image, sprite: Image.Image, px: float, py: float,
           scale: float, dy: float, alpha: float, rot: float = 0.0) -> None:
    """Composite a sprite scaled/rotated about its centre, nudged by dy."""
    if alpha <= 0.002:
        return
    img = sprite
    w, h = img.size
    if abs(rot) > 0.05:
        img = img.rotate(rot, resample=Image.BICUBIC, expand=True)
    if abs(scale - 1.0) > 1e-3:
        nw, nh = max(1, int(round(img.width * scale))), max(1, int(round(img.height * scale)))
        img = img.resize((nw, nh), Image.BICUBIC)
    px -= (img.width - w) / 2
    py -= (img.height - h) / 2
    if alpha < 0.999:
        a = img.split()[3].point(lambda v: int(v * max(0.0, alpha)))
        img = img.copy()
        img.putalpha(a)
    canvas.alpha_composite(img, (int(round(px)), int(round(py + dy))))


def _element_start(el: dict, mode: str) -> float:
    """Reveal start in the 1× timebase."""
    if el.get("start") is not None:
        return float(el["start"])
    if mode == "new_item":
        return _NEW_ITEM_LEAD
    if el["kind"] == "title":
        return 0.0
    return _TITLE_TO_ROWS + el.get("index", 0) * _ROW_STEP


def _element_motion(el: dict, motion: str) -> str:
    return el.get("motion") or motion


def _element_span(el: dict, motion: str) -> float:
    if el.get("entrance") == "card":
        return _CARD_DUR                       # (real secs; close enough for settle calc)
    if _element_motion(el, motion) == "stagger" and el["parts"]:
        return (len(el["parts"]) - 1) * _PART_STEP + _ANIM
    return _ANIM


def _widget_extra(el: dict, motion: str) -> float:
    """A widget rides its row's *label* in stagger mode (number, then label)."""
    return _PART_STEP if (el.get("kind") == "widget" and motion == "stagger") else 0.0


def _has_idle(elements: list[dict]) -> bool:
    return any(el.get("idle") and el.get("hide_at") is None for el in elements)


def _settle_time(elements: list[dict], mode: str, motion: str, speed: float) -> float:
    """When (output seconds) the last *finite* motion lands: reveals (scaled by
    speed) plus the bonus auto-hide and card entrance (real seconds)."""
    end = 0.0
    for el in elements:
        if not el["static"]:
            if el.get("start_real") is not None:      # delayed entrance (bonus)
                e_speed = float(el.get("rev_speed") or speed)
                end = max(end, float(el["start_real"]) + _ANIM / e_speed)
            else:
                s = _element_start(el, mode) + _widget_extra(el, motion)
                if el.get("entrance") == "card":
                    end = max(end, s / speed + _CARD_DUR)
                else:
                    end = max(end, (s + _element_span(el, motion)) / speed)
        if el.get("hide_at") is not None:
            end = max(end, float(el["hide_at"]) + _HIDE_DUR)
    return round(end, 3)


def _sfx_lead(elements: list[dict], mode: str, speed: float) -> float:
    # delayed-entrance elements carry their own SFX (see _delayed_sound), so they
    # must not pull the main reveal sound earlier.
    animated = [e for e in elements if not e["static"] and e.get("start_real") is None]
    return min((_element_start(e, mode) for e in animated), default=0.0) / speed


def _delayed_sound(elements: list[dict]):
    """(path, delay_secs, volume) for a delayed bonus-entrance SFX, or None. The
    bonus element carries its own sound so it lands on the entrance, not the
    reveal start."""
    for el in elements:
        if el.get("start_real") is not None and (el.get("sound") or "none") != "none":
            p = autolayout.combined_sfx_path(el["sound"])
            if p is not None:
                return str(p), float(el["start_real"]), float(el.get("volume", 1.0))
    return None


def _render_frames(elements: list[dict], mode: str, motion: str, speed: float,
                   frames_dir: Path, W: int, H: int, total_dur: float,
                   delay: float = 0.0) -> int:
    """Render one transparent RGBA PNG per tick into frames_dir.

    Reveal progress runs on the speed-scaled clock; idle bob / sparkle float /
    bonus hide / card entrance run on the real clock (see module docstring).

    `delay` (real seconds) holds the whole reveal back — used when a Clip Hook
    covers the opening of the first segment: nothing reveals until the hook has
    shrunk away. Every element is invisible while t < delay (reveal progress is
    negative → alpha 0), then plays normally on a clock shifted by `delay`.

    Returns the frame count."""
    slide_px = H * 0.045
    u = W / 1080.0
    frames_dir.mkdir(parents=True, exist_ok=True)
    n_frames = max(1, int(math.ceil(total_dur * FPS)))
    for f in range(n_frames):
        t_real = f / FPS - delay
        t_anim = t_real * speed
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        for el in elements:
            hide = _hide_progress(el, t_real)
            if hide >= 0.999:
                continue
            i_scale, i_dy, i_rot, i_alpha = _idle_transform(el, t_real, u)
            h_ease = _ease_out_cubic(hide)
            h_alpha = 1.0 - hide
            h_dy = -22.0 * u * h_ease
            h_scale = 1.0 - 0.1 * h_ease

            if el["static"]:
                _paste(canvas, el["sprite"], el["px"], el["py"],
                       i_scale * h_scale, i_dy + h_dy, i_alpha * h_alpha, i_rot)
                continue

            # Delayed entrance (bonus title): its own real-seconds start + speed,
            # independent of the segment reveal clock.
            if el.get("start_real") is not None:
                e_speed = float(el.get("rev_speed") or speed)
                emotion = _element_motion(el, motion)
                prog = (t_real - float(el["start_real"])) * e_speed / _ANIM
                s, dy, a = _transform(emotion, prog, slide_px)
                _paste(canvas, el["sprite"], el["px"], el["py"],
                       s * i_scale * h_scale, dy + i_dy + h_dy,
                       a * i_alpha * h_alpha, i_rot)
                continue

            start = _element_start(el, mode) + _widget_extra(el, motion)
            emotion = _element_motion(el, motion)
            if el.get("entrance") == "card":
                p = _clamp01((t_real - start / speed) / _CARD_DUR)
                a = _ease_out_cubic(min(1.0, p * 1.35))
                dy = (1 - _ease_out_cubic(p)) * 28.0 * u
                _paste(canvas, el["sprite"], el["px"], el["py"], 1.0, dy, a)
            elif emotion == "stagger" and el["parts"]:
                for pi, part in enumerate(el["parts"]):
                    s, dy, a = _transform("pop", (t_anim - start - pi * _PART_STEP) / _ANIM,
                                          slide_px)
                    _paste(canvas, part["sprite"], part["px"], part["py"],
                           s * h_scale, dy + h_dy, a * h_alpha)
            else:
                s, dy, a = _transform(emotion, (t_anim - start) / _ANIM, slide_px)
                _paste(canvas, el["sprite"], el["px"], el["py"],
                       s * i_scale * h_scale, dy + i_dy + h_dy,
                       a * i_alpha * h_alpha, i_rot)
        canvas.save(frames_dir / f"f_{f:05d}.png")
    return n_frames


def render_preview(preset_key: str, overrides: Optional[dict], mode: str, motion: str,
                   sound: str, volume: float, bg_path: str, dst: str,
                   target_index: int = -1, W: int = 1080, H: int = 1920,
                   speed: float = 1.0) -> dict:
    """Render a short animated preview mp4 to `dst`; returns {"duration": secs}."""
    mode = mode if mode in {"intro", "new_item"} else "intro"
    motion = motion if motion in {"pop", "slide", "stagger"} else "pop"
    speed = max(0.25, min(float(speed), 4.0))
    elements = formatter.animation_layers(preset_key, overrides, W, H, mode, target_index)
    settle = _settle_time(elements, mode, motion, speed)
    dur = min(settle, 8.0) + _TAIL + (2.2 if _has_idle(elements) else 0.0)
    lead = _sfx_lead(elements, mode, speed)

    frames_dir = Path(dst).parent / (Path(dst).stem + "_frames")
    shutil.rmtree(frames_dir, ignore_errors=True)
    bsnd = _delayed_sound(elements)
    try:
        _render_frames(elements, mode, motion, speed, frames_dir, W, H, dur)
        _encode(bg_path, frames_dir, sound, volume, lead, dur, dst, W, H,
                extra_sfx=[bsnd] if bsnd else None)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return {"duration": dur}


def bake_reveal(segment_path: str, dst: str,
                preset_key: str, overrides: Optional[dict], mode: str,
                cfg: dict, target_index: int = -1,
                W: int = 1080, H: int = 1920, face_zoom_y: str = "0",
                reveal_delay: float = 0.0) -> dict:
    """Bake the reveal (and any idle motion) onto an already-composed segment.

    `face_zoom_y` is an ffmpeg y-offset expression (pixels, +down, driven by the
    clip PTS `t`) that slides the reveal overlay DOWN in lock-step with a Face Zoom
    punch-in already baked into `segment_path`'s video — so the ranking UI and the
    zoomed gameplay stay in line. "0" (default) = no slide.

    Without idle elements: animation frames for the settle window, then the
    last frame held — the hand-off is seamless because the final frame lands
    everything at rest. With idle elements (widgets bob, sparkles float): one
    animated overlay for the whole segment.

    `cfg` = {motion, speed, sound, volume}. Returns {"duration": reveal_secs}.
    """
    mode = mode if mode in {"intro", "new_item"} else "intro"
    motion = cfg.get("motion", "pop")
    motion = motion if motion in {"pop", "slide", "stagger"} else "pop"
    speed = max(0.25, min(float(cfg.get("speed", 1.0) or 1.0), 4.0))
    sound = cfg.get("sound", "none")
    volume = max(0.0, min(float(cfg.get("volume", 1.0) or 1.0), 2.0))

    reveal_delay = max(0.0, float(reveal_delay or 0.0))
    elements = formatter.animation_layers(preset_key, overrides, W, H, mode, target_index)
    settle = _settle_time(elements, mode, motion, speed) + reveal_delay
    lead = _sfx_lead(elements, mode, speed) + reveal_delay
    seg_dur = max(0.1, autolayout.probe(segment_path).duration)
    full = _has_idle(elements)
    anim_dur = seg_dur if full else min(settle, seg_dur)

    frames_dir = Path(dst).parent / (Path(dst).stem + "_revframes")
    shutil.rmtree(frames_dir, ignore_errors=True)
    try:
        n = _render_frames(elements, mode, motion, speed, frames_dir, W, H,
                           anim_dur + (2.0 / FPS if full else 0.0),
                           delay=reveal_delay)
        has_audio = autolayout._has_audio(segment_path)
        sfx = autolayout._sfx_path(sound) if sound and sound != "none" else None

        # y-offset token for the reveal overlay (slides with a baked Face Zoom)
        oy = f"'{face_zoom_y}':eval=frame" if (face_zoom_y and face_zoom_y != "0") else "0"
        if full:
            # animated overlay for the whole segment (overlay holds its last
            # frame — repeatlast — if the sequence runs out a tick early)
            vparts = [
                f"[1:v]scale={W}:{H},setsar=1[anim]",
                f"[0:v][anim]overlay=0:{oy},format=yuv420p[vout]",
            ]
            inputs = ["-framerate", str(FPS), "-i", str(frames_dir / "f_%05d.png")]
        else:
            hold_png = str(frames_dir / f"f_{n - 1:05d}.png")   # settled overlay
            vparts = [
                f"[1:v]scale={W}:{H},setsar=1[stat]",
                f"[2:v]scale={W}:{H},setsar=1[anim]",
                f"[0:v][anim]overlay=0:{oy}:enable='lt(t,{anim_dur:.3f})':eof_action=pass[va]",
                f"[va][stat]overlay=0:{oy}:enable='gte(t,{anim_dur:.3f})',format=yuv420p[vout]",
            ]
            inputs = ["-loop", "1", "-i", hold_png,
                      "-framerate", str(FPS), "-i", str(frames_dir / "f_%05d.png")]

        chains = list(vparts)
        amaps: list[str] = []
        extra: list[str] = []                       # SFX labels to mix over clip audio
        if has_audio and sfx is not None:
            delay = max(0, int(lead * 1000))
            chains.append(
                f"amovie={autolayout._ff_escape(str(sfx))},"
                f"aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={volume:.2f},adelay={delay}|{delay}[sfx]")
            extra.append("[sfx]")
        if has_audio:
            bsnd = _delayed_sound(elements)         # delayed bonus-entrance SFX
            if bsnd is not None:
                bp, bdelay, bvol = bsnd
                # shift by reveal_delay too (a Clip Hook holds the whole reveal —
                # incl. the bonus entrance — until the cold-open shrinks away)
                bms = max(0, int((bdelay + reveal_delay) * 1000))
                chains.append(
                    f"amovie={autolayout._ff_escape(bp)},"
                    f"aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"volume={bvol:.2f},adelay={bms}|{bms}[bsfx]")
                extra.append("[bsfx]")
        if has_audio and extra:
            ins = ["[0:a]"] + extra
            chains.append("".join(ins) +
                          f"amix=inputs={len(ins)}:normalize=0:duration=first[aout]")
            amaps = ["-map", "[aout]"]
        elif has_audio:
            amaps = ["-map", "0:a"]

        cmd = [
            "ffmpeg", "-y",
            "-i", segment_path,
            *inputs,
            "-filter_complex", ";".join(chains),
            "-map", "[vout]", *amaps,
            "-t", f"{seg_dur:.3f}",             # bound output (looped/held inputs are infinite)
            *autolayout._video_encode_args(),
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
            dst,
        ]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return {"duration": min(settle, seg_dur)}


def _encode(bg_path: str, frames_dir: Path, sound: str, volume: float,
            sfx_lead: float, dur: float, dst: str, W: int, H: int,
            extra_sfx: Optional[list] = None) -> None:
    """`extra_sfx` = list of (path, delay_secs, volume) one-shots (e.g. the delayed
    bonus-entrance SFX) mixed over the reveal sound on a silence base."""
    vol = max(0.0, min(float(volume), 2.0))
    p = autolayout._sfx_path(sound) if sound and sound != "none" else None
    # silence base guarantees a fixed-length stereo track to hang the SFX on
    parts = [f"anullsrc=r=48000:cl=stereo:d={dur:.3f}[abase]"]
    labels = ["[abase]"]
    if p is not None:
        delay = max(0, int(sfx_lead * 1000))
        parts.append(f"amovie={autolayout._ff_escape(str(p))},"
                     f"aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"volume={vol:.2f},adelay={delay}|{delay}[s0]")
        labels.append("[s0]")
    for i, es in enumerate(extra_sfx or []):
        ep, edelay, evol = es
        ems = max(0, int(float(edelay) * 1000))
        parts.append(f"amovie={autolayout._ff_escape(str(ep))},"
                     f"aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"volume={max(0.0, min(float(evol), 2.0)):.2f},adelay={ems}|{ems}[e{i}]")
        labels.append(f"[e{i}]")
    if len(labels) > 1:
        parts.append("".join(labels) +
                     f"amix=inputs={len(labels)}:normalize=0:duration=first,"
                     f"atrim=0:{dur:.3f}[a]")
    else:
        parts.append(f"[abase]atrim=0:{dur:.3f}[a]")
    fc = (f"[0:v]scale={W}:{H},setsar=1[bg];"
          f"[bg][1:v]overlay=0:0:format=auto,format=yuv420p[v];"
          f"{';'.join(parts)}")
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", f"{dur:.3f}", "-i", str(bg_path),
        "-framerate", str(FPS), "-i", str(frames_dir / "f_%05d.png"),
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-r", str(FPS), "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
