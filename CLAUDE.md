# CLAUDE.md

Guidance for working in this repo. Read this before making changes.

## What this is

A **local** content-creation tool for making **YouTube Shorts** (9:16, 1080×1920)
out of landscape gameplay/stream clips — primarily Valorant. A FastAPI backend serves
a static (no-build) frontend and drives `ffmpeg`/`yt-dlp`/OpenCV. Everything runs on
`127.0.0.1:8000` — there is no auth, no database, no cloud, no telemetry. It is a
single-user local app; keep it that way unless asked.

Two tools sit on top of one engine:

1. **Auto-Layout** (single clip) — reflow one landscape clip into a 9:16 vertical
   (facecam on top, gameplay below), with trim, mid-clip cuts, speed, swipe/SFX
   transitions, and an optional template overlay.
2. **Video Formatter** (many clips) — a ranking builder: drop a different clip into
   each rank, edit each with the full Auto-Layout toolset, and assemble them into one
   "Top N" short with a progressive-reveal template and per-join transitions.

## How to work here (working agreement)

- **Interview first on big features.** For anything substantial (a new tool, a new
  subsystem, anything with real product decisions), ask a short set of scoping
  questions *before* building. Just build the small/mechanical stuff.
- **Always drop a review render.** Any change that affects rendered output → produce
  a short sample in `outputs/` and point the user at it (see "Review outputs").
- **Quality bar is "looks like CapCut".** Motion should be eased and smooth, text
  crisp with proper stroke/shadow. Don't ship linear/cheap-looking effects.
- **Don't regress the hard-won workarounds.** This ffmpeg build has **no `drawtext`**
  and **can't ease `xfade`**. Text is rendered as Pillow PNG overlays; swipes are
  eased `overlay` pushes. Do **not** "simplify" either back to `drawtext`/`xfade`
  slides — it will break or look bad. See the relevant sections below.
- **Verify by exercising the real path**, not just imports. Render into
  `storage/output/_test_*.mp4` (or drive the **running** API), inspect a frame, then clean up.
  A fresh `python -c "from backend import …"` always runs your *new* code, so it passes even
  when the live server is still on the *old* code — confirm the fix in the actual render/mp4,
  and restart the server first (see the `--reload` warning below).

## Run / dev

```bash
./run.sh                 # uvicorn backend.main:app on 127.0.0.1:8000
./run.sh --reload        # dev: auto-reload on edits — USE THIS while changing backend code
```

> ⚠️ **`run.sh` does NOT auto-reload.** A server started earlier keeps running your **old**
> backend code after you edit `autolayout.py` / `main.py`, so backend changes don't reach the
> render path (compose / `_build_filter` / the mp4 build) until you **restart the server**.
> Nasty tell: the change *does* show up in a fresh `python -c` import and in `/openapi.json`
> (Pydantic request models are re-read on restart) but **not in the rendered output** — the
> stale engine functions still run. A new field flows end-to-end automatically via `_to_opts`;
> it does **not** need separate wiring in the mp4 renderer. After any backend edit: restart
> (or use `--reload`) and verify against the running server. (Bit us on `facecam_height`, 2026-07-08.)

- The Python venv lives in `.venv/`. Always use `.venv/bin/python`, `.venv/bin/uvicorn`,
  etc. — the system interpreter does **not** have the deps.
- Python deps: **fastapi, uvicorn, pydantic, opencv-python(-headless), numpy, yt-dlp,
  Pillow**. (Pillow powers template text; install with `.venv/bin/pip install Pillow`.)
- External binaries required on PATH: **`ffmpeg`** and **`ffprobe`**. ImageMagick
  (`magick`) is available too but currently unused (Pillow does the text work).
- No test suite. Verify changes by exercising the code directly, e.g.:
  ```bash
  .venv/bin/python -c "from backend import autolayout as a; ..."
  ```
  Render tests should write to `storage/output/_test_*.mp4` and clean up after.

## Review outputs

`outputs/` (git-ignored, `config.REVIEW_DIR`) is where Claude drops artifacts for
the user to eyeball — **do this by default for any visual/audio change**:

- `outputs/renders/` — short sample `.mp4` renders proving a feature works.
- `outputs/samples/` — PNG frames, template previews, overlay mock-ups.

When you change anything that affects the rendered output (layout, transitions,
templates, SFX), render a small sample into `outputs/` and mention it so the user
can review. Keep clips short (a few seconds) and delete scratch copies elsewhere
(`storage/clips`, `storage/output`, `storage/tmp`) so only the review artifacts remain.

## Architecture

