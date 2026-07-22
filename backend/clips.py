"""
Clip retrieval + ranking via yt-dlp.

Search YouTube (by creator + query) and a creator's top Twitch clips, then rank the
merged pool so short, self-contained highlights you can crop as-is into a vertical
short float to the top and long-form VODs/montages sink to the bottom (they are kept,
never dropped). Download a chosen result into storage/clips for the Auto-Layout tool.

This is a **Valorant** clip finder, so every search is anchored to the game: the query
is fanned out into a few highlight-biased variants (all carrying "valorant") that run
concurrently and merge into a wide pool (`_build_queries` + `_search_many`), and the
ranker gates the pool on Valorant evidence (`_VAL_LEX`) so off-topic matches — the band
"Clutch", MLB "best plays", a knife "one tap" tutorial — are demoted or dropped instead
of leading. See `_score()` for the full weighting.

Ranking is intentionally duration-dominated: the sweet spot is ~15-90s (one ace /
clutch / play). Already-vertical YouTube Shorts are demoted (the Auto-Layout tool
reflows *landscape* footage, so a Short is the wrong shape to crop).
"""

from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import yt_dlp

# --------------------------------------------------------------------------- #
# Ranking configuration
# --------------------------------------------------------------------------- #
IDEAL_MIN = 15.0     # below this a clip is usually too thin to stand on its own
IDEAL_MAX = 90.0     # 15-90s = crop-as-is sweet spot (top score)
SHORT_MAX = 90.0     # <= this  -> "short" tier (crop-ready)
MEDIUM_MAX = 600.0   # <= this  -> "medium" tier; above -> "long" (VODs, to the bottom)

# Title tokens that signal a self-contained, SINGLE-PLAY highlight (weight = strength).
# Note: "highlights"/"montage"/"edit" are deliberately NOT here — for a Shorts source
# searcher those words summon multi-clip music edits, which are the wrong material. A
# keeper is one continuous play ("ez ace", "HOSPITAL FLICK", "1v5 clutch"), so the
# single-play vocabulary is rewarded and compilation vocabulary is penalized (below).
_POSITIVE_KW = {
    "ace": 3, "clutch": 3, "1v5": 3, "1v4": 2, "1v3": 2, "1v2": 1,
    "flick": 2, "flicks": 2, "wallbang": 2, "200iq": 2, "1-tap": 2, "one tap": 2,
    "insane": 1, "unreal": 1, "unbelievable": 1, "crazy": 1, "nasty": 1, "clean": 1,
    "headshot": 1, "headshots": 1, "op": 1, "operator": 1, "sheriff": 1, "knife": 1,
    "clip": 1, "play": 1, "moment": 1, "outplay": 2, "pentakill": 2,
}
# Title tokens that signal long-form / compilation / non-croppable content (push to
# the bottom). Compilation words are strong negatives: a "highlights"/"montage"/"best
# of"/"top 10" upload is a stitched music edit, the #1 unusable category for this tool.
_NEGATIVE_KW = {
    "full match": 3, "full game": 3, "vod": 3, "watch party": 3, "watchparty": 3,
    "livestream": 3, "live stream": 3, "podcast": 3, "unboxing": 3, "tier list": 3,
    "tierlist": 3, "montage": 3, "frag movie": 3, "fragmovie": 3, "compilation": 3,
    "highlights": 2, "highlight reel": 3, "best of": 2, "top 10": 2, "top 5 plays": 2,
    "stream": 2, "episode": 2, "ep.": 2, "q&a": 2, "vlog": 2,
    "interview": 2, "tutorial": 2, "guide": 2, "how to": 2, "review": 2, "hours": 2,
    "reacts": 1, "reaction": 1, "live": 1,
}
_STOPWORDS = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for",
              "vs", "with", "valorant", "val", "gameplay"}

