# VALORANT Search — Redesign Brief (Gate → Rank)

> Planning doc only. No code changed yet — this is the build spec for the
> Experimental searcher (`backend/search2.py` + friends). Handoff-ready.

## The one root cause

The searcher **averages** intrinsic quality signals (duration, keyword count, one
static frame) into a single score (`clips._score`, `search2._prescore`). So a fatal
flaw — wrong player, music bed, montage — gets *outweighed* by strengths instead of
being disqualifying. A great ace by the wrong player still wins.

**Fix: stop averaging. Gate, then rank.**

```
gather → GATE (binary, fatal, hard-drop) → RANK (among survivors only) → vision → finalize
```

Gates are relational ("is this good *as source for a VALDaily short about X*?"), which
is exactly what intrinsic frame/keyword scoring can't measure.

## What "good" means (derived from shipped clips, not assumed)

Read out of the clips that appear in both a saved project AND a downloaded package
(the ones actually cut into shorts):

A **raw, single-play clip** — usually a **Twitch clip** of the **specific named
player** — with **real game audio** + a **facecam**, showing one discrete highlight
(ace / clutch / flick / 1vX).

- Keeper sources: mostly `twitch.tv/tenz`, `twitch.tv/tarik`, `twitch.tv/shanks_ttv`,
  ScreaM / aspas / s0m (tagged in `.clipmeta.json`).
- Keeper titles: single-play language every time ("ez ace", "MAD TENZ ACE",
  "HOSPITAL FLICK", "1v5 Reverse ACE"). **Never** "highlights / montage / edit".
- Keeper views: **modest, 4k–100k** → **view count MISLEADS** (over-produced montage
  channels have far more views).
- The one junk package — "top 10 odin clips" — returned freestyles / ASMR / warships
  (off-subject drift, no gate).

On-brand for VALDaily (user-confirmed): **pro/VCT AND streamer/ranked both count**;
**facecam required**; failed gates **hard-drop** (not sink).

## Solutions (priority order)

1. **Montage gate** — `ffmpeg -vf "select='gt(scene,0.4)',metadata=print" -f null -`
   on the low-res clip `framegrab` **already downloads**. Cuts/sec over threshold →
   hard-drop. (The signal 3 middle frames can never carry — montage-ness is in the
   time domain.)

2. **Music-bed gate** — ATTEMPTED, then CUT after calibration. `silencedetect` +
   `astats` (silence-ratio + crest factor) do NOT separate a music bed from raw
   Valorant audio: raw gameplay is also near-continuous (constant gunfire) and also
   compressed by Twitch's pipeline (crest 4–8 dB). The heuristic false-dropped 4 of
   the shipped keepers (`GOD MODE`, tarik `HOSPITAL FLICK`, a 1v5, shanks 4k), so it
   is NOT a hard gate. Audio metrics are still computed (informational). Real
   single-clip music detection needs beat/tempo analysis (a new dep — `librosa`/
   `aubio`) = a deferred decision. In practice music arrives WITH a montage, which
   the validated cut-density gate (#1) already catches. **Calibrated thresholds:
   montage = cuts/sec ≥ 0.35 AND ≥ 6 cuts (keeper ceiling was 0.25).**
   See `scripts/calibrate_gates.py` (re-run after any threshold change; no keeper may gate).

3. **Subject gate** — if a player is named, require evidence (title / channel /
   Twitch-broadcaster / vision HUD read) → hard-drop wrong-subject. Pro AND ranked both
   pass (gate is "is it this person," not "is it a tournament"). Fixes "aspas → JAWGEMO".

4. **Facecam gate** — required. Twitch auto-passes (cam always present); YouTube must
   show facecam in the vision read. Only drop if vision is confident (avoid
   false-drops on a bad frame grab).

5. **Lexicon + view-count rebalance** (`clips.py` `_POSITIVE_KW` / `_NEGATIVE_KW` /
   `_popularity`) — move `highlights / montage / edit / best of / top 10 / compilation`
   to **strong negatives**; keep single-play words positive; cut popularity weight to
   ~0 (or apply only *within* Twitch clips). Better: parser appends single-play terms
   and negative-filters montage terms into the actual YouTube query so montages never
   enter the pool.

6. **Boost raw-Twitch-of-subject** to the top of survivors — keepers are mostly Twitch;
   the searcher currently gives Twitch only +0.05.

7. **Dead code: `aibrain.curate` is never called** — either wire it as a cheap Haiku
   **title-gate** ("subject present? single-play or montage? keep/reject") *before*
   spending vision, or delete it. Today it's the "AI reads titles" story that doesn't run.

8. **Sample the hook** — `framegrab.sample_frames` only pulls the middle 80%; add a
   frame at t≈0 (retention read + montage intro / subscribe-splash detection).

## Phase split

- **Phase 1** (~a day, deterministic, no new deps — ffmpeg already present):
  **#1 montage gate, #2 music gate, #3 subject gate, #5 lexicon + view rebalance.**
  Hits all three named complaints (music/montage, wrong-subject, highlights-vs-ace).
- **Phase 2:** **#4 facecam gate, #6 Twitch-subject boost, #7 title-gate, #8 hook
  frame**, plus a feedback loop that learns gate thresholds from shipped clips (the way
  `packages.py` learns from critiques).

## Verify (per CLAUDE.md working agreement)

Exercise the real path, not just imports. Run live `aspas ace` and `TenZ highlights`
before/after; dump the result lists into `outputs/` and confirm the montages /
wrong-player clips fall out. **Restart the server (or `./run.sh --reload`) before
testing** — a server started earlier keeps running the old engine (the stale-engine trap).

## Parity note

Any capability added to the Search-tab searcher MUST also work in the Formatter's
per-rank finder, calling the SAME function (shared `xEl`/`ctx` engine, `resultRowEl`),
not a forked copy. Gates live in `search2` / `clips`, so both surfaces inherit them —
keep it that way.
