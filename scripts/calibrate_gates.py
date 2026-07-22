"""
Calibrate clipgate thresholds against the user's shipped keeper clips.

The keepers (raw single-play clips that made it into a project) define "good".
No keeper may trip a gate. Run this after changing thresholds; it prints the
per-clip metrics + a PASS/FAIL and the keeper-set min/max so thresholds can be
set just outside the keeper range.

    .venv/bin/python scripts/calibrate_gates.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend import clipgate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "storage" / "clips"

# Folders whose clips were confirmed used (project ∩ downloaded package) = "good".
KEEPER_DIRS = ["tenz-clips", "top-4-tenz-clips", "top-4-tarik-clips",
               "top-4-1v5s-clips", "top-5-flicks-clips", "top-4-spectre-clips",
               "top-4-shanks", "sherrif_content"]

# A few non-single-play files for contrast (tutorials / talking-head / drama).
CONTRAST = [
    "default/How to Actually Edit Shorts That Go Viral Every Time [s8KnpQxrcRo].mp4",
    "default/EWC_Drama_-_Zekken_Not_Happy_Cloud9_Roster_Chaos [mF5VjzEna4Y].mp4",
    "default/The_First_Pro_Game_On_Summit_Was_NOT_GREAT [1CS8VEhTRUI].mp4",
]


def rows(paths):
    out = []
    for p in paths:
        m = clipgate.analyze_media(str(p))
        out.append((p, m))
    return out


def show(label, data):
    print(f"\n=== {label} ===")
    print(f"{'clip':42} {'dur':>6} {'cuts':>5} {'cps':>6} {'sil%':>6} {'crest':>6}  verdict")
    for p, m in data:
        name = p.name[:40]
        if not m.get("analyzed"):
            print(f"{name:42} {'--- not analyzed ---'}")
            continue
        g = clipgate.gate(m)
        verdict = f"GATED:{g}" if g else "pass"
        print(f"{name:42} {m.get('dur',0):6.1f} {m.get('cut_count','?'):>5} "
              f"{m.get('cuts_per_sec',0):6.3f} {m.get('silence_ratio',0)*100:6.1f} "
              f"{str(m.get('crest_db','?')):>6}  {verdict}")


def stats(data, key):
    vals = [m[key] for _, m in data if m.get("analyzed") and m.get(key) is not None]
    return (min(vals), max(vals)) if vals else (None, None)


keeper_paths = []
for d in KEEPER_DIRS:
    keeper_paths += sorted((ROOT / d).glob("*.mp4"))
keepers = rows(keeper_paths)
contrast = rows([ROOT / c for c in CONTRAST if (ROOT / c).exists()])

show("KEEPERS (must all pass)", keepers)
show("CONTRAST (non-single-play)", contrast)

print("\n=== keeper-set ranges (set thresholds just OUTSIDE these) ===")
for k in ("cuts_per_sec", "cut_count", "silence_ratio", "crest_db"):
    lo, hi = stats(keepers, k)
    print(f"  {k:16}: min={lo}  max={hi}")

gated = [p.name for p, m in keepers if clipgate.gate(m)]
print(f"\nKEEPERS GATED (must be EMPTY): {gated or 'none ✓'}")