# --------------------------------------------------------------------------- #
# Valorant lexicon — used to (a) confirm a result is actually Valorant (the app
# is Valorant-only, but a bare "clutch" / "best plays" search drifts into music,
# sports, and movie trailers) and (b) reward on-topic hits in the ranker.
# Matched against the whitespace/number tokens of title+channel, so single-word
# entries are EXACT-token matches ("op" hits the word "op", never "operation").
# --------------------------------------------------------------------------- #
_VAL_TOKENS = {
    # game / ranks / ecosystem
    "valorant", "val", "riot", "vct", "radiant", "immortal", "ascendant",
    "spike", "ranked", "radianite", "episode", "act",
    # agents
    "jett", "reyna", "raze", "phoenix", "sage", "sova", "breach", "omen",
    "brimstone", "viper", "cypher", "killjoy", "skye", "yoru", "astra", "kayo",
    "chamber", "neon", "fade", "harbor", "gekko", "deadlock", "iso", "clove",
    "vyse", "tejo", "waylay",
    # weapons
    "vandal", "phantom", "operator", "sheriff", "guardian", "spectre", "judge",
    "odin", "ares", "marshal", "bulldog", "stinger", "ghost", "classic",
    "shorty", "frenzy", "bucky", "outlaw", "op",
    # maps
    "bind", "haven", "split", "ascent", "icebox", "breeze", "fracture", "pearl",
    "lotus", "sunset", "abyss", "corrode",
    # play terms distinctive to tac-shooters
    "ace", "clutch", "1v5", "1v4", "1v3", "1v2", "wallbang", "flick", "flicks",
    "eco", "retake", "defuse", "plant", "deagle", "aimbot", "aimlab",
    # orgs / pros (abbrevs are exact-token, so low false-positive)
    "sentinels", "sen", "fnatic", "fnc", "loud", "prx", "drx", "nrg", "c9",
    "cloud9", "100t", "eg", "geng", "t1", "kru", "mibr", "leviatan", "th",
    "heretics", "vitality", "tenz", "aspas", "demon1", "zekken", "yay", "derke",
    "alfajer", "chronicle", "boaster", "jinggg", "forsaken", "less", "something",
    "shroud", "tarik", "sinatraa", "wardell", "subroza", "kyedae",
}
# multi-word phrases that also signal Valorant (checked as substrings)
_VAL_PHRASES = ("paper rex", "evil geniuses", "one tap", "one-tap", "1 tap",
                "paper-rex", "team heretics")


# --------------------------------------------------------------------------- #
# yt-dlp option helpers
# --------------------------------------------------------------------------- #
def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }


def _flat_thumb(entry: dict) -> Optional[str]:
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        return thumbs[-1].get("url")
    return entry.get("thumbnail")


def _yt_thumb(entry: dict) -> Optional[str]:
    t = _flat_thumb(entry)
    if t:
        return t
    vid = entry.get("id")
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


def _duration_score(d: Optional[float]) -> float:
    """1.0 in the 15-90s sweet spot, decaying either side; long clips bottom out."""
    if not d or d <= 0:
        return 0.45  # unknown duration: neutral, neither rewarded nor buried
    if d < 5:
        return 0.15
    if d <= IDEAL_MIN:                                        # 5 -> 15s: ramp up
        return 0.60 + (d - 5) / (IDEAL_MIN - 5) * 0.40
    if d <= IDEAL_MAX:                                        # 15 -> 90s: sweet spot
        return 1.0
    if d <= 180:                                             # 90s -> 3m: gentle decay
        return 1.0 - (d - IDEAL_MAX) / (180 - IDEAL_MAX) * 0.30
    if d <= MEDIUM_MAX:                                      # 3m -> 10m
        return 0.70 - (d - 180) / (MEDIUM_MAX - 180) * 0.40
    return max(0.05, 0.30 - (d - MEDIUM_MAX) / 3600 * 0.20)  # 10m+: long tail


def _tier(d: Optional[float]) -> str:
    if not d or d <= 0:
        return "medium"
    if d <= SHORT_MAX:
        return "short"
    if d <= MEDIUM_MAX:
        return "medium"
    return "long"


def _keyword_scores(title: str) -> tuple[float, float]:
    """Return (positive, negative) keyword signals, each normalized to ~0..1."""
    t = (title or "").lower()
    pos = sum(w for kw, w in _POSITIVE_KW.items() if kw in t)
    neg = sum(w for kw, w in _NEGATIVE_KW.items() if kw in t)
    return min(pos, 5) / 5.0, min(neg, 5) / 5.0


