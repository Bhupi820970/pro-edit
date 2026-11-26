"""Gemini picks an ORIGINAL song for the scene mood; audio comes from the box song pool first."""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from .util import REPO, home, log, run

SONG_SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"}, "artist": {"type": "STRING"}, "year": {"type": "INTEGER"},
    "bpm": {"type": "INTEGER"}, "why": {"type": "STRING"}},
    "required": ["title", "artist", "why"]}}

SONG_PROMPT = """Pick songs for a ~38 second beat-synced fan edit (hard cuts on every beat, character dialogue
scenes with the music ducked, full-volume music over action montage).
Category: {category}. Footage: {footage}
Moods seen by the footage checker: {moods}
Style hint: {hint}
Rules:
- ORIGINAL studio recordings only (no remixes, covers, sped-up/slowed, live, mashups, AI songs).
- Must have an official upload (artist channel / VEVO / "Artist - Topic") on YouTube.
- Steady, punchy beat (roughly 90-135 BPM) with an energetic section; it must fit the footage's mood.
- Do NOT pick any of these recently used songs: {recent}
Return the 8 best options, best first, as JSON."""

AUDIO_EXT = {".m4a", ".mp3", ".wav", ".opus", ".webm", ".aac", ".flac"}
TERM_RX = re.compile(r"^[\w '.,:!?()&+/!.-]+$", re.UNICODE)


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", "and").replace("-", " ")
    s = re.sub(r"\.(?=\S)", "", s)  # D.M.C. -> DMC
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", s)).strip()


def _artist_key(s: str) -> str:
    return re.split(r"\b(?:ft|feat|featuring|with)\b", _norm(s))[0].strip()


def safe_search_term(s: str) -> str | None:
    s = (s or "").strip()
    if not s or s.startswith("-") or len(s) > 80 or not TERM_RX.match(s):
        return None
    return s


def catalogue(cfg: dict | None = None) -> list[dict]:
    """Static song pool from config/songs.json plus each category's fallback_songs."""
    songs, seen = [], set()

    def add(item):
        if not isinstance(item, dict):
            return
        title, artist = safe_search_term(item.get("title") or ""), safe_search_term(item.get("artist") or "")
        if not title or not artist:
            return
        tag = _norm(f"{title} - {artist}")
        if tag in seen:
            return
        seen.add(tag)
        songs.append({"title": title, "artist": artist, "why": item.get("why") or "",
                      "genres": clean_genres(item.get("genres"))})

    p = REPO / "config" / "songs.json"
    if p.exists():
        try:
            for item in json.loads(p.read_text()).get("songs") or []:
                add(item)
        except Exception as ex:
            log("songs catalogue unreadable:", str(ex)[:160])
    for name, c in (cfg or {}).get("categories", {}).items():
        for item in c.get("fallback_songs") or []:
            add({**item, "genres": item.get("genres") or [name]})
    return songs


GENRES = ("gta", "gaming", "movie", "sports", "romance", "series")


def clean_genres(g) -> list[str]:
    if isinstance(g, str):
        g = [g]
    return [x for x in (g or []) if isinstance(x, str) and x in GENRES]


def tag_key(title: str, artist: str) -> str:
    return _norm(f"{title} - {artist}")


def genres_of(rec: dict, index: dict | None = None) -> list[str]:
    """Genres from the record itself, else from the tagged config catalogue (title match)."""
    g = clean_genres(rec.get("genres"))
    if g or not index:
        return g
    hit = index.get(tag_key(rec.get("title") or "", rec.get("artist") or ""))
    if hit is None:
        hit = index.get("t:" + _norm(rec.get("title") or ""))
    return list(hit or [])


def genre_index(cfg: dict | None = None) -> dict:
    idx = {}
    for s in catalogue(cfg):
        if s["genres"]:
            idx[tag_key(s["title"], s["artist"])] = s["genres"]
            idx.setdefault("t:" + _norm(s["title"]), s["genres"])
    return idx


# --no-playlist would collapse ytsearchN to a single hit, so search uses a slimmer flag set.
SEARCH_OPTS = ["--no-warnings", "--remote-components", "ejs:github", "--retries", "5",
               "--socket-timeout", "30"]


