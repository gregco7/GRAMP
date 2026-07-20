# Content Tool Manager

A local Claude-assisted app for content-creation prerequisites and editing gimmicks.
Runs entirely on your machine.

## Run

```bash
./run.sh
```

Then open **http://127.0.0.1:8000**

(First run needs the venv, which is already set up in `.venv/`. To rebuild it:
`python3.13 -m venv .venv && .venv/bin/pip install fastapi "uvicorn[standard]" python-multipart opencv-python-headless yt-dlp numpy`)

## Restart / stop

The whole app is **one process**: the uvicorn server on port **8000**. If starting it
prints `ERROR: [Errno 48] ... address already in use`, an old server is still running
(often a detached/background one you can't Ctrl-C). **Kill whatever owns the port, then
start fresh** — this is the reliable "restart everything":

```bash
pkill -f "uvicorn backend.main:app"   # stop the server (matches only uvicorn — NOT your browser)
./run.sh                              # start it again
```

Then hard-refresh the browser tab (**⌘⇧R**) at http://127.0.0.1:8000.

Handy extras:

- **Port-based stop** (if it was launched some other way): kill only the process *listening*
  on 8000 — `lsof -nP -iTCP:8000 -sTCP:LISTEN -t | xargs kill -9`.
  ⚠️ Do **not** use plain `lsof -ti tcp:8000 | xargs kill` — that also matches your browser's
  connection to the server and would kill Chrome.
- **See what's on the port:** `lsof -i tcp:8000` — the `LISTEN` row is the server; the other
  rows are just browser connections (leave those alone).
- If you launched `./run.sh` in a terminal window you can see, **Ctrl-C** there also stops it.
  The commands above are for when it's running detached (no window to Ctrl-C).
- **After changing backend code** (`backend/*.py`) you *must* restart — the server does
  **not** auto-reload. Either restart as above, or run `./run.sh --reload` (dev mode) —
  but stop the old server first, or `--reload` hits the same "address already in use".

## Niches

- **Valorant** — active, with the tools below.
- **Cars** — reserved empty tab.
- **Bodybuilding / Motivation** — reserved empty tab.

## Valorant tools

### Find clips
Search by creator + query (YouTube via `yt-dlp`) and pull a clip into your local
library. Or **import** a local video file.

### Auto-Layout (9:16)
Turns a landscape gameplay/stream clip into a vertical short:

- **Top 35%** — the facecam, shown **fully** (fit, with a blurred fill behind it).
- **Bottom 65%** — the gameplay, **cropped to fill** (never stretched).

Workflow:
1. Pick a clip from the library.
2. Click **Auto-detect facecam** — finds the face (OpenCV YuNet) and drops the red box on it.
3. Drag / resize the red box (or edit X/Y/W/H) so it frames the actual facecam window.
   The preview updates automatically.
4. **Render vertical clip** → download the result.

**Toggles:**

- **Facecam (top pane)** — on = facecam-over-gameplay split; off = gameplay fills the
  whole frame (pair with *Blurred edges* for the no-facecam look). The top pane
  auto-sizes to the facecam's own aspect ratio, so the facecam fills it edge-to-edge
  with no blurred strips.
- **Blurred edges** — the gameplay stays large (zoomed to fill the width) sitting on a
  blurred backdrop that spans the whole render, leaving slim blurred bars at the
  top/bottom edges (the style in the reference image). The **Edge blur %** slider
  controls how thick those bars are (0% = full-bleed, no bars).
- **Mousecam overlay** — for clips that also have a mouse/keyboard/hand cam. Turn it
  on to get a second (green) box; drag it over the mousecam in the source, then choose
  which corner it lands in and its size. It's composited onto the vertical output.
  (Mousecam is manual-position — it isn't a face, so it can't be auto-detected.)
- **Fade audio out** — fades the audio down over the last moments of the clip. Use the
  **Fade length** slider to set how long the fade takes.

**Length / pacing:**

- **Speed** — speed the clip up (or slow it down), 0.5×–3×. Both video and audio are
  retimed and kept in sync; the output gets shorter as you speed up.
- **✂ Cut out** — "trim the fat." Click **Add cut @ playhead** to drop a removable span
  (shown as a hatched band on the scrubber), then fine-tune its from/to seconds. Every
  cut is removed from the render and the gap is closed. Add as many as you like.
- The **final length** readout reflects trim window − cuts, divided by speed.

## Layout / structure

```
backend/
  main.py         FastAPI server + routes
  autolayout.py   facecam detection (YuNet) + ffmpeg compositing
  clips.py        yt-dlp search / download
  config.py       paths, niches, vidiq key
  models/         YuNet ONNX face-detection model
frontend/         static UI (no build step)
storage/          clips/ (inputs), output/ (renders), tmp/ (previews)
```

## Notes

- **vidiq key**: stored in `config.py` for future YouTube SEO/keyword tooling
  (titles, tags, trend research). It is *not* a clip-download source — clip
  retrieval uses `yt-dlp`.
- Face detection gives a strong starting box centered on the face; fine-tune the
  box to the facecam borders before rendering for best framing.
