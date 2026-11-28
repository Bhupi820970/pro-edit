"""Title / hook / description + credit block (Gemini-written, template fallback)."""
from __future__ import annotations
import os

META_SCHEMA = {"type": "OBJECT", "properties": {
    "title": {"type": "STRING"}, "hook": {"type": "STRING"}, "caption": {"type": "STRING"},
    "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["title", "hook", "caption", "hashtags"]}

PROMPT = """Write upload text for a {platform} {fmt} fan edit by the channel "{brand}".
Category: {category}. Footage: {footage}. Song: "{song}" by {artist}.
Lines the characters say on screen: {lines}
- title: <= 70 chars, punchy, no clickbait lies, no emojis at the start. 
- hook: 3-7 words, <= 34 chars, ALL CAPS, no emoji. A SAVAGE viral hook line shown under the video for
  the whole edit: confident, cocky, a little disrespectful, makes people stop scrolling and comment
  (e.g. "HE DIDN'T COME TO TALK", "NOBODY SAW THIS COMING", "SHE CHOSE VIOLENCE TODAY",
  "THIS SCENE STILL HITS DIFFERENT"). Must fit THIS scene; never generic like "SERIES SCENE EDIT" or
  just the show name.
- caption: 1-2 short lines + a question to drive comments (emojis ok).
- hashtags: 15-22 relevant tags without '#', mix of broad + niche.
JSON only."""


SAVAGE_HOOKS = ["NOBODY SAW THIS COMING", "THIS SCENE STILL HITS DIFFERENT", "HE DIDN'T COME TO TALK",
                "WRONG PERSON TO TEST", "MAIN CHARACTER ENERGY", "THEY WERE NOT READY", "PURE DISRESPECT",
                "THIS IS WHY THEY FEAR HIM", "ZERO HESITATION", "WATCH TILL THE END"]


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
    # Hard cap: titles/captions must never eat the attempt budget (Gemini read timeouts used to cost ~12 min).
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        if gem is not None and not (hasattr(gem, "quota_exhausted") and gem.quota_exhausted()):
            fut = ex.submit(gem.generate, [{"text": PROMPT.format(platform=platform, fmt=fmt, brand=brand,
                                                                  category=category, footage=footage,
                                                                  song=song["title"], artist=song["artist"],
                                                                  lines=" / ".join(lines)[:700] or "none")}],
                            schema=META_SCHEMA, temperature=0.9)
            m = fut.result(timeout=int(os.environ.get("VICE_META_MAX_S", "90") or 90))
    except Exception:
        m = None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if not m:
        # Gemini out: text brains write the title/hook (Groq first, then OpenRouter)
        try:
            from .llm_fallback import env_keys, ask, GROQ_URL, GROQ_TEXT, OPENROUTER_URL, OPENROUTER_VISION
            txt = PROMPT.format(platform=platform, fmt=fmt, brand=brand, category=category, footage=footage,
                                song=song["title"], artist=song["artist"], lines=" / ".join(lines)[:700] or "none")
            txt += '\nAnswer ONLY as a JSON array with ONE object: [{"title":"...","hook":"...","caption":"...","hashtags":["..."]}]'
            for url, keys, models in ((GROQ_URL, env_keys("GROQ_API_KEY"), cfg.get("groq_models") or GROQ_TEXT),
                                      (OPENROUTER_URL, env_keys("OPENROUTER_API_KEY"),
                                       cfg.get("openrouter_vision_models") or OPENROUTER_VISION)):
                if not keys:
                    continue
                o = (ask(url, keys, models, txt, timeout=40) or [None])[0]
                if isinstance(o, dict) and all(isinstance(o.get(k), str) and o.get(k).strip()
                                               for k in ("title", "hook", "caption")):
                    o["hashtags"] = [t for t in (o.get("hashtags") or []) if isinstance(t, str)]
                    m = o
                    break
        except Exception:
            m = None
    if not m:
        base = cfg["categories"][category].get("title_fallback", "Edit")
        hook = base
        try:
            from .titles import work_title
            w = next((work_title(c.get("title") or "", category) for c in clips if c.get("title")), "") or ""
            w = str(w).strip()
            if w and len(w) <= 22:
                hook, base = f"{w} Edit", f"{w} Edit"
        except Exception:
            pass
        import random as _r
        hook = _r.choice(SAVAGE_HOOKS)
        m = {"title": f"{base} | {song['title']} Edit", "hook": hook.upper()[:34],
             "caption": "Which scene hit hardest? 👇", "hashtags": cfg["categories"][category].get("tags", [])}
    tags = [t.lstrip("#").replace(" ", "") for t in m["hashtags"]][:24]
    for t in cfg["categories"][category].get("tags", [])[:6]:
        if t not in tags:
            tags.append(t)
    hook = "".join(ch for ch in m["hook"].upper() if ord(ch) < 0x2000).strip()[:34]
    desc = m["caption"].strip() + "\n\n" + credit_block(song, clips, brand) + "\n\n" + " ".join("#" + t for t in tags)
    return {"title": m["title"][:100], "hook": hook, "description": desc, "hashtags": tags}

