"""Hook-style upload titles and captions. Gemini first, deterministic template fallback.

Never writes "AI video". Few or no emojis. Always credits "Song: Title - Artist" when known.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "caption": {"type": "STRING"},
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["title", "caption", "hashtags"],
}

PROMPT = """Write upload text for a {platform} fan-edit by the channel Vice Cartel.
Category: {category}. Footage: {footage}. Song: "{song}" by {artist}.
On-screen hook (if any): {hook}
Existing title (if any): {title}
Rules:
- title: <= 70 chars, punchy hook, no clickbait lies, no emojis, never the words "AI video" or "AI-generated".
- caption: 1-2 short hook lines that make someone watch, then a short question. No emojis. Never say this is an AI video.
- hashtags: 10-18 relevant tags without '#', mix of broad and niche. No "AIvideo" or similar.
- tags: up to 12 YouTube tags (words or short phrases) without '#'.
JSON only."""

# Deterministic hook templates — unique per video via hash, no emojis.
TEMPLATES = {
    "gta": [
        "Vice City never sleeps",
        "One wrong turn in Vice City",
        "Night drive. No rules.",
        "This city hits different",
        "Neon, heat, and a bad idea",
    ],
    "gaming": [
        "This fight does not miss",
        "One moment. Whole lobby silent.",
        "The hit that ended it",
        "Clean shots, no mercy",
        "This is why we play",
    ],
    "movie": [
        "This scene still hits",
        "The cut you replay",
        "Watch this once. Then again.",
        "The moment everything turns",
        "Quiet build. Loud ending.",
    ],
    "series": [
        "This episode moment still hits",
        "The scene everyone paused",
        "One look. Whole story.",
        "You felt that too",
        "The turn you did not see",
    ],
    "sports": [
        "This play ended the argument",
        "Ice in the veins",
        "The moment the crowd broke",
        "One play. Whole season.",
        "Clutch does not blink",
    ],
    "romance": [
        "This look says it all",
        "The scene you feel twice",
        "Soft start. Heavy ending.",
        "You know this feeling",
        "The almost that still hurts",
    ],
}

TEMPLATES.update({
    "marvel": ["Heroes do not ask twice", "The moment the team assembled", "This is where it turned"],
    "dc": ["Gotham never sleeps", "The dark side of the hero", "One look. Whole legend."],
    "anime": ["The scene every fan replays", "Power level: unreal", "This opening still hits"],
    "celebrity": ["The moment the room stood up", "Red carpet, no rehearsal", "Nobody expected this"],
    "comedy": ["Try not to laugh at this one", "The line that broke everyone", "They could not keep a straight face"],
})

DEFAULT_TAGS = {
    "gta": ["GTA6", "GTAVI", "ViceCity", "GrandTheftAuto", "RockstarGames", "GTA"],
    "gaming": ["gaming", "gameplay", "edit"],
    "movie": ["movieedit", "films", "cinema"],
    "series": ["seriesedit", "tvedit"],
    "sports": ["sportsedit", "highlights"],
    "romance": ["romanceedit", "loveedit"],
    "marvel": ["marvel", "mcu", "marveledit"],
    "dc": ["dc", "dcedit", "batman"],
    "anime": ["anime", "animeedit", "crunchyroll"],
    "celebrity": ["celebrity", "redcarpet", "awards"],
    "comedy": ["comedy", "funny", "latenight"],
}

AI_RE = re.compile(
    r"(?i)\b(?:A\.?\s*I\.?|artificial\s+intelligence)\s*[- ]*\s*"
    r"(?:video|generated|made|created|edit|generated\s+video)\b"
    r"|\b(?:made|generated|created)\s+by\s+(?:A\.?\s*I\.?|artificial\s+intelligence)\b"
    r"|\bAIvideo\b|\bAI[-_]?generated\b"
)


def is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    if ch in "\u200d\ufe0e\ufe0f":
        return True
    if unicodedata.category(ch) == "So" and o >= 0x2000:
        return True
    return (
        0x1F000 <= o <= 0x1FAFF
        or 0x2600 <= o <= 0x27BF
        or 0x2300 <= o <= 0x23FF
        or 0x2B50 <= o <= 0x2B55
        or 0x1F1E0 <= o <= 0x1F1FF
        or 0xFE00 <= o <= 0xFE0F
        or 0xE0020 <= o <= 0xE007F
    )


def strip_emojis(text: str) -> str:
    out = []
    for ch in text or "":
        if is_emoji_char(ch):
            continue
        out.append(ch)
    s = "".join(out)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def strip_ai(text: str) -> str:
    return AI_RE.sub("", text or "").strip()


def clean_line(text: str, max_len: int | None = None) -> str:
    s = strip_emojis(strip_ai(text or ""))
    s = " ".join(s.split())
    if max_len is not None:
        s = s[:max_len].rstrip()
    return s


def clean_tags(tags, limit: int = 18) -> list[str]:
    out, seen = [], set()
    for t in tags or []:
        t = strip_emojis(str(t)).lstrip("#").strip().replace(" ", "")
        if not t or AI_RE.search(t) or t.lower() in {"aivideo", "aigenerated"}:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def song_credit(song: dict | None, song_credit_str: str | None = None) -> str:
    if song and song.get("title") and song.get("artist"):
        return f'Song: {song["title"]} - {song["artist"]}'
    if song_credit_str:
        s = strip_emojis(song_credit_str)
        s = re.sub(r'^[\"“”]+|[\"“”]+$', "", s.strip())
        if s.lower().startswith("song:"):
            return "Song:" + s[5:]
        # '"Title" – Artist' or 'Title – Artist'
        m = re.match(r'[\"“]?(.+?)[\"”]?\s+[–—-]\s+(.+)$', s)
        if m:
            return f"Song: {m.group(1).strip(' \"')} - {m.group(2).strip()}"
        return f"Song: {s}"
    return ""


def footage_credit(sources: list | None) -> str:
    foot = []
    for c in sources or []:
        if not isinstance(c, dict):
            continue
        who = c.get("credit") or "official channel"
        title = c.get("title") or c.get("id") or ""
        line = f'{who} – "{title}"' if title else who
        if line not in foot:
            foot.append(line)
    return "Footage: " + "; ".join(foot) if foot else ""


def _pick_template(category: str, video_id: str) -> str:
    opts = TEMPLATES.get(category) or TEMPLATES["gta"]
    h = hashlib.sha1(video_id.encode()).hexdigest()
    return opts[int(h[:8], 16) % len(opts)]


def fallback(item: dict) -> dict:
    category = item.get("category") or "gta"
    vid = item.get("id") or "video"
    hook = clean_line(item.get("hook") or "")
    title = clean_line(item.get("title") or "") or _pick_template(category, vid)
    caption = hook or title or _pick_template(category, vid)
    if len(caption.split()) < 3:
        caption = f"{caption}. Which moment hit hardest?"
    tags = clean_tags((item.get("hashtags") or []) + DEFAULT_TAGS.get(category, []), 18)
    if not tags:
        tags = list(DEFAULT_TAGS.get(category) or ["ViceCartel"])
    return {
        "title": title[:70],
        "caption": caption[:300],
        "hashtags": tags,
        "tags": tags[:12],
    }


def _from_gemini(gem, item: dict, platform: str) -> dict | None:
    if gem is None:
        return None
    song = item.get("song") or {}
    footage = "; ".join(
        sorted({f'{c.get("title")} ({c.get("credit")})' for c in (item.get("sources") or []) if isinstance(c, dict)})
    )[:500] or "official footage"
    try:
        m = gem.generate(
            [{"text": PROMPT.format(
                platform=platform,
                category=item.get("category") or "edit",
                footage=footage,
                song=song.get("title") or "unknown",
                artist=song.get("artist") or "unknown",
                hook=item.get("hook") or "none",
                title=item.get("title") or "none",
            )}],
            schema=SCHEMA,
            temperature=0.8,
        )
    except Exception:
        return None
    if not isinstance(m, dict) or not m.get("title") or not m.get("caption"):
        return None
    title_raw, caption_raw = str(m.get("title") or ""), str(m.get("caption") or "")
    # reject the whole Gemini draft if it mentions AI video, even after cleanup would leave leftovers
    if AI_RE.search(title_raw) or AI_RE.search(caption_raw):
        return None
    tags = clean_tags((m.get("hashtags") or []) + (m.get("tags") or []) + (item.get("hashtags") or []), 18)
    title = clean_line(title_raw, 70)
    caption = clean_line(caption_raw, 400)
    if not title or not caption or AI_RE.search(title + " " + caption):
        return None
    return {"title": title, "caption": caption, "hashtags": tags, "tags": tags[:12]}


def build(item: dict, platform: str, gem=None) -> dict:
    """Return {title, description, caption, hashtags, tags, song_credit} ready to upload."""
    base = fallback(item)
    got = _from_gemini(gem, item, platform) or {}
    title = clean_line(got.get("title") or base["title"], 70)
    caption = clean_line(got.get("caption") or base["caption"], 400)
    tags = clean_tags((got.get("hashtags") or []) + base["hashtags"] + (item.get("hashtags") or []), 18)
    credit = song_credit(item.get("song"), item.get("song_credit"))
    foot = footage_credit(item.get("sources"))
    hash_line = " ".join("#" + t for t in tags)
    body = [caption]
    if credit:
        body += ["", credit]
    if foot:
        body.append(foot)
    body += ["", "Edit by Vice Cartel"]
    if hash_line:
        body += ["", hash_line]
    description = "\n".join(body).strip()
    # Instagram caption: same text, 2200 cap
    ig = description[:2200].rstrip()
    return {
        "title": title[:100],
        "caption": caption,
        "description": description[:5000],
        "ig_caption": ig,
        "hashtags": tags,
        "tags": tags[:12],
        "song_credit": credit,
    }