def find_official(title: str, artist: str, extra: list | None = None) -> dict | None:
    """Search YouTube for the official upload (Topic / VEVO / artist channel)."""
    title, artist = safe_search_term(title) or "", safe_search_term(artist) or ""
    if not title or not artist:
        return None
    entries = []
    for q in (f"ytsearch10:{artist} - {title} official audio",
              f"ytsearch10:{artist} {title} official"):
        r = run(["yt-dlp", *SEARCH_OPTS, *(extra or []), "--flat-playlist", "-J", q],
                timeout=300, check=False)
        if r.returncode != 0:
            continue
        try:
            data = json.loads(r.stdout or "{}")
        except Exception:
            continue
        batch = data.get("entries") or []
        if not batch and data.get("id"):
            batch = [data]
        entries.extend(e for e in batch if isinstance(e, dict) and e.get("id"))
    if not entries:
        return None
    nt, na = _norm(title), _artist_key(artist)
    tokens = [w for w in na.split() if w not in {"the", "and", "of", "a", "an"}]
    best = None
    for e in entries:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        et, ch = _norm(e.get("title") or ""), e.get("channel") or e.get("uploader") or ""
        nch = _norm(ch)
        dur = e.get("duration") or 0
        title_hit = nt in et or (nt.replace(" ", "") in et.replace(" ", "") and len(nt) >= 5)
        if not (90 <= dur <= 600) or not title_hit:
            continue
        if re.search(r"\b(remix|cover|live|slowed|sped|nightcore|8d|karaoke|instrumental|reverb|edit|lyrics)\b", et) and \
                not re.search(r"\b(remix|live|edit)\b", nt):
            continue
        art_hit = bool(na) and (na in nch or nch in na or (tokens and tokens[0] in nch and (len(tokens) == 1 or tokens[1] in nch)))
        official_vevo = nch.endswith("vevo")
        score = 0
        if ch.endswith(" - Topic") and art_hit:
            score = 3
        elif official_vevo and art_hit:
            score = 2.5
        elif art_hit and len(nch) > 2:
            score = 2
        elif e.get("channel_is_verified") and art_hit:
            score = 1.5
        if score and (best is None or score > best[0]):
            best = (score, e, ch)
    if not best:
        return None
    e = best[1]
    vid = e["id"]
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "channel": best[2],
            "verified": e.get("channel_is_verified")}


def load_box_songs(sdir: Path) -> list[dict]:
    recs = []
    for name in ("manifest.json", "songs_manifest.json"):
        p = sdir / name
        if not p.exists():
            continue
        try:
            recs = list(json.loads(p.read_text()).get("songs") or [])
            break
        except Exception:
            continue
    from .song_official import check as official_check
    out = []
    for rec in recs:
        if not isinstance(rec, dict) or not rec.get("title") or not rec.get("artist"):
            continue
        ok_off, why = official_check(rec, rec.get("artist"), rec.get("title"))
        if not ok_off:      # covers / tributes / fan rips are never used, even before the box purges them
            log(f"song pool: skip unofficial {rec.get('title')} - {rec.get('artist')}: {why}")
            continue
        if song_audio_path(sdir, rec):
            out.append(rec)
    return out


def song_audio_path(sdir: Path, rec: dict) -> Path | None:
    names = []
    if rec.get("file"):
        names.append(rec["file"])
    if rec.get("id"):
        names.extend(f"{rec['id']}{ext}" for ext in AUDIO_EXT)
    for name in names:
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            continue
        p = sdir / name
        if p.is_file() and p.suffix.lower() in AUDIO_EXT and p.stat().st_size > 1000:
            return p
    return None


def match_cached(cached: list[dict], title: str, artist: str) -> dict | None:
    nt, na = _norm(title), _artist_key(artist)
    if not nt:
        return None
    hits = []
    for rec in cached:
        if _norm(rec.get("title") or "") != nt:
            continue
        ra = _artist_key(rec.get("artist") or "")
        if not na or not ra or na == ra or na in ra or ra in na:
            hits.append(rec)
    return hits[0] if hits else None