def _relevance(title: str, q_tokens: list[str]) -> float:
    if not q_tokens:
        return 0.5
    t = (title or "").lower()
    hits = sum(1 for tok in q_tokens if tok in t)
    return hits / len(q_tokens)


def _popularity(views: Optional[int]) -> float:
    if not views or views <= 0:
        return 0.0
    return min(1.0, math.log10(views + 1) / 7.0)  # ~1e7 views -> 1.0


def _creator_match(entry: dict, creator: str) -> float:
    if not creator:
        return 0.0
    c = creator.lower()
    channel = (entry.get("channel") or "").lower()
    if c and channel and (c in channel or channel in c):
        return 1.0
    if c in (entry.get("title") or "").lower():
        return 0.5
    return 0.0


def _val_hits(text: str) -> int:
    """Count distinct Valorant-lexicon signals in `text` (title+channel)."""
    toks = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    n = len(toks & _VAL_TOKENS)
    low = (text or "").lower()
    n += sum(1 for p in _VAL_PHRASES if p in low)
    return n


def _valorant_score(entry: dict) -> float:
    """0..1 — how strongly the title/channel reads as Valorant (3+ hits = full)."""
    hay = f"{entry.get('title') or ''} {entry.get('channel') or ''}"
    return min(_val_hits(hay), 3) / 3.0


def _is_valorant(entry: dict, creator: str) -> bool:
    """Gate: is there ANY evidence this is Valorant content? Twitch clips are
    creator-scoped to our (Valorant) roster, so they always pass; otherwise we
    need a lexicon hit or a creator match, else it's off-topic drift."""
    if entry.get("source") == "twitch":
        return True
    if _creator_match(entry, creator) > 0:
        return True
    return _val_hits(f"{entry.get('title') or ''} {entry.get('channel') or ''}") > 0


def _score(entry: dict, q_tokens: list[str], creator: str) -> float:
    """Weighted blend; duration dominates so short croppable clips lead, but
    Valorant relevance is a first-class term so on-topic clips beat off-topic
    ones even when the off-topic title happens to match the query words."""
    dur = _duration_score(entry.get("duration"))
    rel = _relevance(entry.get("title"), q_tokens)
    pos, neg = _keyword_scores(entry.get("title"))
    pop = _popularity(entry.get("view_count"))
    cre = _creator_match(entry, creator)
    val = _valorant_score(entry)
    score = (0.42 * dur + 0.15 * rel + 0.10 * pos + 0.10 * pop + 0.07 * cre
             + 0.16 * val) - 0.32 * neg
    # Twitch clips are short by construction and always carry the streamer's facecam,
    # i.e. exactly the crop-as-is material we want — nudge them up a touch.
    if entry.get("source") == "twitch":
        score += 0.05
    # Already-vertical Shorts are the wrong shape for the landscape→9:16 reflow.
    if entry.get("is_short_form"):
        score -= 0.22
    return score


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _is_short_form(e: dict) -> bool:
    """A YouTube Short (already 9:16). Detect from the /shorts/ URL, and as a
    fallback from a very short duration paired with a portrait aspect ratio when
    flat extraction reports it. Shorts can't be reflowed by the Auto-Layout tool."""
    url = (e.get("url") or e.get("webpage_url") or "").lower()
    if "/shorts/" in url:
        return True
    ar = e.get("aspect_ratio")
    dur = e.get("duration") or 0
    if ar and ar < 1.0 and 0 < dur <= 60:   # portrait + short = almost certainly a Short
        return True
    return False


def _norm_youtube(e: dict) -> dict:
    vid = e.get("id")
    return {
        "id": vid,
        "title": e.get("title"),
        "url": e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
        "channel": e.get("channel") or e.get("uploader"),
        "duration": e.get("duration"),
        "thumbnail": _yt_thumb(e),
        "view_count": e.get("view_count"),
        "source": "youtube",
        "is_short_form": _is_short_form(e),
    }


