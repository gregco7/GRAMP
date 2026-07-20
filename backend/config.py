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
# overview panel (live YouTube stats). Only "valorant" is wired to a channel so
# far; "cars" is an empty placeholder overview.
NICHES = [
    {"id": "valorant", "label": "Valorant", "active": True},
    {"id": "cars", "label": "Cars", "active": False},
]
