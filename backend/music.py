"""
Background-music beds for the Video Formatter.

A curated, license-checked catalog (see the "Edit Music Shortlist" research) of
tracks that fit Valorant-highlight / anime-edit content. Three tiers:

  A  bundle-safe   — CC-BY / CC0 (free-stock-music.com). Redistributable.
  B  NCS           — free in monetized video WITH the credit line in the description.
  C  Pixabay       — no credit, but use-in-output only (never ship the raw file).

Because this app is **local + single-user**, downloading any tier into the user's
own gitignored storage/music/ and overlaying it on their own renders is permitted
use, not redistribution — the one rule is that these files are NEVER committed to a
distributed repo. Only Tier A is safe to ever commit. (See the music-licensing note.)

grab() pulls a track's audio via yt-dlp (same infra as clips.py), filtering out the
1-hour-loop / compilation uploads so you get the actual song. mix_bed() lays the
chosen track under a finished render with volume / keep-clip-audio / song-start /
fade controls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import yt_dlp

from . import config


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def _ncs(artists: str, title: str, slug: str) -> str:
    return (f"Song: {artists} - {title} [NCS Release] / "
            f"Music provided by NoCopyrightSounds / "
            f"Free Download/Stream: http://ncs.io/{slug}")


FSM_PHONK = "https://www.free-stock-music.com/phonk.html"
FSM_SYNTH = "https://www.free-stock-music.com/synthwave.html"
FSM_FB = "https://www.free-stock-music.com/future-bass.html"


def _A(tid, title, artist, lic, url):
    """Tier A — bundle-safe CC-BY/CC0."""
    return {"id": tid, "title": title, "artist": artist, "tier": "A",
            "license": lic, "url": url, "credit": None,
            "query": f"{artist} {title} no copyright"}


def _B(tid, title, artist, slug, genre_tag=""):
    """Tier B — NCS (credit required)."""
    return {"id": tid, "title": title, "artist": artist, "tier": "B",
            "license": "NCS free-with-credit" + (f" · {genre_tag}" if genre_tag else ""),
            "url": f"http://ncs.io/{slug}", "credit": _ncs(artist, title, slug),
            "query": f"{artist} {title} NCS Release"}


def _C(tid, title, artist, url):
    """Tier C — Pixabay (use-in-output only, no credit)."""
    return {"id": tid, "title": title, "artist": artist, "tier": "C",
            "license": "Pixabay Content License", "url": url, "credit": None,
            "query": f"{artist} {title} pixabay"}


CATALOG: dict[str, list[dict]] = {
    "phonk": [
        _A("moonlit", "Moonlit", "FSM Team", "CC BY 4.0", FSM_PHONK),
        _A("acceleration", "Acceleration", "FSM Team", "CC BY 4.0", FSM_PHONK),
        _A("night-drive", "Night Drive", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("night-drive-slowed", "Night Drive (Slowed)", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("phonk-execution", "Phonk Execution", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("brazilian-phonk", "Brazilian Phonk", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("phxntxm", "Phxntxm", "Mehul Choudhary", "CC BY 3.0", FSM_PHONK),
        _A("dark-trap-darkness", "Dark Trap | DARKNESS", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("metaphonk", "Metaphonk", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("ascend-fast", "Ascend (fast version)", "Alex-Productions", "CC BY 3.0", FSM_PHONK),
        _A("incident", "incident", "Rexlambo", "CC BY 3.0", FSM_PHONK),
        _A("broken", "broken", "Rexlambo", "CC BY 3.0", FSM_PHONK),
        _B("kapoeira-phonk", "Kapoeira Phonk", "HXDES, DYNAMIS", "KapoeiraPhonk", "Drift Phonk"),
        _B("bad-habit-phonk", "Bad Habit (Phonk Version)", "Jéja, Zaug", "BadHabit-Phonk", "Drift Phonk"),
        _B("set-me-free", "Set Me Free", "Rameses B", "RB_setmefree", "Phonk/DnB"),
        _C("time-trigger", "Time Trigger (Phonk House)", "Anomy5", "https://pixabay.com/music/search/phonk/"),
        _C("neon-night", "Neon Night (Phonk House)", "Anomy5", "https://pixabay.com/music/search/phonk/"),
        _C("fast-phonk", "Fast Phonk", "Pixabay", "https://pixabay.com/music/electronic-fast-phonk-347843/"),
    ],
    "hardstyle": [
        _B("diamond", "Diamond", "NIVIRO", "diamond", "150 BPM"),
        _B("zig-zag", "Zig Zag", "Clarx", "ZIGZAG", "Hardstyle"),
        _B("ascend-hs", "Ascend", "ElementD, Chris Linton", "Ascend", "Hardstyle"),
        _B("radiate", "Radiate (feat. Mees Van Den Berg)", "ElementD, Chordinatez", "radiate", "Euphoric"),
        _B("fallin", "Fallin' (feat. Micah Martin)", "ElementD", "fallin", "Hardstyle"),
        _B("lost-language", "Lost Language", "The Void", "lostlanguage", "Hardstyle"),
        _B("on-my-mind", "On My Mind", "No Hero", "OnMyMind", "Happy Hardcore"),
        _B("without-you", "Without You (feat. Justin J. Moore)", "NIVIRO", "NWithoutYou", "Happy Hardcore"),
        _C("friday-vibes", "Friday Vibes (Euphoric Hardstyle)", "SoundUniverseStudio",
           "https://pixabay.com/music/dance-friday-vibes-edm-euphoric-hardstyle-203329/"),
        _C("every-time-it-rains", "Every time it rains (150 BPM)", "ThisIsBeatKitchen",
           "https://pixabay.com/music/search/hardstyle/"),
        _C("crazy-hardstyle", "Crazy (150 BPM) Hardstyle", "ThisIsBeatKitchen",
           "https://pixabay.com/music/search/hardstyle/"),
        _C("the-portal", "The Portal", "Rizzlas", "https://pixabay.com/music/search/hardstyle/"),
    ],
    "anime": [
        _A("neonscapes", "Neonscapes", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("magenta-metropolis", "Magenta Metropolis", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("parallel-synthesis", "Parallel Synthesis", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("afterglow-love", "Afterglow Love", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("quasarise", "Quasarise", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("stargazer", "Stargazer", "FSM Team", "CC BY 4.0", FSM_SYNTH),
        _A("eastridge-turnstile", "Eastridge Turnstile", "The Loyalist", "CC BY 3.0", FSM_SYNTH),
        _A("neon-drive", "Neon Drive", "Punch Deck", "CC BY 3.0", FSM_SYNTH),
        _A("burn-up", "Burn Up", "Alex-Productions", "CC BY 3.0", FSM_FB),
        _A("skywish", "Skywish", "Roa Music", "CC BY 3.0", FSM_FB),
        _A("so-good", "So Good", "LiQWYD", "CC BY 3.0", FSM_FB),
        _A("springtime-stroll", "Springtime Stroll", "MAITTRE", "CC BY 3.0", FSM_FB),
        _A("life", "Life", "Alex-Productions", "CC BY 3.0", FSM_FB),
        _A("dreamcatcher", "Dreamcatcher", "Luke Bergs", "CC BY-SA 3.0", FSM_FB),
        _B("tell-me", "Tell Me (feat. Alex Skrindo)", "Killercats", "tellme", "Future Bass"),
        _B("live-a-lie", "Live A Lie (feat. Andreas Stone)", "Rival x Egzod", "LIVEALIE", "Future Bass"),
        _B("lies", "Lies", "Diamond Eyes", "Lies", "Future Trap"),
        _B("change-your-ways", "Change Your Ways (feat. Charlotte Haining)", "High Maintenance", "CYW", "Liquid DnB"),
        _C("nightcore-song-2", "Nightcore Song 2", "Pixabay",
           "https://pixabay.com/music/dance-nightcore-song-2-309592/"),
        _C("future-bass-action", "Future Bass Action", "penguinmusic",
           "https://pixabay.com/music/search/future%20bass/"),
        _C("fashion-chill", "Fashion Chill Future Bass", "EvgenyBardyuzha",
           "https://pixabay.com/music/search/future%20bass/"),
    ],
}

GENRE_LABELS = {"phonk": "Drift & Aggressive Phonk",
                "hardstyle": "Hardstyle / Rawstyle / Hardcore",
                "anime": "Anime-Edit Palette"}

_ALL = {t["id"]: t for tracks in CATALOG.values() for t in tracks}
_AUDIO_EXTS = (".m4a", ".webm", ".mp3", ".opus", ".ogg", ".aac")


def _by_id(track_id: str) -> Optional[dict]:
    return _ALL.get(track_id)


def local_path(track_id: str) -> Optional[Path]:
    """The downloaded audio file for a track, or None if not grabbed yet."""
    for p in config.MUSIC_DIR.glob(track_id + ".*"):
        if p.suffix.lower() in _AUDIO_EXTS:
            return p
    return None


def catalog() -> dict:
    """The full catalog, grouped by genre, with a `downloaded` flag + local file
    per track (for the Formatter music picker)."""
    out = {"genres": []}
    for gkey, tracks in CATALOG.items():
        rows = []
        for t in tracks:
            fp = local_path(t["id"])
            rows.append({**t, "genre": gkey,
                         "downloaded": bool(fp),
                         "file": (f"/storage/music/{fp.name}" if fp else None)})
        out["genres"].append({"key": gkey, "label": GENRE_LABELS[gkey], "tracks": rows})
    return out


def credit_for(track_id: str) -> Optional[str]:
    t = _by_id(track_id)
    return t["credit"] if t else None


# --------------------------------------------------------------------------- #
# Grab — pull a track's audio via yt-dlp (rejects the 1-hour-loop uploads)
# --------------------------------------------------------------------------- #
_BAD_TOKENS = ("1 hour", "1hour", "hour loop", " loop", "compilation",
               "playlist", "full album", "mega mix", "megamix", "10 hour")


def grab(track_id: str) -> dict:
    """Download the track's audio into storage/music/. Returns {id, file, cached}."""
    t = _by_id(track_id)
    if not t:
        raise ValueError(f"unknown track: {track_id}")

    existing = local_path(track_id)
    if existing:
        return {"id": track_id, "file": f"/storage/music/{existing.name}", "cached": True}

    # One-pass ytsearch download (the two-step flat→download flow 403s on the audio
    # itag). match_filter skips the 1-hour-loop / mix uploads; max_downloads=1 stops
    # after the first suitable result is fetched.
    def _match(info, *, incomplete=False):
        dur = info.get("duration") or 0
        title = (info.get("title") or "").lower()
        if dur and not (40 <= dur <= 480):
            return f"duration {int(dur)}s out of song range"
        if any(b in title for b in _BAD_TOKENS):
            return "looks like a loop/mix/compilation"
        return None

    opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/best",
        "match_filter": _match,
        "max_downloads": 1,
        "playlistend": 6,
        "outtmpl": str(config.MUSIC_DIR / (track_id + ".%(ext)s")),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(f"ytsearch6:{t['query']}", download=True)
    except yt_dlp.utils.MaxDownloadsReached:
        pass  # expected — the one track we wanted is already written

    fp = local_path(track_id)
    if not fp:
        raise RuntimeError(f"no suitable audio found for '{t['query']}' "
                           "(only long loops/mixes matched, or download blocked) — "
                           "grab it manually from the source link")
    return {"id": track_id, "file": f"/storage/music/{fp.name}", "cached": False}