def _norm_twitch(e: dict, creator: str) -> dict:
    return {
        "id": e.get("id"),
        "title": e.get("title") or "(untitled clip)",
        "url": e.get("url"),
        "channel": e.get("channel") or e.get("uploader") or creator,
        "duration": e.get("duration"),
        "thumbnail": _flat_thumb(e),
        "view_count": e.get("view_count"),
        "source": "twitch",
    }


def _search_youtube(terms: str, n: int) -> list[dict]:
    opts = _base_opts() | {"extract_flat": "in_playlist"}
    out: list[dict] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{terms}", download=False)
        for e in (info or {}).get("entries", []) or []:
            if e:
                out.append(_norm_youtube(e))
    return out


def _search_twitch_clips(creator: str, n: int) -> list[dict]:
    """A creator's top all-time Twitch clips (short, facecam-present highlights)."""
    slug = re.sub(r"[^a-z0-9_]", "", creator.lower())
    if not slug:
        return []
    url = f"https://www.twitch.tv/{slug}/clips?filter=clips&range=all"
    opts = _base_opts() | {"extract_flat": "in_playlist", "playlistend": max(n, 12)}
    out: list[dict] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        for e in (info or {}).get("entries", []) or []:
            if e:
                out.append(_norm_twitch(e, creator))
    return out


def _has_valorant(text: str) -> bool:
    low = (text or "").lower()
    return "valorant" in low or bool(re.search(r"\bval\b", low))


def _build_queries(query: str, creator: str) -> list[str]:
    """Fan one request out into a few highlight-biased search strings, all anchored
    to Valorant. A single ytsearch is both narrow (thin pool) and prone to drift
    (a bare "clutch" finds the band); several targeted variants fix both.

    - Always anchor with "valorant" unless the user already typed it.
    - A bare creator (no query) → pull their highlights / clutches / aces.
    - A bare query (no creator) → the query + a "highlights" variant.
    """
    query = (query or "").strip()
    creator = (creator or "").strip()
    anchor = "" if (_has_valorant(query) or _has_valorant(creator)) else "valorant"

    def _join(*parts: str) -> str:
        return " ".join(p for p in parts if p).strip()

    queries = [_join(creator, query, anchor)]
    if creator and not query:
        for hint in ("highlights", "clutch", "ace"):
            queries.append(_join(creator, anchor, hint))
    elif query and not creator:
        queries.append(_join(query, anchor, "highlights"))
    elif query and creator:
        queries.append(_join(creator, query, anchor, "highlights"))

    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            out.append(q)
    return out[:4]


def _search_many(queries: list[str], per: int) -> list[dict]:
    """Run several ytsearch queries concurrently and merge (each yt-dlp instance is
    thread-local, so this is safe). Failures in one query don't sink the rest."""
    out: list[dict] = []
    if not queries:
        return out
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as ex:
        futs = [ex.submit(_search_youtube, q, per) for q in queries]
        for f in futs:
            try:
                out += f.result()
            except Exception:
                pass
    return out


