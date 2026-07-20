"""
Video Formatter — the "Valorant Clip Ranking" overlay template.

One template, three visual styles (ported 1:1 from the Claude Design editor
"Shorts Ranking Templates"):

  - **pill**       Blue Pill — Montserrat Black on a rounded pill (the original).
  - **minimal**    Minimal List — Poppins, accent numbers, soft shadows.
  - **editorial**  Big Number — huge Anton rank digits + Bebas labels.

On top of the title + numbered list the template can stamp:

  - **widgets**     per-rank player headshot / weapon skin PNGs (assets/plrs, assets/weapons),
  - **clip info**   a "Now Playing" card (org logos A vs B + caption, styled),
  - **bonus title** a sticker / marker / tag / spark call-out above or below
                    the title (auto-hide is an animation-only behaviour),
  - **icon**        a tinted VCT logo backdrop behind the list.

Everything renders to a transparent RGBA PNG handed to `autolayout.compose`
via `LayoutOpts.overlay_png` (static path), and to per-element sprites for
`animate.py` (baked reveals + idle motion). Text is drawn with Pillow (this
ffmpeg build has no `drawtext`). Fonts are the bundled OFL families in
`assets/fonts/`.

Geometry convention: the design was authored at 1080×1920; px values from it
are stored as fractions of W (x/size) or H (y/gap) so they scale. Free-drag
`offsets` {tx,ty,lx,ly} are extra px at 1080×1920 (scaled by W/1080), and
{ts,ls} scale the title / list blocks about the design's transform anchors.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

_ROOT = Path(__file__).resolve().parent.parent
_FONTS_DIR = _ROOT / "assets" / "fonts"
_ASSETS_DIR = _ROOT / "assets"
ICON_PATH = _ASSETS_DIR / "vct-icon.png"

# UI-facing font keys -> bundled files.
FONTS = {
    "montserrat-black":        "Montserrat-Black.ttf",
    "montserrat-black-italic": "Montserrat-BlackItalic.ttf",
    "montserrat-extrabold":    "Montserrat-ExtraBold.ttf",
    "poppins-extrabold":       "Poppins-ExtraBold.ttf",
    "poppins-black":           "Poppins-Black.ttf",
    "anton":                   "Anton-Regular.ttf",
    "bebas":                   "BebasNeue-Regular.ttf",
}
FONT_CHOICES = [
    {"key": "montserrat-black",        "label": "Montserrat Black"},
    {"key": "montserrat-black-italic", "label": "Montserrat Black Italic"},
    {"key": "montserrat-extrabold",    "label": "Montserrat ExtraBold"},
    {"key": "poppins-extrabold",       "label": "Poppins ExtraBold"},
    {"key": "poppins-black",           "label": "Poppins Black"},
    {"key": "anton",                   "label": "Anton (condensed)"},
    {"key": "bebas",                   "label": "Bebas Neue (tall)"},
]

# Per-style specs — px values are the design's 1080×1920 numbers as fractions.
# `title`/`list` follow the classic spec keys; new keys:
#   title.upper / list.upper_label   CSS text-transform:uppercase equivalents
#   title.shadow / list.shadow       False | "hard" | "soft" (blurred, design-style)
#   list.num_font/num_size_pct/num_accent/num_min_w_pct/label_ml_pct/valign/suffix
#   widget_h_pct                     base widget image height for this style
STYLE_SPECS = {
    "pill": {
        "label": "Blue Pill — original",
        "title": {
            "font": "montserrat-black", "size_pct": 0.070,
            "fill": "#ffffff", "stroke": "#16233f", "stroke_pct": 0.13,
            "style": "pill", "pill_fill": "#a9c9ec", "pill_alpha": 235,
            "pill_pad_pct": 0.030, "pill_radius_pct": 0.5,
            "accent": "#2ea6ff", "accent_words": [], "upper": False,
            "italic": False, "shadow": False,
            "y_pct": 0.300, "align": "center",
        },
        "list": {
            "font": "montserrat-black", "size_pct": 0.052,
            "num_font": "montserrat-black", "num_size_pct": 0.052,
            "num_accent": False, "num_min_w_pct": 0.0, "label_ml_pct": 0.0,
            "fill": "#ffffff", "stroke": "#16233f", "stroke_pct": 0.13,
            "accent": "#2ea6ff", "shadow": False, "italic": False,
            "upper_label": False, "valign": "baseline", "suffix": ".",
            "x_pct": 0.050, "y_pct": 0.3974, "gap_pct": 0.0599,
            "list_band": False,   # shrink-wrapped list → icon hugs the rows (design)
        },
        "offsets": {"tx": -2, "ty": -146, "lx": -9, "ly": -29, "ts": 1.0, "ls": 1.0},
        "widget_h_pct": 92 / 1080,
    },
    "minimal": {
        "label": "Minimal List",
        "title": {
            "font": "poppins-extrabold", "size_pct": 0.0759,
            "fill": "#ffffff", "stroke": "#000000", "stroke_pct": 0.0,
            "style": "plain", "pill_fill": "#a9c9ec", "pill_alpha": 235,
            "pill_pad_pct": 0.030, "pill_radius_pct": 0.5,
            "accent": "#F5B942", "accent_words": [], "upper": True,
            "italic": False, "shadow": "soft",
            "y_pct": 0.15625, "align": "center",
        },
        "list": {
            "font": "poppins-extrabold", "size_pct": 0.0537,
            "num_font": "poppins-black", "num_size_pct": 0.0574,
            "num_accent": True, "num_min_w_pct": 0.0889, "label_ml_pct": 0.0111,
            "fill": "#ffffff", "stroke": "#000000", "stroke_pct": 0.0,
            "accent": "#F5B942", "shadow": "soft", "italic": False,
            "upper_label": False, "valign": "baseline", "suffix": ".",
            "x_pct": 0.0889, "y_pct": 0.3229, "gap_pct": 0.0781,
            "list_band": True,    # left:96/right:96 band → icon centred at W/2 (design)
        },
        "offsets": {"tx": 0, "ty": 172, "lx": -34, "ly": 115, "ts": 1.0, "ls": 1.0},
        "widget_h_pct": 84 / 1080,
    },
    "editorial": {
        "label": "Big Number",
        "title": {
            "font": "anton", "size_pct": 0.0648,
            "fill": "#ffffff", "stroke": "#000000", "stroke_pct": 0.0,
            "style": "plain", "pill_fill": "#a9c9ec", "pill_alpha": 235,
            "pill_pad_pct": 0.030, "pill_radius_pct": 0.5,
            "accent": "#FF5A5F", "accent_words": [], "upper": True,
            "italic": False, "shadow": "soft",
            "y_pct": 0.1406, "align": "left", "x_pct": 0.0741,
        },
        "list": {
            "font": "bebas", "size_pct": 0.0685,
            "num_font": "anton", "num_size_pct": 0.1389,
            "num_accent": True, "num_min_w_pct": 0.1389, "label_ml_pct": 0.0259,
            "fill": "#ffffff", "stroke": "#000000", "stroke_pct": 0.0,
            "accent": "#FF5A5F", "shadow": "soft", "italic": False,
            "upper_label": True, "valign": "center", "suffix": "",
            "x_pct": 0.0741, "y_pct": 0.3125, "gap_pct": 0.1094,
            "list_band": True,    # left:80/right:80 band → icon centred at W/2 (design)
        },
        "offsets": {"tx": 223, "ty": 208, "lx": -34, "ly": 68, "ts": 1.0, "ls": 1.0},
        "widget_h_pct": 116 / 1080,
    },
}

# The single app-level template; the editor picks the style + everything else.
TEMPLATES = {
    "valorant_clip_ranking": {
        "label": "Valorant Clip Ranking",
        "style": "pill",
        "title": {"text": "Top 5 s0m Moments", "max_lines": 2},
        "list": {"count": 5, "items": ["", "", "", "", ""], "highlight": -1},
        "offsets": None,               # falls back to the style's drag defaults
        "widgets": [],                 # per-rank: None | {src, kind, name}
        "clip_info": None,             # this segment's card: {a, b, caption} | None
        "clip_style": {"bg": "#0c0f15", "accent": "#2ea6ff", "text": "#ffffff",
                       "size": 1.0, "text_size": 1.0, "opacity": 0.78},
        "bonus": {"on": False, "text": "just wait for the second one",
                  "style": "sticker", "pos": "above", "size": 1.0, "show": "anim",
                  "hide_on": True, "hide_secs": 5.0},
        "subtitle": {"on": False, "text": "VALORANT", "after": "", "size": 0.70,
                     "streamers": False, "streamer_keys": []},
        "icon": {"on": False, "color": "#ffffff", "opacity": 7, "size": 468},
    },
}

BONUS_STYLES = ["sticker", "marker", "tag", "spark"]

# Medal ranks (Blue Pill only): #1/2/3 numbers → gold / silver / bronze.
_MEDAL_COLORS = ["#FFD24A", "#8A97A8", "#D08A4E"]

# Streamer reaction catalog — headshots in assets/streamer_icons/ (mirrors the
# design's STREAMERS). A clip-info card can pin one streamer (portrait pops over
# the card + @handle chip); the subtitle's "Streamers React" pulls in every
# streamer linked across the ranking's clips. Keyed by `key`.
STREAMERS = [
    {"key": "tenz",   "name": "TenZ",   "src": "/assets/plrs/tenz.png",             "handle": "@TenZ"},
    {"key": "fns",    "name": "FNS",    "src": "/assets/streamer_icons/fns.png",    "handle": "@GOFNS"},
    {"key": "tarik",  "name": "tarik",  "src": "/assets/streamer_icons/tarik.png",  "handle": "@tarik"},
    {"key": "s0m",    "name": "s0m",    "src": "/assets/streamer_icons/s0m.png",    "handle": "s0mcs"},
    {"key": "shanks", "name": "shanks", "src": "/assets/streamer_icons/shanks.png", "handle": "@shanks_ttv"},
]
STREAMER_BY_KEY = {s["key"]: s for s in STREAMERS}


# ----------------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------------- #
def _hex(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    c = (color or "#000000").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        r, g, b = 0, 0, 0
    return (r, g, b, max(0, min(int(alpha), 255)))


def _font(key: str, px: int) -> ImageFont.FreeTypeFont:
    fn = FONTS.get(key, FONTS["montserrat-black"])
    return ImageFont.truetype(str(_FONTS_DIR / fn), max(8, int(px)))


def _asset_image(src: str) -> Optional[Image.Image]:
    """Load a catalog asset ('/assets/...') as RGBA; None if missing/invalid."""
    rel = (src or "").lstrip("/")
    if not rel.startswith("assets/"):
        return None
    p = (_ROOT / rel).resolve()
    if not str(p).startswith(str(_ASSETS_DIR.resolve())) or not p.exists():
        return None
    try:
        return Image.open(p).convert("RGBA")
    except Exception:
        return None


def _merge(preset: dict, overrides: Optional[dict]) -> dict:
    """Resolve preset -> style spec -> overrides into one flat spec."""
    import copy
    ov = overrides or {}
    style = ov.get("style") or preset.get("style") or "pill"
    if style not in STYLE_SPECS:
        style = "pill"
    sty = STYLE_SPECS[style]

    spec = copy.deepcopy(preset)
    spec["style"] = style
    # style spec provides the visual defaults for title/list/offsets
    title = dict(sty["title"]); title.update(spec.get("title") or {})
    lst = dict(sty["list"]); lst.update(spec.get("list") or {})
    spec["title"], spec["list"] = title, lst
    spec["offsets"] = dict(sty["offsets"])
    spec["widget_h_pct"] = sty["widget_h_pct"]
    if isinstance(preset.get("offsets"), dict):
        spec["offsets"].update({k: v for k, v in preset["offsets"].items() if v is not None})

    # overrides on top
    for section in ("title", "list", "offsets", "clip_style", "bonus", "subtitle", "icon"):
        if isinstance(ov.get(section), dict):
            spec[section] = dict(spec.get(section) or {})
            spec[section].update({k: v for k, v in ov[section].items() if v is not None})
    for section in ("widgets",):
        if isinstance(ov.get(section), list):
            spec[section] = ov[section]
    if "clip_info" in ov:
        spec["clip_info"] = ov["clip_info"]
    return spec


def _shear(img: Image.Image, k: float = 0.20) -> Image.Image:
    """Fake-italic slant an RGBA image to the right by factor k."""
    w, h = img.size
    extra = int(h * k)
    canvas = Image.new("RGBA", (w + extra, h), (0, 0, 0, 0))
    canvas.paste(img, (0, 0), img)
    return canvas.transform(
        (w + extra, h), Image.AFFINE, (1, k, 0, 0, 1, 0), resample=Image.BICUBIC
    )


def _shadow_pad(dy: int, blur: int) -> int:
    """Transparent margin `_soft_shadow` adds around the sprite on every side (the
    original artwork ends up at (pad, pad) inside the returned image)."""
    return blur * 2 + abs(dy) + 2


def _soft_shadow(sprite: Image.Image, dy: int, blur: int, alpha: int = 140) -> Image.Image:
    """Design-style blurred drop shadow: a new sprite with the shadow underneath."""
    pad = _shadow_pad(dy, blur)
    out = Image.new("RGBA", (sprite.width + 2 * pad, sprite.height + 2 * pad), (0, 0, 0, 0))
    a = sprite.split()[3].point(lambda v: int(v * alpha / 255))
    sh = Image.new("RGBA", sprite.size, (0, 0, 0, 255))
    sh.putalpha(a)
    out.alpha_composite(sh, (pad, pad + dy))
    out = out.filter(ImageFilter.GaussianBlur(blur))
    out.alpha_composite(sprite, (pad, pad))
    return out


# Inline color-emoji support. The bundled Poppins/etc. have no emoji glyphs, so a
# caption like "TenZ Ace 🔥😭" would render as .notdef tofu. Draw emoji runs with
# the system Apple Color Emoji font (the SAME font Chrome uses on macOS, so the
# render matches the live preview). Falls back to dropping emoji (no tofu) if the
# font is missing (e.g. non-macOS).
_EMOJI_FONT_PATHS = [
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/Library/Fonts/Apple Color Emoji.ttc",
]
_EMOJI_STRIKE = 160                                    # Apple Color Emoji bitmap strike we render at
# Codepoints treated as "emoji" (grouped into runs so ZWJ sequences / skin tones /
# variation selectors stay together). Not exhaustive, but covers the common set.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # pictographs, emoticons, transport, supplemental, ext-A, symbols
    "\U00002600-\U000027BF"       # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"       # regional indicators (flags)
    "\U00002B00-\U00002BFF"       # stars / misc symbols & arrows
    "\U0000FE0E\U0000FE0F"        # variation selectors (text/emoji presentation)
    "\U0000200D"                  # zero-width joiner
    "\U000020E3"                  # combining enclosing keycap
    "\U00002190-\U000021FF]"      # arrows (some emoji-presented)
)


@lru_cache(maxsize=None)
def _emoji_font():
    for p in _EMOJI_FONT_PATHS:
        try:
            return ImageFont.truetype(p, _EMOJI_STRIKE)
        except Exception:
            continue
    return None


def has_emoji(text: str) -> bool:
    return bool(text) and _EMOJI_RE.search(text) is not None


def _emoji_runs(text: str):
    """Split into [(is_emoji, substring), ...], grouping consecutive emoji chars."""
    runs: list[list] = []
    for ch in text:
        e = bool(_EMOJI_RE.match(ch))
        if runs and runs[-1][0] == e:
            runs[-1][1] += ch
        else:
            runs.append([e, ch])
    return runs


def _emoji_glyph(run: str, px: int) -> Optional[Image.Image]:
    """Render an emoji run at the strike size, cropped and scaled to `px` tall."""
    ef = _emoji_font()
    if ef is None:
        return None
    tmp = Image.new("RGBA", (_EMOJI_STRIKE * (len(run) + 1), _EMOJI_STRIKE + 40), (0, 0, 0, 0))
    try:
        ImageDraw.Draw(tmp).text((4, 4), run, font=ef, embedded_color=True)
    except Exception:
        return None
    bb = tmp.getbbox()
    if not bb:
        return None
    g = tmp.crop(bb)
    scale = px / max(1, g.height)
    return g.resize((max(1, int(g.width * scale)), max(1, px)), Image.LANCZOS)


def _rich_line_sprite(text: str, font, color, line_h: int) -> Image.Image:
    """One text line with inline color emoji, in a `line_h`-tall line-box. Text runs
    use `font`; emoji runs use Apple Color Emoji sized ~1em, vertically centered on
    the line. Mirrors how the browser lays out emoji in the preview caption."""
    asc, desc = font.getmetrics()
    y_text = (line_h - asc - desc) / 2.0                 # CSS half-leading
    em = int(round(font.size * 1.10))                    # emoji visual size (~1em, browser-ish)
    pieces = []                                          # (kind, payload, width)
    for is_e, s in _emoji_runs(text):
        if not s:
            continue
        if is_e:
            g = _emoji_glyph(s, em)
            if g is not None:
                pieces.append(("img", g, g.width + max(2, int(font.size * 0.06))))
        else:
            pieces.append(("txt", s, int(font.getlength(s))))
    total = sum(p[2] for p in pieces) + 8
    img = Image.new("RGBA", (max(2, total), max(2, line_h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 4
    for kind, val, w in pieces:
        if kind == "txt":
            d.text((x, y_text), val, font=font, fill=color)
        else:
            img.alpha_composite(val, (x, (line_h - val.height) // 2))
        x += w
    bb = img.getbbox()
    return img.crop((bb[0], 0, bb[2], line_h)) if bb else img


def _emoji_em(font) -> int:
    """Emoji visual size for a given font (~1em, browser-ish)."""
    return int(round(getattr(font, "size", 40) * 1.10))


def _emoji_adv(run: str, font) -> float:
    """On-screen advance of one color-emoji run at ~1em (glyph width + small gap)."""
    g = _emoji_glyph(run, _emoji_em(font))
    return (g.width + max(2, int(getattr(font, "size", 40) * 0.06))) if g is not None else 0.0


def _run_len(text: str, font) -> float:
    """Pixel advance of a line accounting for inline color emoji — the emoji-aware
    counterpart to font.getlength(), used by title fit/wrap so emoji words measure
    at their real (color-glyph) width, not the OFL font's .notdef advance."""
    total = 0.0
    for is_e, s in _emoji_runs(text):
        if not s:
            continue
        total += _emoji_adv(s, font) if is_e else font.getlength(s)
    return total