# --------------------------------------------------------------------------- #
# Mix — lay the bed under a finished render
# --------------------------------------------------------------------------- #
def _probe_dur(path: str) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", path])
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def _has_audio(path: str) -> bool:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", path])
    return bool(out.decode().strip())


def mix_bed(video: str, music: str, out: str, *, music_volume: float = 0.25,
            keep_clip_audio: bool = True, clip_volume: float = 1.0,
            start: float = 0.0, fade_in: float = 0.4, fade_out: float = 2.0) -> str:
    """
    Overlay `music` under `video` and write `out`.

    Video stream is stream-copied (no re-encode) — only the audio is rebuilt.
    The music loops to cover the whole video, begins `start` seconds into the
    track (pick the drop), and fades in/out. With keep_clip_audio the original
    clip audio (game + reactions) plays on top of the bed; otherwise the bed
    replaces it. amix uses normalize=0 so the mix isn't auto-attenuated.
    """
    dur = _probe_dur(video)
    fo_at = max(0.0, dur - max(0.0, fade_out))
    bed = (f"volume={music_volume:.3f},"
           f"afade=t=in:st=0:d={max(0.01, fade_in):.3f},"
           f"afade=t=out:st={fo_at:.3f}:d={max(0.01, fade_out):.3f}")

    inputs = ["-i", video, "-ss", f"{max(0.0, start):.3f}", "-stream_loop", "-1", "-i", music]

    if keep_clip_audio and _has_audio(video):
        fc = (f"[1:a]{bed}[m];"
              f"[0:a]volume={clip_volume:.3f}[c];"
              f"[c][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        fc = f"[1:a]{bed}[aout]"

    cmd = ["ffmpeg", "-y", *inputs,
           "-filter_complex", fc,
           "-map", "0:v", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-t", f"{dur:.3f}", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