```
backend/
  main.py         FastAPI routes + Pydantic request models.
                  Single-clip:  LayoutReq -> _to_opts -> LayoutOpts.
                  Formatter:     FormatterReq / FormatterPreviewReq.
                  Renders run in a background thread; poll GET /api/jobs/{id}. JOBS in-memory.
                  Serves: /api/fx, /api/templates, /api/formatter/{meta,preview,build}.
  autolayout.py   The engine. probe() (ffprobe), facecam detection (YuNet ONNX),
                  compose()/make_preview_frame() (build the ffmpeg filter_complex),
                  the eased-swipe push builder, SFX_CATALOG / TRANSITION_CATALOG /
                  JOIN_CATALOG, and join_segments() (stitch pre-rendered segments).
  formatter.py    The "Valorant Clip Ranking" overlay template — the ONLY template
                  (TEMPLATES has one key; STYLE_SPECS holds its 3 visual styles:
                  pill / minimal / editorial, ported 1:1 from the user's Claude
                  Design "Shorts Ranking Templates" project). Renders the static
                  RGBA overlay PNG (render()) and per-element sprites for animate
                  (animation_layers()): title + numbered list, per-rank player /
                  weapon widget PNGs, the "Now Playing" clip-info card (code-drawn
                  VD channel mark + optional per-clip streamer-reaction portrait /
                  @handle chip from STREAMERS), the "VALORANT" subtitle (+ optional
                  Streamers-React avatars), bonus call-outs (sticker/marker/tag/
                  spark; **above-title only**), tinted VCT icon backdrop.
                  Drag offsets {tx,ty,lx,ly,ts,ls} are design px at 1080×1920.
                  PNG handed to compose() via LayoutOpts.overlay_png.
  animate.py      Baked reveal animations: renders the overlay as a Pillow PNG
                  sequence (pop/slide/stagger reveals, card slide-in, bonus
                  auto-hide) and composites it onto segments (bake_reveal) or a
                  bg still (render_preview). Widget bob + sparkle float are
                  perpetual "idle" motion — when present the overlay animates for
                  the WHOLE segment; otherwise frames stop at settle and the last
                  frame is held. Two clocks: reveals scale with `speed`; idle /
                  hide / card run in real seconds (mirrors the design's CSS).
                  `bake_reveal(reveal_delay=…)` holds the whole reveal back (used by
                  the Clip Hook — nothing reveals until the cold-open shrinks away).
  cta.py          **Subscribe CTA** widget (ported from the design's "Subscribe CTA"
                  mock): a mouse cursor slides in, clicks a YouTube-red button, it
                  flips to "Subscribed" (bell) with a ripple ring + gold sparkles +
                  a one-shot SFX. `render_frames()` bakes a transparent PNG sequence
                  (Pillow, same reasoning as animate.py — no drawtext, no eased
                  xfade); `overlay()` stamps it onto a clip at a start second + mixes
                  the click SFX. Two looks: `classic` (monogram mark + pill) / `card`
                  (glass card w/ channel handle). Channel name / handle / accent come
                  from the user's settings (config.load_settings — the ⚙ Settings
                  screen); the monogram derives from the channel name (subscribe.
                  channel_initials), with a neutral play-triangle fallback when unset.
                  REPLACES the old static subscribe chip in BOTH surfaces: the editor
                  B-roll marker (autolayout EFFECTS_CATALOG `subscribe` →
                  `_apply_subscribe`) and the Formatter's last-X-seconds (main
                  `_run_formatter_build`). Reuses subscribe._vd_mark for the mark.
                  META surfaced at /api/fx (`subscribe`) + /api/formatter/meta.
  hook.py         **Clip Hook** cold-open (Formatter-only, ported from the design's
                  "Clip Hook" mock): opens on a chosen MOMENT from any clip playing in
                  a big framed box, then shrinks away to reveal the ranking. Frame
                  looks glow/shadow/border(spinning conic ring)/brackets + accent +
                  length + SFX. `render_frames()` extracts the clip moment (ffmpeg,
                  object-fit cover) and bakes each 1080×1920 frame in Pillow (box
                  scale-in → hold → shrink, per-style idle motion); `overlay()` stamps
                  it on the FRONT of the final video (no added length). The first
                  segment's reveal is delayed via bake_reveal(reveal_delay) so nothing
                  competes with the cold-open. main `_apply_hook` + `HookCfg`;
                  `hook_preview` renders only the cold-open + first reveal beat.
  killtimes.py    🧪 **Seed Kill Timers** (editor "Experimental" section): detects kill
                  moments in a VALORANT clip so B-roll markers can be seeded on them.
                  Candidates→verify pipeline (like search2): (1) killfeed MOTION inside
                  the user-aimed orange **killfeed box** (first ⌖ Detect press drops the
                  box on the stage, second press scans; default = ranked top-right),
                  coarse ~10fps + full-fps onset snap; (2) a cheap changed-then-frozen
                  "row test" (kill rows are UI: pixels change then HOLD; world motion
                  keeps changing); (3) **aibrain (claude CLI) vision over ALL candidate
                  crops** (batched 12/call, 4 concurrent) — per crop `has_row` verdict
                  + killer/victim read. has_row=false → dropped (that's the precision
                  gate: pixel heuristics CANNOT survive edited comps that zoom/cut the
                  footage — a "Top 4" comp false-fired 181 motion events, vision cut it
                  to the 42 real rows); row present but unreadable → kept with BLANK
                  names the user fills in (UI inputs re-evaluate gold on edit).
                  `mine` = POV-highlight verdict OR name fuzzy-match (per-token, so
                  team tags/OCR typos like "MIBR asas"≈"Aspas" still hit) vs the Player
                  field (auto-parsed from the clip name). There is deliberately NO
                  bottom-center kill-banner signal (muzzle-flash noise in ranked, a
                  player cam sits there in broadcasts) — no-killfeed clips are seeded
                  manually. POST /api/experimental/killtimes {clip, feed, exclude,
                  player} (background job). Times are SOURCE seconds (same clock as
                  B-roll effects). Ground-truthed against the user's hand-placed money
                  markers on rWkSo-i746Y (marker 59.4s == detected 59.37s), 2026-07.
  subscribe.py    Legacy static subscribe chip (render_chip) — kept only for its
                  `channel_initials` + `_vd_mark` channel-mark helpers, reused by
                  cta.py. The chip itself is no longer wired (cta.py's animated CTA
                  replaced it).
  clips.py        yt-dlp search / download (section-aware). Also resolve_stream():
                  resolves a directly-playable progressive URL so the UI can preview a
                  remote clip in-browser (POST /api/clips/stream — used by the result-
                  card ▶ preview + package candidate previews) without downloading it.
  youtube.py      Channel-overview stats for the overview tabs (Valorant = live data
                  for your connected channel). Pure YouTube Data API v3 +
                  YouTube Analytics API v2 — **NOT vidiq** (do not wire vidiq into
                  any app feature). Auth is a Desktop OAuth client at
                  secrets/client_secret.json with a cached, auto-refreshing token
                  at secrets/youtube_token.json (scopes youtube.readonly +
                  yt-analytics.readonly; the token carries a refresh_token so the
                  backend refreshes silently — no request-time sign-in). overview()
                  returns one bundle (channel snapshot + period deltas, a daily
                  views/watch-time/subs series for the chart, recent uploads, top
                  videos w/ likes+comments), cached ~10 min. CHANNELS maps niche→
                  channel ("mine" = the authorized owner channel).
  music.py        Background-music beds for the Formatter. CATALOG = 51 license-
                  checked tracks (phonk / hardstyle / anime-edit), each tagged tier
                  A (CC-BY, bundle-safe) / B (NCS, credit-required) / C (Pixabay,
                  use-in-output). grab() yt-dlp-downloads a track's audio into
                  storage/music/ (one-pass ytsearch + match_filter rejects the
                  1-hour-loop uploads; the two-step flat→download 403s). mix_bed()
                  lays the track UNDER a finished render (video stream-copied; amix
                  normalize=0; loop + song-start + fade). Licensing: local single-user
                  use only — music files stay gitignored, never committed (only tier A
                  CC-BY is ever redistributable). Credit line (tier B) returned in the
                  build job result + shown in the UI to paste into the description.
  packages.py     Clip-package agent. A **package** is a curated set of clip
                  CANDIDATES (title/source/duration/thumb/why-picked reason) the
                  agent assembles for a prompt (e.g. "TenZ clips"); you preview +
                  select from it and the picks download into the library as
                  Formatter ranks. Two engines, one on-disk format: build_heuristic()
                  = offline (no LLM) creator-aware clips.search across YouTube +
                  Twitch, ranked crop-ready-first; the base Claude sub-agent
                  (.claude/agents/clip-package-agent.md + scripts/package_agent.py)
                  = richer hand-curation. request_package() runs the heuristic on a
                  background thread so the app delivers packages itself (even
                  headless). **Learns over time**: every critique on a package is
                  appended to the package AND the agent's learnings (_agent.json,
                  seeded with a TenZ-centred prompt + default_count 20);
                  apply_learnings_to_terms() folds prefer/avoid hints into the
                  heuristic search, the agent reads the full list. Storage under
                  storage/packages/<id>/package.json (+ _agent.json), gitignored.
                  select_clips() downloads picks into a package-named folder (job).
  store.py        Clip storage: FOLDERS + retention. Clips live in folders under
                  storage/clips/ (default/, old/, or any you make); a clip's identity
                  is its relative path `folder/name.mp4`. One folder is the download
                  TARGET (.folders.json) where raw grabs land, and it is the ONLY folder
                  that auto-cleans (newest 20 non-kept, .keep.json pins) — every named
                  folder is permanent. resolve() maps an identity → real file with a
                  basename fallback (so pre-folder refs in saved projects still work).
                  Folder ops: create/rename/delete(→moves clips to default)/move_clip.
                  **Per-clip metadata** (favorite / #tags / creator) lives in a
                  sidecar `.clipmeta.json` (keyed by identity); get_meta/set_meta,
                  all_tags(), duplicate_clip() (on-disk copy, clones tags/creator,
                  un-favorited). Metadata migrates on move/rename/delete like keep.
                  **Creators**: `creator_roster()` = EVERY player in the picker
                  catalog + all formatter.STREAMERS + any custom names — so any
                  player/streamer is taggable as a clip's creator (returns
                  [{name, src}] with headshots, deduped case-insensitively).
                  `.creators.json` stores ONLY custom (non-roster) names the user
                  typed; add_creator() skips roster names so it never bloats.
  config.py       paths (STORAGE/CLIPS/OUTPUT/TMP/MUSIC, ASSETS, REVIEW_DIR), NICHES,
                  vidiq key, and per-user settings: load_settings()/save_settings()
                  read/write secrets/settings.json (channel name/handle/accent +
                  optional Anthropic key) — driven by the ⚙ Settings screen.
frontend/         index.html + app.js + style.css, served statically. No bundler, no framework.
assets/           sfx/ (SFX, served at /assets/sfx), fonts/ (OFL TTFs),
                  streamer_icons/ (streamer-reaction headshots; keys in
                  formatter.STREAMERS), valorant.png + twitch.png (subtitle mark /
                  handle-chip glyph),
                  plrs/ (225 player headshots), weapons/<type>/ (1216 skin PNGs),
                  orgs/ (48 team logos) — the FULL packs the live picker uses, each with
                  an index.json of metadata. minimal/ is a ~1/4-size subset for design
                  mock-ups ONLY (built by scripts/build_minimal_assets.py) — do NOT point
                  the app at it.
                  vct-icon.png (tintable logo), catalog.json (picker metadata the frontend
                  fetches — built from the FULL packs by scripts/build_catalog.py;
                  regenerate it after changing the packs).
storage/          clips/ (inputs, organized into FOLDERS — see store.py), output/ (renders),
                  music/ (bed downloads), tmp/ (preview JPEGs + scratch). Git-ignored.
outputs/          review artifacts Claude drops for you (see "Review outputs"). Git-ignored.
```

