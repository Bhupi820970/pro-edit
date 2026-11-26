"""Find, download and Gemini-vet official clean source clips.

Order: (1) links inbox (inbox/links.txt), (2) official channels from config (primary — the same
Rockstar Games YouTube uploads the approved GTA edit used), (3) fallback: YouTube search limited to
verified/allow-listed channels, Gemini google-search discovery of YouTube / X links.
Preview stills (YouTube thumbnails and/or a short low-res partial) are 4-corner watermark
vetted by Gemini before any full download. Candidates that pass are then downloaded, and 6-8
960px JPEG keyframes are sent to Gemini (flash-lite first) as a second check for watermarks,
logos, burned-in subtitles, HUD/scoreboard overlays, re-uploads and low quality.  Vet results
are cached in pipeline-state per source id and never re-vetted.  The whole video is never uploaded.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .util import home, log, run, probe, REPO
from .gemini import GeminiError, LITE_MODELS, FULL_MODELS
from .titles import SINGLE_WORK_CATEGORIES, is_full_scene_upload, is_gta_title, is_trailer_title, player_name, work_title

YTDLP_CLIENT_FALLBACKS = [
    [],                                                        # default clients (+ PO-token plugin if installed)
    ["--extractor-args", "youtube:player_client=tv,web_safari"],
    ["--extractor-args", "youtube:player_client=mweb"],
    ["--extractor-args", "youtube:player_client=android_vr"],
    ["--extractor-args", "youtube:player_client=web_embedded,tv_simply"],
]
COMMON = ["--no-warnings", "--remote-components", "ejs:github", "--no-playlist", "--retries", "5", "--fragment-retries", "10",
          "--socket-timeout", "30", "--sleep-requests", "1"]

VET_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "accept": {"type": "BOOLEAN"},
        "official_quality": {"type": "BOOLEAN"},
        "watermark": {"type": "BOOLEAN"},
        "channel_logo": {"type": "BOOLEAN"},
        "burned_subtitles": {"type": "BOOLEAN"},
        "hud_overlay": {"type": "BOOLEAN"},
        "editable": {"type": "BOOLEAN"},
        "has_dialogue": {"type": "BOOLEAN"},
        "mood": {"type": "STRING"},
        "characters": {"type": "ARRAY", "items": {"type": "STRING"}},
        "summary": {"type": "STRING"},
        "reject_reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
        "bad_ranges": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "reason": {"type": "STRING"}},
            "required": ["start", "end"]}},
    },
    "required": ["accept", "official_quality", "watermark", "channel_logo", "burned_subtitles",
                 "hud_overlay", "editable", "has_dialogue", "mood", "summary", "reject_reasons", "bad_ranges"],
}

VET_PROMPT = """You are the footage checker for a fan-edit channel. You are shown {n} still keyframes
(low-res JPEGs, in order) taken from the original download at these timestamps (seconds): {times}.
There is no audio. Judge the ORIGINAL's cleanliness from these frames, not the JPEG compression.
The frames are deliberately small 480p proxies: the file's real resolution is verified separately, so never
reject for the proxies' low resolution or JPEG softness. The LAST image may be a 2x2 sheet of the four
corners (top-left, top-right / bottom-left, bottom-right) of one frame at higher detail, separated by
magenta lines: use it to look closely for corner logos, channel bugs and watermarks.
Category: {category}. Source: "{title}" from channel "{channel}".
Provenance ({via}) is checked separately by the pipeline: if the channel is an official studio / publisher /
league account, treat the footage as official. Set official_quality=false ONLY for visual problems
(low resolution, blur, heavy compression, upscaling, screen recording, re-encoded re-upload with overlays).

Accept it ONLY if it is official or full-quality scene footage that can be cleanly re-edited:
- REJECT trailers, teasers, promos and TV spots entirely. We only want full scenes, official clips,
  full episodes, official highlights, or press conferences.{category_rule}
- REJECT if there is any persistent watermark, channel logo / bug in a corner, burned-in subtitles or
  captions over dialogue, game HUD / minimap / scoreboard / score bug / live-broadcast graphics,
  reaction-cam or picture-in-picture, social-media frame or blurred side panels, or low / blurry /
  upscaled quality.
- Carefully inspect ALL FOUR CORNERS (top-left, top-right, bottom-left, bottom-right) of every frame.
  REJECT if you see ANY corner logo or text (including "NETFLIX", "CLIP KINGS", "CLIP EMPIRE",
  channel watermarks, or italic burnt-in subtitles), even on official studio/streamer uploads.
- Opening cards or end slates in a full scene should be listed in bad_ranges (seconds) so the editor
  skips them. Also list any other short stretch with on-screen text, HUD or black frames in bad_ranges.
- editable = has enough clean, visually strong shots (at least ~20 s total) for a beat-synced edit.
- has_dialogue = frames clearly show characters mid-conversation or close-up talking (no audio is provided).
- bad_ranges are in SOURCE seconds. If a frame shows a studio/title/end card, HUD, or on-screen text,
  include a tight range around that frame's timestamp so the editor can skip it.