def _place_bbox(text: str, font, stroke_w: int):
    """Row-placement bbox matching the _text_sprite / _mixed_text_sprite anchor.
    Plain text → font.getbbox verbatim. With color emoji → the vertical extent comes
    from the TEXT glyphs only (so the row aligns like a plain line, unaffected by the
    OFL font's .notdef box for the emoji codepoint) and the right edge spans the real
    rendered width incl. the color emoji (for widget placement after the label)."""
    if not has_emoji(text):
        return font.getbbox(text or " ", stroke_width=stroke_w)
    text_only = "".join(s for is_e, s in _emoji_runs(text) if not is_e) or " "
    tb = font.getbbox(text_only, stroke_width=stroke_w)
    right = tb[0] + _run_len(text, font)                 # emoji-aware advance from the left bearing
    return (tb[0], tb[1], int(round(right)), tb[3])


def _mixed_text_sprite(text: str, font, color, stroke_w: int, stroke_fill,
                       shadow, italic: bool) -> tuple[Image.Image, tuple[int, int]]:
    """_text_sprite for text containing color emoji: text runs keep the font +
    stroke/shadow; emoji runs draw as Apple Color Emoji sized ~1em, centered on the
    TEXT glyph box. Same (sprite, (pad, pad)) contract as _text_sprite — i.e. the
    TEXT glyph-box top-left sits at (pad, pad), so an emoji line aligns exactly like
    a plain line (the emoji just floats centered on the text; it does not push the
    row). See _place_bbox for the matching placement metrics on the caller side."""
    em = _emoji_em(font)
    size = getattr(font, "size", 40)
    hard = shadow == "hard" or shadow is True
    soft = shadow == "soft"
    shadow_off = max(2, stroke_w) if hard else 0
    blur = max(2, int(size * 0.14)) if soft else 0
    pad_base = stroke_w + 4 + (blur * 2 + 5 if soft else 0)

    pieces = []                                          # (kind, payload, advance)
    for is_e, s in _emoji_runs(text):
        if not s:
            continue
        if is_e:
            g = _emoji_glyph(s, em)
            if g is not None:
                pieces.append(("img", g, g.width + max(2, int(size * 0.06))))
        else:
            pieces.append(("txt", s, font.getlength(s)))

    # Vertical reference = the TEXT glyphs only, so the row aligns like a plain line.
    text_only = "".join(p[1] for p in pieces if p[0] == "txt") or " "
    tb = font.getbbox(text_only, stroke_width=stroke_w)   # relative to the text draw origin
    text_top, text_h = tb[1], tb[3] - tb[1]
    text_cy = text_top + text_h / 2.0                     # centre of the text glyph box
    left_bearing = tb[0]                                  # ink-left of the first (text) glyph

    # Emoji are centred on the text box; a tall emoji overhangs it, so grow the pad
    # (kept symmetric — the caller anchors on a single pad value) to hold the overhang.
    max_emoji_h = max([p[1].height for p in pieces if p[0] == "img"] or [0])
    overhang = max(0.0, (max_emoji_h - text_h) / 2.0)
    pad = int(math.ceil(max(pad_base, overhang))) + 1

    line_w = sum(p[2] for p in pieces)
    img = Image.new("RGBA", (int(max(2, line_w + 2 * pad + shadow_off)),
                             int(max(2, text_h + 2 * pad + shadow_off))), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Draw origin so the TEXT glyph box top-left lands at (pad, pad).
    x = pad - left_bearing
    draw_y = pad - text_top
    for kind, val, adv in pieces:
        if kind == "txt":
            if hard:
                d.text((x + shadow_off, draw_y + shadow_off), val, font=font, fill=(0, 0, 0, 150),
                       stroke_width=stroke_w, stroke_fill=(0, 0, 0, 150))
            d.text((x, draw_y), val, font=font, fill=color, stroke_width=stroke_w, stroke_fill=stroke_fill)
        else:
            ey = (pad + text_cy - text_top) - val.height / 2.0   # centre emoji on the text box
            img.alpha_composite(val, (int(round(x)), int(round(ey))))
        x += adv
    if soft:
        base = img
        img = Image.new("RGBA", base.size, (0, 0, 0, 0))
        a = base.split()[3].point(lambda v: int(v * 0.55))
        sh = Image.new("RGBA", base.size, (0, 0, 0, 255)); sh.putalpha(a)
        tmp = Image.new("RGBA", base.size, (0, 0, 0, 0))
        tmp.alpha_composite(sh, (0, max(1, int(size * 0.04))))
        img.alpha_composite(tmp.filter(ImageFilter.GaussianBlur(blur)))
        img.alpha_composite(base)
    if italic:
        img = _shear(img, 0.18)
    return img, (pad, pad)


def _text_sprite(text: str, font, color, stroke_w: int, stroke_fill,
                 shadow, italic: bool) -> tuple[Image.Image, tuple[int, int]]:
    """A tightly-cropped RGBA sprite of one text run. Returns (sprite, (gx, gy))
    where (gx, gy) is where the glyph box's top-left sits inside the sprite.
    `shadow` is False | "hard" | "soft". Text with color emoji routes through
    _mixed_text_sprite so the emoji render (not .notdef tofu)."""
    if has_emoji(text):
        return _mixed_text_sprite(text, font, color, stroke_w, stroke_fill, shadow, italic)
    bb = font.getbbox(text, stroke_width=stroke_w)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    size = getattr(font, "size", 40)
    hard = shadow == "hard" or shadow is True
    soft = shadow == "soft"
    shadow_off = max(2, stroke_w) if hard else 0
    blur = max(2, int(size * 0.14)) if soft else 0
    pad = stroke_w + 4 + (blur * 2 + 5 if soft else 0)
    img = Image.new("RGBA", (max(2, tw + 2 * pad + shadow_off),
                             max(2, th + 2 * pad + shadow_off)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ox, oy = pad - bb[0], pad - bb[1]
    if hard:
        d.text((ox + shadow_off, oy + shadow_off), text, font=font, fill=(0, 0, 0, 150),
               stroke_width=stroke_w, stroke_fill=(0, 0, 0, 150))
    d.text((ox, oy), text, font=font, fill=color,
           stroke_width=stroke_w, stroke_fill=stroke_fill)
    if soft:
        base = img
        img = Image.new("RGBA", base.size, (0, 0, 0, 0))
        a = base.split()[3].point(lambda v: int(v * 0.55))
        sh = Image.new("RGBA", base.size, (0, 0, 0, 255)); sh.putalpha(a)
        tmp = Image.new("RGBA", base.size, (0, 0, 0, 0))
        tmp.alpha_composite(sh, (0, max(1, int(size * 0.04))))
        img.alpha_composite(tmp.filter(ImageFilter.GaussianBlur(blur)))
        img.alpha_composite(base)
    if italic:
        img = _shear(img, 0.18)
    return img, (pad, pad)


# ----------------------------------------------------------------------------- #
# Title block (fit + wrap + pill) — shared by static render and animation
# ----------------------------------------------------------------------------- #
def _line_w(words, font, space: float) -> float:
    if not words:
        return 0.0
    return sum(_run_len(w, font) for w in words) + space * (len(words) - 1)


def _wrap_title(words, font, space: float, max_w: float, max_lines: int):
    """Word-wrap a title into up to `max_lines` balanced lines (see git history)."""
    if len(words) < 2 or max_lines < 2:
        return None
    if any(_run_len(w, font) > max_w for w in words):
        return None
    if max_lines == 2:
        best = None
        for i in range(1, len(words)):
            aw = _line_w(words[:i], font, space)
            bw = _line_w(words[i:], font, space)
            if aw <= max_w and bw <= max_w:
                score = abs(aw - bw)
                if best is None or score < best[0]:
                    best = (score, [words[:i], words[i:]])
        return best[1] if best else None
    lines, cur = [], []
    for w in words:
        if cur and _line_w(cur + [w], font, space) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines if len(lines) <= max_lines else None


def _fit_title(t: dict, W: int, ts: float = 1.0) -> dict:
    """Auto-fit + wrap the title. `ts` scales the block like the design's drag
    handle: the font grows and the fit width grows with it, so the wrap
    decision matches the unscaled layout (a CSS transform doesn't rewrap)."""
    text = (t["text"] or "")
    if t.get("upper"):
        text = text.upper()
    words = text.split()
    margin = W * 0.045
    is_pill = t["style"] == "pill"
    base_max_w = W - 2 * margin - (2 * W * t["pill_pad_pct"] if is_pill else 0)
    # Pill wraps by *rendered* width: the design holds the on-screen text
    # allowance constant while ts scales the font, so shrinking the pill (ts<1)
    # widens its per-line capacity and a longer title stays on one line
    # (design: pillWords.maxWidth = 920/ts, then scale(ts)). The font here is
    # already ×ts, so the equivalent is a ts-independent max_w for the pill.
    # Other styles keep the fixed-box-scaled-by-ts behaviour.
    max_w = base_max_w if is_pill else base_max_w * ts
    max_lines = max(1, int(t.get("max_lines", 2)))

    size = int(W * t["size_pct"] * ts)
    lines = [words]
    for _ in range(60):
        font = _font(t["font"], size)
        space = font.getlength(" ")
        if not words or _line_w(words, font, space) <= max_w:
            lines = [words]
            break
        wrapped = _wrap_title(words, font, space, max_w, max_lines) if max_lines > 1 else None
        if wrapped is not None or size <= 14:
            lines = wrapped if wrapped is not None else [words]
            break
        size = int(size * 0.94)

    font = _font(t["font"], size)
    space = font.getlength(" ")
    stroke_w = max(0, int(size * t["stroke_pct"]))
    line_ws = [int(_line_w(ln, font, space)) for ln in lines]
    block_w = max(line_ws) if line_ws else 0
    asc, desc = font.getmetrics()
    line_h = asc + desc
    line_gap = int(line_h * 0.14) if len(lines) > 1 else 0
    text_h = line_h * len(lines) + line_gap * (len(lines) - 1)
    # Full-title advance on a single (un-wrapped) line — the CSS "max-content"
    # width. The pill box shrink-to-fits to this, capped at its max-width, so a
    # wrapped title fills the wide pill instead of hugging the widest line.
    one_line_w = int(_line_w(words, font, space)) if words else 0
    return {
        "font": font, "size": size, "stroke_w": stroke_w, "space": space,
        "lines": lines, "line_ws": line_ws, "block_w": block_w,
        "line_h": line_h, "line_gap": line_gap, "text_h": text_h,
        "one_line_w": one_line_w,
    }


def _title_scale(spec: dict, W: int) -> float:
    """Effective on-screen scale of the title unit — the factor the canvas applies
    to *everything nested in the title* (the VALORANT subtitle and the bonus
    call-out). For a wrap-to-two-lines title this is just the drag scale ts; for a
    forced single line it's the auto-shrink the fit uses to keep the title on one
    line. Mirrors app.js `vceApplyOffsets`, which scales the whole titleWrap —
    subtitle + bonus included — by that same factor, so they must track it here or
    they render oversized relative to the shrunk title (preview↔render drift)."""
    t = spec["title"]
    off = spec.get("offsets") or {}
    ts = float(off.get("ts", 1.0) or 1.0)
    if int(t.get("max_lines", 2) or 2) == 1:
        base = W * t["size_pct"]
        if base > 0:
            return _fit_title(t, W, ts)["size"] / base
    return ts


def _draw_words(base: Image.Image, words, x: int, y: int, font,
                fill, accent, accent_words, stroke_w: int, stroke_fill,
                shadow):
    d = ImageDraw.Draw(base)
    space = font.getlength(" ")
    accent_set = {w.lower() for w in (accent_words or [])}
    hard = shadow == "hard" or shadow is True
    asc, desc = font.getmetrics()
    line_h = asc + desc
    cx = float(x)
    for w in words:
        if has_emoji(w):
            # mixed word: draw text runs (stroke) + composite emoji glyphs inline
            wx = cx
            for is_e, s in _emoji_runs(w):
                if not s:
                    continue
                if is_e:
                    g = _emoji_glyph(s, _emoji_em(font))
                    if g is not None:
                        base.alpha_composite(g, (int(wx), int(y + (line_h - g.height) // 2)))
                        wx += _emoji_adv(s, font)
                else:
                    if hard:
                        off = max(2, stroke_w)
                        d.text((wx + off, y + off), s, font=font, fill=(0, 0, 0, 150),
                               stroke_width=stroke_w, stroke_fill=(0, 0, 0, 150))
                    d.text((wx, y), s, font=font, fill=fill,
                           stroke_width=stroke_w, stroke_fill=stroke_fill)
                    wx += font.getlength(s)
            cx = wx + space
        else:
            col = accent if w.lower() in accent_set else fill
            if hard:
                off = max(2, stroke_w)
                d.text((cx + off, y + off), w, font=font, fill=(0, 0, 0, 150),
                       stroke_width=stroke_w, stroke_fill=(0, 0, 0, 150))
            d.text((cx, y), w, font=font, fill=col,
                   stroke_width=stroke_w, stroke_fill=stroke_fill)
            cx += font.getlength(w) + space


def _title_padding(t: dict, W: int, stroke_w: int, size: int) -> tuple[int, int]:
    if t["style"] == "pill":
        return int(W * t["pill_pad_pct"] * 1.15), int(size * 0.34)
    return stroke_w + 8, int(size * 0.16)


# The pill's CSS max-width (design px at 1080 wide): app.js sets the title words
# `max-width:920px`. Keep the two in lock-step.
_PILL_MAX_W = 920


def _title_layer_w(t: dict, W: int, one_line_w: int, ink_w: int,
                   pad_h: int) -> int:
    """Width of the title layer / pill box.

    Mirrors the canvas. A *wrapping* pill (max_lines > 1) is a shrink-to-fit box
    capped at its max-width: when the un-wrapped title exceeds the cap the pill
    fills 920px and the wrapped lines centre inside it (app.js sets the title
    words `max-width:920px` in the two-line branch). Sizing by the widest wrapped
    line — the old behaviour — rendered a two-line pill far narrower than preview.
    Single-line pills (the auto-shrink branch, `max-width:none` on the canvas) and
    plain styles keep the tight text box, unchanged."""
    if t["style"] == "pill" and int(t.get("max_lines", 2) or 2) > 1:
        lay_w = min(int(one_line_w) + 2 * pad_h, round(_PILL_MAX_W * W / 1080.0))
        return max(lay_w, ink_w)      # never clip the wrapped ink
    return ink_w + 2 * pad_h


def _draw_title_canvas(t: dict, W: int, ts: float = 1.0):
    """Lay the (possibly wrapped) title out on a scratch canvas; returns
    (canvas, ink_bbox, geom). Soft shadows are baked into the canvas."""
    ft = _fit_title(t, W, ts)
    font, stroke_w, size = ft["font"], ft["stroke_w"], ft["size"]
    lines, line_ws, block_w = ft["lines"], ft["line_ws"], ft["block_w"]
    line_h = ft["line_h"]
    gap = int(size * 0.12) if len(lines) > 1 else 0
    n = len(lines)

    fill = _hex(t["fill"])
    accent = _hex(t.get("accent", t["fill"]))
    stroke_fill = _hex(t["stroke"])
    upper = bool(t.get("upper"))

    pad0 = stroke_w + int(size * 0.5) + 24
    canvas = Image.new("RGBA", (block_w + 2 * pad0, n * line_h + (n - 1) * gap + 2 * pad0),
                       (0, 0, 0, 0))
    line_origins = []
    for i, ln in enumerate(lines):
        ln = [w.upper() for w in ln] if upper else ln
        lx = pad0 + (block_w - line_ws[i]) // 2
        ly = pad0 + i * (line_h + gap)
        line_origins.append((lx, ly))
        _draw_words(canvas, ln, lx, ly, font, fill, accent,
                    t.get("accent_words"), stroke_w, stroke_fill,
                    t["shadow"] if t["shadow"] != "soft" else False)
    if t["shadow"] == "soft":
        blur = max(2, int(size * 0.14))
        a = canvas.split()[3].point(lambda v: int(v * 0.55))
        sh = Image.new("RGBA", canvas.size, (0, 0, 0, 255)); sh.putalpha(a)
        tmp = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        tmp.alpha_composite(sh, (0, max(1, int(size * 0.04))))
        soft = tmp.filter(ImageFilter.GaussianBlur(blur))
        soft.alpha_composite(canvas)
        canvas = soft

    ink = canvas.getbbox() or (0, 0, canvas.width, canvas.height)
    geom = {"font": font, "stroke_w": stroke_w, "size": size, "lines": lines,
            "line_origins": line_origins, "fill": fill, "accent": accent,
            "stroke_fill": stroke_fill, "one_line_w": ft["one_line_w"]}
    return canvas, ink, geom


def _title_block(spec: dict, W: int, H: int) -> tuple[Image.Image, int]:
    """Render the title to its own RGBA layer; return (image, y_top)."""
    t = spec["title"]
    off = spec.get("offsets") or {}
    ts = float(off.get("ts", 1.0) or 1.0)
    canvas, ink, g = _draw_title_canvas(t, W, ts)
    pad_h, pad_v = _title_padding(t, W, g["stroke_w"], g["size"])

    ink_w, ink_h = ink[2] - ink[0], ink[3] - ink[1]
    lay_w = _title_layer_w(t, W, g["one_line_w"], ink_w, pad_h)
    lay_h = ink_h + 2 * pad_v
    text_x = (lay_w - ink_w) // 2          # centre the text block in the pill box
    layer = Image.new("RGBA", (max(2, lay_w), max(2, lay_h)), (0, 0, 0, 0))

    if t["style"] == "pill":
        d = ImageDraw.Draw(layer)
        rad = min(int(lay_h * t["pill_radius_pct"]), lay_h // 2, lay_w // 2)
        d.rounded_rectangle([0, 0, lay_w - 1, lay_h - 1], radius=rad,
                            fill=_hex(t["pill_fill"], t.get("pill_alpha", 235)))

    layer.alpha_composite(canvas.crop(ink), (text_x, pad_v))

    if t.get("italic"):
        layer = _shear(layer, 0.18)

    y_top = int(H * t["y_pct"] + _off_px(spec, W, "ty"))
    return layer, y_top


def _off_px(spec: dict, W: int, key: str) -> float:
    """Drag offsets are px at 1080-wide; scale with the actual W."""
    off = spec.get("offsets") or {}
    return float(off.get(key, 0) or 0) * (W / 1080.0)


def _title_x(spec: dict, W: int, title_w: int) -> int:
    """Title left edge: centered styles anchor at W/2+tx, left styles at x+tx."""
    t = spec["title"]
    tx = _off_px(spec, W, "tx")
    if t.get("align") == "left":
        left = W * float(t.get("x_pct", 0.045)) + tx
    elif t.get("align") == "right":
        left = W - title_w - W * 0.045 + tx
    else:
        left = (W - title_w) / 2 + tx
    return int(max(-title_w + 10, min(left, W - 10)))


# ----------------------------------------------------------------------------- #
# List rows (numbers, labels, widgets)
# ----------------------------------------------------------------------------- #
def _widget_sprite(spec: dict, W: int, i: int, label_px: int) -> Optional[dict]:
    """The rank's widget image scaled per style; returns {img, w, h} or None."""
    widgets = spec.get("widgets") or []
    w = widgets[i] if i < len(widgets) else None
    if not w or not isinstance(w, dict) or not w.get("src"):
        return None
    img = _asset_image(w["src"])
    if img is None:
        return None
    off = spec.get("offsets") or {}
    ls = float(off.get("ls", 1.0) or 1.0)
    base = spec.get("widget_h_pct", 92 / 1080) * W * ls
    h = base * (0.6 if w.get("kind") == "weapon" else 1.0)
    scale = min(h / max(1, img.height), (340 / 1080 * W * ls) / max(1, img.width))
    nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    img = _soft_shadow(img, dy=max(1, int(nh * 0.03)), blur=max(2, int(nh * 0.10)), alpha=140)
    return {"img": img, "ml": int(label_px * 0.4)}


def _iter_rows(spec: dict, W: int, H: int):
    """Yield per-row render data: full row sprite + number/label/widget parts.
    Progressive reveal: an empty item shows just its number. All px geometry is
    scaled by the list drag scale `ls` and shifted by lx/ly."""
    lst = spec["list"]
    if not lst.get("count"):
        return
    off = spec.get("offsets") or {}
    ls = float(off.get("ls", 1.0) or 1.0)

    label_size = int(W * lst["size_pct"] * ls)
    num_size = int(W * lst.get("num_size_pct", lst["size_pct"]) * ls)
    stroke_pct = lst["stroke_pct"]
    fill = _hex(lst["fill"])
    accent = _hex(lst.get("accent", lst["fill"]))
    stroke_fill = _hex(lst["stroke"])
    x = int(W * lst["x_pct"] + _off_px(spec, W, "lx"))
    y = int(H * lst["y_pct"] + _off_px(spec, W, "ly"))
    gap = int(H * lst["gap_pct"] * ls)
    num_min_w = int(W * lst.get("num_min_w_pct", 0) * ls)
    label_ml = int(W * lst.get("label_ml_pct", 0) * ls)
    items = lst.get("items") or []
    suffix = lst.get("suffix", ".")
    highlight = int(lst.get("highlight", -1))
    italic = lst.get("italic")
    shadow = lst.get("shadow")
    upper_label = lst.get("upper_label")
    valign = lst.get("valign", "baseline")
    num_col = accent if lst.get("num_accent") else fill

    right_margin = int(W * 0.045)

    for i in range(int(lst["count"])):
        label = items[i] if i < len(items) else ""
        if upper_label:
            label = label.upper()
        num = f"{i + 1}{suffix}"
        hot = (i == highlight)
        lab_size = int(label_size * (1.14 if hot else 1.0))
        n_size = int(num_size * (1.14 if hot else 1.0))

        # fit the label down so a long one never runs off the frame
        nfont = _font(lst.get("num_font", lst["font"]), n_size)
        n_stroke = max(0, int(n_size * stroke_pct))
        col_w = max(num_min_w, int(nfont.getlength(num) + (0 if num_min_w else nfont.getlength(" "))))
        avail = max(60, W - x - col_w - label_ml - right_margin)
        sz = lab_size
        for _ in range(40):
            lfont = _font(lst["font"], sz)
            l_stroke = max(0, int(sz * stroke_pct))
            bb = lfont.getbbox(label or " ", stroke_width=l_stroke)
            tw = bb[2] - bb[0]
            extra = 0.22 * sz if italic else 0
            if tw + extra <= avail or sz <= 14:
                break
            sz = int(sz * 0.94)
        lfont = _font(lst["font"], sz)
        l_stroke = max(0, int(sz * stroke_pct))
        col = accent if hot else fill
        ncol = accent if hot else num_col
        # Medal ranks (Blue Pill only): #1/2/3 → gold/silver/bronze. Two
        # independent switches — numbers (medals) and label text (medal_text).
        if i < 3 and spec.get("style") == "pill":
            if lst.get("medals"):
                ncol = _MEDAL_COLORS[i]
            if lst.get("medal_text"):
                col = _MEDAL_COLORS[i]

        num_img, (npad, _) = _text_sprite(num, nfont, ncol, n_stroke, stroke_fill, shadow, italic)
        lab_img = lab_pad = None
        if label:
            lab_img, (lab_pad, _) = _text_sprite(label, lfont, col, l_stroke, stroke_fill, shadow, italic)

        # vertical placement inside the row slot
        top = y + i * gap
        n_asc, n_desc = nfont.getmetrics()
        l_asc, l_desc = lfont.getmetrics()
        if valign == "center":
            slot_c = top + gap // 2
            n_top = slot_c - (n_asc + n_desc) // 2
            l_top = slot_c - (l_asc + l_desc) // 2
        else:                                   # baseline-align on the number's baseline
            n_top = top
            l_top = top + n_asc - l_asc
        n_bb = nfont.getbbox(num, stroke_width=n_stroke)
        parts = [{"sprite": num_img, "px": x + n_bb[0] - npad, "py": n_top + n_bb[1] - npad,
                  "kind": "num"}]
        lab_x = x + col_w + label_ml
        widget = None
        if lab_img is not None:
            l_bb = _place_bbox(label, lfont, l_stroke)
            parts.append({"sprite": lab_img, "px": lab_x + l_bb[0] - lab_pad,
                          "py": l_top + l_bb[1] - lab_pad, "kind": "label"})
            wdg = _widget_sprite(spec, W, i, sz)
            if wdg is not None:
                wx = lab_x + l_bb[2] + wdg["ml"]
                l_mid = l_top + (l_asc + l_desc) // 2
                widget = {"sprite": wdg["img"], "px": int(wx),
                          "py": int(l_mid - wdg["img"].height / 2), "index": i}

        # composite the full row sprite (text only — the widget stays separate
        # so the animation can idle-bob it)
        min_x = min(p["px"] for p in parts)
        min_y = min(p["py"] for p in parts)
        max_x = max(p["px"] + p["sprite"].width for p in parts)
        max_y = max(p["py"] + p["sprite"].height for p in parts)
        row = Image.new("RGBA", (max(2, max_x - min_x), max(2, max_y - min_y)), (0, 0, 0, 0))
        for p in parts:
            row.alpha_composite(p["sprite"], (p["px"] - min_x, p["py"] - min_y))

        yield {"index": i, "sprite": row, "px": min_x, "py": min_y,
               "hot": hot, "parts": parts, "widget": widget,
               "has_label": bool(label)}


def _list_content_box(spec: dict, W: int, H: int) -> Optional[tuple]:
    """Union bbox of the rendered rows (+ widgets), measured with the FULL label
    set so the box — and the icon centred on it — stays put across the
    progressive reveal (mirrors the design's opacity-hidden-but-space-keeping
    rows). Falls back to the current items when no full set is supplied."""
    import copy
    full = (spec.get("list") or {}).get("full_items")
    meas = spec
    if full:
        meas = copy.copy(spec)
        meas["list"] = dict(spec["list"]); meas["list"]["items"] = full
    x0 = y0 = x1 = y1 = None
    for r in _iter_rows(meas, W, H):
        rects = [(r["px"], r["py"], r["px"] + r["sprite"].width, r["py"] + r["sprite"].height)]
        wd = r.get("widget")
        if wd:
            rects.append((wd["px"], wd["py"], wd["px"] + wd["sprite"].width, wd["py"] + wd["sprite"].height))
        for a, b, c, d in rects:
            x0 = a if x0 is None else min(x0, a)
            y0 = b if y0 is None else min(y0, b)
            x1 = c if x1 is None else max(x1, c)
            y1 = d if y1 is None else max(y1, d)
    return None if x0 is None else (x0, y0, x1, y1)


def _list_geometry(spec: dict, W: int, H: int) -> dict:
    """Centre point for the icon backdrop. Band styles (minimal/editorial) centre
    on the fixed left/right list band (≈ W/2); the shrink-wrapped pill list
    centres on its actual content so the icon sits behind the ranked rows."""
    lst = spec["list"]
    off = spec.get("offsets") or {}
    ls = float(off.get("ls", 1.0) or 1.0)
    x = W * lst["x_pct"] + _off_px(spec, W, "lx")
    y = H * lst["y_pct"] + _off_px(spec, W, "ly")
    h = H * lst["gap_pct"] * ls * int(lst.get("count") or 0)
    if not lst.get("list_band", True):
        box = _list_content_box(spec, W, H)
        if box:
            return {"cx": (box[0] + box[2]) / 2, "cy": (box[1] + box[3]) / 2, "ls": ls}
    w = max(60.0, (W - 2 * W * lst["x_pct"]) * ls)
    return {"cx": x + w / 2, "cy": y + h / 2, "ls": ls}


def _icon_sprite(spec: dict, W: int, H: int) -> Optional[dict]:
    """Tinted VCT logo backdrop, centered on the list block."""
    ic = spec.get("icon") or {}
    if not ic.get("on"):
        return None
    img = None
    if ICON_PATH.exists():
        try:
            img = Image.open(ICON_PATH).convert("RGBA")
        except Exception:
            img = None
    if img is None:
        return None
    g = _list_geometry(spec, W, H)
    size = max(24, int(float(ic.get("size", 468)) * (W / 1080.0) * g["ls"]))
    scale = min(size / max(1, img.width), size / max(1, img.height))
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                     Image.LANCZOS)
    opacity = max(0.0, min(float(ic.get("opacity", 7)) / 100.0, 1.0))
    r, gg, b, _a = _hex(ic.get("color", "#ffffff"))
    a = img.split()[3].point(lambda v: int(v * opacity))
    tinted = Image.new("RGBA", img.size, (r, gg, b, 255))
    tinted.putalpha(a)
    return {"sprite": tinted, "px": int(g["cx"] - img.width / 2),
            "py": int(g["cy"] - img.height / 2)}


# ----------------------------------------------------------------------------- #
# "Now Playing" clip-info card
# ----------------------------------------------------------------------------- #
def _rgba_f(color: str, frac: float) -> tuple[int, int, int, int]:
    r, g, b, _ = _hex(color)
    return (r, g, b, max(0, min(int(round(frac * 255)), 255)))


def _vd_mark(accent: tuple, diam: int, text_hex: str = "#ffffff") -> Image.Image:
    """The 'VD' channel mark that sits where the dot used to: a hollow circle
    outlined in the accent, italic-black V (white) + D (accent), with an accent
    glow. Everything tracks the clip-info accent (design: renderClipBox `vd`)."""
    d = max(10, int(diam))
    ss = 4                                             # supersample the ring
    border = max(2, int(round(diam * 3 / 51)))
    big = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
    inset = border * ss / 2
    ImageDraw.Draw(big).ellipse(
        [inset, inset, d * ss - inset, d * ss - inset],
        fill=(0, 0, 0, 72), outline=accent, width=border * ss)      # bg rgba(0,0,0,.28)
    circ = big.resize((d, d), Image.LANCZOS)

    glowpad = int(d * 0.34)
    out = Image.new("RGBA", (d + 2 * glowpad, d + 2 * glowpad), (0, 0, 0, 0))
    glow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([glowpad, glowpad, glowpad + d, glowpad + d],
                                 fill=(*accent[:3], 110))
    out.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(2, int(d * 0.14)))))
    out.alpha_composite(circ, (glowpad, glowpad))

    lf = _font("poppins-black", max(8, int(round(diam * 22.8 / 51))))
    v_img, _p = _text_sprite("V", lf, _hex(text_hex), 0, (0, 0, 0, 0), False, True)
    d_img, _q = _text_sprite("D", lf, accent, 0, (0, 0, 0, 0), False, True)
    v_img = v_img.crop(v_img.getbbox() or (0, 0, 1, 1))
    d_img = d_img.crop(d_img.getbbox() or (0, 0, 1, 1))
    overlap = max(1, int(round(diam * 1.0 / 51)))      # letter-spacing:-1px
    lw = v_img.width + d_img.width - overlap
    lh = max(v_img.height, d_img.height)
    letters = Image.new("RGBA", (max(1, lw), max(1, lh)), (0, 0, 0, 0))
    letters.alpha_composite(v_img, (0, (lh - v_img.height) // 2))
    letters.alpha_composite(d_img, (v_img.width - overlap, (lh - d_img.height) // 2))
    cx = glowpad + d // 2 - int(round(diam * -2.75 / 51))         # nudge right of centre
    cy = glowpad + d // 2 - int(round(diam * 0.5 / 51))
    out.alpha_composite(letters, (cx - lw // 2, cy - lh // 2))
    return out


def _card_sprite(spec: dict, W: int, H: int) -> Optional[dict]:
    """Render the clip-info card; returns {sprite, px, py} or None."""
    c = spec.get("clip_info")
    if not c:
        return None
    ranked = (c.get("kind") or "competition") == "ranked"
    has_react = bool(c.get("react") or c.get("streamer"))
    cap_set = bool((c.get("caption") or "").strip())
    if ranked:
        if not (cap_set or has_react):
            return None
    elif not (c.get("a") or c.get("b") or cap_set or has_react):
        return None
    st = dict(TEMPLATES["valorant_clip_ranking"]["clip_style"])
    st.update(spec.get("clip_style") or {})
    u = W / 1080.0
    k = float(st.get("size", 1.0) or 1.0) * u
    tk = float(st.get("text_size", 1.0) or 1.0)

    def r(n): return max(1, int(round(n * k)))
    def tt(n): return max(8, int(round(n * k * tk)))

    bg = _rgba_f(st["bg"], max(0.0, min(float(st.get("opacity", 0.78)), 1.0)))
    accent = _hex(st["accent"])
    text = st["text"]

    pad_v, pad_h, gap = r(22), r(26), r(15)
    max_w = r(720)

    blocks: list[Image.Image] = []

    # header: VD channel mark + NOW PLAYING / BEST CLIPS (both track the accent).
    # `_vd_mark` bakes a soft glow as transparent PADDING around the ring, so lay
    # the header out by the RING diameter — not the padded sprite — or the VD→text
    # gap and header height balloon vs the CSS preview (which glows via box-shadow,
    # a zero-layout effect). The glow spills past the block and is harmlessly
    # clipped; the visible ring aligns flush-left like the preview's VD div.
    head_f = _font("poppins-extrabold", tt(29))
    # preview header is `letter-spacing:2px` (unscaled) — mirror it so the header
    # width, the widest block, matches and the card doesn't render narrower.
    head_img = _tracked_sprite("BEST CLIPS" if ranked else "NOW PLAYING",
                               head_f, accent, 2 * u, shadow=False)
    ring = tt(51)
    vd = _vd_mark(accent, ring, text)
    glow_m = max(0, (vd.width - ring) // 2)          # transparent glow padding baked in
    gap_head = r(12)
    hh = max(ring, head_img.height)
    head = Image.new("RGBA", (ring + gap_head + head_img.width, hh), (0, 0, 0, 0))
    head.alpha_composite(vd, (-glow_m, (hh - ring) // 2 - glow_m))
    head.alpha_composite(head_img, (ring + gap_head, (hh - head_img.height) // 2))
    blocks.append(head)

    # logos row: A vs B — competition clips only (ranked clips are header + caption)
    if not ranked and (c.get("a") or c.get("b")):
        def logo(t):
            img = _asset_image((t or {}).get("src", "")) if t else None
            if img is None:
                q_f = _font("poppins-extrabold", tt(40))
                q, _p = _text_sprite("?", q_f, _rgba_f(text, 0.5), 0, (0, 0, 0, 0), False, False)
                return q
            h = r(92)
            scale = min(h / max(1, img.height), r(190) / max(1, img.width))
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                             Image.LANCZOS)
            return _soft_shadow(img, dy=r(3), blur=r(5), alpha=120)
        a_img, b_img = logo(c.get("a")), logo(c.get("b"))
        vs_f = _font("anton", tt(36))
        vs_img, _p = _text_sprite("vs", vs_f, _rgba_f(text, 0.75), 0, (0, 0, 0, 0), False, False)
        gap2 = r(20)
        mw = a_img.width + gap2 + vs_img.width + gap2 + b_img.width
        mh = max(a_img.height, vs_img.height, b_img.height)
        mid = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
        cx = 0
        for im in (a_img, vs_img, b_img):
            mid.alpha_composite(im, (cx, (mh - im.height) // 2))
            cx += im.width + gap2
        blocks.append(mid)

    # caption
    cap = (c.get("caption") or "").strip()
    if cap:
        # Draw the caption in a `line-height:1.05` line-box (matching the preview's
        # CSS), with inline color emoji (`_rich_line_sprite`). Shrink-to-fit off the
        # rendered sprite width so emoji count at their real (not tofu) width.
        cap_f = _font("poppins-extrabold", tt(38))
        line_h = max(2, int(round(cap_f.size * 1.05)))
        cap_img = _rich_line_sprite(cap, cap_f, _hex(text), line_h)
        for _ in range(20):
            if cap_img.width <= max_w - 2 * pad_h or cap_f.size <= 12:
                break
            cap_f = _font("poppins-extrabold", int(cap_f.size * 0.94))
            line_h = max(2, int(round(cap_f.size * 1.05)))
            cap_img = _rich_line_sprite(cap, cap_f, _hex(text), line_h)
        blocks.append(cap_img)

    cw = min(max_w, max(b.width for b in blocks) + 2 * pad_h)
    ch = sum(b.height for b in blocks) + gap * (len(blocks) - 1) + 2 * pad_v
    card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=r(24), fill=bg,
                        outline=_rgba_f(text, 0.16), width=max(1, r(1)))
    cy = pad_v
    for b in blocks:
        card.alpha_composite(b, (pad_h, cy))
        cy += b.height + gap
    dy_c, blur_c = r(9), r(12)
    card = _soft_shadow(card, dy=dy_c, blur=blur_c, alpha=120)
    pad_c = _shadow_pad(dy_c, blur_c)

    sm = _resolve_streamer(c.get("react") or c.get("streamer"))
    if sm is None:
        # No reaction → anchor the CARD BOX (not the transparent soft-shadow
        # margin) at right:46 / bottom:78, exactly where the live preview sits.
        px = W - int(46 * u) - pad_c - cw
        py = H - int(78 * u) - pad_c - ch
        return {"sprite": card, "px": px, "py": py}

    # Reaction: player/streamer portrait pops over the card top; @handle chip
    # below it (only when a handle is set — players may have none). In the design
    # the flex column is box → handle (portrait absolute over box); the WRAP (not
    # the box) is anchored at right:46 / bottom:78.
    port_sh = None
    pvh = pw = pad_p = 0
    port = _asset_image(sm["src"])
    if port is not None:
        ph = r(172)
        scale = ph / max(1, port.height)
        port = port.resize((max(1, int(port.width * scale)), max(1, ph)), Image.LANCZOS)
        port = port.crop((0, 0, port.width, max(1, int(ph * 0.58))))   # clip bottom 42%
        pw, pvh = port.width, port.height
        dy_p, blur_p = r(6), r(18)
        pad_p = _shadow_pad(dy_p, blur_p)
        port_sh = _soft_shadow(port, dy=dy_p, blur=blur_p, alpha=140)

    # @handle chip: twitch glyph + handle text in a translucent pill (optional)
    chip_sh = None
    whc = hhc = pad_h2 = 0
    handle_txt = (sm.get("handle") or "").strip()
    if handle_txt:
        hf = _font("poppins-extrabold", tt(29))
        htxt, _hpad = _text_sprite(handle_txt, hf, _hex(text), 0, (0, 0, 0, 0), False, False)
        glyph = _asset_image("/assets/twitch.png")
        gsz = tt(34)
        if glyph is not None:
            gs = gsz / max(1, max(glyph.width, glyph.height))
            glyph = glyph.resize((max(1, int(glyph.width * gs)), max(1, int(glyph.height * gs))), Image.LANCZOS)
        gw = glyph.width if glyph is not None else 0
        gh = glyph.height if glyph is not None else 0
        ph_v, ph_h, gap_g = r(9), r(20), r(11)
        inner_w = gw + (gap_g if gw else 0) + htxt.width
        inner_h = max(gh, htxt.height)
        whc = inner_w + 2 * ph_h
        hhc = inner_h + 2 * ph_v
        chip = Image.new("RGBA", (whc, hhc), (0, 0, 0, 0))
        ImageDraw.Draw(chip).rounded_rectangle(
            [0, 0, whc - 1, hhc - 1], radius=hhc // 2, fill=(12, 15, 21, 158),
            outline=_rgba_f(text, 0.14), width=max(1, r(1)))
        hx = ph_h
        if glyph is not None:
            chip.alpha_composite(glyph, (hx, (hhc - gh) // 2)); hx += gw + gap_g
        chip.alpha_composite(htxt, (hx, (hhc - htxt.height) // 2))
        dy_h, blur_h = r(10), r(20)
        pad_h2 = _shadow_pad(dy_h, blur_h)
        chip_sh = _soft_shadow(chip, dy=dy_h, blur=blur_h, alpha=105)

    gap_bh = r(15)
    # Assemble box (core cw×ch) + portrait (above, centered) + optional chip
    # (below, centered) in one canvas; margins hold each sprite's shadow spill.
    mx = max(pad_c, pad_p, pad_h2) + max(0, (max(pw, whc) - cw + 1) // 2) + 4
    my_top = pvh + pad_p + 4
    my_bot = (gap_bh + hhc + pad_h2 + 4) if chip_sh is not None else (pad_c + 4)
    canvas = Image.new("RGBA", (cw + 2 * mx, my_top + ch + my_bot), (0, 0, 0, 0))
    bx0, by0 = mx, my_top                                  # box core top-left
    canvas.alpha_composite(card, (bx0 - pad_c, by0 - pad_c))
    if port_sh is not None:
        pcx = bx0 + (cw - pw) // 2
        canvas.alpha_composite(port_sh, (pcx - pad_p, by0 - pvh - pad_p))
    if chip_sh is not None:
        hcx = bx0 + (cw - whc) // 2
        canvas.alpha_composite(chip_sh, (hcx - pad_h2, by0 + ch + gap_bh - pad_h2))

    # Anchor: wrap right = box right (W-46); wrap bottom = chip bottom (or box
    # bottom when there is no chip) at H-78.
    px = int(W - 46 * u - (bx0 + cw))
    bottom = by0 + ch + (gap_bh + hhc if chip_sh is not None else 0)
    py = int(H - 78 * u - bottom)
    return {"sprite": canvas, "px": px, "py": py}


def _resolve_streamer(s) -> Optional[dict]:
    """A clip-info reaction may be a streamer key ('fns'), or a react dict
    {src, key?, handle?} for a player OR streamer. Returns {src, handle} — the
    handle may be "" (players often have none: portrait shows, chip is skipped)."""
    if not s:
        return None
    if isinstance(s, str):
        return STREAMER_BY_KEY.get(s)
    if isinstance(s, dict):
        if s.get("src"):
            return {"src": s["src"], "handle": (s.get("handle") or "").strip()}
        by_key = STREAMER_BY_KEY.get(s.get("key"))
        return dict(by_key) if by_key else None
    return None


# ----------------------------------------------------------------------------- #
# Bonus title (sticker / marker / tag / spark)
# ----------------------------------------------------------------------------- #
def _star(size: int, color) -> Image.Image:
    """A 4-point sparkle (✦) drawn as a concave-diamond polygon with a glow."""
    s = max(6, size)
    img = Image.new("RGBA", (s * 3, s * 3), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = cy = s * 3 // 2
    R, r0 = s / 2, s * 0.14
    pts = []
    for i in range(8):
        ang = math.pi / 4 * i - math.pi / 2
        rad = R if i % 2 == 0 else r0
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=color)
    glow = img.filter(ImageFilter.GaussianBlur(s * 0.18))
    glow.alpha_composite(img)
    return glow.crop(glow.getbbox() or (0, 0, 1, 1))


def _bonus_sprite(spec: dict, W: int) -> Optional[dict]:
    """Render the bonus call-out; returns {sprite, sparkles:[{img,dx,dy,delay}]}
    with geometry scaled by the effective title scale (drag ts + single-line fit)."""
    b = spec.get("bonus") or {}
    txt = (b.get("text") or "").strip()
    if not b.get("on") or not txt:
        return None
    # Scale with the title unit (canvas nests the bonus inside it) — this tracks
    # the single-line auto-shrink, not just the raw drag scale ts — then apply the
    # per-bonus size multiplier (mirrors the canvas scaling `el` by bonus.size).
    u = (W / 1080.0) * _title_scale(spec, W) * float(b.get("size", 1.0) or 1.0)
    style = b.get("style", "sticker")
    sparkles = []

    if style == "sticker":
        f = _font("poppins-extrabold", int(46 * u))
        timg, _p = _text_sprite(txt, f, _hex("#04121f"), 0, (0, 0, 0, 0), False, False)
        pad_v, pad_h = int(14 * u), int(30 * u)
        wp, hp = timg.width + 2 * pad_h, timg.height + 2 * pad_v
        img = Image.new("RGBA", (wp, hp), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, wp - 1, hp - 1], radius=int(16 * u), fill=_hex("#f5b942"))
        img.alpha_composite(timg, (pad_h, pad_v))
        img = _soft_shadow(img, dy=int(6 * u), blur=int(9 * u), alpha=110)
        img = img.rotate(4, resample=Image.BICUBIC, expand=True)
    elif style == "marker":
        f = _font("anton", int(52 * u))
        timg, _p = _text_sprite(txt.upper(), f, _hex("#ffffff"), 0, (0, 0, 0, 0), "soft", False)
        bb = timg.getbbox() or (0, 0, timg.width, timg.height)
        img = Image.new("RGBA", (timg.width + int(8 * u), timg.height + int(12 * u)), (0, 0, 0, 0))
        bar_top = bb[1] + int((bb[3] - bb[1]) * 0.62)
        d = ImageDraw.Draw(img)
        d.rectangle([bb[0], bar_top, bb[2] + int(8 * u), bb[3] + int(6 * u)],
                    fill=(255, 70, 85, 217))
        img.alpha_composite(timg, (int(4 * u), 0))
    elif style == "tag":
        f = _font("bebas", int(46 * u))
        timg, _p = _text_sprite("► " + txt.upper(), f, _hex("#ffffff"), 0, (0, 0, 0, 0), False, False)
        pad_v, pad_h = int(8 * u), int(24 * u)
        wp, hp = timg.width + 2 * pad_h, timg.height + 2 * pad_v
        img = Image.new("RGBA", (wp, hp), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, wp - 1, hp - 1], radius=hp // 2,
                            fill=(0, 0, 0, 128), outline=(255, 255, 255, 178),
                            width=max(1, int(2 * u)))
        img.alpha_composite(timg, (pad_h, pad_v))
    else:  # spark — Blue Pill title font, no pill, hovering sparkles
        f = _font("montserrat-black", int(50 * u))
        timg, _p = _text_sprite(txt, f, _hex("#ffffff"), max(1, int(4.5 * u)),
                                _hex("#16233f"), False, False)
        pad = int(34 * u)
        img = Image.new("RGBA", (timg.width + 2 * pad, timg.height + int(12 * u)), (0, 0, 0, 0))
        img.alpha_composite(timg, (pad, int(6 * u)))
        gold = _hex("#ffe27a")
        for sz, dx, dy, dl, right in ((44, -6, -26, 0.0, False), (30, -2, -12, 0.7, True),
                                      (26, 18, 34, 1.3, False)):
            st_img = _star(int(sz * u), gold)
            sx = (img.width - pad + int(dx * u)) if right else (pad + int(dx * u))
            sparkles.append({"img": st_img, "dx": sx, "dy": int(6 * u) + int(dy * u),
                             "delay": dl})
    return {"sprite": img, "sparkles": sparkles, "gap": int(26 * u)}


def _bonus_position(spec: dict, W: int, bonus: dict,
                    title_img: Image.Image, tx: int, ty: int) -> tuple[int, int]:
    """Place the bonus line a fixed `gap` above the title, matching the canvas
    preview. Both sprites carry transparent padding (title glow/plain leading;
    bonus shadow / rotation / stroke), so we anchor by each sprite's VISIBLE
    bounds (getbbox) — otherwise the gap inflates by that padding and the bonus
    floats too high (worst on plain titles + shadowed bonuses). Mirrors the
    preview, which lays the bonus out against the title's text box, not its glow.
    """
    img = bonus["sprite"]
    bb = img.getbbox() or (0, 0, img.width, img.height)                  # visible bonus
    tbb = title_img.getbbox() or (0, 0, title_img.width, title_img.height)  # visible title
    if spec["title"].get("align") == "left":
        bx = tx + tbb[0] - bb[0]                                         # align visible left edges
    else:
        # centre the visible bonus over the visible title
        bx = tx + (tbb[0] + tbb[2]) / 2 - (bb[0] + bb[2]) / 2
    # visible bonus bottom sits `gap` above the visible title top
    by = (ty + tbb[1]) - bonus["gap"] - bb[3]
    return int(round(bx)), int(round(by))


# ----------------------------------------------------------------------------- #
# Valorant subtitle ("VALORANT" + red mark, optional "Streamers React" avatars)
# ----------------------------------------------------------------------------- #
def _tracked_sprite(text: str, font, color, tracking: float, shadow: bool = True) -> Image.Image:
    """Draw text with CSS-style letter-spacing (Pillow has none). Optional soft
    drop shadow (design: text-shadow 0 3px 18px rgba(0,0,0,.55))."""
    asc, desc = font.getmetrics()
    widths = [font.getlength(ch) for ch in text]
    total = int(sum(widths) + tracking * max(0, len(text) - 1))
    img = Image.new("RGBA", (max(2, total + 8), max(2, asc + desc + 8)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 4.0
    for ch, w in zip(text, widths):
        d.text((x, 4), ch, font=font, fill=color)
        x += w + tracking
    core = img.crop(img.getbbox() or (0, 0, 2, 2))
    if shadow:
        blur = max(2, int(getattr(font, "size", 40) * 0.14))
        core = _soft_shadow(core, dy=max(1, int(getattr(font, "size", 40) * 0.06)),
                            blur=blur, alpha=140)
    return core


def _circle_avatar(img: Image.Image, size: int, border: int,
                   border_col=(255, 255, 255, 255)) -> Image.Image:
    """Cover-crop to a circle (object-position center top) with a solid ring."""
    size = max(8, int(size))
    scale = size / max(1, min(img.width, img.height))
    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    left = max(0, (img.width - size) // 2)
    img = img.crop((left, 0, left + size, size))               # center-top cover
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    face = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    face.paste(img, (0, 0), mask)
    b = max(1, int(border))
    out = Image.new("RGBA", (size + 2 * b, size + 2 * b), (0, 0, 0, 0))
    ImageDraw.Draw(out).ellipse([0, 0, size + 2 * b - 1, size + 2 * b - 1], fill=border_col)
    out.alpha_composite(face, (b, b))
    return _soft_shadow(out, dy=max(1, int(size * 0.03)), blur=max(2, int(size * 0.08)), alpha=120)


def _row_compose(items: list[Image.Image], gap: int) -> Optional[Image.Image]:
    """Lay sprites left-to-right, vertically centered, `gap` px apart."""
    items = [im for im in items if im is not None]
    if not items:
        return None
    w = sum(im.width for im in items) + gap * (len(items) - 1)
    h = max(im.height for im in items)
    row = Image.new("RGBA", (max(2, w), max(2, h)), (0, 0, 0, 0))
    x = 0
    for im in items:
        row.alpha_composite(im, (x, (h - im.height) // 2))
        x += im.width + gap
    return row


def _subtitle_sprite(spec: dict, W: int) -> Optional[dict]:
    """The 'VALORANT' subtitle under the title (+ optional Streamers-React row).
    Scales with the subtitle size and the effective title scale (drag ts +
    single-line fit) — it lives inside the title unit in the design."""
    sub = spec.get("subtitle") or {}
    txt = (sub.get("text") or "").strip()
    if not sub.get("on") or not txt:
        return None
    z = float(sub.get("size", 0.70) or 0.70)
    # Scale with the title unit (canvas nests the subtitle inside it) — tracks the
    # single-line auto-shrink so it doesn't render oversized against a shrunk title.
    u = (W / 1080.0) * z * _title_scale(spec, W)
    gap = max(2, int(18 * u))
    white = _hex("#ffffff")

    items: list[Image.Image] = []
    wf = _font("poppins-extrabold", max(8, int(48 * u)))
    items.append(_tracked_sprite(txt.upper(), wf, white, 6 * u))
    glyph = _asset_image("/assets/valorant.png")
    if glyph is not None:
        gh = max(6, int(46 * u))
        gs = gh / max(1, glyph.height)
        items.append(glyph.resize((max(1, int(glyph.width * gs)), gh), Image.LANCZOS))

    after = (sub.get("after") or "").strip()
    if after:
        items.append(_tracked_sprite(after.upper(), wf, white, 6 * u))

    if sub.get("streamers"):
        keys = [k for k in (sub.get("streamer_keys") or []) if STREAMER_BY_KEY.get(k)]
        if keys:
            bf = _font("poppins-extrabold", max(8, int(48 * u)))
            bar = lambda: _tracked_sprite("|", bf, _hex("#ffffff", 102), 0, shadow=False)
            items.append(bar())
            items.append(_tracked_sprite("STREAMERS REACT", bf, white, 3 * u))
            items.append(bar())
            for k in keys:
                av = _asset_image(STREAMER_BY_KEY[k]["src"])
                if av is not None:
                    items.append(_circle_avatar(av, int(105 * u), max(2, int(3 * u))))

    row = _row_compose(items, gap)
    if row is None:
        return None
    return {"sprite": row, "gap": max(2, int(24 * u))}


def _subtitle_position(spec: dict, W: int, sub: dict,
                       title_img: Image.Image, tx: int, y_top: int) -> tuple[int, int]:
    """Directly under the title, matching its horizontal alignment."""
    img = sub["sprite"]
    if spec["title"].get("align") == "left":
        bx = tx
    else:
        bx = tx + (title_img.width - img.width) // 2
    by = y_top + title_img.height + sub["gap"]
    return int(bx), int(by)


# ----------------------------------------------------------------------------- #
# Static render + animation layers
# ----------------------------------------------------------------------------- #
def render(preset_key: str, overrides: Optional[dict], dst: str,
           W: int = 1080, H: int = 1920) -> str:
    """Render the full template overlay PNG and return its path."""
    preset = TEMPLATES.get(preset_key) or TEMPLATES["valorant_clip_ranking"]
    spec = _merge(preset, overrides)

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    icon = _icon_sprite(spec, W, H)
    if icon:
        base.alpha_composite(icon["sprite"], (icon["px"], icon["py"]))

    for r in _iter_rows(spec, W, H):
        base.alpha_composite(r["sprite"], (r["px"], r["py"]))
        if r["widget"]:
            base.alpha_composite(r["widget"]["sprite"],
                                 (r["widget"]["px"], r["widget"]["py"]))

    title_img, y_top = _title_block(spec, W, H)
    tx = _title_x(spec, W, title_img.width)
    base.alpha_composite(title_img, (tx, y_top))

    bonus = _bonus_sprite(spec, W)
    if bonus and (spec.get("bonus") or {}).get("show", "anim") != "omit":
        bx, by = _bonus_position(spec, W, bonus, title_img, tx, y_top)
        base.alpha_composite(bonus["sprite"], (bx, by))
        for s in bonus["sparkles"]:
            base.alpha_composite(s["img"], (bx + s["dx"], by + s["dy"]))

    sub = _subtitle_sprite(spec, W)
    if sub:
        sx, sy = _subtitle_position(spec, W, sub, title_img, tx, y_top)
        base.alpha_composite(sub["sprite"], (sx, sy))

    card = _card_sprite(spec, W, H)
    if card:
        base.alpha_composite(card["sprite"], (card["px"], card["py"]))

    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    base.save(dst)
    return dst


def _title_part_sprites(spec: dict, W: int, H: int, tx: int, y_top: int) -> list[dict]:
    """Per-word sprites (+ pill backdrop) for the staggered title reveal."""
    t = spec["title"]
    off = spec.get("offsets") or {}
    ts = float(off.get("ts", 1.0) or 1.0)
    if not (t["text"] or "").split():
        return []
    accent_set = {w.lower() for w in (t.get("accent_words") or [])}
    shadow = t["shadow"]
    italic = t.get("italic")

    canvas, ink, g = _draw_title_canvas(t, W, ts)
    font, stroke_w = g["font"], g["stroke_w"]
    fill, accent, stroke_fill = g["fill"], g["accent"], g["stroke_fill"]
    pad_h, pad_v = _title_padding(t, W, stroke_w, g["size"])
    space = font.getlength(" ")

    ink_w = ink[2] - ink[0]
    lay_w = _title_layer_w(t, W, g["one_line_w"], ink_w, pad_h)
    text_x = (lay_w - ink_w) // 2          # centre the words in the pill box
    ox = tx + text_x - ink[0]
    oy = y_top + pad_v - ink[1]

    parts: list[dict] = []
    if t["style"] == "pill":
        lay_h = (ink[3] - ink[1]) + 2 * pad_v
        pill = Image.new("RGBA", (max(2, lay_w), max(2, lay_h)), (0, 0, 0, 0))
        dd = ImageDraw.Draw(pill)
        rad = min(int(lay_h * t["pill_radius_pct"]), lay_h // 2, lay_w // 2)
        dd.rounded_rectangle([0, 0, lay_w - 1, lay_h - 1], radius=rad,
                             fill=_hex(t["pill_fill"], t.get("pill_alpha", 235)))
        parts.append({"sprite": pill, "px": tx, "py": y_top})

    upper = bool(t.get("upper"))
    for (lx, ly), ln in zip(g["line_origins"], g["lines"]):
        cx = lx
        for w in ln:
            wtxt = w.upper() if upper else w
            col = accent if w.lower() in accent_set else fill
            bb = font.getbbox(wtxt, stroke_width=stroke_w)
            wimg, (wpad, _w) = _text_sprite(wtxt, font, col, stroke_w, stroke_fill, shadow, italic)
            parts.append({"sprite": wimg,
                          "px": int(cx + bb[0] + ox) - wpad,
                          "py": int(ly + bb[1] + oy) - wpad})
            cx += font.getlength(wtxt) + space
    return parts


def animation_layers(preset_key: str, overrides: Optional[dict], W: int, H: int,
                     mode: str, target_index: int = -1) -> list[dict]:
    """
    Build the animatable elements for a reveal. Each element is
    `{kind, index, px, py, sprite, parts, static}` plus optionally:
      - `idle`    {period, delay, amp, rot, scale, alpha} — perpetual float
      - `entrance` "card" — slide-up + fade instead of the reveal motion
      - `start`   explicit start override (secs, 1× timebase)
      - `hide_at` fade the element out at this time (secs, 1× timebase)
    Draw order == list order: icon, title, bonus(+sparkles), rows, widgets, card.
    """
    preset = TEMPLATES.get(preset_key) or TEMPLATES["valorant_clip_ranking"]
    spec = _merge(preset, overrides)
    new_item = (mode == "new_item")
    # The reveal never recolours rows — motion is the emphasis (see git history).
    spec["list"]["highlight"] = -1

    elements: list[dict] = []

    icon = _icon_sprite(spec, W, H)
    if icon:
        elements.append({"kind": "icon", "index": -1, "px": icon["px"], "py": icon["py"],
                         "sprite": icon["sprite"], "parts": [], "static": True})

    title_img, y_top = _title_block(spec, W, H)
    tx = _title_x(spec, W, title_img.width)
    elements.append({
        "kind": "title", "index": -1, "px": tx, "py": y_top,
        "sprite": title_img,
        "parts": _title_part_sprites(spec, W, H, tx, y_top),
        "static": new_item,
    })

    b = spec.get("bonus") or {}
    bonus = _bonus_sprite(spec, W)
    if bonus and b.get("show", "anim") != "omit":
        bx, by = _bonus_position(spec, W, bonus, title_img, tx, y_top)
        b_static = b.get("show") == "static"
        # Optional delayed entrance (intro only): the bonus stays hidden, then
        # pops in `delay` real-seconds after the clip starts, transitioning at its
        # own `speed`, with an optional one-shot SFX (see animate._render_frames /
        # bake_reveal, which read start_real / rev_speed / sound off this element).
        delayed = bool(b.get("delay_on")) and not b_static and not new_item
        delay = max(0.0, float(b.get("delay", 1.0) or 0.0)) if delayed else None
        hide_at = None
        if b.get("hide_on") and not b_static:
            hide_secs = max(0.5, float(b.get("hide_secs", 5.0) or 5.0))
            # when delayed, "visible for hide_secs" counts from the entrance
            hide_at = (delay + hide_secs) if delayed else hide_secs
        belem = {"kind": "bonus", "index": -1, "px": bx, "py": by,
                 "sprite": bonus["sprite"], "parts": [], "static": b_static,
                 "start": 0.22 * 0.8, "motion": "pop"}
        if delayed:
            belem["start_real"] = delay
            belem["rev_speed"] = max(0.25, min(float(b.get("speed", 1.0) or 1.0), 4.0))
            belem["sound"] = b.get("sound") or "none"
            belem["volume"] = max(0.0, min(float(b.get("volume", 1.0) or 1.0), 2.0))
        if hide_at is not None:
            belem["hide_at"] = hide_at
        elements.append(belem)
        for s in bonus["sparkles"]:
            sp = {"kind": "sparkle", "index": -1, "px": bx + s["dx"], "py": by + s["dy"],
                  "sprite": s["img"], "parts": [], "static": b_static,
                  "start": belem["start"], "motion": "pop",
                  "idle": {"period": 2.2, "delay": s["delay"], "amp": 7,
                           "rot": 10, "scale": 0.3, "alpha": (0.75, 1.0)}}
            if delayed:
                sp["start_real"] = delay
                sp["rev_speed"] = belem["rev_speed"]
            if hide_at is not None:
                sp["hide_at"] = hide_at
            elements.append(sp)

    sub = _subtitle_sprite(spec, W)
    if sub:
        sx, sy = _subtitle_position(spec, W, sub, title_img, tx, y_top)
        # Pops in with the title (a hair behind it), inheriting the reveal motion.
        elements.append({"kind": "subtitle", "index": -1, "px": sx, "py": sy,
                         "sprite": sub["sprite"], "parts": [], "static": new_item,
                         "start": 0.10})

    widgets: list[dict] = []
    for r in _iter_rows(spec, W, H):
        if new_item and r["index"] == target_index:
            num = r["parts"][0]
            elements.append({
                "kind": "item", "index": r["index"], "px": num["px"], "py": num["py"],
                "sprite": num["sprite"], "parts": [], "static": True,
            })
            lab = next((p for p in r["parts"] if p["kind"] == "label"), None)
            if lab is not None:
                elements.append({
                    "kind": "item", "index": r["index"], "px": lab["px"], "py": lab["py"],
                    "sprite": lab["sprite"], "parts": [lab], "static": False,
                })
        else:
            elements.append({
                "kind": "item", "index": r["index"], "px": r["px"], "py": r["py"],
                "sprite": r["sprite"], "parts": r["parts"],
                "static": (new_item and r["index"] != target_index),
            })
        if r["widget"]:
            w = r["widget"]
            widgets.append({
                "kind": "widget", "index": r["index"], "px": w["px"], "py": w["py"],
                "sprite": w["sprite"], "parts": [],
                "static": (new_item and r["index"] != target_index),
                "idle": {"period": 2.6, "delay": r["index"] * 0.25, "amp": 8,
                         "rot": 2.5, "scale": 0.0, "alpha": None},
            })
    elements.extend(widgets)

    card = _card_sprite(spec, W, H)
    if card:
        start = 0.15 if new_item else (0.22 + max(0, target_index) * 0.14)
        elements.append({"kind": "card", "index": -1, "px": card["px"], "py": card["py"],
                         "sprite": card["sprite"], "parts": [], "static": False,
                         "start": start, "entrance": "card"})
    return elements