**Request flows:**
- Single clip: frontend `buildOpts()` → `POST /api/autolayout/{preview,render}` →
  `LayoutReq` → `_to_opts()` → `autolayout.LayoutOpts` → ffmpeg.
- Formatter: frontend `buildFmtPayload()` → `POST /api/formatter/build` →
  `_run_formatter_build` (job) → per-rank `compose()` (with a progressive overlay PNG)
  → `autolayout.join_segments()` → one final mp4.
- Remote-clip preview: result-card ▶ / package candidate → `previewRemoteClip()` →
  `POST /api/clips/stream` (`clips.resolve_stream` → progressive URL) → shared
  `openPlayer` modal (no download).

## The ffmpeg pipeline (autolayout.py)

The single most important thing to understand. `compose()` builds one `filter_complex` in
two conceptual stages:

1. **Spatial** — `_build_filter(info, opts, out_label=...)`. Splits the source, crops the
   facecam, covers/blur-fits panes, vstacks facecam-over-gameplay, optional blurred-edge
   backdrop, optional mousecam overlay. Ends in a single video pad (`[v]` or `[vspatial]`).
   `make_preview_frame()` reuses this to render one JPEG frame — so **keep this stage
   purely spatial**; anything time-based must not leak in here or previews break.

2. **Temporal** — `_temporal_filters(...)`, applied only when `_needs_temporal()` is true
   (speed ≠ 1, real cuts, or audio fade). Runs *after* spatial, on `[vspatial]`:
   - **Cuts** (remove middle spans): `select`/`aselect` on the keep-intervals, then
     `setpts=N/FRAME_RATE/TB` (video) and `asetpts=N/SR/TB` (audio) to collapse the gaps.
   - **Speed**: `setpts=PTS/speed` (video) + chained `atempo` factors (audio; atempo only
     accepts 0.5–2.0, so `_atempo_factors()` decomposes larger/smaller ratios).
   - **Audio fade-out**: `afade=t=out` positioned at `final_dur - fade_dur`, where
     `final_dur = (window - removed) / speed`.

A template overlay (`LayoutOpts.overlay_png`) is stamped **last**, after temporal, by
`_apply_overlay()` — so it shows in render + still preview and does not move during swipes.

