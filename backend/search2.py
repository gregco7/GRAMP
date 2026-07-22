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

from . import aibrain, clipgate, clips, framegrab, reddit, twitch

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


# Known pro/streamer handles — so "aspas ace" fixes the subject even when the LLM
# parser is unavailable (the rule parser doesn't extract a creator). Not exhaustive;
# the LLM parse handles the long tail. Lowercased, word-boundary matched.
KNOWN_PLAYERS = [
    "tenz", "aspas", "demon1", "zekken", "yay", "derke", "alfajer", "chronicle",
    "boaster", "jinggg", "forsaken", "less", "something", "shroud", "tarik",
    "sinatraa", "wardell", "subroza", "kyedae", "scream", "s0m", "shanks", "buzz",
    "sylvan", "cned", "leo", "nats", "sacy", "saadhak", "marved", "crashies",
    "victor", "fns", "ethan", "zellsis", "johnqt", "bang", "n4rrate", "mako",
    "stax", "rb", "zest", "buzz", "f0rsakeen", "primmie", "trent", "valyn",
]
_PLAYER_RE = {p: re.compile(r"\b" + re.escape(p) + r"\b", re.I) for p in KNOWN_PLAYERS}


def _subjects(intent: dict) -> list[str]:
    """The named subject(s) a candidate must evidence, if any. Creator wins; else
    detect a known player in the free-text terms."""
    cre = (intent.get("creator") or "").strip()
    if cre:
        return [cre]
    hay = " ".join([intent.get("search_terms") or "", " ".join(intent.get("keywords") or [])])
    found = [p for p in KNOWN_PLAYERS if _PLAYER_RE[p].search(hay)]
    return found


def _has_subject(e: dict, subjects: list[str]) -> bool:
    """Does this candidate evidence the requested player? Title or channel name, or
    a Twitch clip scoped to that broadcaster (its channel is the streamer)."""
    if not subjects:
        return True
    hay = f"{e.get('title') or ''} {e.get('channel') or ''}".lower()
    return any(re.search(r"\b" + re.escape(s.lower()) + r"\b", hay) for s in subjects)


def _intent_text(intent: dict) -> str:
    """A concise natural-language restatement of the intent for the LLM title-gate."""
    parts = [intent.get("search_terms") or "",
             " ".join(intent.get("plays") or []),
             " ".join(intent.get("agents") or []),
             " ".join(intent.get("orgs") or [])]
    cre = intent.get("creator") or ""
    txt = " ".join(p for p in parts if p).strip()
    return (f"{cre} — {txt}" if cre else txt) or "valorant single-play clip"


