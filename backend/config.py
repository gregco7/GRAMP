"""Paths and app configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORAGE = ROOT / "storage"
CLIPS_DIR = STORAGE / "clips"
OUTPUT_DIR = STORAGE / "output"
TMP_DIR = STORAGE / "tmp"
MUSIC_DIR = STORAGE / "music"          # downloaded background-music beds (gitignored, per-user)
PROJECTS_DIR = STORAGE / "projects"   # saved Formatter edits (named projects, resume list)
PACKAGES_DIR = STORAGE / "packages"   # clip-package agent deliveries (+ its learnings)
FRONTEND = ROOT / "frontend"
ASSETS = ROOT / "assets"          # bundled fonts + SFX (served at /assets)
SECRETS = ROOT / "secrets"        # Google OAuth client + cached token (gitignored)
THUMBS_DIR = TMP_DIR / "thumbs"   # cached clip poster frames for the Library browser

# Google / YouTube (channel-overview stats). client_secret.json is a Desktop
# ("installed") OAuth client; youtube_token.json holds the authorized token with a
# refresh token so the backend refreshes silently. Scopes: youtube.readonly +
# yt-analytics.readonly. NOT vidiq — the overview is pure YouTube Data + Analytics.
YT_CLIENT_SECRET = SECRETS / "client_secret.json"
YT_TOKEN = SECRETS / "youtube_token.json"

# Per-user app settings (channel identity + optional Claude key). Lives under the
# gitignored secrets/ dir so a cloner's own config never gets committed. Managed by
# the in-app Settings screen (/api/settings) and read wherever the channel name /
# handle / accent are stamped onto output (subscribe CTA, package agent, stats).
SETTINGS_FILE = SECRETS / "settings.json"

# Review folder: sample renders / template previews Claude drops here for you to
# eyeball. Git-ignored; safe to clear. See CLAUDE.md ("Review outputs").
REVIEW_DIR = ROOT / "outputs"

# Video Library: niche-named folders (e.g. "Valorant/") live directly under the
# project root. The Videos tab browses whatever videos you drop into them.
LIBRARY_DIR = ROOT

for d in (CLIPS_DIR, OUTPUT_DIR, TMP_DIR, MUSIC_DIR, PROJECTS_DIR, PACKAGES_DIR,
          REVIEW_DIR, THUMBS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Clips are organized into folders under CLIPS_DIR; "default" is the seeded
# download target (see store.py). Ensure it exists so downloads always have a home.
(CLIPS_DIR / "default").mkdir(parents=True, exist_ok=True)

# vidiq key is for YouTube SEO/keyword tooling (titles, tags, trend research),
# NOT clip downloading. Set the VIDIQ_API_KEY env var to enable those features;
# never hardcode the key here (this file is version-controlled).
VIDIQ_API_KEY = os.environ.get("VIDIQ_API_KEY", "")

# Channel niches shown as the leading overview tabs. Each maps to a channel
# overview panel (live YouTube stats via youtube.py). "valorant" is the one wired
# niche; add your own here + a matching entry in youtube.CHANNELS to light up more.
NICHES = [
    {"id": "valorant", "label": "Valorant", "active": True},
]


# --------------------------------------------------------------------------- #
# Per-user settings (channel identity + optional Claude key)
# --------------------------------------------------------------------------- #
# Neutral defaults so a fresh clone renders sensibly before the user sets a
# channel: an empty name/handle degrades to a generic "Subscribe" CTA (no chip).
SETTINGS_DEFAULTS = {
    # Ships pointed at this repo's own channel (VALDaily / @valclips4u); cloners
    # override it in the ⚙ Settings screen to stamp their own channel instead.
    "channel_name": "VALDaily",       # stamped on the subscribe CTA + mark
    "channel_handle": "@valclips4u",  # the channel's @handle
    "channel_accent": "#ff0033",
    "anthropic_api_key": "",  # optional; injected into the `claude` CLI subprocess env
}


def load_settings() -> dict:
    """Read the user's settings.json, merged over defaults (missing file → defaults)."""
    data = dict(SETTINGS_DEFAULTS)
    try:
        if SETTINGS_FILE.exists():
            saved = json.loads(SETTINGS_FILE.read_text())
            if isinstance(saved, dict):
                data.update({k: v for k, v in saved.items() if k in SETTINGS_DEFAULTS})
    except Exception:
        pass
    return data


def save_settings(patch: dict) -> dict:
    """Merge `patch` into the stored settings and persist. Returns the full settings."""
    data = load_settings()
    data.update({k: v for k, v in (patch or {}).items() if k in SETTINGS_DEFAULTS})
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    return data
