# VALORANT Ranking Template — YouTube Shorts builder

A local, Claude-assisted tool for turning landscape VALORANT gameplay/stream
clips into polished vertical **YouTube Shorts** (9:16, 1080×1920) — including
progressive-reveal **"Top N" ranking** videos. It runs entirely on your own
machine: a small FastAPI server drives `ffmpeg` / `yt-dlp` / OpenCV and serves a
no-build static frontend at `http://127.0.0.1:8000`. No cloud, no accounts, no
telemetry — clone it, add your own keys, and start building your channel.

Two tools sit on one rendering engine:

1. **Auto-Layout** (single clip) — reflow one landscape clip into a 9:16 vertical
   (facecam on top, gameplay below), with trim, mid-clip cuts, speed, swipe/SFX
   transitions, B-roll punch effects, an animated Subscribe CTA, and optional
   template overlay.
2. **Video Formatter** (many clips) — a ranking builder: drop a clip into each
   rank, edit each with the full Auto-Layout toolset, and assemble them into one
   "Top N" short with a progressive-reveal template, per-join transitions, and a
   background music bed.

> This is an **unofficial fan-made tool**. VALORANT and Riot Games are trademarks
> of Riot Games, Inc.; the bundled game art is Riot's IP and is not covered by
> this project's license. See [NOTICE](NOTICE).

---

## Prerequisites

- **Python 3.13** (a `.venv` is used for all deps).
- **ffmpeg** and **ffprobe** on your `PATH` (`brew install ffmpeg` on macOS).
- *(Optional, for AI features)* **[Claude Code](https://claude.com/claude-code)**
  signed in, **or** an **Anthropic API key** you paste in Settings.
- Built and tested on macOS. Linux should work; Windows is untested.

## Install & run

```bash
git clone https://github.com/gregco7/Content_Tool_Manager.git
cd Content_Tool_Manager

python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

./run.sh                 # serves http://127.0.0.1:8000
./run.sh --reload        # dev mode: auto-reload backend on edits
```

Open **http://127.0.0.1:8000** and you're running. Everything below is optional
configuration to unlock more features.

> **Restart after backend edits.** `run.sh` does **not** auto-reload — a server
> started earlier keeps running the old `backend/*.py`. Stop it and start again,
> or use `./run.sh --reload`:
> ```bash
> pkill -f "uvicorn backend.main:app"   # stops only the server, not your browser
> ./run.sh
> ```

## First-run setup (⚙ Settings)

Click the **⚙** button (top-right) to open Settings. Nothing is required, but each
integration you configure lights up more of the app:

| Set this | To get | Where to get the keys |
|----------|--------|-----------------------|
| **Channel name / handle / accent** | Your own channel on the animated Subscribe CTA + as the clip-agent's default | — (just type it) |
| **Claude** (CLI or API key) | AI clip search + the clip-package agent | Install Claude Code, or paste an Anthropic API key |
| **YouTube** (Connect button) | The channel-overview stats tab for **your** channel | A Google Desktop OAuth client — see [secrets/README.md](secrets/README.md) |
| **Twitch** client id/secret | Twitch as a clip-search source | Free app at [dev.twitch.tv](https://dev.twitch.tv/console/apps) |
| **Reddit** client id/secret | Reddit as a clip-search source | Free "script" app at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) |

All credentials live in the gitignored `secrets/` folder and are **never
committed**. Copy the `secrets/*.example.json` templates if you'd rather set them
by hand. Full details: [secrets/README.md](secrets/README.md).

## How it works

- **Find clips** — search YouTube (and, if configured, Twitch/Reddit) by creator +
  query, or import a local video. A 🧪 **Experimental** searcher adds AI parsing +
  vision verification when Claude is available.
- **Auto-Layout** — pick a clip, click **Auto-detect facecam** (OpenCV YuNet),
  fine-tune the box, and render a vertical short. Toggle blurred edges, a mousecam
  overlay, audio fade; add speed changes, mid-clip cuts, swipe transitions, B-roll
  punch effects, and an animated Subscribe CTA.
- **Formatter** — build a "Top N": one clip per rank, each editable with the full
  Auto-Layout toolset, assembled with a progressive-reveal ranking template,
  per-join transitions, and a licensed music bed.
- **Library** — folder + thumbnail file management: favorites, #tags, per-clip
  creator tagging, duplicate/move, retention.

## Project layout

```
backend/     FastAPI server + the ffmpeg/Pillow rendering engine
  main.py         routes + request models (incl. /api/settings)
  config.py       paths, niches, per-user settings (load/save_settings)
  autolayout.py   facecam detection + the ffmpeg filter_complex + B-roll effects
  formatter.py    the "Valorant Clip Ranking" overlay template
  animate.py      baked reveal animations (Pillow PNG sequences)
  cta.py          animated Subscribe call-to-action (uses your channel identity)
  clips.py        yt-dlp search / download + progressive-stream resolver
  twitch.py reddit.py search2.py aibrain.py framegrab.py   experimental searcher
  youtube.py      channel-overview stats (your own channel, via OAuth)
  music.py packages.py store.py hook.py killtimes.py       supporting features
frontend/    index.html + app.js + style.css (served statically, no build step)
assets/      fonts (OFL), sfx, and VALORANT picker art — see NOTICE
scripts/     youtube_stats.py + asset/catalog build helpers
secrets/     your gitignored credentials (+ *.example.json templates)
storage/     clips, renders, music, tmp (all gitignored, per-user)
```

## License

Source code: **MIT** — see [LICENSE](LICENSE). Bundled game art / fonts / SFX
carry their own separate terms — see [NOTICE](NOTICE). This project is not
affiliated with or endorsed by Riot Games.
