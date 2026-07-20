"""
Reddit clip source — search over the VALORANT clip subreddits.

The VALORANT clip subs are dense with single-play highlights that link out to
YouTube / Twitch / Streamable / v.redd.it / Medal — exactly the crop-ready single
moments this tool wants, and often not surfaced by a YouTube search. Upvotes act
as a quality/popularity proxy.

AUTH: Reddit locked down unauthenticated `search.json` (403 "Blocked") in 2023-24,
so this uses **app-only OAuth** (client-credentials) when creds are present at
`secrets/reddit.json` ({client_id, client_secret}) — a free 2-min "script" app at
https://www.reddit.com/prefs/apps. Without creds it tries the legacy public JSON
once (works on some networks) and otherwise returns [] cleanly, so the orchestrator
just leans on YouTube + Twitch. Mirrors the Twitch creds pattern.

Only posts that link to a **downloadable video host** (yt-dlp-supported) are kept;
self/text and image posts are dropped. Durations are usually unknown for external
links (filled later by enrichment / vision), except native v.redd.it.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import config

_SECRETS = Path(config.SECRETS) / "reddit.json"
_lock = threading.Lock()
_token: dict = {"access_token": None, "expires_at": 0.0}

# Subreddits searched together (Reddit supports the r/a+b+c multi form).
_SUBS = "VALORANT+valorantclips+ValorantCompetitive+ValoRantClips"
_UA = "ContentToolManager/1.0 (local VALORANT clip finder)"

# Hosts yt-dlp can fetch a clip from — used to keep only downloadable link posts.
_VIDEO_DOMAINS = (
    "youtube.com", "youtu.be", "twitch.tv", "clips.twitch.tv", "streamable.com",
    "medal.tv", "v.redd.it", "streamja.com", "streamwo.com", "streamin.one",
)


def _domain_ok(url: str) -> bool:
    u = (url or "").lower()
    return any(d in u for d in _VIDEO_DOMAINS)


def _source_of(url: str) -> str:
    u = (url or "").lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "twitch.tv" in u:
        return "twitch"
    return "reddit"


def _creds() -> Optional[dict]:
    try:
        c = json.loads(_SECRETS.read_text())
        if c.get("client_id") and c.get("client_secret"):
            return c
    except Exception:
        pass
    return None


def available() -> bool:
    """True when Reddit OAuth creds are configured (the reliable path)."""
    return _creds() is not None


def _oauth_token() -> Optional[str]:
    """Cached app-only (client-credentials) bearer token, or None if no creds."""
    with _lock:
        now = time.time()
        if _token["access_token"] and now < _token["expires_at"] - 60:
            return _token["access_token"]
        c = _creds()
        if not c:
            return None
        basic = base64.b64encode(
            f"{c['client_id']}:{c['client_secret']}".encode()).decode()
        data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token", data=data, method="POST",
            headers={"Authorization": f"Basic {basic}", "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                tok = json.load(r)
        except Exception:
            return None
        _token["access_token"] = tok.get("access_token")
        _token["expires_at"] = now + float(tok.get("expires_in", 3600))
        return _token["access_token"]


def _get(path_qs: str) -> Optional[dict]:
    """GET a search path+querystring via OAuth (oauth.reddit.com) when authed,
    else the legacy public host. Returns parsed JSON or None."""
    at = _oauth_token()
    if at:
        req = urllib.request.Request(
            f"https://oauth.reddit.com{path_qs}",
            headers={"Authorization": f"bearer {at}", "User-Agent": _UA})
    else:
        req = urllib.request.Request(
            f"https://www.reddit.com{path_qs}", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


def _thumb(d: dict) -> Optional[str]:
    # prefer a real preview image over Reddit's "default"/"nsfw" placeholders
    prev = (d.get("preview") or {}).get("images") or []
    if prev:
        src = (prev[0].get("source") or {}).get("url")
        if src:
            return src.replace("&amp;", "&")
    t = d.get("thumbnail")
    return t if t and t.startswith("http") else None


def _duration(d: dict) -> Optional[float]:
    rv = ((d.get("media") or {}).get("reddit_video")
          or (d.get("secure_media") or {}).get("reddit_video") or {})
    dur = rv.get("duration")
    return float(dur) if dur else None


def _norm(d: dict) -> Optional[dict]:
    link = d.get("url_overridden_by_dest") or d.get("url")
    if not link or not _domain_ok(link):
        return None
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "url": link,
        "channel": f"r/{d.get('subreddit')}",
        "duration": _duration(d),
        "thumbnail": _thumb(d),
        "view_count": d.get("score"),   # upvotes as a popularity proxy
        "source": _source_of(link),     # so download + scoring treat it like its host
        "via": "reddit",
        "permalink": "https://www.reddit.com" + (d.get("permalink") or ""),
        "created_utc": d.get("created_utc"),
        "domain": d.get("domain"),
    }


def _time_filter(days: int) -> str:
    if not days or days <= 0:
        return "all"
    if days <= 2:
        return "day"
    if days <= 8:
        return "week"
    if days <= 32:
        return "month"
    if days <= 370:
        return "year"
    return "all"


def search(terms: str, days: int = 0, limit: int = 25, sort: str = "top") -> list[dict]:
    """Search the VALORANT clip subs for `terms`, newest-relevant first.

    `days` maps to Reddit's time window (top-of-week/month/year/all). Returns
    normalized result dicts for downloadable-video posts only.
    """
    terms = (terms or "").strip()
    q = urllib.parse.urlencode({
        "q": terms or "clip", "restrict_sr": 1, "sort": sort,
        "t": _time_filter(days), "limit": min(100, max(10, limit * 2)),
        "type": "link", "raw_json": 1,
    })
    data = _get(f"/r/{_SUBS}/search.json?{q}")
    out: list[dict] = []
    for child in ((data or {}).get("data") or {}).get("children") or []:
        d = child.get("data") or {}
        row = _norm(d)
        if row:
            out.append(row)
    return out[:limit]
