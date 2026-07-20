"""
The Experimental search engine — "a more powerful search on a new plane."

Pipeline (each stage is best-effort and degrades if a dependency is down):

  1. PARSE     natural-language query -> structured intent (aibrain, LLM) with a
               rule-based fallback (_rule_parse). Explicit UI filters override it.
  2. FAN OUT   query YouTube (clips.py: multi-query, Valorant-gated), Twitch Helix
               (twitch.py: game-wide / per-streamer top clips), and Reddit
               (reddit.py) concurrently; merge + dedup.
  3. PRESCORE  rank the pool on metadata (duration sweet-spot + Valorant relevance
               + intent-filter matches: agent/map/weapon/org, recency, views,
               duration cap). Cheap, deterministic.
  4. VERIFY    for the top N, sample a few real frames and run vision (aibrain)
               concurrently: is it clean landscape gameplay? facecam? edited /
               center-clutter? which agent/map/weapon? This is the "is it actually
               clippable" signal the metadata can't give.
  5. RANK      fuse prescore + vision into a final clippability score, attach a
               human "why", and return rich result cards.

Runs behind a background job (main.py) because the LLM + frame sampling take
~30-90s; `deep_search(..., progress=cb)` reports stage/percent as it goes.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from . import aibrain, clips, framegrab, reddit, twitch

# --------------------------------------------------------------------------- #
# Canonical VALORANT vocab — for the rule-based parser + filter matching.
# (Title-case canonical form -> matched case-insensitively against titles.)
# --------------------------------------------------------------------------- #
AGENTS = ["Jett", "Reyna", "Raze", "Phoenix", "Sage", "Sova", "Breach", "Omen",
          "Brimstone", "Viper", "Cypher", "Killjoy", "Skye", "Yoru", "Astra",
          "KAY/O", "Kayo", "Chamber", "Neon", "Fade", "Harbor", "Gekko",
          "Deadlock", "Iso", "Clove", "Vyse", "Tejo", "Waylay"]
MAPS = ["Bind", "Haven", "Split", "Ascent", "Icebox", "Breeze", "Fracture",
        "Pearl", "Lotus", "Sunset", "Abyss", "Corrode"]
WEAPONS = ["Vandal", "Phantom", "Operator", "Sheriff", "Guardian", "Spectre",
           "Judge", "Odin", "Ares", "Marshal", "Bulldog", "Stinger", "Ghost",
           "Classic", "Shorty", "Frenzy", "Bucky", "Outlaw"]
ORGS = ["Sentinels", "Fnatic", "LOUD", "Paper Rex", "PRX", "DRX", "NRG", "C9",
        "Cloud9", "100 Thieves", "100T", "EG", "Evil Geniuses", "Gen.G", "T1",
        "KRÜ", "MIBR", "Leviatan", "Team Heretics", "Vitality", "G2", "TL"]
PLAYS = ["ace", "clutch", "1v5", "1v4", "1v3", "1v2", "flick", "flicks",
         "wallbang", "one tap", "1 tap", "headshot", "pentakill", "outplay",
         "reverse ace", "no scope", "operator ace"]

_PLAY_RE = {p: re.compile(r"\b" + re.escape(p) + r"\b", re.I) for p in PLAYS}


def _match_terms(title: str, vocab: list[str]) -> list[str]:
    t = (title or "").lower()
    return [v for v in vocab if re.search(r"\b" + re.escape(v.lower()) + r"\b", t)]


# --------------------------------------------------------------------------- #
# Rule-based intent (fallback when the LLM is off / unavailable)
# --------------------------------------------------------------------------- #
def _rule_parse(query: str) -> dict:
    q = (query or "").strip()
    low = q.lower()
    days = 0
    if re.search(r"\b(today|now)\b", low):
        days = 2
    elif re.search(r"\b(this week|lately|recent)\b", low):
        days = 30
    elif re.search(r"\b(this month)\b", low):
        days = 31
    elif re.search(r"\b(this year|2026|2025)\b", low):
        days = 365
    return {
        "creator": "",
        "agents": _match_terms(q, AGENTS),
        "maps": _match_terms(q, MAPS),
        "weapons": _match_terms(q, WEAPONS),
        "orgs": _match_terms(q, ORGS),
        "plays": [p for p in PLAYS if _PLAY_RE[p].search(q)],
        "keywords": [],
        "recency_days": days,
        "min_views": 0,
        "duration_max": 0,
        "sources": [],
        "want_facecam": bool(re.search(r"\bfacecam\b", low)),
        "pro_play": bool(re.search(r"\b(vct|champions|masters|pro)\b", low)),
        "search_terms": q,
    }


def _merge_filters(intent: dict, filters: Optional[dict]) -> dict:
    """Explicit UI filters win over parsed intent (list filters are unioned)."""
    intent = dict(intent or {})
    if not filters:
        return intent
    for k in ("agents", "maps", "weapons", "orgs", "plays", "keywords", "sources"):
        extra = filters.get(k)
        if extra:
            base = intent.get(k) or []
            seen = {x.lower() for x in base}
            intent[k] = base + [x for x in extra if x.lower() not in seen]
    for k in ("creator", "search_terms"):
        if filters.get(k):
            intent[k] = filters[k]
    for k in ("recency_days", "min_views", "duration_max"):
        if filters.get(k):
            intent[k] = int(filters[k])
    if filters.get("want_facecam") is not None:
        intent["want_facecam"] = bool(filters["want_facecam"])
    return intent


# --------------------------------------------------------------------------- #
# Source fan-out
# --------------------------------------------------------------------------- #
def _yt_query(intent: dict) -> str:
    parts = [intent.get("search_terms") or "",
             " ".join(intent.get("plays") or []),
             " ".join(intent.get("agents") or []),
             " ".join(intent.get("maps") or []),
             " ".join(intent.get("weapons") or []),
             " ".join(intent.get("orgs") or [])]
    q = " ".join(p for p in parts if p).strip()
    return q or "valorant highlights"


def _fetch_youtube(intent: dict, limit: int) -> list[dict]:
    try:
        return clips.search(_yt_query(intent), creator=intent.get("creator", ""),
                            limit=limit, include_twitch=False)
    except Exception:
        return []


def _fetch_twitch(intent: dict, limit: int) -> list[dict]:
    if not twitch.available():
        return []
    play = (intent.get("plays") or [""])[0]
    days = intent.get("recency_days") or 90
    try:
        return twitch.search_clips(query=play, creator=intent.get("creator", ""),
                                   days=days, limit=limit)
    except Exception:
        return []


def _fetch_reddit(intent: dict, limit: int) -> list[dict]:
    terms = _yt_query(intent)
    try:
        return reddit.search(terms, days=intent.get("recency_days") or 0, limit=limit)
    except Exception:
        return []


def _gather(intent: dict, sources: list[str], per_source: int) -> list[dict]:
    jobs = {}
    if "youtube" in sources:
        jobs["youtube"] = lambda: _fetch_youtube(intent, per_source)
    if "twitch" in sources:
        jobs["twitch"] = lambda: _fetch_twitch(intent, per_source)
    if "reddit" in sources:
        jobs["reddit"] = lambda: _fetch_reddit(intent, per_source)
    pool: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for f in as_completed(futs):
            try:
                pool += f.result() or []
            except Exception:
                pass
    return pool


def _dedup(pool: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for e in pool:
        key = e.get("url") or f"{e.get('source')}:{e.get('id')}"
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Prescore (metadata only)
# --------------------------------------------------------------------------- #
def _prescore(e: dict, intent: dict) -> float:
    title = e.get("title") or ""
    base = clips._duration_score(e.get("duration")) * 0.42
    base += clips._valorant_score(e) * 0.18
    pos, neg = clips._keyword_scores(title)
    base += 0.08 * pos - 0.22 * neg
    base += clips._popularity(e.get("view_count")) * 0.08
    if e.get("source") == "twitch":
        base += 0.06   # raw, facecam-in, one play
    if e.get("is_short_form"):
        base -= 0.20

    # intent-filter matches (each hit is on-topic evidence)
    hit = 0
    for vocab_key in ("agents", "maps", "weapons", "orgs"):
        want = intent.get(vocab_key) or []
        if want and _match_terms(title, want):
            hit += 1
    for p in intent.get("plays") or []:
        if p in _PLAY_RE and _PLAY_RE[p].search(title):
            hit += 1
            break
    base += min(hit, 3) * 0.05

    cre = intent.get("creator") or ""
    if cre and cre.lower() in (e.get("channel") or "").lower():
        base += 0.06
    return base


def _passes_hard_filters(e: dict, intent: dict) -> bool:
    mv = intent.get("min_views") or 0
    if mv and (e.get("view_count") or 0) < mv:
        return False
    dm = intent.get("duration_max") or 0
    d = e.get("duration") or 0
    if dm and d and d > dm:
        return False
    return True


# --------------------------------------------------------------------------- #
# Vision verify (top N, concurrent)
# --------------------------------------------------------------------------- #
def _verify_one(e: dict) -> dict:
    frames, work = framegrab.sample_frames(e.get("url"), e.get("duration"), n=3)
    try:
        verdict = aibrain.analyze_clip(frames, title=e.get("title") or "")
    finally:
        framegrab.cleanup(work)
    return verdict


def _apply_vision(e: dict, v: dict) -> float:
    """Fuse a vision verdict into the clip's score and enrich its metadata."""
    e["vision"] = v
    if not v.get("analyzed"):
        return e.get("score", 0.0)
    s = e.get("score", 0.0)
    if not v.get("is_valorant_gameplay", True):
        s -= 0.60
    if v.get("clippable"):
        s += 0.18
    if v.get("center_clutter"):
        s -= 0.15
    if v.get("edited"):
        s -= 0.12
    if v.get("facecam"):
        e["facecam"] = True
        s += 0.05
    # fill missing metadata from the HUD read
    for k in ("agent", "map", "weapon"):
        if v.get(k) and not e.get(k):
            e[k] = v[k]
    return s