def as_wav(src: Path, sdir: Path) -> Path | None:
    if not src or not src.exists():
        return None
    if src.suffix.lower() == ".wav" and src.stat().st_size > 1000:
        return src
    wav = sdir / f"{src.stem}.wav"
    if wav.exists() and wav.stat().st_size > 1000:
        return wav
    r = run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(wav)],
            timeout=300, check=False)
    if r.returncode == 0 and wav.exists() and wav.stat().st_size > 1000:
        return wav
    log("ffmpeg wav convert failed:", src.name, (r.stderr or "")[-200:])
    wav.unlink(missing_ok=True)
    return None


def _result(title: str, artist: str, why: str, wav: Path, rec: dict, how: str) -> dict:
    q = urllib.parse.quote(f"{artist} {title}")
    return {"title": title, "artist": artist, "why": why, "path": str(wav),
            "url": rec.get("url") or "", "official_channel": rec.get("channel") or rec.get("official_channel") or "",
            "spotify_search": f"https://open.spotify.com/search/{q}", "picked_by": how}


SONG_PROMPT_GENRE = """Pick ONE song for a ~38 second beat-synced {category} fan edit (hard cuts on the beat,
dialogue scenes with the music ducked). The whole edit uses this single song from start to end.
Footage: {footage}
Moods seen by the footage checker: {moods}
Genre vibe: {hint}
Songs already in our {category} stock (prefer one of these if it truly fits): {stock}
Rules:
- The song must truly match the {category} vibe and these scenes; if no stock song fits well you may name a
  different well-known ORIGINAL studio recording (no remixes, covers, sped-up/slowed, live, mashups, AI songs)
  that has an official upload (artist channel / VEVO / "Artist - Topic") on YouTube.
- Steady, punchy beat (roughly 90-135 BPM) with an energetic section.
- Do NOT pick any of these recently used songs: {recent}
Return the 6 best options, best first, as JSON."""


def _remember_new_song(sdir: Path, rec: dict) -> None:
    """Add a song the runner downloaded to the local manifest + new_songs.json (published to the stock later)."""
    for name in ("manifest.json", "new_songs.json"):
        p = sdir / name
        try:
            data = json.loads(p.read_text()) if p.exists() else {"songs": []}
        except Exception:
            data = {"songs": []}
        if not isinstance(data.get("songs"), list):
            data = {"songs": []}
        if not any(r.get("id") == rec["id"] for r in data["songs"] if isinstance(r, dict)):
            data["songs"].append(rec)
        p.write_text(json.dumps(data, indent=1))


def download_official(o: dict, sdir: Path, genres: list[str], by: str, extra: list | None = None) -> dict | None:
    """Search + download one ORIGINAL studio recording (m4a). Title/artist must be plain search terms
    (data only — never used as a URL or shell). Tries SoundCloud, YouTube frontends, Archive, Pixabay
    (and official YouTube first when not on GitHub Actions). See pipeline_lib.song_fetch."""
    from .song_fetch import fetch_original
    return fetch_original(o, sdir, genres, by, extra=extra)