**B-roll point effects** (`LayoutOpts.effects` = `[{"type","t","dur","sound"}]`, `t` in
source seconds; `dur`/`sound` are optional per-marker overrides of the catalog default
flash length + chosen SFX) are timeline "moments" the editor drops on the trim bar. Markers
are draggable along the scrubber and carry a by-hand seconds field, a flash-length slider
(range `dur_min..dur_max`), and a sound picker (`EFFECTS_CATALOG[*].sounds`; first = default). They run as a **second ffmpeg pass**
on the composed clip (`_apply_effects`), because the finished file's own PTS is the final
timeline — so effect times come straight from `_effect_final_time()` (maps a source marker
through trim + removed cuts + speed; a marker in a removed gap snaps to its boundary),
keeping the effect code decoupled from the temporal branch's clocks. Each effect eases a
tinted+blurred copy of the frame in/out (its **own** overlay pass — chaining every effect's
`fade` onto one shared copy fails, since a later `fade=in` zeroes earlier flashes) and mixes
a one-shot SFX so the punch lands on the marker. The catalog is `EFFECTS_CATALOG` (seeded:
`money` = green blur flash + `cash.wav`); it surfaces at `/api/fx` under `effects`. Add an
entry (+ optional sfx file) and it appears in the editor's B-roll tray automatically. Do
**not** ease the flash with `gblur`'s `enable` toggle (hard on/off) — use the eased-alpha
overlay so it reads CapCut-punchy, not cheap.

When temporal edits are active the audio is filtered and mapped as `[a]`; otherwise audio
is passed straight through with `-map 0:a?`. `_has_audio()` guards the audio graph so
sources without an audio stream don't error.

### Conventions / gotchas

- **Coordinates are always source pixels.** `Rect` (x,y,w,h) is in the original video's
  pixel space; the frontend maps display↔source via `scale()`. Clamp with `Rect.clamp(W,H)`.
- **Even dimensions**: H.264 needs even width/height — use `_even()` for any computed size.
- **Trim uses input seek**: `-ss` *before* `-i` (fast seek) resets the filtergraph clock to
  ~0. Therefore **cut spans, which arrive in source seconds, are converted to
  window-relative time** (`t - trim_start`) inside `_keep_intervals()`. Don't feed absolute
  source times into the `between(t,...)` expressions.
- Cut spans are merged + sorted and their complement (the "keep" intervals) is what actually
  drives `select`. Overlapping cuts are fine.
- Preview (`/api/autolayout/preview`) renders a still frame and is spatial-only — speed,
  cuts, and fade deliberately do **not** change it. The frontend reflects those in a
  "final length" text readout instead of re-rendering.

## Transitions & SFX (autolayout.py)

Transitions live at **cut joins** (where footage rejoins after a removed span),
so they only exist on the segmented temporal path (`_temporal_segmented`).

