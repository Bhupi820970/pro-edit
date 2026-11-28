"""Backup song brain for when every Gemini key is out of quota.

Order: OpenRouter free *vision* models (they see 3-4 frames of the clips) -> Groq text models (they get a
scene description the code builds from the clips: category, title, mood words, cut speed, brightness,
dialogue). Each provider rotates over KEY, KEY_2, KEY_3 on 429/errors. Keys come only from env; they are
never logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from .util import log

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_VISION = ["nvidia/nemotron-3.5-super-vl:free", "google/gemma-4-31b-it:free",
                     "google/gemma-4-26b-a4b-it:free"]
GROQ_TEXT = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]


def env_keys(prefix: str) -> list[str]:
    out = []
    for name in (prefix, f"{prefix}_2", f"{prefix}_3", f"{prefix}_4", f"{prefix}_5"):
        v = (os.environ.get(name) or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def parse_json_list(text: str) -> list[dict]:
    """Pull a JSON array of song dicts out of a model reply (handles ```json fences / wrapper objects)."""
    if not text:
        return []
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    for rx in (r"\[.*\]", r"\{.*\}"):
        m = re.search(rx, text, re.S)
        if not m:
            continue
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(obj, dict):
            obj = next((v for v in obj.values() if isinstance(v, list)), [obj])
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict) and o.get("title") and o.get("artist")]
    return []


def _post(url: str, key: str, body: dict, timeout: float) -> str:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "pro-edit/1.0",
        "HTTP-Referer": "https://github.com/Bhupi820970/pro-edit", "X-Title": "pro-edit"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def ask(url: str, keys: list[str], models: list[str], content, timeout: float = 45) -> list[dict]:
    for model in models:
        for i, key in enumerate(keys):
            try:
                txt = _post(url, key, {"model": model, "temperature": 0.6,
                                       "messages": [{"role": "user", "content": content}]}, timeout)
                opts = parse_json_list(txt)
                if opts:
                    log(f"backup song brain: {model} (key {i + 1}) gave {len(opts)} options")
                    return opts
                log(f"backup song brain: {model} key {i + 1} returned no usable JSON")
                break  # model answered but badly -> next model, not next key
            except Exception as ex:
                code = getattr(ex, "code", "")
                log(f"backup song brain: {model} key {i + 1} failed {type(ex).__name__} {code}"[:160])
                if code not in (429, 402, 401, 403):
                    break
    return []


def clip_frames(clips: list[dict], per_clip: int = 1, limit: int = 4) -> list[str]:
    """base64 jpeg frames (512px) from the middle of each clip."""
    out = []
    for c in clips:
        p = Path(c.get("path") or "")
        if not p.is_file() or len(out) >= limit:
            continue
        for k in range(per_clip):
            with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
                try:
                    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                                "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                                               timeout=20).stdout.strip() or 0)
                    t = dur * (k + 1) / (per_clip + 1)
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(p),
                                    "-frames:v", "1", "-vf", "scale=512:-2", f.name], timeout=30, check=True)
                    out.append(base64.b64encode(Path(f.name).read_bytes()).decode())
                except Exception:
                    pass
    return out[:limit]


def clip_stats(path: str) -> dict:
    """Cheap scene facts: cuts per 10s and average brightness (0-255) over the first 30s."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "info", "-t", "30", "-i", path, "-vf",
                            "scale=160:-2,select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=60)
        cuts = r.stderr.count("pts_time:")
        r2 = subprocess.run(["ffmpeg", "-v", "info", "-t", "30", "-i", path, "-vf",
                             "fps=1,scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
                             "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=60)
        ys = [float(x) for x in re.findall(r"YAVG=([\d.]+)", r2.stderr)]
        return {"cuts_per_10s": round(cuts / 3.0, 1), "brightness": round(sum(ys) / len(ys)) if ys else None}
    except Exception:
        return {}


def scene_text(category: str, clips: list[dict]) -> str:
    lines = []
    for c in clips[:5]:
        st = clip_stats(c["path"]) if c.get("path") and Path(c["path"]).is_file() else {}
        b = st.get("brightness")
        tone = "" if b is None else ("dark" if b < 70 else "bright" if b > 150 else "normal light")
        pace = "" if not st else ("fast cuts" if st["cuts_per_10s"] >= 3 else
                                  "slow, long shots" if st["cuts_per_10s"] < 1 else "medium pace")
        lines.append(f'- "{c.get("title") or "?"}": mood {c.get("mood") or "n/a"}; '
                     f'{("what happens: " + str(c.get("summary"))[:160] + "; ") if c.get("summary") else ""}{pace}; {tone}; '
                     f'{"has spoken dialogue" if c.get("has_dialogue") else "no dialogue"}')
    return f"Edit category: {category}\nClips:\n" + "\n".join(lines)


MOOD_RULE = ("\n\nMOOD MATCH IS THE #1 RULE: first decide the scene's emotion from the frames/mood "
             "(fight/chase -> aggressive hype; sad/loss -> emotional; romance -> love song; funny -> playful; "
             "tense -> dark/suspense). Every option must match that emotion and energy; a famous song with the "
             "wrong mood is a wrong answer. Put the scene emotion in each 'why'.")

JSON_TAIL = ('\nAnswer ONLY with a JSON array (no prose), up to 6 items, best first: '
             '[{"title": "...", "artist": "...", "bpm": 120, "why": "short reason"}]. '
             'Prefer songs from the stock list when one fits.')


def backup_song_options(prompt: str, category: str, clips: list[dict], cfg: dict | None = None) -> list[dict]:
    cfg = cfg or {}
    if not cfg.get("song_backup_ai", True):
        return []
    orkeys, gkeys = env_keys("OPENROUTER_API_KEY"), env_keys("GROQ_API_KEY")
    if not orkeys and not gkeys:
        return []
    desc = scene_text(category, clips)
    if orkeys:
        frames = clip_frames(clips)
        content = [{"type": "text", "text": prompt + MOOD_RULE + "\n\nLook at the attached frames from the clips to judge "
                    "the real mood and energy.\n" + desc + JSON_TAIL}]
        content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in frames]
        opts = ask(OPENROUTER_URL, orkeys, cfg.get("openrouter_vision_models") or OPENROUTER_VISION, content)
        if opts:
            return opts
    if gkeys:
        opts = ask(GROQ_URL, gkeys, cfg.get("groq_models") or GROQ_TEXT, prompt + MOOD_RULE + "\n\n" + desc + JSON_TAIL)
        if opts:
            return opts
    return []


def parse_json_obj(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        o = json.loads(m.group(0))
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


VET_KEYS = ["accept", "official_quality", "watermark", "channel_logo", "burned_subtitles", "hud_overlay",
            "text_overlay", "editable", "has_dialogue", "mood", "summary", "reject_reasons", "bad_ranges"]


def backup_vet(prompt: str, image_paths: list, cfg: dict | None = None, timeout: float = 60) -> dict | None:
    """Footage check with an OpenRouter free vision model when every Gemini key is out. Strict: any overlay
    flag forces a reject, and a reply missing fields counts as no answer."""
    cfg = cfg or {}
    keys = env_keys("OPENROUTER_API_KEY")
    if not keys or not cfg.get("vet_backup_ai", True):
        return None
    content = [{"type": "text", "text": prompt + "\n\nAnswer ONLY with one JSON object with exactly these keys: "
                + ", ".join(VET_KEYS) + ". bad_ranges is a list of {start, end, reason}."}]
    for p in image_paths:
        try:
            b = base64.b64encode(Path(p).read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
        except Exception:
            pass
    for model in cfg.get("openrouter_vision_models") or OPENROUTER_VISION:
        for i, key in enumerate(keys):
            try:
                res = parse_json_obj(_post(OPENROUTER_URL, key, {"model": model, "temperature": 0.1,
                                     "messages": [{"role": "user", "content": content}]}, timeout))
            except Exception as ex:
                code = getattr(ex, "code", "")
                log(f"backup vet: {model} key {i + 1} failed {type(ex).__name__} {code}"[:160])
                if code in (429, 402, 401, 403):
                    continue
                break
            if not all(k in res for k in VET_KEYS):
                log(f"backup vet: {model} gave incomplete JSON")
                break
            flags = [k for k in ("watermark", "channel_logo", "burned_subtitles", "hud_overlay", "text_overlay")
                     if res.get(k) is True]
            if flags or res.get("official_quality") is False or res.get("editable") is False:
                res["accept"] = False
                res["reject_reasons"] = list(res.get("reject_reasons") or []) + flags
            if not isinstance(res.get("bad_ranges"), list):
                res["bad_ranges"] = []
            res["checked_by"] = f"backup:{model}"
            res["vet_via"] = "keyframes"
            return res
    return None