# --------------------------------------------------------------------------- #
# Public search
# --------------------------------------------------------------------------- #
def search(query: str, creator: str = "", limit: int = 16,
           include_twitch: bool = True) -> list[dict]:
    """
    Return ranked, deduped metadata (no download).

    Fetches a larger candidate pool than `limit`, scores it, and returns the best
    short/medium clips first followed by a short tail of long-form results (kept so
    montages/VODs are never lost, just pushed to the bottom). Each result carries
    `score`, `tier` ("short"|"medium"|"long") and `source` ("youtube"|"twitch").
    """
    query = (query or "").strip()
    creator = (creator or "").strip()
    if not query and not creator:
        return []
    q_tokens = _tokens(query) or _tokens(creator)

    pool: list[dict] = []

    # YouTube: fan the request into a few Valorant-anchored, highlight-biased queries
    # and run them concurrently so the ranker works over a wide, on-topic pool.
    queries = _build_queries(query, creator)
    per = min(30, max(limit * 2, 20))
    try:
        pool += _search_many(queries, per)
    except Exception:
        pass  # fall through — Twitch may still return something

    # Twitch is creator-scoped (there is no free-text clip search via yt-dlp).
    if include_twitch and creator:
        try:
            pool += _search_twitch_clips(creator, limit)
        except Exception:
            pass

    # Dedup (a clip can surface from more than one query, or from search + channel).
    seen: set[str] = set()
    uniq: list[dict] = []
    for e in pool:
        key = e.get("url") or f"{e.get('source')}:{e.get('id')}"
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(e)

    for e in uniq:
        e["score"] = round(_score(e, q_tokens, creator), 4)
        e["tier"] = _tier(e.get("duration"))
        e["is_valorant"] = _is_valorant(e, creator)

    # Split off clearly off-topic results (the band "Clutch", MLB "best plays", …).
    # They're never dropped outright, but they sit below every on-topic result and
    # only surface as a backfill when the on-topic pool is thin.
    on = [e for e in uniq if e["is_valorant"]]
    off = [e for e in uniq if not e["is_valorant"]]

    def _rank(items: list[dict]) -> tuple[list[dict], list[dict]]:
        crop = sorted((e for e in items if e["tier"] != "long"),
                      key=lambda e: e["score"], reverse=True)
        longs = sorted((e for e in items if e["tier"] == "long"),
                       key=lambda e: e["score"], reverse=True)
        return crop, longs

    crop_on, long_on = _rank(on)
    crop_off, long_off = _rank(off)

    # On-topic croppable first, then a short on-topic long-form tail, then — only if
    # we're short of `limit` — a small off-topic backfill (croppable before long).
    ordered = crop_on[:limit] + long_on[:max(2, limit // 4)]
    if len(crop_on) < limit:
        need = limit - len(crop_on)
        ordered += (crop_off + long_off)[:need]
    return ordered


def download(url: str, dest_dir: str, max_height: int = 1080,
             start: Optional[float] = None, end: Optional[float] = None) -> dict:
    """
    Download a single video (YouTube or Twitch clip) into dest_dir.

    When `start`/`end` (source seconds) describe a valid span, only that section is
    fetched — handy for grabbing a 5-minute moment out of a 15-minute VOD instead of
    the whole thing. `force_keyframes_at_cuts` re-encodes around the boundaries so the
    section starts/ends cleanly. Returns {path, title, id}.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"bestvideo[height<=?{max_height}]+bestaudio/best[height<=?{max_height}]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
    }

    # Partial download: fetch only [start, end] when a valid span is given.
    if start is not None and end is not None and end > (start or 0):
        from yt_dlp.utils import download_range_func
        s = max(0.0, float(start))
        opts["download_ranges"] = download_range_func(None, [(s, float(end))])
        opts["force_keyframes_at_cuts"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        # after merge the extension may be normalized to mp4
        p = Path(path)
        if not p.exists():
            mp4 = p.with_suffix(".mp4")
            if mp4.exists():
                p = mp4
        return {"path": str(p), "title": info.get("title"), "id": info.get("id")}


# --------------------------------------------------------------------------- #
# Channel browse — the "Nb1 grabber" source
# --------------------------------------------------------------------------- #
# The compilation channel this BETA is built around: "Im_Nb1 أمين". It samples
# streamer reactions to the same clip into 7-10 min montages; the workflow is to
# browse his uploads and grab just the moment (or one reaction) you want.
# Parameterized (channel_id) so the phase-2 "straight from the source" streamer
# lookup can reuse the same list/grab plumbing against a streamer's own VODs.
NB1_CHANNEL_ID = "UCK16a1sUUqribbZfyHAYeyg"
NB1_CHANNEL_NAME = "Im_Nb1"

_CHANNEL_TTL = 600.0  # seconds — flat listing is cheap but we page/sort over it a lot
_CHANNEL_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _fetch_channel(channel_id: str) -> list[dict]:
    """Flat-list a channel's uploads (newest first). No dates in flat mode — YouTube
    returns the /videos tab in reverse-chronological order, so list order *is* recency
    (see enrich_dates() to fill exact upload dates lazily for a visible page)."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "playlistend": 300,
    }
    out: list[dict] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        for i, e in enumerate((info or {}).get("entries", []) or []):
            if not e:
                continue
            vid = e.get("id")
            out.append({
                "id": vid,
                "title": e.get("title"),
                "url": e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
                "duration": e.get("duration"),
                "view_count": e.get("view_count"),
                "thumbnail": _yt_thumb(e),
                "index": i,  # channel order == recency (0 = newest)
            })
    return out


def list_channel(channel_id: str = NB1_CHANNEL_ID, limit: int = 30, offset: int = 0,
                 sort: str = "recent", query: str = "", force: bool = False) -> dict:
    """
    Browse a channel's uploads for the grabber (cached ~10 min).

    `sort`: "recent" (channel order, newest first) | "views" | "longest" | "shortest".
    `query`: case-insensitive title substring filter (the montage titles name the
    play + streamers, e.g. "...NS Dambi CRAZY 4k...", so this doubles as a play search).
    Returns {channel, total, offset, limit, sort, videos:[...]} — `videos` is one page.
    """
    now = time.time()
    cached = _CHANNEL_CACHE.get(channel_id)
    if force or not cached or (now - cached[0]) > _CHANNEL_TTL:
        entries = _fetch_channel(channel_id)
        _CHANNEL_CACHE[channel_id] = (now, entries)
    entries = _CHANNEL_CACHE[channel_id][1]

    rows = list(entries)
    q = (query or "").strip().lower()
    if q:
        rows = [e for e in rows if q in (e.get("title") or "").lower()]

    if sort == "views":
        rows.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    elif sort == "longest":
        rows.sort(key=lambda e: e.get("duration") or 0, reverse=True)
    elif sort == "shortest":
        rows.sort(key=lambda e: e.get("duration") if e.get("duration") else 1e9)
    # "recent" keeps channel order (already newest-first).

    total = len(rows)
    offset = max(0, int(offset))
    page = rows[offset:offset + max(1, int(limit))]
    return {"channel": channel_id, "total": total, "offset": offset,
            "limit": limit, "sort": sort, "videos": page}


def resolve_stream(url: str, max_height: int = 720) -> dict:
    """
    Resolve a directly-playable *progressive* (single-file, audio+video) URL so a
    browser <video> can scrub the source without downloading it first.

    Progressive YouTube formats top out ~720p — plenty for setting in/out points; the
    actual grab (download()) still pulls up to 1080p independently, so preview quality
    never limits the grabbed clip. Returns {url, duration, title, id}.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        # progressive-only (has both streams in one file) so it plays in a bare <video>.
        # The last two fallbacks are for sources like Twitch clips, whose progressive
        # mp4 formats DON'T report acodec/vcodec in metadata (both come back None) —
        # the codec-requiring selectors above reject them even though they play fine,
        # so fall back to mp4-by-height / plain best.
        "format": (f"best[ext=mp4][acodec!=none][vcodec!=none][height<=?{max_height}]"
                   f"/best[acodec!=none][vcodec!=none][height<=?{max_height}]"
                   f"/best[acodec!=none][vcodec!=none]"
                   f"/best[ext=mp4][height<=?{max_height}]"
                   f"/best[ext=mp4]/best[height<=?{max_height}]/best"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {"url": info.get("url"), "duration": info.get("duration"),
                "title": info.get("title"), "id": info.get("id")}


# --- phase 2 (not wired): "straight from the source" streamer-reaction lookup -----
# The montage titles already name the streamers/orgs ("NS Dambi", "TH benjyfishy").
# Assisted lookup will resolve a streamer handle -> list_channel(their Twitch/YT VODs)
# and reuse resolve_stream()/download() with the SAME grab panel. Auto-discovery
# (identify which streamers appear in a given Nb1 clip + locate the moment in their
# VODs) is a later step. Left as a documented seam, intentionally unimplemented.
def find_source(title: str) -> list[dict]:  # noqa: D401  (phase-2 stub)
    raise NotImplementedError("phase 2: streamer-source lookup")
