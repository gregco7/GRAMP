"""
Branded "Subscribe" prompt chip — a clean, audioless VALDaily call-out that eases
in and out over the video without interrupting it.

The chip is a dark glass rounded pill: the VALDaily "VD" channel mark (reused from
the formatter's house style), the channel name, and a small red SUBSCRIBE tag. It
is rendered once as an RGBA PNG here; `autolayout._overlay_chips()` does the eased
fade/slide overlay in ffmpeg.

Two places use it:
  - the single-clip editor, as a placeable B-roll marker (type "subscribe" in
    autolayout.EFFECTS_CATALOG) dropped anywhere on the trim bar; and
  - the Video Formatter, pinned to the last X seconds of the finished Top-N render.

Design px below are for a 1080-wide frame; a `scale` multiplier resizes the whole
chip. Kept small + centred by default per the brief ("clean, not interrupting").
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

# Reuse the formatter's Pillow helpers so the chip matches the house style exactly.
from . import formatter as _f

# Default channel-red accent (VALDaily). Bright, YouTube-adjacent.
ACCENT = "#ff2740"
BG = (16, 17, 23, 224)          # dark glass fill (rgba)
BORDER = (255, 255, 255, 40)    # hairline top border

# Design geometry at scale 1.0 (1080-wide frame) --------------------------------
_PAD_X = 30          # inner horizontal padding
_PAD_Y = 24          # inner vertical padding
_GAP = 20            # gap between VD mark / name / pill
_VD = 80             # VD mark diameter
_NAME_PX = 43        # channel-name font size
_PILL_H = 62         # SUBSCRIBE pill height
_PILL_PAD = 27       # pill horizontal text padding
_PILL_PX = 29        # SUBSCRIBE font size
_RADIUS = 30         # chip corner radius


def _crop(img: Image.Image) -> Image.Image:
    return img.crop(img.getbbox() or (0, 0, 1, 1))


def _vd_mark(diam: int, accent: tuple, text_hex: str = "#ffffff") -> Image.Image:
    """A tighter, properly-centred VD channel mark for the chip. Same look as the
    formatter's card mark (red ring, italic V white + D accent, soft glow) but with
    the letters optically centred in the ring and only a slim glow pad — so the name
    that follows sits close, not floating far right."""
    ss = 4
    d = max(10, int(diam))
    border = max(2, int(round(d * 3 / 51)))
    big = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
    inset = border * ss / 2
    ImageDraw.Draw(big).ellipse(
        [inset, inset, d * ss - inset, d * ss - inset],
        fill=(0, 0, 0, 72), outline=accent, width=border * ss)
    circ = big.resize((d, d), Image.LANCZOS)

    glowpad = int(d * 0.18)                             # slim (card mark uses 0.34)
    out = Image.new("RGBA", (d + 2 * glowpad, d + 2 * glowpad), (0, 0, 0, 0))
    glow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([glowpad, glowpad, glowpad + d, glowpad + d],
                                 fill=(*accent[:3], 100))
    out.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(2, int(d * 0.12)))))
    out.alpha_composite(circ, (glowpad, glowpad))

    lf = _f._font("poppins-black", max(8, int(round(d * 22.8 / 51))))
    v_img, _p = _f._text_sprite("V", lf, _f._hex(text_hex), 0, (0, 0, 0, 0), False, True)
    d_img, _q = _f._text_sprite("D", lf, accent, 0, (0, 0, 0, 0), False, True)
    v_img = _crop(v_img)
    d_img = _crop(d_img)
    overlap = max(1, int(round(d * 1.0 / 51)))          # letter-spacing:-1px
    lw = v_img.width + d_img.width - overlap
    lh = max(v_img.height, d_img.height)
    letters = Image.new("RGBA", (max(1, lw), max(1, lh)), (0, 0, 0, 0))
    letters.alpha_composite(v_img, (0, (lh - v_img.height) // 2))
    letters.alpha_composite(d_img, (v_img.width - overlap, (lh - d_img.height) // 2))
    # centre the letter block in the ring — nudge a few px right to sit dead-centre
    cx = glowpad + d // 2 + int(round(d * 1.5 / 51))
    cy = glowpad + d // 2
    out.alpha_composite(letters, (cx - lw // 2, cy - lh // 2))
    return out


def render_chip(dst: str, scale: float = 1.0, name: str = "VALDaily",
                accent: str = ACCENT) -> str:
    """Render the branded subscribe chip to `dst` (RGBA PNG). Supersampled 2x for
    crisp edges, then downscaled to the requested `scale`."""
    ss = 2
    acc = _f._hex(accent)

    # --- pieces (all at 2x) ---------------------------------------------------
    vd_d = _VD * ss
    vd = _vd_mark(vd_d, acc, "#ffffff")             # tight, centred mark

    name_font = _f._font("montserrat-black", _NAME_PX * ss)
    name_img, _ = _f._text_sprite(name, name_font, (255, 255, 255, 255),
                                  0, (0, 0, 0, 0), False, False)
    name_img = _crop(name_img)

    pill_font = _f._font("montserrat-extrabold", _PILL_PX * ss)
    sub_img, _ = _f._text_sprite("SUBSCRIBE", pill_font, (255, 255, 255, 255),
                                 0, (0, 0, 0, 0), False, False)
    sub_img = _crop(sub_img)
    pill_h = _PILL_H * ss
    pill_w = sub_img.width + 2 * _PILL_PAD * ss
    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pd.rounded_rectangle([0, 0, pill_w - 1, pill_h - 1], radius=pill_h // 2, fill=acc)
    pill.alpha_composite(sub_img, ((pill_w - sub_img.width) // 2,
                                   (pill_h - sub_img.height) // 2))

    # --- chip box -------------------------------------------------------------
    pad_x, pad_y, gap = _PAD_X * ss, _PAD_Y * ss, _GAP * ss
    inner_h = max(vd.height, name_img.height, pill.height)
    cw = pad_x + vd.width + gap + name_img.width + gap + pill.width + pad_x
    ch = pad_y + inner_h + pad_y

    # soft drop shadow behind the glass
    glow_pad = 26 * ss
    canvas = Image.new("RGBA", (cw + 2 * glow_pad, ch + 2 * glow_pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [glow_pad, glow_pad + 4 * ss, glow_pad + cw, glow_pad + ch + 4 * ss],
        radius=_RADIUS * ss, fill=(0, 0, 0, 150))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * ss)))

    box = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    bd = ImageDraw.Draw(box)
    bd.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=_RADIUS * ss, fill=BG,
                         outline=BORDER, width=max(1, ss))
    canvas.alpha_composite(box, (glow_pad, glow_pad))

    # place pieces, vertically centred within the box
    def _y(h):
        return glow_pad + (ch - h) // 2

    x = glow_pad + pad_x
    canvas.alpha_composite(vd, (x, _y(vd.height)))
    x += vd.width + gap
    canvas.alpha_composite(name_img, (x, _y(name_img.height)))
    x += name_img.width + gap
    canvas.alpha_composite(pill, (x, _y(pill.height)))

    # downscale (2x supersample -> requested scale)
    tw = max(2, int(round(canvas.width / ss * max(0.2, scale))))
    th = max(2, int(round(canvas.height / ss * max(0.2, scale))))
    canvas = canvas.resize((tw, th), Image.LANCZOS)
    canvas.save(dst)
    return dst


# UI-facing defaults + slider bounds (surfaced at /api/fx and used by the Formatter).
META = {
    "dur": 3.0, "dur_min": 1.0, "dur_max": 8.0,
    "scale": 1.0, "scale_min": 0.6, "scale_max": 1.6,
    "x": 0.5, "y": 0.5,
}
