"""Title / hook / description + credit block (Gemini-written, template fallback)."""
from __future__ import annotations

META_SCHEMA = {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"}, "hook": {"type": "STRING"}, "caption": {"type": "STRING"},
    "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["title", "hook", "caption", "hashtags"]}

PROMPT = """Write upload text for a {platform} {fmt} fan edit by the channel "{brand}".
Category: {category}. Footage: {footage}. Song: "{song}" by {artist}.
Lines the characters say on screen: {lines}
- title: <= 70 chars, punchy, no clickbait lies, no emojis at the start. 
- hook: <= 26 chars, ALL CAPS, no emoji (shown in a white bar above the video).
- caption: 1-2 short lines + a question to drive comments (emojis ok).
- hashtags: 15-22 relevant tags without '#', mix of broad + niche.
JSON only."""


def credit_block(song: dict, clips: list[dict], brand: str) -> str:
    foot = []
    for c in clips:
        s = f'{c.get("credit") or "official channel"} – "{c.get("title") or c["id"]}"'
        if s not in foot:
            foot.append(s)
    return "\n".join([
        f'🎵 Song: "{song["title"]}" – {song["artist"]} (original recording)',
        f'🔗 Official audio: {song["url"]}',
        f'🎧 Spotify: {song["spotify_search"]}',
        "🎬 Footage: " + "; ".join(foot),
        f"✂️ Edit by {brand}",
        "Fan-made edit for entertainment. All rights to the music and footage belong to their owners; "
        "no copyright infringement intended. Rights holders: contact us and we'll credit or remove.",
    ])


def write_meta(gem, platform: str, fmt: str, category: str, song: dict, clips: list[dict], lines: list[str],
               cfg: dict) -> dict:
    brand = cfg.get("brand", "Vice Cartel")
    footage = "; ".join(sorted({f'{c.get("title")} ({c.get("credit")})' for c in clips}))[:500]
    m = None
    try:
        m = gem.generate([{"text": PROMPT.format(platform=platform, fmt=fmt, brand=brand, category=category,
                                                 footage=footage, song=song["title"], artist=song["artist"],
                                                 lines=" / ".join(lines)[:700] or "none")}],
                         schema=META_SCHEMA, temperature=0.9)
    except Exception:
        pass
    if not m:
        base = cfg["categories"][category].get("title_fallback", "Edit")
        m = {"title": f"{base} | {song['title']} Edit", "hook": base.upper()[:26],
             "caption": "Which scene hit hardest? 👇", "hashtags": cfg["categories"][category].get("tags", [])}
    tags = [t.lstrip("#").replace(" ", "") for t in m["hashtags"]][:24]
    for t in cfg["categories"][category].get("tags", [])[:6]:
        if t not in tags:
            tags.append(t)
    hook = "".join(ch for ch in m["hook"].upper() if ord(ch) < 0x2000)[:28]
    desc = m["caption"].strip() + "\n\n" + credit_block(song, clips, brand) + "\n\n" + " ".join("#" + t for t in tags)
    return {"title": m["title"][:100], "hook": hook, "description": desc, "hashtags": tags}

