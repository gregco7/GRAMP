"""
Twitch clip source — real game-wide VALORANT clip search via the Helix API.

This is the crop-ready jackpot for a Shorts tool: Twitch clips are short by
construction (5-60s), almost always carry the streamer's facecam, and are raw
gameplay (no montage editing / added music). The old creator-only scraper in
`clips.py` could only pull ONE streamer's clips; this pulls the whole VALORANT
category, sorted by views over a time window, and (optionally) scopes to a
streamer — which is exactly the discovery power the Experimental engine needs.

Auth is the **client-credentials** flow (an app access token from the Client ID +
Secret at `secrets/twitch.json`), cached with its expiry — no user OAuth, no
redirect, purely server-side. See `secrets/twitch.json` (gitignored).

IMPORTANT LIMITATION: Helix has **no full-text clip search**. You query clips by
`game_id` or `broadcaster_id` over a date window, sorted by views — you cannot
search "ace" across all clips. So keyword intent is handled upstream: Twitch
returns a strong *top-clips* pool (optionally per-streamer) and the AI / vision
layer in `search2.py` judges each clip's actual content + clippability. A title
substring is only applied as a soft filter (Twitch titles are often "LOL" / an
emote, so they're unreliable as the primary signal).
"""

from __future__ import annotations

import datetime
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import config

# --------------------------------------------------------------------------- #
# Credentials + token cache
# --------------------------------------------------------------------------- #
_SECRETS = Path(config.SECRETS) / "twitch.json"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_HELIX = "https://api.twitch.tv/helix"

_lock = threading.Lock()
_token: dict = {"access_token": None, "expires_at": 0.0}
_game_id_cache: dict[str, str] = {}


def _creds() -> Optional[dict]:
    try:
        c = json.loads(_SECRETS.read_text())
        if c.get("client_id") and c.get("client_secret"):
            return c
    except Exception:
        pass
    return None


def available() -> bool:
    """True when Twitch creds are configured (the source can be used)."""
    return _creds() is not None


def _app_token() -> Optional[str]:
    """A cached app access token (client-credentials). Refreshed ~1 min before
    expiry. Thread-safe so concurrent source fan-out shares one token."""
    with _lock:
        now = time.time()
        if _token["access_token"] and now < _token["expires_at"] - 60:
            return _token["access_token"]
        c = _creds()
        if not c:
            return None
        data = urllib.parse.urlencode({
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "grant_type": "client_credentials",
        }).encode()
        req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.load(r)
        _token["access_token"] = tok.get("access_token")
        _token["expires_at"] = now + float(tok.get("expires_in", 3600))
        return _token["access_token"]


def _headers() -> Optional[dict]:
    c = _creds()
    at = _app_token()
    if not c or not at:
        return None
    return {"Client-ID": c["client_id"], "Authorization": f"Bearer {at}"}


def _get(path: str, params: dict) -> dict:
    h = _headers()
    if not h:
        return {}
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{_HELIX}/{path}?{qs}", headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def game_id(name: str = "VALORANT") -> Optional[str]:
    key = name.lower()
    if key in _game_id_cache:
        return _game_id_cache[key]
    try:
        data = _get("games", {"name": name}).get("data") or []
        if data:
            _game_id_cache[key] = data[0]["id"]
            return data[0]["id"]
    except Exception:
        pass
    return None


def _broadcaster_id(login_or_name: str) -> Optional[str]:
    """Resolve a streamer handle → broadcaster id. Try exact login first (the
    handle), then the fuzzy channel search (display names, spaces)."""
    slug = "".join(ch for ch in login_or_name.lower() if ch.isalnum() or ch == "_")
    if slug:
        try:
            data = _get("users", {"login": slug}).get("data") or []
            if data:
                return data[0]["id"]
        except Exception:
            pass
    try:
        data = _get("search/channels", {"query": login_or_name, "first": 5}).get("data") or []
        want = login_or_name.lower().replace(" ", "")
        for ch in data:
            if ch.get("display_name", "").lower().replace(" ", "") == want:
                return ch["id"]
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Clip search
# --------------------------------------------------------------------------- #
def _iso(days_ago: int) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(c: dict) -> dict:
    """Helix clip → the app's standard result dict (source='twitch')."""
    thumb = c.get("thumbnail_url") or ""
    return {
        "id": c.get("id"),
        "title": c.get("title") or "(untitled clip)",
        "url": c.get("url"),
        "channel": c.get("broadcaster_name") or c.get("creator_name"),
        "duration": c.get("duration"),
        "thumbnail": thumb,
        "view_count": c.get("view_count"),
        "source": "twitch",
        "via": "helix",
        "language": c.get("language"),
        "created_at": c.get("created_at"),
        "creator_name": c.get("creator_name"),
    }


def search_clips(query: str = "", creator: str = "", days: int = 60,
                 limit: int = 40, language: str = "", pages: int = 3) -> list[dict]:
    """
    Top VALORANT Twitch clips over the last `days`, sorted by views.

    - `creator`: scope to one streamer's clips (strong signal); else the whole
      VALORANT category (discovery pool).
    - `query`: soft, case-insensitive title substring filter (Twitch titles are
      unreliable, so a no-match does NOT drop everything — see below).
    - `language`: e.g. "en" to bias English clips (applied as a soft filter).
    - `pages`: Helix pages of 100 to walk (bigger pool for the ranker/vision).

    Returns normalized result dicts (source='twitch'), best-effort — any error
    yields [] so the orchestrator degrades to its other sources.
    """
    if not available():
        return []
    gid = game_id("VALORANT")
    params: dict = {"first": 100, "started_at": _iso(days), "ended_at": _iso(0)}
    bid = _broadcaster_id(creator) if creator else None
    if bid:
        params["broadcaster_id"] = bid
    elif gid:
        params["game_id"] = gid
    else:
        return []

    out: list[dict] = []
    cursor = None
    try:
        for _ in range(max(1, pages)):
            p = dict(params)
            if cursor:
                p["after"] = cursor
            resp = _get("clips", p)
            for c in resp.get("data") or []:
                out.append(_norm(c))
            cursor = (resp.get("pagination") or {}).get("cursor")
            if not cursor:
                break
    except Exception:
        pass

    # Soft filters — applied only if they leave a usable pool (Twitch titles lie).
    q = (query or "").strip().lower()
    if q:
        hit = [c for c in out if q in (c.get("title") or "").lower()]
        if len(hit) >= max(6, limit // 3):
            out = hit
    if language:
        lang = [c for c in out if (c.get("language") or "").startswith(language)]
        if len(lang) >= max(6, limit // 3):
            out = lang

    out.sort(key=lambda c: c.get("view_count") or 0, reverse=True)
    return out[:limit]
