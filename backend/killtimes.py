"""Seed Kill Timers — detect kill moments in a VALORANT clip + name who killed.

Experimental (editor 🧪 section). ONE detection signal, aimed by the user:

**Killfeed motion + the ROW TEST** inside a region the user drags onto the
source stage (the orange "killfeed" box; defaults to the ranked top-right
corner when unset). Every kill lands a feed row → a motion spike inside the
box — but the game world MOVES BEHIND the feed slot, so raw motion also fires
on every camera pan (183 "kills" on a broadcast VOD). The row test separates
them: a kill row is UI — its pixels CHANGE then FREEZE while the row holds
(~3-5s); world motion changes and KEEPS changing. A peak only survives if
enough pixels changed across the peak AND then held still (this also rejects
row fade-outs: those pixels change and then keep moving as world again).
There is deliberately NO bottom-center kill-banner signal: in ranked clips it
was half muzzle-flash noise (red gun skins), and broadcast layouts put a
player cam there — the user seeds manually when a clip has no killfeed.
The row's kill-red color mass is an ASSIST (boosts confidence), not a gate —
broadcast feed rows are team-colored (teal/red), not VALORANT red.

**Name OCR** (best-effort, aibrain / local `claude` CLI): a crop of the box is
saved per event and ONE vision call reads the newest row of each →
killer/victim names + whether the row carries the POV highlight box. Names are
fuzzy-matched against the highlight player's username (e.g. "Aspas" out of
"Aspas 5k.mp4") to mark each kill `mine` — the UI highlights those and lets
the user edit/fill names the OCR left blank. Unreadable events are KEPT
(blank names), never dropped — the user fills them in.

Two passes for timing precision: a coarse ~10 fps scan builds the motion
curve, then each candidate onset is re-decoded at FULL fps in a small window
and snapped to the frame where the row actually lands (validated against the
user's hand-placed money markers on rWkSo-i746Y: marker == row pop).

detect() returns {"times": [{t, conf, feed, killer, victim, mine}]} with t in
SOURCE seconds — the same clock B-roll effect markers use. A result-level
`warning` is set when the event rate suggests the box is misplaced.
"""

from __future__ import annotations

import difflib
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from . import aibrain, config

# Default killfeed region (fractions of the frame) when the user hasn't placed
# the box: the ranked/POV top-right corner. Broadcast layouts need the box.
DEFAULT_FEED_ROI = (0.62, 0.02, 1.00, 0.30)

SAMPLE_FPS = 10.0        # coarse-pass analysis rate
BASELINE_WIN_S = 12.0    # rolling quiet-floor window
MIN_GAP_S = 0.35         # two kills can't be closer than this
REFINE_PRE_S = 0.8       # refinement window around a coarse onset
REFINE_POST_S = 0.4
CROP_DELAY_S = 0.25      # snapshot the feed slightly after onset (row slid in)
OCR_BATCH = 12           # crops per vision call
OCR_WORKERS = 4          # concurrent vision calls (mirrors search2's fan-out)
WARN_EVENTS_PER_MIN = 20 # above this the box is probably misplaced


def _red_fraction(bgr, holes=()):
    """Fraction of pixels that read as VALORANT kill-red (crimson, saturated).
    Confidence ASSIST only — team-colored broadcast rows won't show it."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    mask = ((h <= 6) | (h >= 172)) & (s > 130) & (v > 110)
    total = mask.size
    if holes:
        keep = np.ones(mask.shape, dtype=bool)
        for (x0, y0, x1, y1) in holes:
            keep[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = False
        mask = mask & keep
        total = max(1, int(keep.sum()))
    return float(mask.sum()) / total


def _crop(frame, roi):
    x0, y0, x1, y1 = roi
    return frame[y0:y1, x0:x1]


def _feed_roi_px(feed, W, H):
    """The scan region: the user's box (source px, clamped) or the default."""
    if feed and all(k in feed for k in ("x", "y", "w", "h")):
        x0 = max(0, min(int(feed["x"]), W - 2))
        y0 = max(0, min(int(feed["y"]), H - 2))
        x1 = max(x0 + 2, min(x0 + int(feed["w"]), W))
        y1 = max(y0 + 2, min(y0 + int(feed["h"]), H))
        return (x0, y0, x1, y1)
    fx0, fy0, fx1, fy1 = DEFAULT_FEED_ROI
    return (int(fx0 * W), int(fy0 * H), int(fx1 * W), int(fy1 * H))