- mood: 2-5 words for the scene's vibe (e.g. "tense neon crime night", "romantic sunset longing").
- characters: names if known.
Return JSON only."""



# GTA: Rockstar's own uploads ("Now Available", update showcases, gameplay videos) are in-engine
# game footage — the approved Give It Up edit is cut from exactly this. Title/rating/end cards are skipped
# via bad_ranges instead of rejecting the whole source. Corner logos / HUD / subtitles still reject.
GTA_ROCKSTAR_RULE = """
  EXCEPTION for this source: it is an official Rockstar Games upload of in-engine game footage. Treat
  update showcases / "Now Available" / gameplay videos as official gameplay (NOT a banned promo). Put any
  rating card, title card, logo slate, "available now" text or end slate in bad_ranges, and set
  editable=true if there are at least ~20 s of clean in-game shots outside those ranges."""
VET_RULES_VERSION = "gta-rockstar-v1"


def category_rule(category: str, channel: str) -> str:
    if category == "gta" and "rockstar" in (channel or "").lower():
        return GTA_ROCKSTAR_RULE
    return ""


# Preview-frame fetch (pre-download watermark vet). Hosts and paths are constructed here,
# never taken from model output. yt-dlp is always an argv list (no shell).
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SAFE_SID_RE = re.compile(r"^[\w.-]{1,80}$")
YTIMG_HOSTS = frozenset({"i.ytimg.com", "img.youtube.com"})
YTIMG_NAMES = ("maxresdefault", "sddefault", "hqdefault")
MEDIA_URL_RX = re.compile(
    r"^https://(www\.|m\.|mobile\.)?(youtube\.com/(watch\?v=[\w-]{11}|shorts/[\w-]{11})|"
    r"youtu\.be/[\w-]{11}|(x|twitter)\.com/\w{1,30}/status/\d{5,25})([?&][\w=&%.-]{0,60})?$"
)
BAD_URL_CHARS = set(";|`$<>\\\"{}[]*\n\r\t")

PRE_VET_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "accept": {"type": "BOOLEAN"},
        "watermark": {"type": "BOOLEAN"},
        "channel_logo": {"type": "BOOLEAN"},
        "reject_reasons": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["accept", "watermark", "channel_logo", "reject_reasons"],
}

PRE_VET_PROMPT = """You are checking preview stills (YouTube thumbnail and/or low-res preview frames)
for a fan-edit pipeline, BEFORE the full video is downloaded.
Source: "{title}" from channel "{channel}". Category: {category}.
There are {n} stills. There is no audio.

Carefully inspect ALL FOUR CORNERS (top-left, top-right, bottom-left, bottom-right) of every frame.
REJECT (accept=false) if you see ANY corner logo, channel bug, or watermark (including "NETFLIX",
"CLIP KINGS", "CLIP EMPIRE", or other channel watermarks), or italic burnt-in subtitles.
A small official studio/streamer bug in a corner still counts as a reject.

