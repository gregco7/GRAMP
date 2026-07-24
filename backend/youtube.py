"""
YouTube channel overview — live stats for the channel-overview tabs.

Pure YouTube Data API v3 + YouTube Analytics API v2 (NOT vidiq). Auth is a Desktop
OAuth client (config.YT_CLIENT_SECRET) with a cached, refreshable token
(config.YT_TOKEN, scopes youtube.readonly + yt-analytics.readonly). The token
already carries a refresh_token, so the backend refreshes silently — no user
sign-in at request time.

overview() returns one bundle the frontend renders directly: channel snapshot
(subs/views/videos + period deltas), a daily views/watch-time/subs series for the
growth chart, the most recent uploads, and the top videos by views (with
likes/comments engagement). Results are cached ~10 min to stay quota-friendly.

Channels are mapped by niche id in CHANNELS. "mine" resolves to the authorized
account's own channel (so owner Analytics works). Niches with no channel return
{configured: false} and the tab shows an empty placeholder.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from . import config

# niche id -> channel selector. "mine" == the authorized owner channel.
CHANNELS = {"valorant": "mine"}

# Read-only: private analytics for owned channels + public metadata.
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

_TTL = 600  # seconds
_cache: dict[tuple, tuple[float, dict]] = {}


def connect() -> dict:
    """Run the Desktop OAuth InstalledAppFlow to mint the cached token — opens a
    browser on the local machine, the user clicks Allow once. Requires
    client_secret.json to be present. Used by the in-app Settings screen."""
    if not config.YT_CLIENT_SECRET.exists():
        return {"ok": False, "error": "client_secret.json missing — add your Google "
                "Desktop OAuth client to secrets/client_secret.json first."}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(str(config.YT_CLIENT_SECRET), SCOPES)
        creds = flow.run_local_server(port=0)
        config.YT_TOKEN.write_text(creds.to_json())
        _cache.clear()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _creds():
    """Load + refresh the cached OAuth credentials, or None if not authorized."""
    if not config.YT_TOKEN.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(config.YT_TOKEN))
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                config.YT_TOKEN.write_text(creds.to_json())
            else:
                return None
        return creds
    except Exception:
        return None


def status() -> dict:
    """Whether the overview can pull live data (used to show an auth prompt)."""
    return {"authorized": _creds() is not None,
            "client_configured": config.YT_CLIENT_SECRET.exists()}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iso_seconds(iso: str) -> int:
    """PT#H#M#S -> total seconds (video durations)."""
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _thumb(snippet: dict) -> str:
    t = (snippet or {}).get("thumbnails", {})
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in t:
            return t[k]["url"]
    return ""


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
def overview(niche: str, days: int = 28, force: bool = False) -> dict:
    if niche not in CHANNELS:
        return {"configured": False, "niche": niche}

    key = (niche, days)
    now = time.time()
    if not force and key in _cache and now - _cache[key][0] < _TTL:
        return _cache[key][1]

    creds = _creds()
    if creds is None:
        data = {"configured": True, "authorized": False, "niche": niche,
                "client_configured": config.YT_CLIENT_SECRET.exists()}
        return data

    try:
        data = _build_overview(creds, niche, days)
    except Exception as e:
        return {"configured": True, "authorized": True, "niche": niche,
                "error": str(e)[:300]}

    data["fetched_at"] = int(now)
    _cache[key] = (now, data)
    return data


def _build_overview(creds, niche: str, days: int) -> dict:
    from googleapiclient.discovery import build
    sel = CHANNELS[niche]
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)

    # --- channel snapshot ---------------------------------------------------
    kw = {"mine": True} if sel == "mine" else {"id": sel}
    ch = yt.channels().list(part="snippet,statistics,contentDetails", **kw).execute()
    if not ch.get("items"):
        return {"configured": True, "authorized": True, "niche": niche,
                "error": "channel not found"}
    it = ch["items"][0]
    stats = it.get("statistics", {})
    snip = it.get("snippet", {})
    uploads = it["contentDetails"]["relatedPlaylists"]["uploads"]
    channel = {
        "id": it["id"],
        "title": snip.get("title", ""),
        "handle": snip.get("customUrl", ""),
        "thumb": _thumb(snip),
        "subscribers": _int(stats.get("subscriberCount")),
        "views": _int(stats.get("viewCount")),
        "videos": _int(stats.get("videoCount")),
    }

    # --- all uploads (this channel is small) + per-video stats --------------
    vids = _channel_videos(yt, uploads, limit=50)
    recent = sorted(vids, key=lambda v: v["published_at"], reverse=True)[:8]
    top = sorted(vids, key=lambda v: v["views"], reverse=True)[:6]

    # --- analytics: current + previous window for deltas & the chart --------
    series, totals, prev_totals = _analytics(creds, days) if sel == "mine" else ([], {}, {})

    return {
        "configured": True, "authorized": True, "niche": niche, "days": days,
        "channel": channel,
        "recent": recent,
        "top": top,
        "series": series,          # [{date, views, minutes, subs}] over the current window
        "totals": totals,          # summed over current window
        "prev_totals": prev_totals,  # summed over the previous window (for % change)
    }


def _channel_videos(yt, uploads: str, limit: int = 50) -> list[dict]:
    ids: list[str] = []
    token = None
    while len(ids) < limit:
        pl = yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                     maxResults=min(50, limit - len(ids)),
                                     pageToken=token).execute()
        ids += [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        token = pl.get("nextPageToken")
        if not token:
            break

    out: list[dict] = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        vr = yt.videos().list(part="snippet,statistics,contentDetails",
                              id=",".join(batch)).execute()
        for v in vr.get("items", []):
            s = v.get("snippet", {})
            st = v.get("statistics", {})
            out.append({
                "id": v["id"],
                "title": s.get("title", ""),
                "thumb": _thumb(s),
                "published_at": s.get("publishedAt", ""),
                "duration": _iso_seconds(v.get("contentDetails", {}).get("duration", "")),
                "views": _int(st.get("viewCount")),
                "likes": _int(st.get("likeCount")),
                "comments": _int(st.get("commentCount")),
                "url": f"https://www.youtube.com/watch?v={v['id']}",
            })
    return out


def _analytics(creds, days: int) -> tuple[list, dict, dict]:
    """Daily views/watch-time/subs for the current window plus totals for the
    current and previous windows (one API call over a 2*days span, split in half)."""
    from googleapiclient.discovery import build
    ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    end = date.today()
    cur_start = end - timedelta(days=days - 1)
    span_start = cur_start - timedelta(days=days)  # previous window start
    rep = ya.reports().query(
        ids="channel==MINE",
        startDate=span_start.isoformat(), endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched,subscribersGained,likes,comments",
        dimensions="day", sort="day",
    ).execute()

    rows = rep.get("rows", [])
    series, totals, prev = [], _blank_totals(), _blank_totals()
    cur_iso = cur_start.isoformat()
    for r in rows:
        d, views, minutes, subs, likes, comments = r[0], r[1], r[2], r[3], r[4], r[5]
        bucket = totals if d >= cur_iso else prev
        bucket["views"] += views
        bucket["minutes"] += minutes
        bucket["subs"] += subs
        bucket["likes"] += likes
        bucket["comments"] += comments
        if d >= cur_iso:
            series.append({"date": d, "views": views, "minutes": minutes, "subs": subs})
    return series, totals, prev


def _blank_totals() -> dict:
    return {"views": 0, "minutes": 0, "subs": 0, "likes": 0, "comments": 0}