def _title_rerank(intent: dict, pool: list[dict], enabled: bool) -> None:
    """Let the LLM READ the titles (the eyeball pass regex can't do): reward
    on-intent single plays, sink compilations/off-subject the keyword scorer missed.
    A RE-RANK, never a hard drop — worst case it's a no-op — so an LLM miss can only
    reorder, not delete a keeper. Mutates `score` in place. Best-effort."""
    if not enabled or len(pool) < 4:
        return
    head = sorted(pool, key=lambda e: e.get("score", 0), reverse=True)[:30]
    picks = aibrain.curate(_intent_text(intent), head)
    if not picks:
        return
    for i, e in enumerate(head):
        p = picks.get(i)
        if p:  # kept by the LLM — nudge up by its fit score, keep the reason
            e["score"] = round(e.get("score", 0) + 0.15 * float(p.get("score") or 0), 4)
            if p.get("reason"):
                e["curate_reason"] = p["reason"]
        else:  # omitted by the LLM = likely a montage / off-subject it read in the title
            e["score"] = round(e.get("score", 0) - 0.10, 4)


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
    # View count MISLEADS for a Shorts source search: the keepers sit at 4k-100k
    # views while over-produced montage channels have far more. Popularity is only
    # a faint tie-breaker now (was 0.08).
    base += clips._popularity(e.get("view_count")) * 0.02
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
    """Download a low-res copy once, then run BOTH checks on it: the deterministic
    montage gate (cut density, from the video file) and the vision verdict (from
    sampled frames). Returns {"vision":..., "media":...}."""
    frames, work, video = framegrab.sample_frames(e.get("url"), e.get("duration"), n=3)
    try:
        vision = aibrain.analyze_clip(frames, title=e.get("title") or "")
        media = clipgate.analyze_media(video, e.get("duration")) if video else {"analyzed": False}
    finally:
        framegrab.cleanup(work)
    return {"vision": vision, "media": media}


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

    # 3b) SUBJECT GATE — if a player was named, drop candidates with no evidence of
    # them (title/channel/Twitch broadcaster). Fixes "aspas ace" -> a JAWGEMO ace.
    subjects = _subjects(intent)
    gated_subject = 0
    if subjects:
        kept = []
        for e in pool:
            if _has_subject(e, subjects):
                # a raw Twitch clip OF the named subject is the gold seam — boost it.
                if e.get("source") == "twitch":
                    e["score"] = round(e["score"] + 0.10, 4)
                kept.append(e)
            else:
                gated_subject += 1
        pool = kept

    # 3c) TITLE RE-RANK — the LLM reads the titles (reward single plays, sink
    # compilations the keyword scorer missed) so the vision budget lands on the
    # right clips. Re-rank only; never deletes.
    _title_rerank(intent, pool, opts.get("use_title_gate", True) and use_ai)
    pool.sort(key=lambda e: e["score"], reverse=True)

    # 4) VERIFY (top N): deterministic montage gate + vision, on one shared download
    verified = 0
    gated_montage = 0
    gated_facecam = 0
    require_facecam = bool(opts.get("require_facecam", intent.get("want_facecam")))
    if use_vision and pool:
        top = pool[:vision_count]
        _p(55, f"watching {len(top)} clips (frames + AI)…")
        with ThreadPoolExecutor(max_workers=min(5, len(top))) as ex:
            futs = {ex.submit(_verify_one, e): e for e in top}
            done = 0
            for f in as_completed(futs):
                e = futs[f]
                try:
                    res = f.result()
                except Exception:
                    res = {"vision": {"analyzed": False}, "media": {"analyzed": False}}
                v, media = res.get("vision") or {}, res.get("media") or {}
                e["media"] = media
                e["score"] = round(_apply_vision(e, v), 4)
                # montage gate (hard): cut-density says compilation
                reason = clipgate.gate(media)
                if reason:
                    e["gated"] = reason
                    gated_montage += 1
                # facecam gate: Twitch always has a cam; a non-Twitch clip that vision
                # confidently reads as camless fails only when facecam is REQUIRED —
                # otherwise it's a soft penalty (keeps Pro/VCT observer clips available).
                elif (v.get("analyzed") and not v.get("facecam")
                      and e.get("source") != "twitch" and v.get("confidence", 0) >= 0.5):
                    if require_facecam:
                        e["gated"] = "no facecam"
                        gated_facecam += 1
                    else:
                        e["score"] = round(e["score"] - 0.10, 4)
                done += 1
                verified += 1 if v.get("analyzed") else 0
                _p(55 + int(35 * done / len(top)), f"verified {done}/{len(top)} clips…")
        pool.sort(key=lambda e: e["score"], reverse=True)

    # 5) FINALIZE — drop hard-gated clips (user chose hard-drop), keep the rest.
    _p(95, "finalizing…")
    survivors = [e for e in pool if not e.get("gated")]
    results = []
    for e in survivors[:limit]:
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
            "subjects": subjects,
            "gated": {"subject": gated_subject, "montage": gated_montage,
                      "facecam": gated_facecam},
            "twitch_available": twitch.available(),
            "reddit_available": reddit.available(),
            "ai_available": aibrain.available(),
        },
    }