- **Swipe (visual)** = an eased directional *push*, built with `overlay` + an
  ease-out-cubic x-offset (`_push_chain`), **not** `xfade`. This ffmpeg build's `xfade`
  custom-expr can't translate pixels, so `xfade` slides look linear/cheap — the overlay
  push is the CapCut-style motion. Video uses a slice-and-concat model: each keep segment
  yields an optional `head` (a swipe's incoming), a `body` (plays alone), and an optional
  `tail` (a swipe's outgoing); output is `body0, push0, body1, …`. Audio keeps the simpler
  acrossfade/concat accumulator — its per-join D-second overlap lines up 1:1 with the video
  so A/V stay in sync. Legacy `slideleft/slideright` alias to `swipeleft/swiperight`.
- **Sound** = an SFX from `SFX_CATALOG` (whooshes/impacts/risers; the impacts + riser are
  synthesized with ffmpeg — no external files). Mixed on top at each join with a
  per-transition `volume`. Files live in `assets/sfx/`; a missing file falls back to a
  synthesized whoosh (`_whoosh_source`).
- Catalogs (`SFX_CATALOG`, `TRANSITION_CATALOG`) are surfaced at `GET /api/fx`; the
  frontend "FX studio" mini-tab previews sounds (plays the wav) and swipes (CSS demo) and
  sets the default transition applied to new cuts.

## Video Formatter — the ranking builder (separate tool)

The Formatter is its **own top-level tab**, not part of the single-clip editor.
Its job: assemble **many different clips into one ranking short** (a "Top N"),
one clip per rank.

- **Per rank**: a clip + a list label + a per-clip layout. The single-clip editor is
  *reused* via "Open editor" — `openSlotEditor` loads the slot's clip into the editor and
  `saveSlotFromEditor` saves `buildOpts()` back into the slot's `layout`. `AL.editingSlot`
  is the flag; while set, the editor shows "Save to rank" instead of "Render".
- **Progressive reveal**: each segment gets its own overlay. In play order (countdown
  `desc` N→1 or `asc`), the segment for rank r reveals labels (and widgets) of ranks played
  so far; the clip-info card shown is rank r's; the bonus call-out pops on the intro
  segment (later segments: omitted if auto-hide, else held static). No accent highlight —
  the reveal motion is the emphasis.
- **The editor** (left column, `AL.vce` state) is a port of the user's Claude Design
  "Shorts Ranking Templates" component: style switch (pill/minimal/editorial), reveal
  mode/motion, per-rank label + ◈ widget picker (`/plrs` · `/weapon` search over
  `/assets/catalog.json`) + ▦ clip-info (team A/B org logos + caption), clip-card shared
  style, bonus title, VCT icon backdrop, title/list position + size sliders, sound /
  volume / reveal speed. Rank labels are **shared state** with the rank slots
  (`AL.fmt.ranks[i].label`) — edits in either place sync the other.
- **Live preview** is an in-browser 1080×1920 canvas (`#vce-stage`, scaled to fit): DOM
  builders + rAF reveal (`vcePlay`) whose easing/timing constants mirror `animate.py`
  exactly. Title/list are draggable (move) with a corner handle (resize). "over the video
  clip" fetches the first clip's composed frame via `POST /api/formatter/preview` with
  `bare: true` (frame only — the canvas draws the template itself). 🎬 Render preview
  (`POST /api/formatter/animate`) renders the exact server output as an mp4.
- **Single-clip preview**: each rank has a **🎬 Preview** button (`buildOneRank`) that
  renders ONLY that rank's segment — reusing `/api/formatter/build` with `only` set to the
  rank's index in the filled set. The build still walks the play order so the progressive
  reveal state is correct at that position, but only the target segment is composed (join +
  music skipped); the result plays in the shared player modal. Fast way to eyeball one clip's
  change without building the whole Top-N.
- **Joins between segments**: `join_segments()` — same slice/concat model as
  `_temporal_segmented` but over whole rendered files. `JOIN_CATALOG` is the option set:
  `cut` (default), eased `swipeleft/right` (push), and xfade built-ins (`fade`, `wipeleft`,
  `circleopen`, …). Per-join SFX mixed on top. Each segment is normalized
  (fps/scale/sar/audio, silence if missing) before joining.

`formatter.py`:
- One preset: `TEMPLATES["valorant_clip_ranking"]`; the UI sends
  `{style, title, list, offsets, widgets, clip_infos, clip_style, bonus, icon}` overrides
  (see `_merge` for resolution: preset → `STYLE_SPECS[style]` → overrides). Base sizes /
  positions are **fractions of W/H**; drag `offsets` are design px at 1080×1920 (`_off_px`
  scales them). `ts`/`ls` scale the title/list like the design's transform (fit width
  scales too, so wrapping matches the unscaled layout).
- The build converts per-rank arrays per segment (`main._run_formatter_build`): `widgets`
  masked to revealed ranks, `clip_infos[ri]` → `clip_info`, `bonus.show` =
  anim | static | omit.
- Fonts are bundled OFL TTFs in `assets/fonts/` (`FONTS`). Italic slant for non-italic
  faces (Anton) is faked by shearing the rendered title (`_shear`).
- **Color emoji ARE rendered** (Apple Color Emoji via Pillow). Text runs use the
  OFL font; emoji runs render as color glyphs sized ~1em, centered on the line —
  in the **title** (`_draw_words`), **list labels** + **bonus** (`_text_sprite` →
  `_mixed_text_sprite`), and the **caption** (`_rich_line_sprite`). Title fit/wrap
  measures emoji at real width via `_run_len` (not the OFL `.notdef` advance).
  Emoji-only on macOS (Apple Color Emoji); on a boxless host emoji are dropped (no
  tofu). The editor has a **😀 emoji picker** (`wireEmojiButtons`/`openEmojiPicker`,
  seeded `EMOJI_SET`) on the Title + Bonus inputs; the live canvas renders emoji
  natively so preview↔render match.
- Widget/logo images resolve through `_asset_image` (catalog `src` like
  `/assets/minimal/...` → repo file; traversal-guarded).

## Experimental search engine (search2) — AI-parsed, multi-source, vision-verified

A second, more powerful clip finder living on the **Search → 🧪 Experimental**
sub-tab (the original searcher is unchanged on the **Valorant** sub-tab). It runs
as a **background job** (`POST /api/search2` → poll `/api/jobs/{id}`) because the
LLM + frame sampling take ~30-120s; the UI shows a live progress bar.

Pipeline (`backend/search2.py :: deep_search`), every stage best-effort +
degrades if a dependency is down:
1. **Parse** — natural-language query → structured intent via `aibrain.parse_query`
   (LLM), with a regex `_rule_parse` fallback. Explicit UI filters override it.
2. **Fan out** — concurrent `clips.search` (YouTube, the improved Valorant-gated
   multi-query engine), `twitch.search_clips` (Helix), `reddit.search`; merge+dedup.
3. **Prescore** — deterministic metadata rank (duration sweet-spot + Valorant
   relevance + agent/map/weapon/org matches + recency/views/duration hard filters),
   reusing `clips._*` scoring helpers.
4. **Verify** — for the top N (`vision_count`), `framegrab.sample_frames` pulls a
   few low-res frames and `aibrain.analyze_clip` (vision) judges: clean landscape
   gameplay? facecam (+ corner)? edited / center-clutter? agent/map/weapon. Run
   concurrently (~5 at a time).
5. **Rank + reason** — fuse prescore + vision into a final clippability score and a
   human "why", return rich cards.

**The AI is the local `claude` CLI (Claude Code headless), NOT the paid API** —
`aibrain._run` shells out to `claude -p --json-schema … [--allowedTools Read]`, so
parse/curate/**vision** draw on the user's **Claude Max subscription** at zero
per-search dollar cost (just Max rate limits + ~10-30s latency). Vision works by
letting the CLI's Read tool view local JPEG frames. If `claude` is missing/errors,
every AI step returns None/neutral and the engine falls back to rule-based ranking.

**New backend modules:**
- `twitch.py` — Helix client. App-only **client-credentials** token (cached) from
  `secrets/twitch.json` ({client_id, client_secret}). `search_clips()` pulls top
  VALORANT clips over a date window by `game_id` (whole category) or
  `broadcaster_id` (one streamer). ⚠ Helix has **no full-text clip search** — a
  title substring is only a soft filter; Twitch is a top-clips *discovery* pool and
  the vision layer judges content. VALORANT `game_id=516575`.
- `reddit.py` — VALORANT clip-sub search. Reddit 403-blocks unauth'd JSON now, so
  it uses **app-only OAuth** from `secrets/reddit.json` ({client_id, client_secret},
  a free "script" app at reddit.com/prefs/apps); **absent creds → returns [] cleanly**
  (source just disabled, UI checkbox greyed). Keeps only downloadable-video link posts.
- `aibrain.py` — the `claude` CLI wrapper (parse_query / analyze_clip / curate).
- `framegrab.py` — yt-dlp low-res section/full fetch + ffmpeg frame extract into
  `storage/tmp/_framegrab/<id>/` (self-cleaned per clip).
- `search2.py` — the orchestrator + canonical VALORANT vocab (AGENTS/MAPS/WEAPONS/
  ORGS/PLAYS) used for the rule parser, filter matching, and the UI's filter chips.

**Secrets** (all gitignored under `secrets/`): `twitch.json` **configured**;
`reddit.json` **not yet** (add it to light up Reddit). Frontend: the deep-search
engine is **context-driven and reused in two places** (see "Search feature parity"
below). Its functions (`xInit/xRun/xPoll/xCard/xRender/…`) all take a `ctx`
(`{p, onGet, results, filter}`) whose `p` prefix (`"x"` = Search tab, `"rx"` =
Formatter rank finder) resolves every element via `xEl(ctx, suffix)` → `#{p}-{suffix}`,
and whose `onGet(r, btn, start, end)` is the destination (library download vs.
rank assign). `xInit(prefix, onGet)` wires one instance; `XCTX` caches them; `xMeta`
is fetched once and shared. HTML lives in `index.html` (`data-sub="experimental"` for
the Search tab, `data-mode="deep"` inside `#rank-search` for the Formatter) + `.x-*`
styles. Result cards (`xCard`, and the basic searcher's `resultRowEl`) have a **▶
preview** button — `previewRemoteClip(url, title)` resolves a progressive stream via
`/api/clips/stream` and plays it in the shared `openPlayer` modal (no download). Sub-tabs
are deep-linkable (`#search:experimental`) for headless screenshots.

### Search feature parity (IMPORTANT)

Any capability added to the **Search tab** searcher (the normal Valorant searcher
*and* the 🧪 Experimental deep search) MUST also be usable from the **searcher plugged
into the Formatter** (the per-rank `#rank-search` finder — Basic + 🧪 Deep panes) — and
they must **call the same thing**, not a forked copy. The two surfaces already share the
same primitives: `resultRowEl(r, onGet)` + `downloadClip()` for basic search, the
`xEl`/`ctx` experimental engine (`p="x"` vs `p="rx"`) for deep search, and
`previewRemoteClip` for preview. When you extend the searcher,
extend the shared function and instantiate it in both places (the destination — library
vs. rank — is injected via the `onGet`/callback), rather than duplicating logic.

## Target platform & safe zones (YouTube Shorts)

Output is **1080×1920** aimed at YouTube Shorts. Keep meaningful text/graphics inside the
central safe area:

- **Bottom ~15%**: covered by the Shorts progress bar, title, channel, and description —
  keep list items and titles above it.
- **Right ~12%**: like/comment/share/remix rail — avoid putting text there.
- Templates already bias text to the upper-middle and left. When adding/adjusting a
  template, respect these zones (positions are H/W fractions, so nudge those).

## Frontend conventions (app.js)

- Vanilla JS, `$`/`$$` are `querySelector` helpers. **All state lives in the `AL` object.**
  No framework, no build. Key `AL` fields:
  - `natW/natH/duration/rect/cuts` — the single-clip editor state.
  - `fx` — default transition applied to new cuts (`{autoApply, sound, visual, volume}`);
    `fxData` is the `/api/fx` catalog.
  - `vce` — the Valorant Clip Ranking editor state (`{style, mode, target, bake, title,
    anim:{intro,new_item→{motion,sound,volume,speed}}, accent:{style→hex}, pillFill,
    accentWords[], off:{style→{tx,ty,lx,ly,ts,ls}}, widgets[], clips[], clipStyle, bonus,
    icon}`). `accent` is the per-style title/number accent colour (`vceAccent()`), `pillFill`
    the pill background (pill style only), `accentWords` the lowercased title words drawn in
    the accent colour — toggled by **clicking a title word on the canvas** (`vceToggleAccentWord`
    via the tap path in `vceAddMove`). These map to backend `title.accent` / `title.pill_fill`
    / `title.accent_words` / `list.accent` (see `formatter.STYLE_SPECS`). Reveal Motion/Sound/Volume/Speed are
    stored **per phase** (`anim.intro` / `anim.new_item`); the `mode` toggle (intro |
    new_item) selects which phase the controls edit + preview. `vceCfg()` returns the
    active phase's config; `vceNormalizeAnim()` fills defaults + migrates legacy flat
    `motion/sound/volume/speed`. `vcePicker` — `/assets/catalog.json` (players/weapons/orgs);
    `fmt` — `{order, ranks:[{label, clip, layout, join}]}`; `fmtMeta` is `/api/formatter/meta`.
  - `editingSlot` — the rank index the single-clip editor is currently editing (or null).
  - `tabBtns` — map of tab id → button (for programmatic tab switches).
- **Tabs** are built in `initTabs()`: niches (from `/api/niches`) + top-level tools
  `formatter` and `videos`. `selectTab(id, btn)` toggles panels by `data-niche`.
- Draggable source regions ("boxes") are in `BOXES`, keyed by name (`facecam`, `mousecam`).
  Trim + cuts are drawn as overlays on `#al-trim-bar`.
- `buildOpts()` is the single source of truth for the single-clip render/preview payload —
  add any new option there **and** in `LayoutReq`/`_to_opts` on the backend, matching field
  names. `buildFmtPayload()` is the equivalent for the Formatter.
- Preview is debounced (`refreshPreview` 150ms; the Formatter stage bg `refreshStageBg`
  250ms). Only trigger a preview for edits that change the composed *frame*; time-only
  edits update the length readout only. The Formatter template itself needs **no server
  round-trip** — the canvas re-renders client-side (`vceRebuild`).

## Checklists

**Add a single-clip layout option:**
1. `LayoutOpts` field in `autolayout.py` (+ use it in the filter builder).
2. `LayoutReq` field in `main.py` + wire it in `_to_opts()`.
3. UI control in `index.html`, state + `buildOpts()` entry in `app.js`, styles in `style.css`.
4. Decide: does it change the still preview (spatial) or only length (temporal)?
5. Verify with a direct render, then drop a short sample in `outputs/`.

**Add an SFX:** drop a loudness-normalised `.wav` in `assets/sfx/`, add an entry to
`SFX_CATALOG` (key/label/category/file). It surfaces in `/api/fx` automatically.

**Add a B-roll effect:** add an entry to `autolayout.EFFECTS_CATALOG`
(key/label/emoji/flash_dur/dur_min/dur_max/blur/tint/peak/lead + a `sounds` list of
{key,label,file}, `sound` = default file) and drop each `sounds` `.wav` in `assets/sfx/`.
It surfaces at `/api/fx` (`effects`, with per-sound preview URLs + slider range) and in the
editor's B-roll tray automatically; no frontend change needed. Per-marker `dur`/`sound`
overrides merge onto the catalog spec in `_normalize_effects`; render lives in `_apply_effects`.

**Add a join/transition effect:** if it's an ffmpeg `xfade` built-in, just add it to
`JOIN_CATALOG` (`_XFADE_JOINS` picks it up). Eased pushes go through `_push_chain`.

**Add a template style:** add to `formatter.STYLE_SPECS` (title/list specs as W/H
fractions + default drag offsets + `widget_h_pct`), port its look into a matching
`vceBuild<Style>` DOM builder in `app.js`, and add its button to the style segment in
`index.html`. It appears in `/api/formatter/meta` → `styles`. Keep the Pillow render and
the canvas builder visually in sync — the canvas is the live preview of the render.

**Add a picker asset:** drop the PNG under `assets/plrs`, `assets/weapons/<type>`, or
`assets/orgs`, add its metadata to that folder's `index.json`, then run
`.venv/bin/python scripts/build_catalog.py` to rebuild `assets/catalog.json` (the picker
list the frontend fetches — `src`/`name`/`sub`/`q` for players & weapons, `tag` for orgs).

## Roadmap (agreed priorities)

In rough priority order — interview before building each:

1. ~~**Animated text / list reveals.**~~ **Done** — `animate.py` bakes reveals + idle
   motion into builds; the Formatter editor previews them live on the canvas.
2. **More templates & niches.** More ranking styles (tier-list, versus, stat-card…),
   and support additional niches (add to `config.NICHES` + `youtube.CHANNELS`).
3. **YouTube SEO / titles.** Use `config.VIDIQ_API_KEY` (currently unused) for
   title/tag/keyword/hook suggestions per clip.
4. **In-app template builder.** A UI to create & save custom templates (not just
   code-defined `TEMPLATES` presets) — implies persisting user templates somewhere.

## Channel analytics (your YouTube data)

The overview tab + stats script read **the authorized account's own channel** (via
`youtube.CHANNELS = {"valorant": "mine"}` — "mine" resolves to whatever channel the
OAuth account owns; nothing is hardcoded). Two ways to read its performance data;
**prefer the first** (free, richer, no credit ceiling):

**1. `scripts/youtube_stats.py` — direct Google API pull (preferred).**
Pulls your channel's own stats straight from Google, free, no VidIQ credits. The
report header + default start date come from the authorized channel itself.

```bash
.venv/bin/python scripts/youtube_stats.py            # all uploads, since channel start
.venv/bin/python scripts/youtube_stats.py --from 2026-07-06   # date-range analytics
.venv/bin/python scripts/youtube_stats.py --json     # also dump rows to outputs/
```

- **Auth:** OAuth (owned-channel *private* analytics needs it, not just an API key).
  A Desktop OAuth client JSON lives at `secrets/client_secret.json`; the token is
  cached at `secrets/youtube_token.json` after the first run. **`secrets/` is
  gitignored — never commit it.** First run opens a browser to authorize (sign in as
  the channel-owning Google account); every run after is silent. If the token is
  missing/expired the script re-opens the browser. (The ⚙ Settings screen's "Connect
  YouTube" button runs the same flow.) Deps: `google-api-python-client`,
  `google-auth-oauthlib`, `google-auth-httplib2` (already in `.venv`).
- **Two number streams, different latencies — this matters:**
  - **Public counts** (views/likes/comments via the *Data API* `videos.list`,
    `part=statistics`) are the **live odometer** — near-real-time, what you see on the
    channel. Column `PubViews`.
  - **Analytics** (retention %, watch time, subs gained via the *YouTube Analytics
    API* report, `dimensions=video`) **lags ~2-3 days.** Today is always partial;
    T-3 and older is complete + permanent. Column `AnaViews`/`Ret%`.
  - The script prints both plus a `Lag` column (`PubViews - AnaViews`) — during a
    surge the odometer runs thousands ahead of Analytics; the gap closes as the
    pipeline catches up. Don't "reconcile" them — they're the same views at different
    processing stages. The only real-time source for a <48h surge is the public count.
- Lists **all** uploads via the channel's uploads playlist (`list_uploads()`), so
  brand-new videos with no Analytics rows yet still appear (AnaViews 0, PubViews live).
- Analytics can go deeper than the script currently prints: second-by-second
  retention curves (`dimensions=elapsedVideoTimeRatio`, `metrics=audienceWatchRatio`,
  needs a `video==` filter), traffic sources, geography, day-by-day — all
  retroactively queryable for free. Extend the script rather than reaching for VidIQ.

**2. VidIQ MCP (`mcp__vidiq__*`) — fallback / competitor research.**
Deferred tools (load schemas via ToolSearch `select:mcp__vidiq__…`), authed via your
own VidIQ account if you connect one. **Free plan: 150 credits/cycle, ~5 credits per
data call, resets monthly** — runs dry fast, so don't burn it on own-channel numbers
the script gives you free. `vidiq_balance` and `vidiq_user_channels` cost 0. VidIQ's
real edge is what the Analytics API can't do:
competitor/outlier discovery, keyword research, trending videos, thumbnail/title
scoring. Save credits for that.

## Design source (Claude Design)

The Formatter's ranking template + editor are ported from the user's **Claude Design**
project *"YouTube Shorts ranking template"* — file `Shorts Ranking Templates.dc.html`.
That `.dc.html` is the visual source of truth for the editor panel layout and the reveal
animation timing/easing (`animate.py` mirrors its constants). Keep the app's canvas/editor
in sync with it when the design changes.

**How to get the design (as of 2026-07-07):**

- **Live project (MCP)** — Claude Design MCP at `https://api.anthropic.com/v1/design/mcp`.
  Authorize once with `/design-login`, then use the `DesignSync` tool's *read* methods
  (`list_projects` → `get_file`) to pull files. Project URL:
  `https://claude.ai/design/p/4702ac88-ad09-4327-b14f-308574cbf5b2?file=Shorts+Ranking+Templates.dc.html`
  (`DesignSync` is oriented at *pushing* a local component library up; for import you only
  need its read methods.)
- **Local handoff bundle** — a full self-contained export lives at
  `~/Downloads/youtube-shorts-ranking-template/` (`README.md` + `project/Shorts Ranking
  Templates.dc.html` + its imports `support.js` / `picker-data.js` + fonts, sfx, and the
  plrs/weapons/orgs picker PNGs). **You do not need the MCP to read the design** — read the
  `.dc.html` and follow its imports directly from this bundle.

Deviation from the mock: the `.dc.html` uses a **single shared** Motion/Sound/Volume/
Reveal-speed set — its Intro / New-item toggle is only a *preview selector*. The render
pipeline (`main._run_formatter_build`) has always applied **separate** `anim.intro` vs
`anim.new_item` configs per segment, so the app **extends** the mock: the editor's Reveal
toggle is *toggle-scoped* — Motion/Sound/Volume/Speed edit whichever phase is selected, and
`buildFmtPayload()` sends the two phases independently. This is intentional and should stay.

## Seeding "usable" clips from published videos

When the user asks to "save/seed the clips from the past N YouTube videos" into folders
(the `usable: raw` / `usable: edited` pattern), here's the whole recipe — it's driven from
plain Python (no running server needed; `autolayout.compose()` is a pure function):

- **Match videos → projects by TITLE.** There is **no stored link** between a channel
  upload and a project/render (uploads have no `youtube_id` on projects; output mp4s are
  random-hex named). List uploads via `backend/youtube.py` (read-only OAuth, token in
  `secrets/`), then fuzzy-match against `storage/projects/*.json` names/`state.vce.title`.
  Several projects share near-identical titles (multiple TenZ/s0m/Aspas) and a few are
  mis-titled — **show the user the mapping and flag the shaky rows** before committing.
- **Where the clips live.** A project's `state.fmt.ranks[i]` holds `clip` (source identity
  `folder/name.mp4`) + `layout` (the full per-clip edit: trim/cuts/effects/speed/facecam…).
  Verify each `store.resolve(clip)` exists first — `default/` auto-cleans, so older
  references can be gone.
- **`usable: raw`** = the source files. `store.create_folder("usable: raw")`, `shutil.copy2`
  each resolved source in (don't move — keep originals + project refs intact).
- **`usable: edited`** = the per-clip edit, which **does not exist as a file** — render it:
  `LayoutReq(**rank["layout"])` → `main._to_opts(req)` → set `opts.overlay_png = None`
  (pure clip edit, **no** ranking-template overlay — that's added at Formatter assembly, not
  per-clip) → `autolayout.compose(src, dst, opts)`. ~1080×1920, ~20–220 s each; run ~3 at a
  time. This reproduces exactly what "Load project → Open editor" shows.
- **⚠ Strip the Subscribe CTA for `usable: edited`.** A subscribe CTA baked into a clip is a
  `{"type":"subscribe", …}` entry in `layout["effects"]`. The ranking title/list can't be
  composited over a pre-rendered CTA, so usable edited clips must **omit** it: drop only the
  `subscribe`-type effects (keep `money`/`sfx`/`face_zoom`) and render with
  `compose(..., skip_subscribe=True)`. Removing it does not change duration (it's an overlay).
- **Tag players.** `store.set_meta("usable: …/<name>.mp4", creator=<player>)` on both copies.
  Single-player videos → tag every clip with that player; weapon/event videos (Operator /
  Spectre / World Cup) have a different pro per rank → leave untagged unless a label names one.
- **Naming.** `<video-key>_r<rank>_<sourceStem>.mp4`, identical in both folders so raw↔edited
  pair 1:1 (e.g. `aspas_r1_rWkSo-i746Y.mp4`).
- Done 2026-07-22: seeded the last 10 uploads (37 clips) this way; 5 had a subscribe CTA
  stripped. Drop review frames/a sample in `outputs/` as usual.

## Notes

- `VIDIQ_API_KEY` in `config.py` is for future YouTube SEO tooling, **not** clip download
  (that's yt-dlp). Set it via the `VIDIQ_API_KEY` env var (defaults to unset/off).
- **Tabs:** `Valorant` · `Search` · `Library` · `Formatter`. Valorant is a
  **channel-overview** panel (live YouTube stats via `youtube.py` for the connected
  channel). **Search** holds the clip searcher + Auto-Layout editor (a sub-tab,
  `data-niche="search"` / `data-sub`, expandable to more niches). **Library** is the
  file-management system. Formatter wraps the ranking template in a formatter-type
  selector (only one type today). The old **Videos** tab + `bodybuilding` niche were
  removed. Tabs are deep-linkable via `#hash` (used for headless screenshots).
- The **Library / clip browser** (`renderClipBrowser` in app.js) is the single
  folder+thumbnail UI used everywhere a clip/folder is chosen — the Library tab
  (manage mode), the Search editor's "Your clips" picker, and the rank/move pickers
  (pick mode). **No `<select>` dropdowns for clip/folder selection** — favorites,
  #tags, creator marking, duplicate, move, pin all go through it (thumbnails come
  from `GET /api/clips/thumb`). Keep it that way; don't reintroduce clip dropdowns.
  The **creator picker** (`openCreatorPicker`) searches the full roster
  (`AL.libRoster` from `/api/clips` → `creator_roster`) — every player + streamer
  with headshots — or type a custom name. Filter chips show only creators in use.
- Fonts and SFX bundled so far are OFL / royalty-free (safe for commercial use); keep new
  bundled assets license-clear and note the source (see `assets/sfx/README.md`).
- **Clip packages** live behind the top-right **📦** button (a red badge = unseen packages).
  It opens a list → detail modal (`openPackages`/`openPkgDetail` in app.js): preview a
  candidate (streams it via `/api/clips/stream`, or plays the local file once grabbed), tick the
  ones you want, **Open selected in Formatter** downloads them (`/api/packages/{id}/select`
  job) and fills `AL.fmt.ranks`. The critiques box POSTs to `/api/packages/{id}/critique`,
  which trains the agent (see `packages.py`). Badge polls `/api/packages` every 30s.
- **Shared preview player** (`openPlayer`/`#player-modal`): a lightweight `<video>` modal to
  play a clip WITHOUT opening the editor. Used by the Library thumbnail **▶** button (hover
  to reveal) and the package candidate previews. Reuse it for any "just watch this clip" need
  rather than routing through the editor.
- This ffmpeg build has **no `drawtext`** and can't ease `xfade` — that's why text is Pillow
  overlays and swipes are overlay pushes. Don't "simplify" either back to those filters.

## Note From Dev

- A bug that often slips through is because the backend server isn't reloaded (so I can't use xyz feature), see README.md
- I can do this myself but it would be nice if you could refresh/deploy it for me after integration.
- do not program any functionality based on the wired vidiq connector. It can be used in sessions if you ask me though. I don't want to have vidiq tokens aten when I open the web app.