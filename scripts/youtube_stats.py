#!/usr/bin/env python3
"""Pull per-video analytics for your OWN YouTube channel via the YouTube
Analytics + Data APIs — the free, no-VidIQ-credits path.

One-time setup (see the Part A walkthrough): create a Google Cloud project,
enable "YouTube Data API v3" + "YouTube Analytics API", make a Desktop OAuth
client, and drop its JSON at secrets/client_secret.json.

First run opens a browser to authorize; the token is cached at
secrets/youtube_token.json so later runs are non-interactive.

    .venv/bin/python scripts/youtube_stats.py                # since channel start
    .venv/bin/python scripts/youtube_stats.py --from 2026-07-01
    .venv/bin/python scripts/youtube_stats.py --json         # also dump to outputs/

Analytics data lags ~2-3 days — "today" numbers are usually incomplete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRET = ROOT / "secrets" / "client_secret.json"
TOKEN = ROOT / "secrets" / "youtube_token.json"
OUT_DIR = ROOT / "outputs"

# Read-only: private analytics for owned channels + public video metadata.
SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Metrics the "video" dimension supports for an owned channel.
METRICS = [
    "views",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
    "subscribersGained",
    "likes",
    "comments",
]


def get_credentials() -> Credentials:
    if not CLIENT_SECRET.exists():
        sys.exit(
            f"Missing {CLIENT_SECRET}\n"
            "Download your Desktop OAuth client JSON from Google Cloud and save it there "
            "(see the setup walkthrough)."
        )
    creds: Credentials | None = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            # Opens a browser on this Mac; you click Allow once.
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
        print(f"✓ token cached at {TOKEN}", file=sys.stderr)
    return creds


def fmt_dur(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def list_uploads(yt) -> list[dict]:
    """Every video on the authorized channel with LIVE public counts.

    Walks the channel's uploads playlist (all uploads, incl. any the Analytics
    report omits), then batch-fetches public statistics — the near-real-time
    odometer numbers, ahead of the Analytics pipeline during a surge.
    """
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads_pl = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    ids: list[str] = []
    page = None
    while True:
        pl = yt.playlistItems().list(
            part="contentDetails", playlistId=uploads_pl,
            maxResults=50, pageToken=page,
        ).execute()
        ids += [it["contentDetails"]["videoId"] for it in pl.get("items", [])]
        page = pl.get("nextPageToken")
        if not page:
            break

    videos: list[dict] = []
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(
            part="snippet,statistics", id=",".join(ids[i : i + 50]),
        ).execute()
        for it in resp.get("items", []):
            s = it.get("statistics", {})
            videos.append({
                "videoId": it["id"],
                "title": it["snippet"]["title"],
                "published": it["snippet"]["publishedAt"][:10],
                "pubViews": int(s.get("viewCount", 0)),
                "pubLikes": int(s.get("likeCount", 0)),
                "pubComments": int(s.get("commentCount", 0)),
            })
    return videos


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="start", default="2024-04-07",
                   help="start date YYYY-MM-DD (default: channel creation)")
    p.add_argument("--to", dest="end",
                   default=dt.date.today().isoformat(),
                   help="end date YYYY-MM-DD (default: today)")
    p.add_argument("--json", action="store_true", help="also dump raw rows to outputs/")
    args = p.parse_args()

    creds = get_credentials()
    yta = build("youtubeAnalytics", "v2", credentials=creds)
    yt = build("youtube", "v3", credentials=creds)

    # 1) EVERY upload with LIVE public counts (near-real-time — this is the
    #    channel odometer, ahead of the Analytics pipeline during a surge).
    videos = list_uploads(yt)  # [{videoId,title,published,pubViews,pubLikes,pubComments}]

    # 2) Analytics report keyed by video (retention/watch time — lags ~2-3 days).
    report = yta.reports().query(
        ids="channel==MINE",
        startDate=args.start,
        endDate=args.end,
        metrics=",".join(METRICS),
        dimensions="video",
        sort="-views",
        maxResults=200,
    ).execute()
    headers = [h["name"] for h in report.get("columnHeaders", [])]
    col = {name: headers.index(name) for name in headers}
    analytics = {r[col["video"]]: r for r in report.get("rows", [])}

    # Merge, newest-surge-first by live public views.
    for v in videos:
        r = analytics.get(v["videoId"])
        v["anaViews"] = r[col["views"]] if r else 0
        v["watch"] = r[col["estimatedMinutesWatched"]] if r else 0
        v["avgpct"] = r[col["averageViewPercentage"]] if r else 0
        v["subs"] = r[col["subscribersGained"]] if r else 0
    videos.sort(key=lambda v: v["pubViews"], reverse=True)

    print(f"\nVALDaily — per-video  (public = live; analytics {args.start} → {args.end})\n")
    print(f"{'PubViews':>9}  {'AnaViews':>8}  {'Watch(min)':>10}  {'Ret%':>4}  "
          f"{'Likes':>6}  {'Cmts':>4}  {'Lag':>5}  Title")
    print("-" * 108)
    for v in videos:
        lag = v["pubViews"] - v["anaViews"]  # how far the odometer is ahead
        print(f"{v['pubViews']:>9}  {v['anaViews']:>8}  {v['watch']:>10}  "
              f"{v['avgpct']:>3.0f}%  {v['pubLikes']:>6}  {v['pubComments']:>4}  "
              f"{lag:>+5}  {v['title'][:46]}")

    if args.json:
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / f"youtube_stats_{args.start}_{args.end}.json"
        out.write_text(json.dumps(videos, indent=2))
        print(f"\n✓ raw rows → {out}")


if __name__ == "__main__":
    main()