def _holes_for(roi, exclude):
    """Map excluded source-px rects (facecam/mousecam) into ROI-local coords."""
    x0, y0, x1, y1 = roi
    out = []
    for r in exclude or []:
        rx0, ry0 = int(r["x"]), int(r["y"])
        rx1, ry1 = rx0 + int(r["w"]), ry0 + int(r["h"])
        ix0, iy0 = max(rx0, x0), max(ry0, y0)
        ix1, iy1 = min(rx1, x1), min(ry1, y1)
        if ix1 > ix0 and iy1 > iy0:
            out.append((ix0 - x0, iy0 - y0, ix1 - x0, iy1 - y0))
    return out


def _rolling_floor(sig, win, pctl):
    """Per-sample quiet floor: a low percentile over a trailing window."""
    n = len(sig)
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo = max(0, i - win)
        out[i] = np.percentile(sig[lo:i + 1], pctl)
    return out


def _local_peaks(dev, thr, min_gap):
    """Indices of local maxima of `dev` above `thr`, separated by min_gap."""
    idx = []
    for i in range(1, len(dev) - 1):
        if dev[i] >= thr and dev[i] >= dev[i - 1] and dev[i] >= dev[i + 1]:
            if not idx or i - idx[-1] >= min_gap:
                idx.append(i)
            elif dev[i] > dev[idx[-1]]:
                idx[-1] = i
    return idx


# --------------------------------------------------------------------------- #
# Killfeed name OCR (best-effort, local claude CLI via aibrain)
# --------------------------------------------------------------------------- #
_OCR_SCHEMA = {
    "type": "object",
    "properties": {
        "reads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer", "description": "image index from the listing"},
                    "has_row": {"type": "boolean",
                                "description": "a kill row/banner IS present in this crop (even if the names are unreadable)"},
                    "killer": {"type": "string", "description": "newest (TOP) row's killer name, exact text; '' if unreadable"},
                    "victim": {"type": "string", "description": "newest (TOP) row's victim name; '' if unreadable"},
                    "pov_highlight": {"type": "boolean",
                                      "description": "the newest row carries the POV player's highlight box/outline"},
                },
                "required": ["i", "has_row", "killer", "victim", "pov_highlight"],
            },
        },
    },
    "required": ["reads"],
}

_OCR_SYS = (
    "You verify and read VALORANT killfeed crops for a Shorts editor. Each image "
    "is a crop of a suspected killfeed region at a moment a kill may have "
    "happened — either the ranked stacked feed (newest row on TOP) or a "
    "broadcast/esports kill banner (killer left → weapon icon → victim right, "
    "team-colored plates). FIRST decide has_row: is an actual kill row/banner "
    "visible in the crop? Game world, walls, cams, ads, scoreboards = has_row "
    "false. If a row IS present, report the newest/top row with the EXACT "
    "displayed text (keep team tags like 'SEN'/'LEV'; streamer mode may show "
    "agent names or 'Me' instead of real names — report what is written, do not "
    "translate). Unreadable names on a real row → has_row true, empty strings."
)


