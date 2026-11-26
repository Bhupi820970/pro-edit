"""Fetch ORIGINAL studio audio for the genre song stock, with CI-safe source fallbacks.

Order on GitHub Actions (YouTube downloads are often blocked there):
  1. SoundCloud (yt-dlp scsearch)
  2. YouTube via public Piped/Invidious search + yt-dlp player_client variants
  3. Internet Archive
  4. Pixabay Music (optional PIXABAY_API_KEY; last resort, still title/artist + duration checked)

On a box with cookies, official YouTube is tried first, then the same chain.
URLs must pass the HTTPS allowlist. Fetched bytes are written as audio only — never executed.
Gemini output is never used as a URL or shell command; only pre-validated title/artist search terms.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .song_official import check as official_check
from .songs import AUDIO_EXT, SEARCH_OPTS, _artist_key, _norm, find_official, safe_search_term
from .sourcing import ytdlp
from .util import log, now_local, run

MIN_DUR, MAX_DUR = 90, 600
MAX_BYTES = 40 * 1024 * 1024
JUNK_RX = re.compile(
    r"\b(remix|cover|covers|live|slowed|sped|speed\s*up|nightcore|8d|karaoke|instrumental|"
    r"reverb|edit|edits|lyrics|lyric|lofi|lo-fi|ai\s*cover|mashup|bootleg|mix|demo|snippet)\b",
    re.I,
)
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{3,80}$")

ALLOWED_HOSTS = frozenset({
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "api.soundcloud.com",
    "api-v2.soundcloud.com", "sndcdn.com", "cf-media.sndcdn.com", "cf-hls-media.sndcdn.com",
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com",
    "www.youtu.be", "googlevideo.com",
    "archive.org", "www.archive.org",
    "pixabay.com", "www.pixabay.com", "cdn.pixabay.com",
    # public YouTube frontends used only as search/stream proxies
    "pipedapi.kavin.rocks", "pipedapi.adminforge.de", "pipedapi.darkness.services",
    "inv.nadeko.net", "invidious.nerdvpn.de", "yewtu.be", "iv.ggtyler.dev",
})
ALLOWED_SUFFIXES = (
    ".sndcdn.com", ".soundcloud.com", ".googlevideo.com", ".archive.org",
    ".pixabay.com", ".youtube.com",
)
PIPED = ("https://pipedapi.kavin.rocks", "https://pipedapi.adminforge.de")
INVIDIOUS = ("https://inv.nadeko.net", "https://invidious.nerdvpn.de", "https://yewtu.be")

# skip official YouTube first on Actions — it usually burns the timeout on bot checks
CI_ORDER = ("soundcloud", "yt_mirrors", "archive", "pixabay")
BOX_ORDER = ("youtube", "soundcloud", "yt_mirrors", "archive", "pixabay")


def allowed_url(url: str) -> bool:
    """HTTPS-only allowlist. No userinfo, no non-443 ports, no raw IPs."""
    try:
        u = urllib.parse.urlparse((url or "").strip())
    except Exception:
        return False
    if u.scheme != "https" or u.username or u.password or not u.hostname:
        return False
    if u.port not in (None, 443):
        return False
    host = u.hostname.lower().rstrip(".")
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host) or ":" in host:
        return False
    return host in ALLOWED_HOSTS or any(host.endswith(suf) for suf in ALLOWED_SUFFIXES)


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise urllib.error.URLError("redirect host not allowlisted")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_AllowlistRedirect)


def http_json(url: str, timeout: int = 25) -> dict | list | None:
    raw = http_bytes(url, timeout=timeout, max_bytes=2_000_000, expect_json=True)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, (dict, list)) else None


def http_bytes(url: str, timeout: int = 60, max_bytes: int = MAX_BYTES, expect_json: bool = False) -> bytes | None:
    if not allowed_url(url):
        log("blocked non-allowlisted url host")
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "ViceCartelSongStock/1.0", "Accept": "*/*"})
    try:
        with _opener().open(req, timeout=timeout) as r:
            final = r.geturl()
            if not allowed_url(final):
                log("blocked redirect host")
                return None
            data = r.read(max_bytes + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as ex:
        log("http fetch failed:", type(ex).__name__)
        return None
    if len(data) > max_bytes or len(data) < 32:
        return None
    return data


def is_junk(text: str, official_title: str = "") -> bool:
    if JUNK_RX.search(text or ""):
        # allow if the official title itself contains the word (e.g. "The Message")
        if official_title and JUNK_RX.search(official_title):
            return False
        return True
    return False


def title_hit(got: str, want: str) -> bool:
    nt, ng = _norm(want), _norm(got)
    if not nt or not ng:
        return False
    return nt in ng or (len(nt) >= 5 and nt.replace(" ", "") in ng.replace(" ", ""))


def artist_hit(got: str, want: str) -> bool:
    na, ng = _artist_key(want), _norm(got)
    if not na or not ng:
        return False
    if na in ng or ng in na:
        return True
    tokens = [w for w in na.split() if w not in {"the", "and", "of", "a", "an"}]
    return bool(tokens) and tokens[0] in ng and (len(tokens) == 1 or tokens[1] in ng)


def duration_ok(dur) -> bool:
    try:
        d = float(dur or 0)
    except (TypeError, ValueError):
        return False
    return MIN_DUR <= d <= MAX_DUR


def safe_id(source: str, raw: str) -> str:
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "", raw)[:40]
    h = hashlib.sha1(f"{source}:{raw}".encode()).hexdigest()[:12]
    prefix = {"soundcloud": "sc", "youtube": "yt", "archive": "ia", "pixabay": "px", "piped": "yt"}.get(source, "so")
    sid = f"{prefix}_{tag or h}"
    if source == "youtube" and re.fullmatch(r"[\w-]{11}", raw):
        return raw
    return sid if SAFE_ID.match(sid) else f"{prefix}_{h}"


def _existing(sdir: Path, sid: str) -> Path | None:
    return next((p for p in sdir.glob(f"{sid}.*")
                 if p.suffix.lower() in AUDIO_EXT and p.stat().st_size > 1000), None)


def _probe_dur(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            timeout=60, check=False)
    try:
        return float((r.stdout or "").strip() or 0)
    except ValueError:
        return 0.0


def _ytdlp_audio(url: str, sdir: Path, sid: str, extra: list | None = None) -> Path | None:
    if not allowed_url(url) or not SAFE_ID.match(sid):
        return None
    got = _existing(sdir, sid)
    if got:
        return got
    ok, _ = ytdlp([*(extra or []), "-f", "ba/b", "-x", "--audio-format", "m4a", "--audio-quality", "0",
                   "-o", str(sdir / f"{sid}.%(ext)s"), url], timeout=900)
    got = _existing(sdir, sid)
    return got if ok and got else None


def _save_audio(data: bytes, sdir: Path, sid: str) -> Path | None:
    if not SAFE_ID.match(sid) or len(data) < 1000:
        return None
    raw = sdir / f"{sid}.bin"
    raw.write_bytes(data)
    out = sdir / f"{sid}.m4a"
    r = run(["ffmpeg", "-y", "-i", str(raw), "-vn", "-c:a", "aac", "-b:a", "192k", str(out)],
            timeout=300, check=False)
    raw.unlink(missing_ok=True)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
        return out
    out.unlink(missing_ok=True)
    return None


def _record(o: dict, sid: str, path: Path, url: str, channel: str, source: str, genres: list[str], by: str) -> dict | None:
    if not duration_ok(_probe_dur(path)):
        log("rejected duration after download:", o["title"])
        path.unlink(missing_ok=True)
        return None
    return {"id": sid, "file": path.name, "url": url, "title": o["title"], "artist": o["artist"],
            "channel": channel, "why": o.get("why", ""), "genres": genres, "source": source,
            "added": now_local().isoformat(), "by": by}


# --- source 1: SoundCloud ----------------------------------------------------
def find_soundcloud(title: str, artist: str, extra: list | None = None) -> dict | None:
    q = f"{artist} {title}"
    r = run(["yt-dlp", *SEARCH_OPTS, *(extra or []), "--flat-playlist", "-J", f"scsearch10:{q}"],
            timeout=300, check=False)
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout or "{}")
    except Exception:
        return None
    entries = data.get("entries") or []
    best = None
    for e in entries:
        if not isinstance(e, dict):
            continue
        et, up = e.get("title") or "", e.get("uploader") or e.get("channel") or ""
        url = e.get("webpage_url") or e.get("url") or ""
        if url and not url.startswith("http"):
            url = f"https://soundcloud.com/{url.lstrip('/')}" if "/" in str(url) else ""
        if not allowed_url(url) or not title_hit(et, title) or is_junk(et, title):
            continue
        if not duration_ok(e.get("duration")):
            continue
        if not artist_hit(up, artist) and not artist_hit(et, artist):
            continue
        ok_off, why = official_check({"source": "soundcloud", "url": url, "channel": up, "track_title": et},
                                     artist, title)
        if not ok_off:
            log(f"skip unofficial SoundCloud upload ({why}):", up, url[:80])
            continue
        score = 3 if artist_hit(up, artist) else 1
        if best is None or score > best[0]:
            best = (score, {"id": safe_id("soundcloud", str(e.get("id") or url)), "url": url,
                             "channel": up, "source": "soundcloud"})
    return None if not best else best[1]


# --- source 2: YouTube frontends + player clients ----------------------------
def _piped_search(title: str, artist: str) -> list[dict]:
    q = urllib.parse.quote(f"{artist} {title} official audio")
    out = []
    for base in PIPED:
        data = http_json(f"{base}/search?q={q}&filter=videos")
        items = (data or {}).get("items") if isinstance(data, dict) else data
        if not items:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            url = it.get("url") or it.get("webpageUrl") or ""
            vid = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("v") or [None])[0]
            if not vid and it.get("id") and re.fullmatch(r"[\w-]{11}", str(it["id"])):
                vid = it["id"]
            if not vid or not re.fullmatch(r"[\w-]{11}", vid):
                continue
            out.append({"id": vid, "title": it.get("title") or "", "channel": it.get("uploaderName") or it.get("uploader") or "",
                        "duration": it.get("duration") or it.get("length") or 0, "verified": it.get("uploaderVerified")})
        if out:
            break
    return out


def _invidious_search(title: str, artist: str) -> list[dict]:
    q = urllib.parse.quote(f"{artist} {title} official audio")
    out = []
    for base in INVIDIOUS:
        data = http_json(f"{base}/api/v1/search?q={q}&type=video")
        if not isinstance(data, list):
            continue
        for it in data:
            if not isinstance(it, dict):
                continue
            vid = it.get("videoId") or ""
            if not re.fullmatch(r"[\w-]{11}", str(vid)):
                continue
            out.append({"id": vid, "title": it.get("title") or "", "channel": it.get("author") or "",
                        "duration": it.get("lengthSeconds") or 0, "verified": it.get("authorVerified")})
        if out:
            break
    return out


def _pick_yt_entry(title: str, artist: str, entries: list[dict]) -> dict | None:
    best = None
    for e in entries:
        et, ch = e.get("title") or "", e.get("channel") or ""
        if not title_hit(et, title) or is_junk(et, title) or not duration_ok(e.get("duration")):
            continue
        art = artist_hit(ch, artist) or artist_hit(et, artist)
        if not official_check({"source": "youtube", "channel": ch, "verified": e.get("verified"),
                               "track_title": et}, artist, title)[0]:
            continue
        nch = _norm(ch)
        score = 0
        if ch.endswith(" - Topic") and art:
            score = 3
        elif nch.endswith("vevo") and art:
            score = 2.5
        elif art and e.get("verified"):
            score = 2
        elif art:
            score = 1.5
        if score and (best is None or score > best[0]):
            best = (score, e, ch)
    if not best:
        return None
    e, ch = best[1], best[2]
    return {"id": e["id"], "url": f"https://www.youtube.com/watch?v={e['id']}", "channel": ch, "source": "yt_mirrors"}


def find_yt_mirrors(title: str, artist: str, extra: list | None = None) -> dict | None:
    entries = _piped_search(title, artist) + _invidious_search(title, artist)
    hit = _pick_yt_entry(title, artist, entries)
    if hit:
        return hit
    # last: official YouTube search (flat, no download) then player_client download later
    off = find_official(title, artist, extra=extra)
    if off:
        off = {**off, "source": "yt_mirrors"}
    return off


def _piped_audio_url(vid: str) -> str | None:
    if not re.fullmatch(r"[\w-]{11}", vid):
        return None
    for base in PIPED:
        data = http_json(f"{base}/streams/{vid}")
        if not isinstance(data, dict):
            continue
        streams = data.get("audioStreams") or []
        for s in streams:
            if not isinstance(s, dict):
                continue
            url = s.get("url") or ""
            if allowed_url(url) and "audio" in (s.get("mimeType") or s.get("type") or "audio"):
                return url
    return None


# --- source 3: Internet Archive ----------------------------------------------
def find_archive(title: str, artist: str, extra: list | None = None) -> dict | None:
    q = f'title:("{title}") AND (creator:("{artist}") OR description:("{artist}")) AND mediatype:(audio)'
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode({
        "q": q, "fl[]": "identifier", "sort[]": "downloads desc", "rows": "12", "page": "1", "output": "json",
    })
    # urllib.parse.urlencode with fl[] once; add extra fl fields manually
    url = "https://archive.org/advancedsearch.php?q=" + urllib.parse.quote(q) + \
        "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=mediatype&rows=12&page=1&output=json"
    data = http_json(url)
    docs = ((data or {}).get("response") or {}).get("docs") if isinstance(data, dict) else None
    if not docs:
        return None
    for d in docs:
        if not isinstance(d, dict):
            continue
        ident = str(d.get("identifier") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", ident):
            continue
        t = d.get("title") or ident
        cr = d.get("creator") or ""
        if isinstance(cr, list):
            cr = " ".join(str(x) for x in cr)
        if is_junk(t, title) or not title_hit(t, title) or not (artist_hit(cr, artist) or artist_hit(t, artist)):
            continue
        page = f"https://archive.org/details/{ident}"
        if not allowed_url(page):
            continue
        return {"id": safe_id("archive", ident), "url": page, "channel": cr or "Internet Archive",
                "source": "archive", "ia_id": ident}
    return None


def _archive_file_url(ident: str) -> str | None:
    meta = http_json(f"https://archive.org/metadata/{ident}")
    if not isinstance(meta, dict):
        return None
    files = meta.get("files") or []
    prefer = ("VBR MP3", "mp3", "256Kbps MP3", "Ogg Vorbis", "Flac", "WAVE", "AIFF")
    for want in prefer:
        for f in files:
            if not isinstance(f, dict):
                continue
            name, fmt = f.get("name") or "", (f.get("format") or "")
            if "/" in name or name.startswith(".") or ".." in name:
                continue
            if want.lower() not in fmt.lower() and not name.lower().endswith((".mp3", ".ogg", ".flac", ".m4a", ".wav")):
                continue
            if want.lower() not in fmt.lower() and want not in ("mp3",):
                continue
            url = f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}"
            if allowed_url(url):
                return url
    return None


# --- source 4: Pixabay Music (last resort) -----------------------------------
def find_pixabay(title: str, artist: str, extra: list | None = None) -> dict | None:
    key = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not key:
        return None
    # key is only used as an HTTPS query param to pixabay.com; it is never logged
    q = urllib.parse.urlencode({"key": key, "q": f"{title} {artist}", "per_page": "10"})
    data = http_json("https://pixabay.com/api/v2/music/?" + q)
    hits = (data or {}).get("hits") if isinstance(data, dict) else None
    if not hits:
        data = http_json("https://pixabay.com/api/?" + q + "&audio=1")
        hits = (data or {}).get("hits") if isinstance(data, dict) else None
    if not hits:
        return None
    for h in hits:
        if not isinstance(h, dict):
            continue
        t = h.get("name") or h.get("tags") or h.get("title") or ""
        user = h.get("user") or ""
        dur = h.get("duration") or 0
        if is_junk(t, title) or not title_hit(t, title) or not duration_ok(dur):
            continue
        if not (artist_hit(t, artist) or artist_hit(user, artist)):
            continue
        url = ((h.get("audio") or {}) if isinstance(h.get("audio"), dict) else {}).get("url") or h.get("url") or ""
        if not allowed_url(str(url)):
            page = h.get("pageURL") or h.get("page_url") or ""
            if allowed_url(str(page)):
                url = page
            else:
                continue
        return {"id": safe_id("pixabay", str(h.get("id") or title)), "url": url,
                "channel": user or "Pixabay", "source": "pixabay"}
    return None


FINDERS = {
    "youtube": lambda t, a, e: (lambda off: {**off, "source": "youtube"} if off else None)(find_official(t, a, extra=e)),
    "soundcloud": find_soundcloud,
    "yt_mirrors": find_yt_mirrors,
    "archive": find_archive,
    "pixabay": find_pixabay,
}


def source_order() -> tuple[str, ...]:
    return CI_ORDER if os.environ.get("GITHUB_ACTIONS") else BOX_ORDER


def fetch_original(o: dict, sdir: Path, genres: list[str], by: str, extra: list | None = None) -> dict | None:
    """Search + download one original studio track. Title/artist must already be safe_search_term."""
    title, artist = safe_search_term(o.get("title") or "") or "", safe_search_term(o.get("artist") or "") or ""
    if not title or not artist:
        return None
    sdir.mkdir(parents=True, exist_ok=True)
    for src in source_order():
        try:
            hit = FINDERS[src](title, artist, extra)
        except Exception as ex:
            log(f"{src} search failed:", type(ex).__name__, str(ex)[:80])
            hit = None
        if not hit or not allowed_url(hit.get("url") or ""):
            continue
        ok_off, why = official_check(hit, artist, title)
        if not ok_off:
            log(f"{src}: rejected unofficial upload for {title} - {artist}: {why}")
            continue
        sid = hit["id"]
        if not SAFE_ID.match(sid):
            continue
        log(f"trying {src} for", f"{title} - {artist}")
        path = None
        if src == "archive" and hit.get("ia_id"):
            file_url = _archive_file_url(hit["ia_id"])
            if file_url:
                data = http_bytes(file_url)
                path = _save_audio(data, sdir, sid) if data else None
            if not path:
                path = _ytdlp_audio(hit["url"], sdir, sid, extra)
        elif src == "pixabay" and hit["url"].endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
            data = http_bytes(hit["url"])
            path = _save_audio(data, sdir, sid) if data else None
        elif src in {"yt_mirrors", "youtube"} and re.fullmatch(r"[\w-]{11}", sid):
            path = _ytdlp_audio(hit["url"], sdir, sid, extra)
            if not path:
                alt = _piped_audio_url(sid)
                if alt:
                    data = http_bytes(alt)
                    path = _save_audio(data, sdir, sid) if data else None
        else:
            path = _ytdlp_audio(hit["url"], sdir, sid, extra)
        if not path:
            log(f"{src} download failed:", f"{title} - {artist}")
            continue
        rec = _record(o, sid, path, hit["url"], hit.get("channel") or "", src, genres, by)
        if rec:
            return rec
    return None