def pick_song(gem, state, category: str, clips: list[dict], cfg: dict, banned: set, rng=None) -> dict:
    """ONE song per video, from the songs tagged with this video's genre, rotated by song_cooldown_days.
    Gemini may rank the genre stock or ask for a specific song not in stock; that song is then fetched from
    its official upload, used for this edit and added to the genre stock (new_songs.json -> songs release)."""
    import random
    rng = rng or random.Random()
    moods = "; ".join(sorted({c.get("mood") or "" for c in clips if c.get("mood")}))[:600]
    footage = "; ".join(sorted({f'{c.get("title")} ({c.get("credit")})' for c in clips}))[:600]
    recent = state.recent_songs(cfg.get("song_cooldown_days", 10)) + sorted(banned)
    recent_l = {_norm(x) for x in recent}
    sdir = home() / "songs"
    sdir.mkdir(exist_ok=True)
    idx = genre_index(cfg)
    cached = load_box_songs(sdir)
    pool = [r for r in cached if category in genres_of(r, idx)]
    log(f"song stock for {category}: {len(pool)} tagged songs with audio")

    def usable(o: dict) -> bool:
        tag = f'{o["title"]} - {o["artist"]}'
        return _norm(tag) not in recent_l and tag not in banned

    fresh = [r for r in pool if usable(r)]
    rng.shuffle(fresh)
    opts = []
    if gem is not None:
        try:
            opts = gem.generate([{"text": SONG_PROMPT_GENRE.format(
                category=category, footage=footage, moods=moods or "n/a",
                hint=cfg["categories"][category].get("song_hint", ""),
                stock="; ".join(f'{r["title"]} - {r["artist"]}' for r in fresh[:40]) or "none",
                recent=", ".join(recent[-60:]) or "none")}], schema=SONG_SCHEMA, temperature=0.7)
        except Exception as ex:
            log("gemini song pick failed, rotating the genre stock:", str(ex)[:160])
            opts = []
    opts = [o for o in (opts or []) if isinstance(o, dict) and safe_search_term(o.get("title") or "")
            and safe_search_term(o.get("artist") or "")][:6]

    # 1) Gemini's choice when it is in the genre stock (or any cached song Gemini names for this genre)
    for o in opts:
        if not usable(o):
            continue
        rec = match_cached(pool, o["title"], o["artist"]) or match_cached(cached, o["title"], o["artist"])
        if rec:
            wav = as_wav(song_audio_path(sdir, rec), sdir)
            if wav:
                log("using stock song (gemini pick):", f'{rec["title"]} - {rec["artist"]}')
                return _result(rec["title"], rec["artist"], o.get("why", ""), wav, rec, "gemini-stock")
    # 2) Gemini wants a song we don't have: fetch it once (best option only), use it, add it to the stock
    for o in opts[:2]:
        if not usable(o) or match_cached(cached, o["title"], o["artist"]):
            continue
        tag = f'{o["title"]} - {o["artist"]}'
        rec = download_official(o, sdir, [category], "runner-gemini")
        if not rec:
            log("gemini song not fetchable:", tag)
            banned.add(tag)
            continue
        wav = as_wav(sdir / rec["file"], sdir)
        if wav:
            _remember_new_song(sdir, rec)
            log("using new song (gemini request, added to stock):", tag)
            return _result(rec["title"], rec["artist"], o.get("why", ""), wav, rec, "gemini-new")
    # 3) rotate the genre stock: random unused song tagged with this genre
    for rec in fresh:
        wav = as_wav(song_audio_path(sdir, rec), sdir)
        if wav:
            log("using genre stock song:", f'{rec["title"]} - {rec["artist"]}')
            return _result(rec["title"], rec["artist"], rec.get("why", ""), wav, rec, "genre-stock")
    # 4) genre-tagged catalogue / seed songs not yet downloaded
    seeds = [s for s in catalogue(cfg) if category in s["genres"]]
    seeds += [{**x, "genres": [category]} for x in (load_songs_cfg().get("seeds") or {}).get(category, [])
              if isinstance(x, dict)]
    for o in seeds[:12]:
        if not usable(o) or match_cached(cached, o["title"], o["artist"]):
            continue
        rec = download_official(o, sdir, [category], "runner-seed")
        wav = as_wav(sdir / rec["file"], sdir) if rec else None
        if wav:
            _remember_new_song(sdir, rec)
            return _result(rec["title"], rec["artist"], o.get("why", ""), wav, rec, "genre-seed")
        banned.add(f'{o["title"]} - {o["artist"]}')
    # 5) last resort: any unused song in the pool (logged loudly; genre stock is empty)
    for rec in cached:
        if usable(rec):
            wav = as_wav(song_audio_path(sdir, rec), sdir)
            if wav:
                log(f"WARNING: {category} song stock empty; using untagged pool song")
                return _result(rec["title"], rec["artist"], rec.get("why", ""), wav, rec, "box-pool")
    raise RuntimeError("no usable song found")


def load_songs_cfg() -> dict:
    try:
        return json.loads((REPO / "config" / "songs.json").read_text())
    except Exception:
        return {}