# --------------------------------------------------------------------------- #
# Reasons
# --------------------------------------------------------------------------- #
def _reason(e: dict, intent: dict) -> str:
    bits = []
    d = e.get("duration")
    if d:
        bits.append("crop-ready length" if 12 <= d <= 90 else f"{int(d)}s")
    v = e.get("vision") or {}
    if v.get("analyzed"):
        if v.get("clippable"):
            bits.append("clean gameplay")
        if v.get("facecam"):
            bits.append(f"facecam {v.get('facecam_corner') or 'present'}")
        if v.get("edited"):
            bits.append("⚠ edited")
        if v.get("center_clutter"):
            bits.append("⚠ center overlay")
        who = " ".join(x for x in (v.get("agent"), v.get("map")) if x)
        if who:
            bits.append(who)
    else:
        matched = []
        for k in ("agents", "maps", "weapons", "orgs"):
            matched += _match_terms(e.get("title") or "", intent.get(k) or [])
        if matched:
            bits.append(" ".join(matched[:2]))
    if e.get("source") == "twitch":
        bits.append("raw Twitch clip")
    return " · ".join(dict.fromkeys(bits))  # dedup, keep order


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #
def deep_search(query: str, filters: Optional[dict] = None,
                opts: Optional[dict] = None,
                progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """Run the full pipeline. `progress(pct, msg)` is called as stages complete.
    Returns {intent, results, meta}."""
    opts = opts or {}
    use_ai = opts.get("use_ai_parse", True) and aibrain.available()
    use_vision = opts.get("use_vision", True) and aibrain.available()
    vision_count = int(opts.get("vision_count", 8))
    limit = int(opts.get("limit", 20))
    per_source = int(opts.get("per_source", 24))
    sources = opts.get("sources") or ["youtube", "twitch", "reddit"]

    def _p(pct, msg):
        if progress:
            try:
                progress(pct, msg)
            except Exception:
                pass

    # 1) PARSE
    _p(5, "understanding your query…")
    intent = (parse := (aibrain.parse_query(query) if use_ai else None)) or _rule_parse(query)
    intent = _merge_filters(intent, filters)
    # honor an explicit source subset from the parsed intent, if the UI didn't set one
    if intent.get("sources") and not (filters or {}).get("sources"):
        want = [s for s in sources if s in intent["sources"]]
        if want:
            sources = want
    sources = [s for s in sources if s in ("youtube", "twitch", "reddit")]

    # 2) FAN OUT
    _p(20, f"searching {', '.join(sources)}…")
    pool = _dedup(_gather(intent, sources, per_source))

    # 3) PRESCORE + hard filters
    _p(45, f"ranking {len(pool)} candidates…")
    pool = [e for e in pool if _passes_hard_filters(e, intent)]
    for e in pool:
        e["score"] = round(_prescore(e, intent), 4)
        e["tier"] = clips._tier(e.get("duration"))
    pool.sort(key=lambda e: e["score"], reverse=True)

    # 4) VISION VERIFY (top N)
    verified = 0
    if use_vision and pool:
        top = pool[:vision_count]
        _p(55, f"watching {len(top)} clips (frames + AI)…")
        with ThreadPoolExecutor(max_workers=min(5, len(top))) as ex:
            futs = {ex.submit(_verify_one, e): e for e in top}
            done = 0
            for f in as_completed(futs):
                e = futs[f]
                try:
                    v = f.result()
                except Exception:
                    v = {"analyzed": False}
                e["score"] = round(_apply_vision(e, v), 4)
                done += 1
                verified += 1 if v.get("analyzed") else 0
                _p(55 + int(35 * done / len(top)), f"verified {done}/{len(top)} clips…")
        pool.sort(key=lambda e: e["score"], reverse=True)

    # 5) FINALIZE
    _p(95, "finalizing…")
    results = []
    for e in pool[:limit]:
        e["reason"] = _reason(e, intent)
        results.append(e)
    _p(100, "done")

    return {
        "intent": intent,
        "results": results,
        "meta": {
            "pool": len(pool),
            "sources": sources,
            "ai_parse": bool(parse),
            "vision_verified": verified,
            "twitch_available": twitch.available(),
            "reddit_available": reddit.available(),
            "ai_available": aibrain.available(),
        },
    }