Accept (accept=true) if the corners are clean. Ignore JPEG compression, letterboxing, or a
normal title/artwork thumbnail without a persistent logo overlay.
Return JSON only."""


def safe_sid(sid) -> str | None:
    s = str(sid or "")
    return s if SAFE_SID_RE.match(s) else None


def safe_media_url(u) -> str | None:
    """Only plain https YouTube / X links; same rules as box_fetch.safe_url."""
    if not isinstance(u, str) or len(u) > 200 or u.startswith("-") or set(u) & BAD_URL_CHARS:
        return None
    return u if MEDIA_URL_RX.match(u) else None


def youtube_video_id(e: dict) -> str | None:
    sid = str((e or {}).get("id") or "")
    if YT_ID_RE.match(sid):
        return sid
    url = (e or {}).get("url") or ""
    m = re.search(r"(?:youtube\.com/watch\?v=|youtube\.com/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m and YT_ID_RE.match(m.group(1)) else None


def ytimg_url(vid: str, name: str) -> str | None:
    if not YT_ID_RE.match(vid or "") or name not in YTIMG_NAMES:
        return None
    return f"https://i.ytimg.com/vi/{vid}/{name}.jpg"


class _HostLimitRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlparse(newurl)
        if p.scheme != "https" or p.netloc not in YTIMG_HOSTS:
            return None
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def fetch_ytimg(url: str, dest: Path, timeout: float = 12) -> bool:
    """Download one allow-listed i.ytimg.com / img.youtube.com thumbnail. No other hosts."""
    if not isinstance(url, str):
        return False
    p = urlparse(url)
    if p.scheme != "https" or p.netloc not in YTIMG_HOSTS or p.query or p.fragment or p.params:
        return False
    if not re.fullmatch(r"/vi/[A-Za-z0-9_-]{11}/(maxresdefault|sddefault|hqdefault)\.jpg", p.path):
        return False
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_HostLimitRedirect())
    try:
        with opener.open(url, timeout=timeout) as r:
            final = urlparse(r.geturl())
            if final.scheme != "https" or final.netloc not in YTIMG_HOSTS:
                return False
            data = r.read(500_000)
            if len(data) < 2000 or data[:2] != b"\xff\xd8":
                return False
            dest.write_bytes(data)
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return False


def ytdlp_cookie_args() -> list[str]:
    """Cookies from the box Chrome profile, an explicit browser, or a cookies.txt path.

    Never committed. On GitHub Actions this stays off unless VICE_YT_COOKIES* is set.
    """
    import os
    browser = (os.environ.get("VICE_YT_COOKIES_BROWSER") or "").strip()
    if not browser and not os.environ.get("GITHUB_ACTIONS"):
        chrome = Path.home() / ".config/google-chrome/Default/Cookies"
        if chrome.exists():
            browser = "chrome"
    if browser and not browser.startswith("-") and re.fullmatch(r"[\w:/.+-]{1,200}", browser):
        return ["--cookies-from-browser", browser]
    path = (os.environ.get("VICE_YT_COOKIES") or "").strip()
    if path and Path(path).exists():
        return ["--cookies", path]
    return []


JS_RUNTIME_RX = re.compile(r"^(deno|node|bun|quickjs)(:/[\w./-]{1,200})?$")


def ytdlp_js_args() -> list[str]:
    """Box only: VICE_YTDLP_JS_RUNTIME (e.g. deno:/home/box/.deno/bin/deno) for YouTube's JS challenge.

    Ignored on GitHub Actions and when the value is not a plain runtime name / absolute path.
    """
    import os
    if os.environ.get("GITHUB_ACTIONS"):
        return []
    r = (os.environ.get("VICE_YTDLP_JS_RUNTIME") or "").strip()
    return ["--js-runtimes", r] if r and JS_RUNTIME_RX.match(r) else []


def _cookie_flags_in(args: list) -> bool:
    return any(a in ("--cookies-from-browser", "--cookies") for a in args)


def ytdlp(args: list, timeout=1800) -> tuple[bool, str]:
    """Run yt-dlp with --remote-components ejs:github, box cookies, and client fallbacks."""
    err = ""
    cookies = [] if _cookie_flags_in(args) else ytdlp_cookie_args()
    bot_hits = 0
    for i, extra in enumerate(YTDLP_CLIENT_FALLBACKS):
        r = run(["yt-dlp", *COMMON, *ytdlp_js_args(), *cookies, *extra, *args], timeout=timeout, check=False)
        if r.returncode == 0:
            return True, ""
        err = (r.stderr or r.stdout)[-600:]
        if re.search(r"confirm your age|Private video|Video unavailable|removed|copyright|members-only", err):
            log("yt-dlp: not downloadable without login (age-restricted/private/removed) -> skip")
            return False, err
        bot = "not a bot" in err or "confirm you" in err or "bot" in err.lower()
        log(f"yt-dlp attempt {i + 1}/{len(YTDLP_CLIENT_FALLBACKS)} failed{' (bot check)' if bot else ''}: "
            f"{err.strip().splitlines()[-1][:160] if err.strip() else '?'}")
        if bot:
            bot_hits += 1
            # CI has no YouTube cookies; 5 client retries just hang the produce job for hours
            if bot_hits >= 2:
                log("yt-dlp: bot check persists, skipping remaining clients")
                return False, err
        time.sleep(3 + 4 * i)
    return False, err


def list_channel(url: str, limit: int = 60) -> list[dict]:
    for attempt in range(3):
        r = run(["yt-dlp", *COMMON, *ytdlp_js_args(), *ytdlp_cookie_args(), "--flat-playlist", "-I", f"1:{limit}",
                 "-J", url], timeout=300, check=False)
        # YouTube intermittently answers the tab continuation page with HTTP 403: wait and retry
        if r.returncode == 0 or "403" not in (r.stderr or "") or attempt == 2:
            break
        time.sleep(15 * (attempt + 1))
    if r.returncode != 0:
        log("channel listing failed:", url, (r.stderr or "")[-200:].strip())
        return []
    d = json.loads(r.stdout)
    out = []
    for e in d.get("entries") or []:
        out.append({"id": e.get("id"), "title": e.get("title") or "", "duration": e.get("duration") or 0,
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "channel": d.get("channel") or d.get("uploader") or "",
                    "verified": bool(d.get("channel_is_verified", True))})
    return out


def search_youtube(query: str, n: int = 15) -> list[dict]:
    r = run(["yt-dlp", *COMMON, *ytdlp_js_args(), *ytdlp_cookie_args(), "--flat-playlist", "-J", f"ytsearch{n}:{query}"], timeout=300, check=False)
    if r.returncode != 0:
        return []
    out = []
    for e in json.loads(r.stdout).get("entries") or []:
        out.append({"id": e.get("id"), "title": e.get("title") or "", "duration": e.get("duration") or 0,
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "channel": e.get("channel") or e.get("uploader") or "",
                    "verified": bool(e.get("channel_is_verified"))})
    return out


def search_x(query: str, n: int = 8) -> list[dict]:
    """Best-effort X (Twitter) video search for full-scene uploads. Returns [] on failure."""
    from urllib.parse import quote
    url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=video"
    r = run(["yt-dlp", *COMMON, *ytdlp_js_args(), *ytdlp_cookie_args(), "--flat-playlist", "-I", f"1:{n}", "-J", url],
            timeout=90, check=False)
    if r.returncode != 0 or not (r.stdout or "").strip():
        log("x search failed:", query[:80], ((r.stderr or "")[-180:]).strip())
        return []
    try:
        data = json.loads(r.stdout)
    except Exception:
        return []
    out = []
    for e in data.get("entries") or []:
        if not e:
            continue
        eid = str(e.get("id") or "")
        u = e.get("url") or e.get("original_url") or ""
        if not u and eid:
            u = f"https://x.com/i/status/{eid}"
        if not u:
            continue
        out.append({"id": f"x{eid}" if eid and not eid.startswith("x") else (eid or u),
                    "title": e.get("title") or e.get("description") or "",
                    "duration": int(e.get("duration") or 0),
                    "url": u,
                    "channel": e.get("uploader") or e.get("channel") or e.get("uploader_id") or "",
                    "verified": bool(e.get("channel_is_verified") or e.get("uploader_id_is_verified")),
                    "via": "x-search"})
    return out


def gemini_discover(gem, category: str, cfg: dict) -> list[dict]:
    """Gemini + Google Search: find official, clean video links (YouTube or X) for the category."""
    q = (f"Find up to 8 video links (YouTube watch URLs or x.com status URLs) for: {cfg.get('discover', category)}. "
         "Want FULL SCENES, official clips, full episodes, official gameplay, official highlights or press "
         "conferences. NEVER trailers, teasers, promos or TV spots. Prefer official studio / streamer / "
         "clip-channel / league / club accounts, or full-quality official scene uploads. No reaction or recap "
         "channels. Answer as JSON list of objects with url, title, channel.")
    if getattr(gem, "quota_exhausted", lambda: False)():
        log("gemini discovery skipped: all keys quota-exhausted")
        return []
    try:
        txt = gem.generate([{"text": q}], search=True, models=LITE_MODELS + FULL_MODELS)
        from .gemini import parse_json
        items = parse_json(txt) if isinstance(txt, str) else txt
        out = []
        for it in items if isinstance(items, list) else []:
            u = str(it.get("url", ""))
            m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", u)
            if m:
                out.append({"id": m.group(1), "url": f"https://www.youtube.com/watch?v={m.group(1)}",
                            "title": it.get("title", ""), "channel": it.get("channel", ""), "duration": 0,
                            "verified": False, "via": "gemini-search"})
            elif re.search(r"(x|twitter)\.com/\w+/status/\d+", u):
                sid = re.search(r"status/(\d+)", u).group(1)
                out.append({"id": f"x{sid}", "url": u, "title": it.get("title", ""), "channel": it.get("channel", ""),
                            "duration": 0, "verified": False, "via": "gemini-search"})
        return out
    except Exception as e:
        log("gemini discovery failed:", str(e)[:200])
        return []


def inbox_links(category: str) -> list[dict]:
    """inbox/links.txt lines: `<category> <url> [credit...]` (added by a scheduler / Grok / Gemini / user)."""
    p = REPO / "inbox" / "links.txt"
    out = []
    if not p.exists():
        return out
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0] == category:
            u = parts[1]
            m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", u)
            sid = m.group(1) if m else "l" + re.sub(r"\W", "", u)[-24:]
            out.append({"id": sid, "url": u, "title": "", "channel": parts[2] if len(parts) > 2 else "",
                        "duration": 0, "verified": True, "via": "inbox"})
    return out


SID_RX = re.compile(r"^[\w-]{1,40}$")


def load_manual_picks(cfg: dict) -> dict:
    """queue/manual_picks.json (branch pipeline-state, restored by produce.yml) — plain data, never executed.

    Only used while Gemini is out of quota: a bot / the owner looked at the clips listed in
    queue/needs_manual_pick.json and wrote
      {"picks": [{"id": "<source id>", "category": "gta", "accept": true, "has_dialogue": false,
                  "reasons": ["..."]}]}
    Unknown categories, bad ids and non-boolean accept values are dropped.
    """
    raw = None
    try:
        raw = json.loads((home() / "queue" / "manual_picks.json").read_text())
    except Exception:
        return {}
    out = {}
    for it in (raw.get("picks") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict):
            continue
        sid, cat, acc = it.get("id"), it.get("category"), it.get("accept")
        if not isinstance(sid, str) or not SID_RX.match(sid) or cat not in cfg.get("categories", {}) \
                or not isinstance(acc, bool):
            continue
        reasons = [str(r)[:120] for r in (it.get("reasons") or []) if isinstance(r, (str, int, float))][:5]
        out[sid] = {"category": cat, "accept": acc, "has_dialogue": bool(it.get("has_dialogue")),
                    "reasons": reasons}
    return out


class Sourcer:
    def __init__(self, cfg: dict, state, gem):
        self.cfg, self.state, self.gem = cfg, state, gem
        self.dir = home() / "sources"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.proxies = home() / "work" / "proxies"
        self.proxies.mkdir(parents=True, exist_ok=True)
        self.stats = {"downloaded": 0, "download_failed": 0, "vetted": 0, "rejected": [], "accepted": [],
                      "bot_checks": 0, "fallback_used": False, "thin": [], "runner_skipped": 0,
                      "box_clips_used": 0}
        # After this many YouTube bot checks in one run the runner stops downloading from YouTube and
        # uses only the clips the box already put in the "sources" release (runner = fallback only).
        self.bot_limit = max(1, int(os.environ.get("VICE_RUNNER_BOT_LIMIT", "4") or 4))
        self.runner_blocked = False
        # Candidates that could not be watch-checked because every Gemini key is out of quota.
        # Written to queue/needs_manual_pick.json so a bot can pick by hand (no polling).
        self.needs_manual: dict[str, list[dict]] = {}
        self.manual = load_manual_picks(self.cfg)
        self._migrate_vet_rules()

    def _migrate_vet_rules(self):
        """Forget old GTA/Rockstar rejects that were only 'trailer/promo/not editable' so they get re-vetted
        under GTA_ROCKSTAR_RULE. Watermark / logo / HUD / subtitle / low-res rejects stay rejected."""
        d = self.state.d
        if d.get("vet_rules") == VET_RULES_VERSION:
            return
        hard = re.compile(r"(?i)watermark|logo|subtitle|hud|minimap|\d{3,4}p|blur|upscal|reaction|picture")
        vet = d.setdefault("vet", {})
        for key, v in list(vet.items()):
            if v.get("accept") or v.get("category") != "gta":
                continue
            if not category_rule("gta", v.get("channel") or v.get("credit") or ""):
                continue
            if any(hard.search(str(r)) for r in (v.get("reject_reasons") or [])):
                continue
            vet.pop(key, None)
            (d.get("pre_vet") or {}).pop(key, None)
        d["vet_rules"] = VET_RULES_VERSION

    # ---------------------------------------------------------------- candidates
    def candidates(self, category: str, fallback: bool = False) -> list[dict]:
        c = self.cfg["categories"][category]
        out = inbox_links(category)
        # clips the BOX downloaded for us (box_fetch.py -> release tag "sources"), already on disk
        man = self.dir / "manifest.json"
        if man.exists():
            for e in json.loads(man.read_text()).get("clips", []):
                if e.get("category") == category and self.local_file(e["id"]):
                    out.append({"duration": 0, "verified": True, **e, "via": "box-fetch"})
        if not fallback:
            for ch in c.get("channels", []):
                key = f"{ch['url']}|{time.strftime('%Y-%m-%d')}"
                lst = self.state.d["listing_cache"].get(key)
                if lst is None:
                    lst = list_channel(ch["url"], ch.get("limit", 60))
                    self.state.d["listing_cache"] = {k: v for k, v in self.state.d["listing_cache"].items()
                                                     if k.endswith(time.strftime('%Y-%m-%d'))}
                    self.state.d["listing_cache"][key] = lst
                rx = re.compile(ch.get("title_re", "."), re.I)
                skip = re.compile(ch.get("skip_re", r"$^"), re.I)
                for e in lst:
                    if rx.search(e["title"]) and not skip.search(e["title"]) and \
                            c.get("min_len", 30) <= (e["duration"] or 0) <= c.get("max_len", 600):
                        out.append({**e, "credit": ch.get("credit") or e["channel"], "via": "official-channel"})
            for pin in c.get("pinned", []):
                out.append({"duration": 0, "verified": True, "via": "official-pinned", **pin})
            # seed search queries work without Gemini (official allow-listed / verified channels only)
            allow = {a.lower() for a in c.get("allow_channels", [])}
            for q in c.get("search", []):
                for e in search_youtube(q):
                    ch = (e.get("channel") or "").lower()
                    scene = is_full_scene_upload(e.get("title") or "")
                    if (ch in allow or e.get("verified") or scene) and \
                            c.get("min_len", 30) <= (e["duration"] or 0) <= c.get("max_len", 600):
                        out.append({**e, "credit": e["channel"], "via": "youtube-search"})
            # X / Twitter full-scene posts (movie, series, romance, sports)
            if category in SINGLE_WORK_CATEGORIES or category == "sports":
                for q in c.get("search", [])[:2]:
                    xq = q if re.search(r"(?i)full\s+scene", q) else f"{q} full scene"
                    for e in search_x(xq, n=6):
                        title = e.get("title") or ""
                        if is_trailer_title(title):
                            continue
                        dur = e.get("duration") or 0
                        if dur and not (c.get("min_len", 30) <= dur <= c.get("max_len", 600)):
                            continue
                        out.append({**e, "credit": e["channel"], "via": "x-search"})
        else:
            for e in gemini_discover(self.gem, category, c):
                out.append({**e, "credit": e["channel"]})
        seen, uniq = set(), []
        for e in out:
            if is_trailer_title(e.get("title") or ""):
                continue
            if e["id"] and e["id"] not in seen:
                seen.add(e["id"])
                uniq.append(e)
        return uniq

    # ---------------------------------------------------------------- download
    def local_file(self, sid: str) -> Path | None:
        for p in self.dir.glob(f"{sid}.*"):
            if p.suffix in (".mkv", ".mp4", ".webm") and p.stat().st_size > 100_000:
                return p
        return None


    def _thumb_frames(self, e: dict) -> list[Path]:
        vid = youtube_video_id(e)
        sid = safe_sid(e.get("id")) or (vid or "")
        if not vid or not safe_sid(sid):
            return []
        tdir = self.proxies / "preview"
        tdir.mkdir(parents=True, exist_ok=True)
        out = []
        for name in YTIMG_NAMES:
            url = ytimg_url(vid, name)
            if not url:
                continue
            fp = tdir / f"{sid}_{name}.jpg"
            if (fp.exists() and fp.stat().st_size >= 2000) or fetch_ytimg(url, fp):
                out.append(fp)
                break
        return out

    def _short_preview(self, e: dict) -> Path | None:
        """8s low-res partial download for preview frames. Never a full-quality copy."""
        sid, url = safe_sid(e.get("id")), safe_media_url(e.get("url"))
        if not sid or not url or self.runner_blocked:
            return None
        pdir = self.proxies / "preview"
        pdir.mkdir(parents=True, exist_ok=True)
        for p in pdir.glob(f"{sid}_prev.*"):
            if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4v"} and p.stat().st_size > 2000:
                return p
        tmpl = pdir / f"{sid}_prev.%(ext)s"
        ok, _err = ytdlp([
            "-f", "wv*[height<=360]/w*[height<=360]/worst",
            "--download-sections", "*0-8",
            "--merge-output-format", "mp4",
            "-o", str(tmpl),
            url,
        ], timeout=240)
        if not ok:
            return None
        for p in pdir.glob(f"{sid}_prev.*"):
            if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".m4v"} and p.stat().st_size > 2000:
                return p
        return None

    def _preview_frames(self, e: dict) -> list[Path]:
        frames = list(self._thumb_frames(e))
        prev = self._short_preview(e)
        if prev:
            key = f"{safe_sid(e.get('id')) or 'prev'}_pre"
            try:
                dur = min(8.0, float(probe(prev)["format"]["duration"]))
            except Exception:
                dur = 8.0
            for _ts, fp in self._keyframes(prev, key, dur, n=6, width=960):
                frames.append(fp)
        return frames

    def pre_vet(self, e: dict, category: str) -> bool:
        """4-corner watermark check on preview stills. True means it is ok to full-download."""
        key = e["id"]
        cached = (self.state.d.get("vet") or {}).get(key)
        if cached:
            return bool(cached.get("accept"))
        prior = (self.state.d.get("pre_vet") or {}).get(key)
        if prior is not None:
            return bool(prior.get("accept"))
        mp = self.manual.get(key)
        if mp and mp["category"] == category:
            return mp["accept"]
        if self.gem.quota_exhausted():
            self.note_manual(e, category)
            raise GeminiError(f"skip pre-vet for {key}: all Gemini keys quota-exhausted")
        frames = self._preview_frames(e)
        if not frames:
            log(f"pre-vet skip (no preview frames) [{key}] — not downloading")
            return False
        prompt = PRE_VET_PROMPT.format(n=len(frames), category=category,
                                       title=e.get("title", ""), channel=e.get("channel", ""))
        try:
            res = self.gem.generate([{"text": prompt}], schema=PRE_VET_SCHEMA,
                                    images=frames, temperature=0.1,
                                    models=LITE_MODELS + FULL_MODELS)
        finally:
            root = self.proxies.resolve()
            for fp in frames:
                try:
                    if root in Path(fp).resolve().parents:
                        Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass
        flags = [k for k in ("watermark", "channel_logo") if res.get(k)]
        if flags:
            res["accept"] = False
            res.setdefault("reject_reasons", [])
            res["reject_reasons"] += [f"flag:{f}" for f in flags]
        accepted = bool(res.get("accept"))
        reasons = res.get("reject_reasons") or ([] if accepted else ["preview watermark"])
        self.state.d.setdefault("pre_vet", {})[key] = {
            "accept": accepted, "reject_reasons": reasons, "vet_via": "preview",
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
        if not accepted:
            self.state.d["vet"][key] = {
                "accept": False, "reject_reasons": reasons, "bad_ranges": [],
                "checked_by": "gemini", "vet_via": "preview",
                "title": e.get("title"), "channel": e.get("channel"), "credit": e.get("credit"),
                "url": e.get("url"), "via": e.get("via"), "category": category,
                "at": time.strftime("%Y-%m-%d %H:%M"),
            }
            self.stats["vetted"] += 1
            self.stats["rejected"].append(
                {"id": key, "title": e.get("title"), "reasons": reasons, "via": e.get("via")})
            log(f"pre-vet REJECT [{key}] {str(e.get('title', ''))[:70]} — {'; '.join(reasons)}")
        else:
            log(f"pre-vet PASS [{key}] {str(e.get('title', ''))[:70]}")
        self.state.save()
        return accepted

    def download(self, e: dict) -> Path | None:
        f = self.local_file(e["id"])
        if f:
            return f
        if self.runner_blocked:
            self.stats["runner_skipped"] += 1
            return None
        out = self.dir / f"{e['id']}.%(ext)s"
        h = self.cfg.get("download_height", 1440)
        ok, err = ytdlp(["-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b", "-S", f"res:{h},fps",
                         "--merge-output-format", "mkv", "-o", str(out), e["url"]])
        if "not a bot" in err:
            self.stats["bot_checks"] += 1
            if self.stats["bot_checks"] >= self.bot_limit and not self.runner_blocked:
                self.runner_blocked = True
                log(f"runner: {self.stats['bot_checks']} YouTube bot checks -> no more runner downloads this run; "
                    f"using box clips (release 'sources') only")
        if not ok:
            self.stats["download_failed"] += 1
            self.stats.setdefault("failed_ids", []).append(e["id"])
            return None
        self.stats["downloaded"] += 1
        return self.local_file(e["id"])

    # ---------------------------------------------------------------- vet
    def _keyframes(self, path: Path, key: str, dur: float, n: int = 8, width: int = 854) -> list[tuple[float, Path]]:
        """6-8 evenly spaced LOW-QUALITY 480p JPEG proxies (+1 corner-crop sheet). Never send the whole
        video to Gemini; final renders always use the original full-quality file."""
        n = max(6, min(8, int(n)))
        # skip the first ~5% and last ~8% (studio cards / end slates still usually appear)
        lo, hi = dur * 0.05, dur * 0.92
        if hi <= lo:
            lo, hi = 0.0, max(dur, 0.1)
        times = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        frames = []
        kdir = self.proxies / "keyframes"
        kdir.mkdir(parents=True, exist_ok=True)
        for i, ts in enumerate(times):
            fp = kdir / f"{key}_k{i}.jpg"
            if not fp.exists() or fp.stat().st_size < 400:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.3f}", "-i", path,
                     "-frames:v", "1", "-vf", f"scale={width}:-2,format=yuvj420p", "-q:v", "6", str(fp)], timeout=60)
            if fp.exists() and fp.stat().st_size >= 400:
                frames.append((ts, fp))
        # one small sheet of the 4 corners at higher detail (where channel bugs / watermarks sit)
        if frames and width < 960:
            try:
                sheet = self._corner_sheet(path, key, frames[len(frames) // 2][0], kdir)
                if sheet:
                    frames.append((frames[len(frames) // 2][0], sheet))
            except Exception as ex:
                log("corner sheet skipped:", str(ex)[:120])
        return frames

    def _corner_sheet(self, path: Path, key: str, ts: float, kdir: Path) -> Path | None:
        from PIL import Image
        full = kdir / f"{key}_full.jpg"
        out = kdir / f"{key}_corners.jpg"
        if out.exists() and out.stat().st_size >= 400:
            return out
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.3f}", "-i", path, "-frames:v", "1",
             "-vf", "scale=1920:-2,format=yuvj420p", "-q:v", "4", str(full)], timeout=60)
        try:
            im = Image.open(full).convert("RGB")
            w, h = im.size
            cw, ch = int(w * 0.22), int(h * 0.2)
            boxes = [(0, 0, cw, ch), (w - cw, 0, w, ch), (0, h - ch, cw, h), (w - cw, h - ch, w, h)]
            sheet = Image.new("RGB", (cw * 2 + 8, ch * 2 + 8), (255, 0, 255))
            for i, b in enumerate(boxes):
                sheet.paste(im.crop(b), ((i % 2) * (cw + 8), (i // 2) * (ch + 8)))
            sheet.save(out, quality=80)
        finally:
            full.unlink(missing_ok=True)
        return out if out.exists() else None

    def vet(self, e: dict, path: Path, category: str) -> dict:
        key = e["id"]
        cached = (self.state.d.get("vet") or {}).get(key)
        if cached:  # never re-vet the same source id
            return cached
        info = probe(path)
        vs = [s for s in info["streams"] if s.get("codec_type") == "video"]
        h = max((s.get("height") or 0) for s in vs) if vs else 0
        dur = float(info["format"]["duration"])
        if h < self.cfg.get("min_height", 720):
            res = {"accept": False, "reject_reasons": [f"only {h}p (< {self.cfg.get('min_height', 720)}p)"],
                   "bad_ranges": [], "checked_by": "local"}
        elif (self.manual.get(key) or {}).get("category") == category:
            mp = self.manual[key]
            res = {"accept": mp["accept"], "reject_reasons": mp["reasons"] if not mp["accept"] else [],
                   "bad_ranges": [], "has_dialogue": mp["has_dialogue"], "mood": "", "summary": "",
                   "checked_by": "manual", "vet_via": "queue/manual_picks.json"}
        elif self.gem.quota_exhausted():
            self.note_manual(e, category, local=True)
            raise GeminiError(f"skip new vet for {key}: all Gemini keys quota-exhausted (use cached vet only)")
        else:
            frames = self._keyframes(path, key, dur)
            if len(frames) < 4:
                raise GeminiError(f"could not extract keyframes for {key}")
            times = ", ".join(f"{ts:.1f}" for ts, _ in frames)
            prompt = VET_PROMPT.format(n=len(frames), times=times, category=category,
                                       title=e.get("title", ""), channel=e.get("channel", ""),
                                       via=e.get("via", "?"),
                                       category_rule=category_rule(category, e.get("channel", "")))
            try:
                res = self.gem.generate([{"text": prompt}], schema=VET_SCHEMA,
                                        images=[fp for _, fp in frames], temperature=0.1,
                                        models=LITE_MODELS + FULL_MODELS)
                res["checked_by"] = "gemini"
                res["vet_via"] = "keyframes"
                res["keyframe_times"] = [round(ts, 2) for ts, _ in frames]
            finally:
                for _, fp in frames:
                    fp.unlink(missing_ok=True)
            flags = [k for k in ("watermark", "channel_logo", "burned_subtitles", "hud_overlay") if res.get(k)]
            if flags or not res.get("official_quality") or not res.get("editable"):
                res["accept"] = False
                res.setdefault("reject_reasons", [])
                res["reject_reasons"] += [f"flag:{f}" for f in flags] + \
                    ([] if res.get("official_quality") else ["not official/full quality"]) + \
                    ([] if res.get("editable") else ["not editable"])
        res.update({"title": e.get("title"), "channel": e.get("channel"), "credit": e.get("credit"),
                    "url": e.get("url"), "via": e.get("via"), "height": h, "duration": dur,
                    "category": category, "at": time.strftime("%Y-%m-%d %H:%M")})
        self.state.d["vet"][key] = res
        self.state.save()
        self.stats["vetted"] += 1
        tag = "ACCEPT" if res["accept"] else "REJECT"
        log(f"vet {tag} [{key}] {e.get('title', '')[:70]} — {'; '.join(res.get('reject_reasons') or []) or res.get('mood', '')}")
        (self.stats["accepted"] if res["accept"] else self.stats["rejected"]).append(
            {"id": key, "title": e.get("title"), "reasons": res.get("reject_reasons"), "via": e.get("via")})
        return res

    def note_manual(self, e: dict, category: str, local: bool | None = None):
        """Remember a candidate Gemini could not check (quota) for queue/needs_manual_pick.json."""
        sid = safe_sid(e.get("id"))
        if not sid:
            return
        lst = self.needs_manual.setdefault(category, [])
        if any(x["id"] == sid for x in lst) or len(lst) >= 25:
            return
        lst.append({"id": sid, "url": safe_media_url(e.get("url")) or "", "title": str(e.get("title") or "")[:140],
                    "channel": str(e.get("channel") or e.get("credit") or "")[:80], "via": e.get("via") or "",
                    "on_runner": bool(self.local_file(sid)) if local is None else local})

    def manual_pick_doc(self, platform: str) -> dict:
        from .gemini import next_quota_reset
        return {
            "platform": platform,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "reason": "all Gemini keys quota-exhausted: these clips were not watch-checked (logo/watermark)",
            "gemini_quota_resets": next_quota_reset().strftime("%Y-%m-%d %H:%M IST"),
            "categories": {k: v for k, v in self.needs_manual.items() if v},
            "how_to_pick": "Check each clip for watermarks, channel logos, burned subtitles and HUD. Then write "
                           "queue/manual_picks.json on branch pipeline-state: {\"picks\": [{\"id\": \"...\", "
                           "\"category\": \"...\", \"accept\": true|false, \"has_dialogue\": true|false, "
                           "\"reasons\": [\"...\"]}]}. The next produce run uses accepted picks.",
        }

    # ---------------------------------------------------------------- long sources -> chunks
    def chunk_long(self, sid: str, path: Path, dur: float, max_len: float = 300) -> list[tuple[str, Path]]:
        if dur <= max_len * 1.3:
            return [(sid, path)]
        cdir = self.dir / "chunks"
        cdir.mkdir(exist_ok=True)
        n = int(dur // max_len)
        out = []
        for i in range(n):
            p = cdir / f"{sid}_c{i:02d}.mkv"
            if not p.exists():
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{i * max_len:.1f}", "-i", path, "-t", f"{max_len:.1f}",
                     "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", p], timeout=1800)
            out.append((f"{sid}_c{i:02d}", p))
        return out

    # ---------------------------------------------------------------- main entry
    def gather(self, category: str, seed: int, want: int, need_dialogue: int = 1,
               chunks_per_source: int = 2) -> list[dict]:
        """Return `want` vetted clean clips: [{id, path, credit, title, url, mood, bad_ranges, has_dialogue}].

        Box clips (release "sources", already on disk) are tried first; runner YouTube downloads are only
        a fallback and stop for the run after repeated bot checks. movie/series/romance lock to one title
        and sports to one star player; if the top group yields nothing usable the next group is tried.
        """
        rng = random.Random(seed)
        picked: list[dict] = []
        lock_key = None  # one movie / series / player for the whole gather
        state = {"dialogue": 0}

        def rank(e):
            v = self.state.d["vet"].get(e["id"])
            if v is not None and not v.get("accept"):
                return None
            mp = self.manual.get(e["id"])
            if mp is not None and not mp["accept"]:
                return None
            # strongly prefer sources unused in the last 3 days so trailer 1/2 aren't re-picked
            uses = self.state.source_uses(e["id"], days=3)
            unused_penalty = 0 if uses == 0 else 20 + uses
            # clips already on disk (box release / cache) first: no runner download, no bot check
            local = -3.0 if self.local_file(e["id"]) else 0.0
            cached = -1.0 if v and v.get("accept") else 0.0  # already-vetted: no Gemini cost
            extra = 0.0
            if category == "gaming":
                if is_gta_title(e.get("title") or ""):
                    extra += 80  # GTA belongs in the gta category, not "other games"
                ch = (e.get("channel") or e.get("credit") or "").lower()
                if "rockstar" in ch:
                    extra += 4  # keep Rockstar, but don't fill the video with only RDR
            return unused_penalty + rng.random() * 0.5 + local + cached + extra

        def take(ranked: list[dict]):
            for e in ranked:
                if len(picked) >= want and state["dialogue"] >= need_dialogue:
                    break
                if any(p.get("src_id") == e["id"] for p in picked):
                    continue
                local = self.local_file(e["id"])
                if not local:
                    if self.runner_blocked:
                        self.stats["runner_skipped"] += 1
                        continue  # don't spend Gemini quota on clips the runner can't download
                    try:
                        if not self.pre_vet(e, category):
                            continue
                    except Exception as ex:
                        log("pre-vet error (will retry next run):", e["id"], str(ex)[:200])
                        continue
                path = self.download(e)
                if not path:
                    continue
                try:
                    v = self.vet(e, path, category)
                except Exception as ex:
                    log("vet error (will retry next run):", e["id"], str(ex)[:200])
                    continue
                if not v.get("accept"):
                    continue
                if e.get("via") in ("box-fetch", "pool"):
                    self.stats["box_clips_used"] += 1
                chunks = self.chunk_long(e["id"], path, v["duration"])
                rng.shuffle(chunks)
                for cid, cpath in chunks[:max(1, chunks_per_source)]:
                    off = 0.0
                    m = re.search(r"_c(\d+)$", cid)
                    if m:
                        off = int(m.group(1)) * 300.0
                    bad = [[b["start"] - off, b["end"] - off] for b in v.get("bad_ranges", [])
                           if b["end"] - off > 0 and b["start"] - off < 300]
                    picked.append({"id": cid, "src_id": e["id"], "path": str(cpath), "credit": v.get("credit"),
                                   "title": v.get("title"), "url": v.get("url"), "mood": v.get("mood"),
                                   "summary": v.get("summary"), "bad_ranges": bad,
                                   "has_dialogue": v.get("has_dialogue"), "work": e.get("work")})
                    state["dialogue"] += 1 if v.get("has_dialogue") else 0

        for fallback in (False, True):
            cands = self.candidates(category, fallback)
            if fallback:
                if len(picked) >= want:
                    break
                self.stats["fallback_used"] = True
                if category not in self.stats["thin"]:
                    self.stats["thin"].append(category)
                log(f"content thin for {category} ({len(picked)}/{want}) -> fallback search ({len(cands)} candidates)")
            # prefer box clips, then already-accepted, then never-vetted; rotate by recent use; never retry rejects
            ranked = sorted((e for e in cands if rank(e) is not None), key=rank)
            if category in SINGLE_WORK_CATEGORIES or category == "sports":
                groups: dict[str, list[dict]] = {}
                for e in ranked:
                    if category == "sports":
                        k = player_name(e.get("title") or "", e.get("channel") or e.get("credit") or "")
                        if not k:
                            continue
                    else:
                        # box title groups: the 2-3 extra videos of one title share "work"
                        k = (e.get("work") or work_title(e.get("title") or "", category) or e.get("id"))
                    groups.setdefault(k, []).append(e)

                def gkey(kv):
                    k, es = kv
                    ranks = [r for r in (rank(x) for x in es) if r is not None]
                    local = sum(1 for x in es if self.local_file(x["id"]))
                    return (-local, -len(es), min(ranks) if ranks else 99, k)
                order = [k for k, _ in sorted(groups.items(), key=gkey)]
                if lock_key is not None:
                    order = [lock_key] if lock_key in groups else []
                if not order and category == "sports":
                    log("sports: no candidate names a star player" + (" (fallback)" if fallback else ""))
                for i, k in enumerate(order[:6]):
                    before = len(picked)
                    take(groups[k])
                    if len(picked) > before:
                        lock_key = k
                        if i:
                            log(f"{category}: top group had no usable clips -> fell back to '{k}'")
                        break
            else:
                # Gaming: first pass prefers distinct games / publishers.
                if category == "gaming":
                    unique, dup = [], []
                    seen = set()
                    for e in ranked:
                        w = work_title(e.get("title") or "", "gaming") or e.get("id")
                        if w in seen:
                            dup.append(e)
                        else:
                            seen.add(w)
                            unique.append(e)
                    ranked = unique + dup
                take(ranked)
        return picked

    def cleanup(self, keep_gb: float = 12.0):
        """Keep the source cache under keep_gb (LRU by atime)."""
        files = sorted((p for p in self.dir.rglob("*") if p.is_file()), key=lambda p: p.stat().st_atime)
        total = sum(p.stat().st_size for p in files)
        while files and total > keep_gb * 1e9:
            p = files.pop(0)
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


def gemini_queries(gem, category: str, cfg: dict) -> list[str]:
    """Gemini writes YouTube search queries for the box downloader queue."""
    try:
        r = gem.generate([{"text": f"Write 4 YouTube search queries that find OFFICIAL or full-quality "
                                   f"full scenes / official clips / full episodes / official highlights "
                                   f"(NEVER trailers, teasers, promos or TV spots; no logo/subtitles/HUD) "
                                   f"for: {cfg.get('discover', category)}. Prefer official studio, streamer, "
                                   f"clip, league or club channels, or full-quality official scene uploads. "
                                   f"JSON list of strings."}],
                         schema={"type": "ARRAY", "items": {"type": "STRING"}}, temperature=0.7)
        return [q for q in r if isinstance(q, str)][:4]
    except Exception:
        return cfg.get("search", [])[:2]

