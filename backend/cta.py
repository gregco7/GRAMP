"""
Animated "Subscribe" call-to-action — a baked PNG frame sequence.

Ports the Claude Design mock ("Subscribe CTA" widget in Shorts Ranking
Templates.dc.html): a mouse cursor slides in, presses a YouTube-red **Subscribe**
button, the button flips to **Subscribed** (bell icon), and a ripple ring + gold
sparkles burst on the click while a one-shot SFX fires. Two looks: `classic`
(channel monogram mark + pill, side by side) and `card` (glass card with handle).
The channel name / handle / accent come from the user's settings (see
config.load_settings / the in-app Settings screen).

Why a baked sequence and not a static chip: this ffmpeg build has no `drawtext`
and can't ease `xfade`, so — exactly like `animate.py`'s reveals — we render each
frame in Pillow (easing scale / offset / alpha ourselves) and hand the PNG
sequence to ffmpeg `overlay`. `render_frames()` writes `f_%05d.png`; `overlay()`
composites the sequence onto a finished clip at a chosen start second and mixes
the click SFX. Used in TWO places (replacing the old static `subscribe.render_chip`):

  - the single-clip editor, as an insertable B-roll marker (catalog key
    `subscribe`, dropped on the trim bar); and
  - the Video Formatter, pinned to the LAST X seconds of the finished Top-N.

Geometry below is design px at a 1080-wide stage; `size` scales the whole widget.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import autolayout, formatter as _f, subscribe as _sub

FPS = 30

ACCENT = "#2ea6ff"          # legacy design accent (blue) — no longer the mark default
VD_ACCENT = "#ff0033"       # VD mark ring + "D" — defaults to red
RED = (255, 0, 0, 255)      # YouTube-red button
SUBBED_BG = (255, 255, 255, 41)   # translucent white after subscribing (~.16 alpha)
GOLD = (255, 210, 74, 255)
STYLES = ["classic", "card"]

# Timeline constants (seconds) — mirror the design's playCTA()/fireSubscribe().
_WRAP_IN = 0.32
_INNER_IN = 0.42
_MOVE_START = 0.30
_PRESS = 0.12
_FADE_OUT = 0.44
_RING = 0.55
_SPARK = 0.62


# --- easing ------------------------------------------------------------------ #
def _clip01(p: float) -> float:
    return 0.0 if p < 0 else (1.0 if p > 1 else p)


def _ease_out_back(p: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _ease_io(p: float) -> float:
    # approximates cubic-bezier(.33,0,.2,1): fast-in, eased settle
    return p * p * (3 - 2 * p)


# --- static pieces ----------------------------------------------------------- #
def _crop(img: Image.Image) -> Image.Image:
    return img.crop(img.getbbox() or (0, 0, 1, 1))


def _arrow(px: int) -> Image.Image:
    """The white mouse cursor (dark outline), tip at the image top-left."""
    s = px / 24.0
    pts = [(6, 3.4), (6, 21.6), (10.7, 17.1), (13.8, 23.6),
           (16.7, 22.1), (13.6, 15.8), (20.5, 15.8)]
    ss = 3
    big = Image.new("RGBA", (int(px * ss * 1.1), int(px * ss * 1.1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    poly = [(x * s * ss, y * s * ss) for x, y in pts]
    d.polygon(poly, fill=(255, 255, 255, 255),
              outline=(12, 15, 21, 255), width=max(2, int(1.4 * s * ss)))
    img = big.resize((int(px * 1.1), int(px * 1.1)), Image.LANCZOS)
    # soft drop shadow
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.putalpha(img.split()[3].point(lambda v: int(v * 0.5)))
    black = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(black).bitmap((0, 0), sh.split()[3], fill=(0, 0, 0, 140))
    out = Image.new("RGBA", (img.width + 6, img.height + 8), (0, 0, 0, 0))
    out.alpha_composite(black.filter(ImageFilter.GaussianBlur(3)), (2, 5))
    out.alpha_composite(img, (0, 0))
    return out


def _bell(px: int, color=(231, 236, 243, 255)) -> Image.Image:
    """A small notification-bell glyph (post-subscribe)."""
    s = px / 24.0
    ss = 3
    big = Image.new("RGBA", (int(px * ss), int(px * ss)), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    # body: rounded trapezoid-ish bell via arc + polygon (approximate)
    cx = 12 * s * ss
    d.rounded_rectangle([6.7 * s * ss, 5.5 * s * ss, 17.3 * s * ss, 15.5 * s * ss],
                        radius=5.3 * s * ss, fill=color)
    d.polygon([(4.6 * s * ss, 17.2 * s * ss), (19.4 * s * ss, 17.2 * s * ss),
               (17.3 * s * ss, 14.0 * s * ss), (6.7 * s * ss, 14.0 * s * ss)], fill=color)
    d.rounded_rectangle([10.9 * s * ss, 2.4 * s * ss, 13.1 * s * ss, 5.5 * s * ss],
                        radius=1.1 * s * ss, fill=color)             # top knob
    d.ellipse([cx - 2.3 * s * ss, 17.0 * s * ss, cx + 2.3 * s * ss, 21.0 * s * ss],
              fill=color)                                            # clapper
    return big.resize((px, px), Image.LANCZOS)


def _pill(text: str, font_px: int, pad_x: int, pad_y: int,
          bg, fg=(255, 255, 255, 255), bell: bool = False,
          min_w: int = 0) -> Image.Image:
    """A rounded button pill. If `bell`, prepend the notification bell + gap."""
    font = _f._font("poppins-extrabold", font_px)
    txt, _ = _f._text_sprite(text, font, fg, 0, (0, 0, 0, 0), False, False)
    txt = _crop(txt)
    gap = int(font_px * 0.28) if bell else 0
    bell_img = _bell(int(font_px * 0.92), fg) if bell else None
    content_w = (bell_img.width + gap if bell_img else 0) + txt.width
    content_h = max(txt.height, bell_img.height if bell_img else 0)
    pw = max(min_w, content_w + 2 * pad_x)
    ph = content_h + 2 * pad_y
    pill = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle([0, 0, pw - 1, ph - 1],
                                           radius=ph // 2, fill=bg)
    x = (pw - content_w) // 2
    if bell_img:
        pill.alpha_composite(bell_img, (x, (ph - bell_img.height) // 2))
        x += bell_img.width + gap
    pill.alpha_composite(txt, (x, (ph - txt.height) // 2))
    return pill


def _shadowed(block: Image.Image, blur: int, alpha: int, dy: int) -> Image.Image:
    """Add a soft drop shadow beneath an RGBA block (grows the canvas)."""
    pad = blur * 2 + 8
    out = Image.new("RGBA", (block.width + 2 * pad, block.height + 2 * pad + dy),
                    (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    a = Image.new("L", block.size, 0)
    a.paste(block.split()[3], (0, 0))
    tmp = Image.new("RGBA", out.size, (0, 0, 0, 0))
    tmp.paste((0, 0, 0, alpha), (pad, pad + dy), block.split()[3])
    out.alpha_composite(tmp.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(block, (pad, pad))
    return out, (pad, pad)


def _classic_block(size: float, subscribed: bool, accent: str = VD_ACCENT,
                   monogram: str = "VD"):
    """(image, button_rect, origin_pad) for the mark + pill layout."""
    acc = _f._hex(accent)
    mark = _sub._vd_mark(int(122 * size), acc, "#ffffff", monogram)
    gap = int(34 * size)
    if subscribed:
        pill = _pill("Subscribed", int(42 * size), int(52 * size), int(24 * size),
                     SUBBED_BG, (231, 236, 243, 255), bell=True)
    else:
        pill = _pill("Subscribe", int(42 * size), int(52 * size), int(24 * size), RED)
    h = max(mark.height, pill.height)
    w = mark.width + gap + pill.width
    block = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    block.alpha_composite(mark, (0, (h - mark.height) // 2))
    bx = mark.width + gap
    by = (h - pill.height) // 2
    block.alpha_composite(pill, (bx, by))
    shadowed, (px, py) = _shadowed(block, int(20 * size), 130, int(12 * size))
    return shadowed, (px + bx, py + by, pill.width, pill.height)


def _card_block(size: float, subscribed: bool, accent: str = VD_ACCENT,
                name: str = "Your Channel", handle: str = "",
                monogram: str = ""):
    acc = _f._hex(accent)
    pad = int(38 * size)
    mark = _sub._vd_mark(int(94 * size), acc, "#ffffff", monogram)
    name_font = _f._font("poppins-extrabold", int(36 * size))
    handle_font = _f._font("poppins-extrabold", int(24 * size))
    name_img = _crop(_f._text_sprite(name, name_font, (255, 255, 255, 255),
                                     0, (0, 0, 0, 0), False, False)[0])
    handle_img = _crop(_f._text_sprite(handle, handle_font, (147, 160, 180, 255),
                                       0, (0, 0, 0, 0), False, False)[0])
    row_gap = int(20 * size)
    col_gap = int(6 * size)
    col_w = max(name_img.width, handle_img.width)
    col_h = name_img.height + col_gap + handle_img.height
    row_h = max(mark.height, col_h)
    row_w = mark.width + row_gap + col_w
    if subscribed:
        btn = _pill("Subscribed", int(38 * size), int(40 * size), int(22 * size),
                    SUBBED_BG, (231, 236, 243, 255), bell=True, min_w=row_w)
    else:
        btn = _pill("Subscribe", int(38 * size), int(40 * size), int(22 * size),
                    RED, min_w=row_w)
    gap2 = int(24 * size)
    inner_w = max(row_w, btn.width)
    inner_h = row_h + gap2 + btn.height
    cw = inner_w + 2 * pad
    ch = inner_h + 2 * pad
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle(
        [0, 0, cw - 1, ch - 1], radius=int(32 * size),
        fill=(12, 15, 21, 189), outline=(255, 255, 255, 36), width=max(1, int(size)))
    # row
    rx = pad + (inner_w - row_w) // 2
    ry = pad
    card.alpha_composite(mark, (rx, ry + (row_h - mark.height) // 2))
    colx = rx + mark.width + row_gap
    coly = ry + (row_h - col_h) // 2
    card.alpha_composite(name_img, (colx, coly))
    card.alpha_composite(handle_img, (colx, coly + name_img.height + col_gap))
    # button
    bx = pad + (inner_w - btn.width) // 2
    by = pad + row_h + gap2
    card.alpha_composite(btn, (bx, by))
    shadowed, (spx, spy) = _shadowed(card, int(28 * size), 150, int(20 * size))
    return shadowed, (spx + bx, spy + by, btn.width, btn.height)


# --- frame baking ------------------------------------------------------------ #
def render_frames(frames_dir, style: str = "classic", size: float = 1.0,
                  anim: float = 0.7, dur: float = 4.0,
                  cx: float = 540, cy: float = 960, accent: str | None = None,
                  name: str | None = None, handle: str | None = None,
                  W: int = 1080, H: int = 1920, fps: int = FPS) -> dict:
    """Bake the CTA animation to `frames_dir/f_%05d.png` (transparent RGBA stage
    frames). Returns {n, total, click_t} — click_t (s) is when the SFX fires.

    `name`/`handle`/`accent` default to the user's channel settings (config.
    load_settings); the mark's monogram is derived from the channel name (or a
    neutral play triangle when unset). `accent` tints the mark ring + 2nd letter."""
    from . import config
    st = config.load_settings()
    if name is None:
        name = st.get("channel_name") or ""
    if handle is None:
        handle = st.get("channel_handle") or ""
    accent = accent or st.get("channel_accent") or VD_ACCENT
    monogram = _sub.channel_initials(name)

    style = style if style in STYLES else "classic"
    size = max(0.4, min(float(size), 2.5))
    anim = max(0.3, min(float(anim), 2.5))
    dur = max(1.0, min(float(dur), 12.0))

    if style == "card":
        before, brect = _card_block(size, False, accent, name or "Subscribe",
                                    handle, monogram)
        after, arect = _card_block(size, True, accent, name or "Subscribe",
                                   handle, monogram)
    else:
        before, brect = _classic_block(size, False, accent, monogram)
        after, arect = _classic_block(size, True, accent, monogram)  # after width may differ

    move_start, t_move = _MOVE_START, anim
    press_at = move_start + t_move
    fire_at = press_at + _PRESS
    hide_at = max(fire_at + 0.80, dur)
    total = hide_at + _FADE_OUT
    n = max(2, int(round(total * fps)))

    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    arrow = _arrow(int(74 * size))
    # cursor entry offset (design: translate(130,155)*implicit scale)
    off_x, off_y = 130 * size, 155 * size

    for i in range(n):
        t = i / fps
        subscribed = t >= fire_at
        blk, rect = (after, arect) if subscribed else (before, brect)

        # wrap opacity (fade in, then fade out after hide)
        op = _clip01(t / _WRAP_IN) * (1.0 - _clip01((t - hide_at) / _FADE_OUT))
        # inner scale (pop in; slight shrink on hide)
        s_in = 0.82 + _ease_out_back(_clip01(t / _INNER_IN)) * 0.18
        if t > hide_at:
            s_in *= 1.0 - 0.05 * _clip01((t - hide_at) / _FADE_OUT)

        frame = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if op <= 0.01:
            frame.save(frames_dir / f"f_{i:05d}.png")
            continue

        sw, sh = max(1, int(blk.width * s_in)), max(1, int(blk.height * s_in))
        scaled = blk.resize((sw, sh), Image.LANCZOS)
        if op < 0.999:
            a = scaled.split()[3].point(lambda v: int(v * op))
            scaled.putalpha(a)
        tlx, tly = cx - sw / 2, cy - sh / 2
        frame.alpha_composite(scaled, (int(tlx), int(tly)))

        # button click point (60% into the button rect), in scaled stage space
        bx, by, bw, bh = rect
        click_x = tlx + (bx + bw * 0.60) * s_in
        click_y = tly + (by + bh * 0.60) * s_in

        d = ImageDraw.Draw(frame)
        # ripple ring + sparkles (post-click burst)
        if fire_at <= t <= fire_at + max(_RING, _SPARK) + 0.05:
            rp = _clip01((t - fire_at) / _RING)
            if rp < 1.0:
                r = (70 * size) * (0.2 + 3.2 * rp) / 2
                ra = int(255 * 0.85 * (1 - rp) * op)
                if ra > 4:
                    d.ellipse([click_x - r, click_y - r, click_x + r, click_y + r],
                              outline=(255, 255, 255, ra), width=max(2, int(5 * size)))
            sp = (t - fire_at) / _SPARK
            if 0 <= sp < 1.0:
                for k in range(8):
                    ang = (math.pi * 2 * k) / 8 + 0.35
                    dist = (74 + (k % 3) * 26) * size * min(1.0, sp * 1.1)
                    ease = sp * sp
                    px_ = click_x + math.cos(ang) * dist * ease
                    py_ = click_y + math.sin(ang) * dist * ease
                    sa = int(255 * (1 - sp) * op)
                    _sparkle(d, px_, py_, int(11 * size), (GOLD[0], GOLD[1], GOLD[2], sa))

        # cursor
        if t >= move_start - 0.05:
            if t < move_start:
                pos = (off_x, off_y); csc = 1.15; cop = 0.0
            elif t < press_at:
                p = _ease_io(_clip01((t - move_start) / t_move))
                pos = (off_x * (1 - p), off_y * (1 - p))
                csc = 1.15 - 0.15 * p
                cop = _clip01((t - move_start) / 0.30)
            elif t < fire_at:
                pos = (-2 * size, -2 * size); csc = 0.8; cop = 1.0
            else:
                pos = (0, 0); csc = 1.0; cop = 1.0
            cop *= op
            if cop > 0.02:
                cw_ = max(1, int(arrow.width * csc))
                ch_ = max(1, int(arrow.height * csc))
                ca = arrow.resize((cw_, ch_), Image.LANCZOS)
                if cop < 0.999:
                    ca.putalpha(ca.split()[3].point(lambda v: int(v * cop)))
                frame.alpha_composite(ca, (int(click_x + pos[0]), int(click_y + pos[1])))

        frame.save(frames_dir / f"f_{i:05d}.png")

    return {"n": n, "total": total, "click_t": fire_at}


def _sparkle(d: ImageDraw.ImageDraw, x: float, y: float, r: int, color) -> None:
    """A 4-pointed gold sparkle (two crossed spokes)."""
    r2 = r * 0.34
    d.polygon([(x, y - r), (x + r2, y - r2), (x + r, y), (x + r2, y + r2),
               (x, y + r), (x - r2, y + r2), (x - r, y), (x - r2, y - r2)],
              fill=color)


# --- compositing ------------------------------------------------------------- #
def overlay(inp: str, outp: str, frames_dir, start: float, click_t: float,
            sound: str = "none", volume: float = 1.0,
            W: int = 1080, H: int = 1920, fps: int = FPS) -> str:
    """Overlay the baked CTA frame sequence onto `inp` starting at `start` seconds;
    mix the click SFX at `start + click_t`. Base video length is preserved."""
    frames_dir = Path(frames_dir)
    seq = str(frames_dir / "f_%05d.png")
    base_dur = max(0.1, autolayout.probe(inp).duration)
    start = max(0.0, float(start))

    chains = [
        f"[1:v]format=rgba,setpts=PTS+{start:.3f}/TB[seq]",
        f"[0:v][seq]overlay=0:0:eof_action=pass:enable='gte(t,{start:.3f})',"
        f"format=yuv420p[vout]",
    ]
    amaps: list[str] = []
    sfx = autolayout._sfx_path(sound) if sound and sound != "none" else None
    if autolayout._has_audio(inp):
        if sfx is not None:
            delay = max(0, int((start + click_t) * 1000))
            vol = max(0.0, min(float(volume), 2.0))
            chains.append(
                f"amovie={autolayout._ff_escape(str(sfx))},"
                f"aformat=sample_rates=48000:channel_layouts=stereo,"
                f"volume={vol:.2f},adelay={delay}|{delay}[sfx]")
            chains.append("[0:a][sfx]amix=inputs=2:normalize=0:duration=first[aout]")
            amaps = ["-map", "[aout]"]
        else:
            amaps = ["-map", "0:a"]

    cmd = [
        "ffmpeg", "-y",
        "-i", inp,
        "-framerate", str(fps), "-i", seq,
        "-filter_complex", ";".join(chains),
        "-map", "[vout]", *amaps,
        "-t", f"{base_dur:.3f}",
        *autolayout._video_encode_args(),
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", outp,
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return outp


# UI-facing defaults + slider bounds (surfaced at /api/fx + /api/formatter/meta).
META = {
    "style": "classic", "styles": [
        {"key": "classic", "label": "Classic"}, {"key": "card", "label": "Card"}],
    "size": 1.0, "size_min": 0.5, "size_max": 2.0,
    "anim": 0.7, "anim_min": 0.3, "anim_max": 2.5,
    "dur": 4.0, "dur_min": 1.0, "dur_max": 10.0,
    "sound": "pop", "volume": 1.0,
    "x": 0.5, "y": 0.5,
    "accent": VD_ACCENT,        # VD mark ring + "D" colour (default red)
}