def _read_killfeed(crops: list[tuple[int, str]], player_hint: str,
                   progress=None) -> dict[int, dict]:
    """Vision-verify + read ALL saved feed crops (batched, a few concurrent
    calls — same fan-out pattern as search2's vision stage) →
    {event_i: {has_row, killer, victim, pov_highlight}}. Returns {} if the CLI
    is unavailable or every batch fails."""
    if not crops or not aibrain.available():
        return {}
    hint = (f' The highlight likely features player "{player_hint}" — watch for '
            f"spelling variants of that name." if player_hint else "")
    dirs = sorted({str(config.TMP_DIR / "_killtimes")})

    def run_batch(batch):
        listing = "\n".join(f"- image {i}: {p}" for i, p in batch)
        res = aibrain._run(
            f"Verify and read these {len(batch)} suspected VALORANT killfeed "
            f"crops:{hint}\n{listing}",
            schema=_OCR_SCHEMA, system=_OCR_SYS, allow_read=True,
            add_dirs=dirs, timeout=150,
        )
        return res.get("reads") if isinstance(res, dict) else None

    batches = [crops[i:i + OCR_BATCH] for i in range(0, len(crops), OCR_BATCH)]
    out: dict[int, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
        for reads in ex.map(run_batch, batches):
            done += 1
            if progress:
                progress(84 + int(done / len(batches) * 15),
                         f"reading killfeed names ({done}/{len(batches)})")
            for r in reads or []:
                try:
                    i = int(r["i"])
                except Exception:
                    continue
                out[i] = {"has_row": bool(r.get("has_row")),
                          "killer": str(r.get("killer") or "").strip(),
                          "victim": str(r.get("victim") or "").strip(),
                          "pov_highlight": bool(r.get("pov_highlight"))}
    return out


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _names_match(a: str, b: str) -> bool:
    """Fuzzy handle match: team tags, casing, l33t variance and OCR typos
    tolerated. Also compares per-token so a team tag can't drag a whole-string
    ratio under threshold ("MIBR asas" vs "Aspas" → token "asas" ≈ 0.89)."""
    def close(x, y):
        if not x or not y:
            return False
        if x in y or y in x:
            return True
        return difflib.SequenceMatcher(None, x, y).ratio() >= 0.72
    na, nb = _norm_name(a), _norm_name(b)
    if close(na, nb):
        return True
    toks = [_norm_name(t) for t in re.split(r"\s+", a or "") if len(t) >= 3]
    return any(close(t, nb) for t in toks)


def detect(path: str, feed: dict | None = None, exclude: list[dict] | None = None,
           progress=None, player: str = "") -> dict:
    """Scan a clip for kill moments via killfeed motion inside `feed` (the
    user-placed box, source px) or the default top-right ROI. Blocking; run on
    a job thread."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not np.isfinite(fps) or fps <= 1:
        fps = 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = n_frames / fps if n_frames else 0.0

    feed_roi = _feed_roi_px(feed, W, H)
    feed_holes = _holes_for(feed_roi, exclude)

    step = max(1, int(round(fps / SAMPLE_FPS)))

    # ---- pass 1: coarse scan — feed motion + red-mass assist curves -------- #
    # Sampled gray crops are KEPT (~30KB each) so the row test below can
    # compare before/after content without re-decoding.
    f_diff, f_red, times, grays = [0.0], [], [], []
    prev_gray = None
    fi = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if fi % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            times.append(fi / fps)
            crop = _crop(frame, feed_roi)
            f_red.append(_red_fraction(crop, feed_holes))
            gw = 320
            gh = max(2, int(gw * crop.shape[0] / max(1, crop.shape[1])))
            g = cv2.cvtColor(cv2.resize(crop, (gw, gh)), cv2.COLOR_BGR2GRAY)
            if feed_holes:                       # blank the cam holes for the diff too
                sx, sy = gw / max(1, crop.shape[1]), gh / max(1, crop.shape[0])
                for (hx0, hy0, hx1, hy1) in feed_holes:
                    g[int(hy0 * sy):int(hy1 * sy), int(hx0 * sx):int(hx1 * sx)] = 0
            if prev_gray is not None and prev_gray.shape == g.shape:
                f_diff.append(float(np.mean(cv2.absdiff(g, prev_gray))) / 255.0)
            elif len(times) > 1:
                f_diff.append(0.0)
            grays.append(g)
            prev_gray = g
            if progress and n_frames:
                progress(int(fi / n_frames * 68), "scanning killfeed")
        fi += 1
    cap.release()
    if len(times) < 5:
        return {"times": [], "duration": duration, "fps": fps, "note": "clip too short"}

    f_red = np.asarray(f_red)
    f_diff = np.asarray(f_diff[:len(f_red)])
    if len(f_diff) < len(f_red):
        f_diff = np.pad(f_diff, (0, len(f_red) - len(f_diff)))
    times = np.asarray(times[:len(f_red)])
    win = int(BASELINE_WIN_S * SAMPLE_FPS)

    # deviation above the rolling quiet floor; robust k·MAD threshold w/ floor
    d_dev = np.maximum(0.0, f_diff - _rolling_floor(f_diff, win, 50))
    mad = float(np.median(np.abs(d_dev - np.median(d_dev)))) or 0.0
    d_thr = max(5.0 * mad, 0.020)
    r_dev = np.maximum(0.0, f_red - _rolling_floor(f_red, win, 30))

    min_gap = max(1, int(MIN_GAP_S * SAMPLE_FPS))
    peaks = _local_peaks(d_dev, d_thr, min_gap)

    # ---- ROW TEST: keep only "changed-then-frozen" peaks ------------------- #
    # A kill row is UI: its pixels change across the peak, then HOLD while the
    # row persists. World motion behind the feed slot (camera pans) changes and
    # keeps changing; row fade-outs change back to world and keep changing too.
    # Count pixels that changed (before → just-after) AND froze (just-after →
    # later); enough of them = a real row landing. Windows kept short (0.3s /
    # 0.7s) so a follow-up streak kill shifting the stack doesn't erase this one.
    events = []
    n = len(grays)
    for p in peaks:
        ia = max(0, p - int(0.5 * SAMPLE_FPS))
        ib = min(n - 1, p + int(round(0.3 * SAMPLE_FPS)))
        ic = min(n - 1, p + int(round(0.7 * SAMPLE_FPS)))
        fa, fb, fc = grays[ia], grays[ib], grays[ic]
        if fa.shape != fb.shape or fb.shape != fc.shape:
            continue
        changed = cv2.absdiff(fb, fa) > 25
        frozen = cv2.absdiff(fc, fb) < 12
        row_px = int(np.count_nonzero(changed & frozen))
        min_area = max(60, int(fa.size * 0.003))
        if row_px >= min_area:
            events.append(p)
    del grays

    # ---- pass 2: snap onsets to the row-landing frame at FULL fps ---------- #
    crop_dir = config.TMP_DIR / "_killtimes" / uuid.uuid4().hex[:8]
    crop_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    refined = []
    for k, si in enumerate(events):
        t0 = float(times[si])
        f_lo = max(0, int((t0 - REFINE_PRE_S) * fps))
        f_hi = int((t0 + REFINE_POST_S) * fps)
        if n_frames:
            f_hi = min(n_frames - 1, f_hi)
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_lo)
        # change-vs-window-start curve: rises as the row lands. Color-agnostic,
        # unlike red-mass — broadcast rows are team-colored.
        base_gray, curve = None, []
        for f in range(f_lo, f_hi + 1):
            ok, frame = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(_crop(frame, feed_roi), cv2.COLOR_BGR2GRAY)
            if base_gray is None:
                base_gray = g
            curve.append(float(np.mean(cv2.absdiff(g, base_gray))) / 255.0)
        t = t0
        if len(curve) >= 3:
            c = np.asarray(curve)
            base, peak = float(c.min()), float(c.max())
            if peak - base >= d_thr * 0.5:            # window contains a real rise
                half = base + 0.5 * (peak - base)
                t = (f_lo + int(np.argmax(c >= half))) / fps
        # killfeed snapshot just after the row lands (for the name OCR)
        cf = int((t + CROP_DELAY_S) * fps)
        if n_frames:
            cf = min(n_frames - 1, cf)
        cap.set(cv2.CAP_PROP_POS_FRAMES, cf)
        ok, frame = cap.read()
        crop_path = ""
        if ok:
            crop_path = str(crop_dir / f"evt_{k}.jpg")
            cv2.imwrite(crop_path, _crop(frame, feed_roi),
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
        # conf: threshold margin + kill-red assist (ranked rows / own-kill box)
        margin = float(d_dev[si] / max(d_thr, 1e-6))
        conf = 0.6 + 0.1 * max(0.0, min(margin - 1.0, 2.0))
        if r_dev[si] > 0.004:
            conf += 0.1
        refined.append({
            "t": round(max(0.0, t), 2),
            "conf": round(min(0.95, conf), 2),
            "feed": True,
            "_crop": crop_path,
        })
        if progress:
            progress(68 + int((k + 1) / max(1, len(events)) * 14), "refining onsets")
    cap.release()

    refined.sort(key=lambda e: e["t"])
    out = []
    for e in refined:                     # final min-gap sweep in real seconds
        if out and e["t"] - out[-1]["t"] < MIN_GAP_S:
            if e["conf"] > out[-1]["conf"]:
                out[-1] = e
        else:
            out.append(e)

    # ---- vision verify + name OCR over ALL candidate crops ----------------- #
    # The vision pass is the real precision gate: pixel heuristics cannot cope
    # with edited compilations that zoom/cut the footage (a "Top 4" comp's
    # punch-ins made every fixed-region signal fire). Verdicts:
    #   has_row=false → verified NON-kill (world/cam/ad in the crop) → dropped.
    #   has_row=true, names read → listed + named.
    #   has_row=true, unreadable → listed with BLANK names (user fills them in).
    #   no verdict (CLI missing/failed) → listed low-conf, flagged via `ocr`.
    if progress:
        progress(84, "reading killfeed names")
    crops = [(i, e["_crop"]) for i, e in enumerate(out) if e["_crop"]]
    reads = _read_killfeed(crops, player, progress=progress)
    ocr_ok = bool(reads)
    kept = []
    for i, e in enumerate(out):
        r = reads.get(i)
        if r is not None and not r["has_row"]:
            continue                          # vision-verified ghost
        r = r or {}
        e["killer"] = r.get("killer", "")
        e["victim"] = r.get("victim", "")
        e["verified"] = bool(r.get("has_row"))
        # An event whose read duplicates an earlier event's row within ~3s is
        # the SAME row re-detected (highlight/shuffle animation) — keep the
        # first sighting. Window kept TIGHT: streamer mode can make DISTINCT
        # streak kills read identically ("Me → Reyna" twice in an ace).
        if (e["killer"]
                and any(k["killer"] == e["killer"] and k["victim"] == e["victim"]
                        and e["t"] - k["t"] < 3.0 for k in kept)):
            continue
        # `mine` = the highlight player's kill: the row's POV highlight box or
        # an OCR name match (the UI re-evaluates on edit, `pov` as a floor).
        e["pov"] = bool(r.get("pov_highlight"))
        e["mine"] = e["pov"] or (bool(player) and _names_match(e["killer"], player))
        if e["verified"]:
            e["conf"] = round(min(0.99, e["conf"] + 0.1), 2)
        if e["mine"]:
            e["conf"] = round(min(0.99, e["conf"] + 0.05), 2)
        e.pop("_crop", None)
        kept.append(e)
    out = kept
    shutil.rmtree(crop_dir, ignore_errors=True)

    res = {"times": out, "duration": round(duration, 2), "fps": round(fps, 2),
           "analyzed_fps": round(fps / step, 2), "frame_size": [W, H],
           "player": player, "ocr": ocr_ok,
           "feed_roi": list(feed_roi), "feed_box": bool(feed)}
    if duration > 5 and len(out) / (duration / 60.0) > WARN_EVENTS_PER_MIN:
        res["warning"] = ("very high kill rate — the killfeed box is probably "
                          "misplaced (drag it tight around the feed rows)")
    return res
