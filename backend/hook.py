"""
Clip Hook — a framed "cold open" baked as a PNG frame sequence.

Ports the Claude Design mock ("Clip Hook" widget in Shorts Ranking
Templates.dc.html): the short opens on a chosen MOMENT from any clip, playing
inside a big centred framed box, which then shrinks away to reveal the ranking.
Four frame looks — `glow` / `shadow` / `border` / `brackets` — each with a chosen
accent colour, a hold length, and a click SFX.

It is a **Formatter** widget only. The box overlays the OPENING of the finished
Top-N (no added length); the first segment's ranking reveal is held back
(`animate.bake_reveal(reveal_delay=…)`) until the hook has shrunk away, so nothing
competes with the cold open.

Same baking philosophy as `cta.py` / `animate.py`: this ffmpeg build has no
`drawtext` and can't ease `xfade`, so every frame is composited in Pillow (we ease
the box scale-in / hold / shrink and the frame's idle motion ourselves) and the
sequence is handed to ffmpeg `overlay`. The box CONTENT is live clip footage:
`render_frames()` extracts the moment to stills (object-fit cover) and bakes them
into each frame. `overlay()` stamps the sequence onto the front of the final clip
and fires the SFX at t=0.

Geometry is design px at a 1080×1920 stage (box 936×1360, centred).
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import autolayout

FPS = 30
STAGE_W, STAGE_H = 1080, 1920
# The box is 9:16 — it frames the EDITED vertical clip (facecam + gameplay, exactly
# what the ranking segment shows), so the whole composed frame fits with no crop.
BOX_W, BOX_H = 936, 1664
RADIUS = 36
STYLES = ["glow", "shadow", "border", "brackets"]
HOOK_COLORS = ["#2ea6ff", "#ff0033", "#f5b942", "#59e0c5", "#a259ff", "#ffffff"]

_ENTRANCE = 0.45          # hookIn scale-in
_SHRINK = 0.56            # shrink-away
_PAD = 120                # canvas margin around the box (glow / slab / brackets)


# --- easing ------------------------------------------------------------------ #
def _clip01(p: float) -> float:
    return 0.0 if p < 0 else (1.0 if p > 1 else p)


def _ease_out_back(p: float) -> float:
    c1, c3 = 1.4, 2.4
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _ease_io(p: float) -> float:                 # ~cubic-bezier(.6,0,.35,1)
    return p * p * (3 - 2 * p)


def _hex(h: str) -> tuple[int, int, int]:
    h = (h or "#2ea6ff").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# --- static helpers ---------------------------------------------------------- #
def _rounded_mask(w: int, h: int, r: int) -> Image.Image:
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return m


def _conic_ring(size: int, accent: tuple) -> Image.Image:
    """A conic-gradient accent ring (numpy) — rotated per frame for the `border`
    spin. Two bright accent arcs sweeping to transparent, matching the design."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    cx = cy = (size - 1) / 2.0
    ang = (np.arctan2(yy - cy, xx - cx) + math.pi) / (2 * math.pi)   # 0..1
    # two bright zones (accent), fading to near-transparent between (design stops)
    a = 0.5 + 0.5 * np.cos((ang * 2 - 0.15) * 2 * math.pi)
    a = np.clip(a ** 1.4, 0.0, 1.0)
    rgba = np.zeros((size, size, 4), np.uint8)
    rgba[..., 0] = accent[0]
    rgba[..., 1] = accent[1]
    rgba[..., 2] = accent[2]
    rgba[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def _bracket(size: int, accent: tuple, corner: str) -> Image.Image:
    """One L-shaped corner bracket (7px arms, rounded outer)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = 7
    col = (*accent, 255)
    if "l" in corner:
        d.rounded_rectangle([0, 0, w, size], radius=3, fill=col)
    if "r" in corner:
        d.rounded_rectangle([size - w, 0, size, size], radius=3, fill=col)
    if "t" in corner:
        d.rounded_rectangle([0, 0, size, w], radius=3, fill=col)
    if "b" in corner:
        d.rounded_rectangle([0, size - w, size, size], radius=3, fill=col)
    return img


# --- box builders (per style) ------------------------------------------------ #
def _build_box(content: Image.Image, style: str, accent: tuple,
               t_real: float, spin_cache) -> Image.Image:
    """Composite the framed box (content + decoration) onto a padded canvas.
    Returns an RGBA canvas of size (BOX_W+2P, BOX_H+2P) with the box centred."""
    P = _PAD
    cw, ch = BOX_W + 2 * P, BOX_H + 2 * P
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    bx, by = P, P                                    # box top-left within canvas

    # rounded content (object-fit cover already applied on extract)
    content = content.copy()
    content.putalpha(_rounded_mask(BOX_W, BOX_H, RADIUS))

    if style == "shadow":
        # offset colored slab, bobbing (hookSlab)
        bob = math.sin(t_real / 3.2 * 2 * math.pi) * 6
        slab = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
        ImageDraw.Draw(slab).rounded_rectangle([0, 0, BOX_W - 1, BOX_H - 1],
                                               radius=RADIUS, fill=(*accent, 235))
        canvas.alpha_composite(slab, (bx + int(24 + bob), by + int(32 + bob)))
        _drop_shadow(canvas, bx, by, BOX_W, BOX_H, RADIUS, 46, 185)
        canvas.alpha_composite(content, (bx, by))

    elif style == "border":
        # spinning conic-gradient ring, confined to the box (content inset 8px sits
        # on top, so only a thin animated accent frame shows — not a full bleed)
        _drop_shadow(canvas, bx, by, BOX_W, BOX_H, RADIUS, 40, 150)
        ring = spin_cache["ring"]
        angle = (t_real / 3.4) * 360.0
        rot = ring.rotate(-angle, resample=Image.BILINEAR, expand=False)
        rl = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
        rl.alpha_composite(rot, (BOX_W // 2 - rot.width // 2, BOX_H // 2 - rot.height // 2))
        rl.putalpha(ImageChops.multiply(rl.split()[3], _rounded_mask(BOX_W, BOX_H, RADIUS)))
        canvas.alpha_composite(rl, (bx, by))
        inset = 8
        ic = content.resize((BOX_W - 2 * inset, BOX_H - 2 * inset), Image.LANCZOS)
        ic.putalpha(_rounded_mask(BOX_W - 2 * inset, BOX_H - 2 * inset, RADIUS - 6))
        canvas.alpha_composite(ic, (bx + inset, by + inset))

    elif style == "brackets":
        _drop_shadow(canvas, bx, by, BOX_W, BOX_H, RADIUS, 40, 155)
        # faint accent hairline border on the box
        bordered = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
        ImageDraw.Draw(bordered).rounded_rectangle(
            [0, 0, BOX_W - 1, BOX_H - 1], radius=RADIUS,
            outline=(*accent, 90), width=2)
        canvas.alpha_composite(content, (bx, by))
        canvas.alpha_composite(bordered, (bx, by))
        # 4 corner brackets, pulsing alpha (hookBracket, staggered)
        bsz = 106
        for i, cn in enumerate(("lt", "rt", "lb", "rb")):
            pulse = 0.7 + 0.3 * (0.5 + 0.5 * math.sin(
                (t_real / 1.8 - i * 0.15) * 2 * math.pi))
            br = _bracket(bsz, accent, cn)
            br.putalpha(br.split()[3].point(lambda v: int(v * pulse)))
            ox = bx - 4 if "l" in cn else bx + BOX_W + 4 - bsz
            oy = by - 4 if "t" in cn else by + BOX_H + 4 - bsz
            canvas.alpha_composite(br, (ox, oy))
        # pulsing dot, top-left inside
        dp = 0.55 + 0.4 * (0.5 + 0.5 * math.sin(t_real / 1.3 * 2 * math.pi))
        dot = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(dot).ellipse([19, 19, 41, 41], fill=(*accent, int(255 * dp)))
        dot = dot.filter(ImageFilter.GaussianBlur(1))
        ImageDraw.Draw(dot).ellipse([19, 19, 41, 41], fill=(*accent, 255))
        canvas.alpha_composite(dot, (bx + 36 - 30, by + 36 - 30))

    else:  # glow (default)
        pulse = 0.62 + 0.38 * (0.5 + 0.5 * math.sin(t_real / 2.4 * 2 * math.pi))
        glow = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            [bx, by, bx + BOX_W, by + BOX_H], radius=RADIUS,
            fill=(*accent, int(200 * pulse)))
        canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))
        _drop_shadow(canvas, bx, by, BOX_W, BOX_H, RADIUS, 40, 150)
        canvas.alpha_composite(content, (bx, by))
        # inset accent border
        bd = Image.new("RGBA", (BOX_W, BOX_H), (0, 0, 0, 0))
        ImageDraw.Draw(bd).rounded_rectangle([1, 1, BOX_W - 2, BOX_H - 2],
                                             radius=RADIUS, outline=(*accent, 150), width=2)
        canvas.alpha_composite(bd, (bx, by))

    return canvas


def _drop_shadow(canvas, x, y, w, h, r, blur, alpha):
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([x, y + 18, x + w, y + h + 18],
                                         radius=r, fill=(0, 0, 0, alpha))
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))


# --- content extraction ------------------------------------------------------ #
def _extract_content(clip_path: str, moment: float, secs: float, fps: int,
                     out_dir: Path) -> int:
    """Extract `secs` from `clip_path` at `moment`, cover-scaled + cropped to the
    box, as c_%05d.png. Returns the frame count found."""
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={BOX_W}:{BOX_H}:force_original_aspect_ratio=increase,"
          f"crop={BOX_W}:{BOX_H},fps={fps}")
    def _grab(ss: float) -> int:
        for p in out_dir.glob("c_*.png"):
            p.unlink()
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{max(0.0, ss):.3f}", "-t", f"{secs:.3f}",
             "-i", clip_path, "-vf", vf, "-fps_mode", "cfr",
             str(out_dir / "c_%05d.png")],
            capture_output=True, text=True, check=True)
        return len(list(out_dir.glob("c_*.png")))

    n = _grab(moment)
    if n == 0 and moment > 0:                    # moment past the clip end → fall back to the start
        n = _grab(0.0)
    return n


# --- timing ------------------------------------------------------------------ #
def timings(length: float, in_speed: float = 1.0, out_speed: float = 1.0) -> dict:
    """Resolve the hook's on-screen timing. There is NO pop-in — the box is full
    from frame 0 (`entrance = 0`); the clip just starts playing inside it. `out_speed`
    scales the shrink-OUT duration (higher = snappier); `length` (the hold) is
    untouched. `in_speed` is accepted for payload compatibility but unused.
    Returns {entrance, hold, shrink, total}."""
    osp = max(0.3, min(float(out_speed or 1.0), 3.0))
    hold = max(0.5, min(float(length), 8.0))
    entrance = 0.0                       # no pop-in — box appears full immediately
    shrink = _SHRINK / osp
    return {"entrance": entrance, "hold": hold, "shrink": shrink,
            "total": hold + shrink}


# --- frame baking ------------------------------------------------------------ #
def render_frames(frames_dir, clip_path: str, moment: float = 0.0,
                  length: float = 1.0, style: str = "glow",
                  accent: str = "#2ea6ff", in_speed: float = 1.0,
                  out_speed: float = 1.0, W: int = STAGE_W, H: int = STAGE_H,
                  fps: int = FPS) -> dict:
    """Bake the hook to `frames_dir/f_%05d.png` (transparent RGBA stage frames).
    Returns {n, total, shrink_start, entrance, shrink}."""
    style = style if style in STYLES else "glow"
    acc = _hex(accent)
    tm = timings(length, in_speed, out_speed)
    entrance, length, shrink, total = tm["entrance"], tm["hold"], tm["shrink"], tm["total"]
    n = max(2, int(round(total * fps)))

    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    cdir = Path(tempfile.mkdtemp(prefix="hookcontent_"))
    spin_cache = {}
    if style == "border":
        ring_sz = int(max(BOX_W, BOX_H) * 1.5)
        spin_cache["ring"] = _conic_ring(ring_sz, acc)
    try:
        n_content = _extract_content(clip_path, moment, total + 0.2, fps, cdir)
        n_content = max(1, n_content)
        cx0, cy0 = W / 2.0, H / 2.0

        for i in range(n):
            t = i / fps
            # box transform: full from frame 0 (no pop-in) → hold → shrink away
            if t < length:
                s, op = 1.0, 1.0
            else:
                p = _clip01((t - length) / shrink)
                s = 1.0 - _ease_io(p) * 0.9
                op = 1.0 - _ease_io(p)

            frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            if op <= 0.01 or s <= 0.02:
                frame.save(frames_dir / f"f_{i:05d}.png")
                continue

            ci = min(n_content - 1, i)
            cpath = cdir / f"c_{ci + 1:05d}.png"
            if not cpath.exists():
                cpath = sorted(cdir.glob("c_*.png"))[min(ci, n_content - 1)]
            content = Image.open(cpath).convert("RGB")

            box = _build_box(content, style, acc, t, spin_cache)
            bw, bh = max(1, int(box.width * s)), max(1, int(box.height * s))
            scaled = box.resize((bw, bh), Image.LANCZOS)
            if op < 0.999:
                scaled.putalpha(scaled.split()[3].point(lambda v: int(v * op)))
            frame.alpha_composite(scaled, (int(cx0 - bw / 2), int(cy0 - bh / 2)))
            frame.save(frames_dir / f"f_{i:05d}.png")
    finally:
        shutil.rmtree(cdir, ignore_errors=True)

    return {"n": n, "total": total, "shrink_start": length,
            "entrance": entrance, "shrink": shrink}


# --- compositing ------------------------------------------------------------- #
def overlay(inp: str, outp: str, frames_dir, sound: str = "none",
            volume: float = 1.0, duck_until: float = 0.0, duck_fade: float = _SHRINK,
            pop_in: float = _ENTRANCE,
            hook_clip: str | None = None, hook_moment: float = 0.0,
            hook_volume: float = 1.0, freeze: bool = False, freeze_dur: float = 0.0,
            W: int = STAGE_W, H: int = STAGE_H, fps: int = FPS) -> str:
    """Overlay the baked hook sequence onto the FRONT of `inp` (starts at t=0);
    fire the SFX at t=0.

    Audio during the cold-open: the HOOK CLIP's own audio (from `hook_clip` at
    `hook_moment`) plays while the box is up, then CROSSFADES over `duck_fade` into
    the ranking audio as the box shrinks — the clip's own audio persists, it isn't
    replaced by silence. `duck_until` = how long the box holds before the shrink.

    `freeze`: instead of playing the background under the hook, PAUSE it on its first
    frame for `freeze_dur` seconds (the whole cold-open), then start the video —
    adds `freeze_dur` of length, and the ranking plays from its true start."""
    frames_dir = Path(frames_dir)
    seq = str(frames_dir / "f_%05d.png")
    base_dur = max(0.1, autolayout.probe(inp).duration)
    U = max(0.0, float(duck_until))
    F = max(0.05, float(duck_fade))
    FR = max(0.0, float(freeze_dur)) if freeze else 0.0
    out_dur = base_dur + FR

    if FR > 0:
        # hold frame 0 for the cold-open, then play the video
        chains = [
            f"[0:v]tpad=start_duration={FR:.3f}:start_mode=clone[bgv]",
            f"[bgv][1:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]",
        ]
    else:
        chains = [
            f"[0:v][1:v]overlay=0:0:eof_action=pass,format=yuv420p[vout]",
        ]
    inputs = ["-i", inp, "-framerate", str(fps), "-i", seq]
    amaps: list[str] = []
    sfx = autolayout._sfx_path(sound) if sound and sound != "none" else None
    # does the hook clip carry usable audio for the moment window?
    hook_has_audio = bool(hook_clip and autolayout._has_audio(hook_clip))

    if autolayout._has_audio(inp):
        if FR > 0:
            # frozen background = silent for the freeze, then the clip audio starts
            ms = max(0, int(FR * 1000))
            chains.append(f"[0:a]adelay={ms}|{ms},afade=t=in:st={FR:.3f}:d=0.12[abg]")
        else:
            # ranking audio: silent while the hook holds, ramp 0→1 over the shrink
            env_bg = (f"volume='if(lt(t,{U:.3f}),0,"
                      f"if(lt(t,{U + F:.3f}),(t-{U:.3f})/{F:.3f},1))':eval=frame")
            chains.append(f"[0:a]{env_bg}[abg]")
        labels = ["[abg]"]
        if hook_has_audio:
            # hook clip's own audio: full while it holds, fade out over the shrink
            idx = inputs.count("-i")   # next ffmpeg input index (0=inp, 1=seq → 2)
            inputs += ["-ss", f"{max(0.0, float(hook_moment)):.3f}",
                       "-t", f"{U + F + 0.2:.3f}", "-i", hook_clip]
            hv = max(0.0, min(float(hook_volume), 2.0))
            # fade IN over the pop-in (nothing before the box appears), hold, then
            # fade OUT over the shrink — mirrors the box's on-screen opacity
            I = max(0.05, float(pop_in))
            env_hk = (f"volume='if(lt(t,{I:.3f}),t/{I:.3f},"
                      f"if(lt(t,{U:.3f}),1,"
                      f"if(lt(t,{U + F:.3f}),1-(t-{U:.3f})/{F:.3f},0)))':eval=frame")
            chains.append(
                f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={hv:.2f},{env_hk}[ahook]")
            labels.append("[ahook]")
        if sfx is not None:
            vol = max(0.0, min(float(volume), 2.0))
            chains.append(
                f"amovie={autolayout._ff_escape(str(sfx))},"
                f"aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={vol:.2f}[sfx]")
            labels.append("[sfx]")
        if len(labels) > 1:
            chains.append("".join(labels) +
                          f"amix=inputs={len(labels)}:normalize=0:duration=first[aout]")
            amaps = ["-map", "[aout]"]
        else:
            amaps = ["-map", "[abg]"]

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", ";".join(chains),
        "-map", "[vout]", *amaps,
        "-t", f"{out_dur:.3f}",
        *autolayout._video_encode_args(),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", outp,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return outp


# UI-facing defaults + bounds (surfaced via /api/formatter/meta).
META = {
    "styles": [{"key": "glow", "label": "Glow"}, {"key": "shadow", "label": "Shadow"},
               {"key": "border", "label": "Border"}, {"key": "brackets", "label": "Brackets"}],
    "colors": HOOK_COLORS,
    "style": "glow", "accent": "#2ea6ff",
    "length": 1.5, "length_min": 0.5, "length_max": 8.0,
    "moment": 0.0,
    "out_speed": 1.0, "speed_min": 0.5, "speed_max": 3.0,
    "sound": "whoosh", "volume": 1.0,
    "entrance": 0.0, "shrink": _SHRINK,
}
