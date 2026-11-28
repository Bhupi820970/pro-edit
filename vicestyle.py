#!/usr/bin/env python3
"""vicestyle — beat-synced "Vice City" fan-edit generator.

Style: hard cuts on the beat, full-screen 16:9 montage (music 100%) alternating
with 21:9 letterboxed dialogue scenes (music ducked, real dialogue + SFX loud),
word-synced neon captions beside the speaker, Miami-Vice grade, cut to black.

Two entry points:
  vicestyle.py auto   --song S --clips DIR --out OUT.mp4   (fully automatic)
  vicestyle.py render --song S --plan PLAN.json --out OUT.mp4 (hand-picked EDL)
Both accept --mode wide|shorts.  See README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import caption_patterns as CP  # noqa: E402

HERE = Path(__file__).resolve().parent
FPS = 30
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}

FONT_CANDIDATES = [
    str(HERE / "fonts" / "Anton-Regular.ttf"),
    "/usr/share/fonts/truetype/sand-box/google/Anton/Anton-Regular.ttf",
    "/usr/share/fonts/truetype/sand-box/google/Bebas Neue/BebasNeue-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
NEON = {
    "cyan": (0, 238, 255),
    "pink": (255, 46, 196),
    "yellow": (255, 228, 40),
    "purple": (178, 92, 255),
}
KEY_CYCLE = ["cyan", "pink", "yellow", "purple"]
FILLER = set("""a an the to of and or but so is are am was were be been i you he she it we they
me him her us them my your his its our their this that these those in on at for with as by
from up out do does did not no yes yeah oh uh um huh hey just got get gonna gotta will would
can could should there here what who how why when where if then than all some kind""".split())
GRADE = (
    "eq=contrast=1.13:saturation=1.38:brightness=0.012:gamma=0.97,"
    "colorbalance=rs=0.03:gs=-0.01:bs=0.05:rm=0.05:gm=0.0:bm=-0.03:rh=0.04:gh=0.01:bh=-0.04,"
    "vibrance=intensity=0.18"
)


def log(*a):
    print("[vicestyle]", *a, flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"cmd failed: {' '.join(map(str, cmd))}\n{r.stderr[-2000:]}")
    return r


def font_path(pref: str | None = None) -> str:
    for p in ([pref] if pref else []) + FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    raise FileNotFoundError("no bold font found; pass --font")


def cache_dir() -> Path:
    d = Path(os.environ.get("VICESTYLE_CACHE", HERE / "cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_key(path: Path, *extra) -> str:
    st = Path(path).stat()
    h = hashlib.sha1(f"{Path(path).resolve()}|{st.st_size}|{st.st_mtime}|{extra}".encode())
    return h.hexdigest()[:16]


def probe_duration(path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(r.stdout.strip())


def load_audio(path, sr, start=0.0, dur=None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-i", str(path)]
    if dur:
        cmd += ["-t", f"{dur:.4f}"]
    cmd += ["-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).copy()
    if dur:
        n = int(round(dur * sr))
        if len(a) < n:
            a = np.vstack([a, np.zeros((n - len(a), 2), np.float32)])
        a = a[:n]
    return a


# ---------------------------------------------------------------- beats
def detect_beats(song: Path) -> dict:
    ck = cache_dir() / f"beats_{file_key(song)}.json"
    if ck.exists():
        return json.loads(ck.read_text())
    import librosa
    y, sr = librosa.load(str(song), sr=22050, mono=True)
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time", tightness=200)
    beats = [float(b) for b in beats]
    # regularise: fit a straight grid through detected beats (disco = steady tempo)
    idx = np.arange(len(beats))
    k, b0 = np.polyfit(idx, np.array(beats), 1)
    resid = np.abs(np.array(beats) - (k * idx + b0))
    good = resid < 0.06
    k, b0 = np.polyfit(idx[good], np.array(beats)[good], 1)
    grid = [float(b0 + k * i) for i in range(len(beats) + 8) if 0 <= b0 + k * i < len(y) / sr]
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    info = {"tempo": 60.0 / k, "period": float(k), "beats": grid, "raw_beats": beats,
            "rms": rms[::4].tolist(), "rms_hop": 512 * 4 / sr, "duration": len(y) / sr}
    ck.write_text(json.dumps(info))
    return info


def pick_song_start(binfo: dict, total_beats: int) -> float:
    """Phrase-aligned (16-beat) start whose window has the most energy."""
    grid, rms, hop = binfo["beats"], np.array(binfo["rms"]), binfo["rms_hop"]
    period = binfo["period"]
    thr = 0.6 * np.percentile(rms, 90)
    first_loud = next(i for i, b in enumerate(grid) if rms[min(int(b / hop), len(rms) - 1)] > thr)
    best, best_s = -1, grid[first_loud]
    for i in range(first_loud % 16, len(grid) - total_beats, 16):
        s = grid[i]
        e = s + total_beats * period
        seg = rms[int(s / hop): int(e / hop)]
        tail = rms[int((e - 8) / hop): int(e / hop)]
        if len(seg) == 0:
            continue
        sc = seg.mean() + 0.5 * tail.mean() - (0.0 if seg.min() > thr * 0.3 else 1.0)
        if sc > best:
            best, best_s = sc, s
    return best_s


# ---------------------------------------------------------------- whisper
_WHISPER = None


def transcribe(src: Path, start: float, dur: float) -> list[dict]:
    """Word timings (absolute source seconds) for src[start:start+dur]. Cached."""
    ck = cache_dir() / f"words_{file_key(src, round(start, 2), round(dur, 2))}.json"
    if ck.exists():
        return json.loads(ck.read_text())
    global _WHISPER
    from faster_whisper import WhisperModel
    if _WHISPER is None:
        size = os.environ.get("VICESTYLE_WHISPER", "small")
        _WHISPER = WhisperModel(size, device="cpu", compute_type="int8")
    a = load_audio(src, 16000, start, dur).mean(axis=1).astype(np.float32)
    segs, _ = _WHISPER.transcribe(a, language="en", word_timestamps=True, vad_filter=True,
                                  beam_size=5, condition_on_previous_text=False)
    words = []
    for s in segs:
        n = len(s.words or [])
        span = s.end - s.start
        # reject sung lyrics / hallucinations: absurdly slow or no-speech segments
        if n == 0 or s.no_speech_prob > 0.6 or (span > 7 and n / span < 0.9):
            continue
        for w in s.words:
            words.append({"w": w.word.strip(), "s": start + w.start, "e": start + w.end,
                          "p": w.probability, "seg": round(start + s.start, 3)})
    ck.write_text(json.dumps(words))
    return words


# ---------------------------------------------------------------- data
@dataclass
class Seg:
    kind: str                # "montage" | "dialogue"
    src: str
    start: float             # source seconds
    beats: int
    name: str | None = None  # big neon name pop (only when the plan says so)
    name_color: str = "cyan"
    zoom: float = 1.0        # >1 crops in (hides HUD / native bars)
    fix: dict = field(default_factory=dict)       # word corrections
    drop: list = field(default_factory=list)      # words (lowercase) not to caption
    side: str | None = None  # force caption side "left"/"right"
    sfx: float = 0.0         # montage: source-audio level (0 = music only)
    gamma: float | None = None  # exposure lift; None = automatic for dark shots
    fix_phrases: list = field(default_factory=list)  # [{"from": "...", "to": "..."}]
    # filled at render time
    t0: float = 0.0
    t1: float = 0.0
    f0: int = 0
    f1: int = 0


# ---------------------------------------------------------------- video cuts
def shot_luma(src: str, t: float) -> float:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", src, "-frames:v", "1",
                        "-vf", "scale=160:90,format=gray", "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, np.uint8)
    return float(a.mean() / 255) if len(a) else 0.3


def seg_filter(seg: Seg, W: int, H: int) -> str:
    z = max(1.0, seg.zoom)
    if seg.gamma is None:
        dur = (seg.f1 - seg.f0) / FPS
        lum = np.mean([shot_luma(seg.src, seg.start + dur * k) for k in (0.2, 0.5, 0.8)])
        seg.gamma = round(float(min(1.45, (0.30 / lum) ** 0.5)) if lum < 0.24 else 1.0, 2)
    f = [f"scale={int(W * z)}:{int(H * z)}:force_original_aspect_ratio=increase:flags=lanczos",
         f"crop={W}:{H}", "setsar=1",
         "unsharp=5:5:0.35:5:5:0.0"]          # light luma sharpen after the Lanczos scale (sources >= 1080p)
    if seg.gamma and abs(seg.gamma - 1) > 0.01:
        f.append(f"eq=gamma={seg.gamma}")
    f.append(GRADE)
    if seg.kind == "dialogue":
        f.append("vignette=angle=PI/4.6")
        bar = int(round((H - W * 9 / 21) / 2))
        f.append(f"drawbox=x=0:y=0:w={W}:h={bar}:color=black:t=fill")
        f.append(f"drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=black:t=fill")
    f.append("noise=alls=5:allf=t")
    f.append(f"fps={FPS},format=yuv420p")
    return ",".join(f)


def cut_segment(seg: Seg, i: int, W: int, H: int, tmp: Path) -> Path:
    nfr = seg.f1 - seg.f0
    key = hashlib.sha1(f"{seg.src}|{seg.start:.4f}|{nfr}|{W}x{H}|{seg_filter(seg, W, H)}".encode()).hexdigest()[:20]
    cdir = cache_dir() / "cuts"
    cdir.mkdir(exist_ok=True)
    out = cdir / f"{key}.mp4"
    if out.exists() and out.stat().st_size > 1000:
        return out
    pre = max(0.0, seg.start - 2.0)
    # Unique temp name per worker: a looped timeline repeats the same cut, and two parallel workers
    # writing one shared .part file made the second os.replace fail (FileNotFoundError, 26 Nov).
    import threading
    part = f"{out}.{os.getpid()}-{threading.get_ident()}.part.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{pre:.4f}", "-i", seg.src,
           "-ss", f"{seg.start - pre:.4f}", "-vf", seg_filter(seg, W, H),
           "-frames:v", str(nfr), "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "13",
           "-pix_fmt", "yuv420p", part]
    run(cmd)
    try:
        os.replace(part, out)
    except FileNotFoundError:
        if not out.exists():
            raise
    return out


# ---------------------------------------------------------------- captions
def chunk_words(words: list[dict], max_words: int = 3) -> list[list[dict]]:
    chunks, cur = [], []
    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            if len(cur) >= max_words or gap > 0.28 or (w["e"] - cur[0]["s"]) > 1.1 or \
               re.search(r"[.,!?]$", cur[-1]["raw"]):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def apply_phrase_fixes(ws: list[dict], fixes: list[dict]) -> list[dict]:
    """Replace a mis-heard word run with the correct words, keeping its timing span."""
    norm = lambda t: re.sub(r"[^a-z']", "", t.lower())
    for fx in fixes:
        src = [norm(t) for t in fx["from"].split()]
        dst = fx["to"].split()
        keys = [norm(w["raw"]) for w in ws]
        for i in range(len(ws) - len(src) + 1):
            if keys[i:i + len(src)] == src:
                s0, e0 = ws[i]["s"], ws[i + len(src) - 1]["e"]
                step = (e0 - s0) / len(dst)
                new = [{"raw": t, "s": s0 + k * step, "e": s0 + (k + 1) * step} for k, t in enumerate(dst)]
                ws = ws[:i] + new + ws[i + len(src):]
                break
    return ws


def clean_word(w: str) -> str:
    return re.sub(r"[^A-Za-z0-9'?!]", "", w).upper()


def style_word(txt: str, color_i: int, H: int) -> tuple[tuple, int, int]:
    base = re.sub(r"[^a-z']", "", txt.lower())
    if base in FILLER or len(base) <= 2:
        return (255, 255, 255), int(H * 0.052), color_i
    col = NEON[KEY_CYCLE[color_i % len(KEY_CYCLE)]]
    return col, int(H * (0.098 if len(base) <= 8 else 0.082)), color_i + 1


def render_block(items: list[tuple], fpath: str, align: str, outline: str = "shadow",
                 visible: int | None = None) -> Image.Image:
    """items: (text, rgb, size[, small]). Consecutive small words share a line; big words get their own.
    outline: shadow (approved look) | heavy | glow.  visible: draw only the first N words (reveal)."""
    items = [(it[0], it[1], it[2], it[3] if len(it) > 3 else it[1] == (255, 255, 255)) for it in items]
    lines, cur, cur_small = [], [], None
    for t, c, s, small in items:
        if cur and (not small or not cur_small):
            lines.append(cur)
            cur = []
        cur.append((t, c, s))
        cur_small = small
    if cur:
        lines.append(cur)
    rendered = []
    for ln in lines:
        parts = []
        for t, c, s in ln:
            f = ImageFont.truetype(fpath, s)
            bb = f.getbbox(t)
            parts.append((t, c, s, f, bb))
        sp = int(max(p[2] for p in parts) * 0.25)
        lw = sum(p[4][2] - p[4][0] for p in parts) + sp * (len(parts) - 1)
        lh = max(p[4][3] for p in parts)
        rendered.append((parts, lw, lh, sp))
    pad = 24 if outline != "glow" else 40
    Wb = max(r[1] for r in rendered) + pad * 2
    gap = 6
    Hb = sum(r[2] for r in rendered) + gap * (len(rendered) - 1) + pad * 2
    img = Image.new("RGBA", (Wb, Hb), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (Wb, Hb), (0, 0, 0, 0))
    glow = Image.new("RGBA", (Wb, Hb), (0, 0, 0, 0)) if outline == "glow" else None
    d, ds = ImageDraw.Draw(img), ImageDraw.Draw(shadow)
    dg = ImageDraw.Draw(glow) if glow is not None else None
    y = pad
    n = 0
    for parts, lw, lh, sp in rendered:
        x = pad if align == "left" else (Wb - pad - lw if align == "right" else (Wb - lw) // 2)
        for t, c, s, f, bb in parts:
            n += 1
            if visible is not None and n > visible:
                x += bb[2] - bb[0] + sp
                continue
            off = max(3, s // 16)
            ty = y + (lh - bb[3])
            sw = max(2, s // 28) if outline != "heavy" else max(4, s // 13)
            ds.text((x - bb[0] + off, ty + off), t, font=f, fill=(0, 0, 0, 235))
            if dg is not None:
                dg.text((x - bb[0], ty), t, font=f, fill=c + (255,), stroke_width=max(4, s // 12), stroke_fill=c + (255,))
            d.text((x - bb[0], ty), t, font=f, fill=c + (255,),
                   stroke_width=sw, stroke_fill=(0, 0, 0, 255))
            x += bb[2] - bb[0] + sp
        y += lh + gap
    if glow is not None:
        g = glow.filter(ImageFilter.GaussianBlur(max(6, int(Hb * 0.06))))
        shadow = Image.alpha_composite(shadow, g)
    return Image.alpha_composite(shadow, img)


def neon_name(text: str, color: str, H: int, fpath: str) -> Image.Image:
    col = NEON.get(color, NEON["cyan"])
    s = int(H * 0.19)
    f = ImageFont.truetype(fpath, s)
    bb = f.getbbox(text)
    pad = int(s * 0.35)
    w, h = bb[2] - bb[0] + pad * 2, bb[3] + pad * 2
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((pad - bb[0], pad), text, font=f, fill=col + (255,))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.09))
    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    o = max(4, s // 18)
    deep = tuple(int(c * 0.25) for c in col)
    d.text((pad - bb[0] + o, pad + o), text, font=f, fill=deep + (255,))
    d.text((pad - bb[0], pad), text, font=f, fill=col + (255,),
           stroke_width=max(3, s // 40), stroke_fill=(255, 255, 255, 230))
    out = Image.alpha_composite(glow, glow)
    return Image.alpha_composite(out, base)


# Approved glass-text brand mark (see /tmp/glass2.py). Applied per frame so the
# letters blur the actual pixels underneath. Single logo, bottom-center only.
WM_HEIGHT = 0.034
WM_BLUR = 10
WM_BRIGHTNESS = 1.18
WM_TINT_ALPHA = 22
WM_SHADOW = 0.12
WM_GLASS_MASK = 0.60
WM_EDGE = 0.38
WM_BOTTOM = 0.14       # fraction of frame height above the bottom edge
WM_PAD = 12
HOLD_EXTRA_S = 1.5     # each montage shot holds ~this many seconds longer, still on the beat


def hold_beats(period: float, base: int = 1, extra_s: float = HOLD_EXTRA_S) -> int:
    """Raise a 1- or 2-beat cut by ~1.5s (at least 2 beats), staying on the beat grid."""
    extra = max(2, int(round(extra_s / max(period, 1e-6))))
    return int(base + extra)


def glass_logo_box(text: str, W: int, H: int, fpath: str, frac: float = WM_BOTTOM):
    """Return (x, y, bw, bh, font, bbox, pad) for the approved glass logo."""
    f = ImageFont.truetype(fpath, int(H * WM_HEIGHT))
    bb = f.getbbox(text)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pad = WM_PAD
    bw, bh = tw + 2 * pad, th + 2 * pad
    x = (W - bw) // 2
    y = H - bh - int(H * frac)
    return x, y, bw, bh, f, bb, pad


def apply_glass_logo(im: Image.Image, text: str, fpath: str, frac: float = WM_BOTTOM) -> Image.Image:
    """Paste the glass VICE CARTEL mark, blurring the live frame under the letters."""
    im = im.convert("RGBA")
    W, H = im.size
    x, y, bw, bh, f, bb, pad = glass_logo_box(text, W, H, fpath, frac)
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).text((pad - bb[0], pad - bb[1]), text, font=f, fill=255)
    # clip to frame in case of tiny canvases
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + bw), min(H, y + bh)
    if x1 <= x0 or y1 <= y0:
        return im
    # If the box hangs off the canvas, crop mask/reg the same way
    mx0, my0 = x0 - x, y0 - y
    reg = im.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(WM_BLUR))
    reg = ImageEnhance.Brightness(reg).enhance(WM_BRIGHTNESS)
    tint = Image.new("RGBA", reg.size, (255, 255, 255, WM_TINT_ALPHA))
    reg = Image.alpha_composite(reg.convert("RGBA"), tint)
    use = mask.crop((mx0, my0, mx0 + reg.size[0], my0 + reg.size[1]))
    shadow = use.filter(ImageFilter.GaussianBlur(4)).point(lambda v: int(v * WM_SHADOW))
    sx, sy = x0 + 2, y0 + 3
    im.paste(Image.new("RGBA", reg.size, (0, 0, 0, 255)), (sx, sy), shadow)
    im.paste(reg, (x0, y0), use.point(lambda v: int(v * WM_GLASS_MASK)))
    edge = ImageChops.subtract(use, use.filter(ImageFilter.MinFilter(3))).point(lambda v: int(v * WM_EDGE))
    im.paste(Image.new("RGBA", reg.size, (255, 255, 255, 255)), (x0, y0), edge)
    return im


def watermark_pos(text: str, W: int, H: int, fpath: str) -> tuple[int, int]:
    """Bottom-center of the glass logo box. Never top-right."""
    x, y, bw, bh, *_ = glass_logo_box(text, W, H, fpath)
    return x, y


_FACE = None
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def find_face(frame: np.ndarray):
    """Largest face (cx, cy, w) in relative coords via OpenCV YuNet, else None."""
    global _FACE
    import cv2
    if _FACE is None:
        mp = HERE / "models" / "face_detection_yunet_2023mar.onnx"
        if not mp.exists():
            mp.parent.mkdir(exist_ok=True)
            import urllib.request
            urllib.request.urlretrieve(YUNET_URL, mp)
        _FACE = cv2.FaceDetectorYN.create(str(mp), "", (640, 360), 0.55)
    h, w = frame.shape[:2]
    sc = 640 / w
    small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), (640, int(h * sc)))
    _FACE.setInputSize((small.shape[1], small.shape[0]))
    _, faces = _FACE.detect(small)
    if faces is None or len(faces) == 0:
        return None
    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
    return ((x + fw / 2) / small.shape[1], (y + fh / 2) / small.shape[0], fw / small.shape[1])


def find_faces(frame: np.ndarray) -> list[tuple]:
    """All faces as relative boxes (x0, y0, x1, y1) via OpenCV YuNet."""
    global _FACE
    import cv2
    if _FACE is None:
        find_face(np.zeros((360, 640, 3), np.uint8))
    h, w = frame.shape[:2]
    sc = 640 / w
    small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), (640, int(h * sc)))
    _FACE.setInputSize((small.shape[1], small.shape[0]))
    _, faces = _FACE.detect(small)
    if faces is None:
        return []
    sw, sh = small.shape[1], small.shape[0]
    return [(float(f[0] / sw), float(f[1] / sh), float((f[0] + f[2]) / sw), float((f[1] + f[3]) / sh)) for f in faces]


def _inter(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return float(w * h) if w > 0 and h > 0 else 0.0


def person_boxes(faces: list[tuple], W: int, H: int, tight: bool = False) -> list[tuple]:
    """Grow faces into person boxes (face + shoulders/torso). tight=face only (+25%)."""
    out = []
    for x0, y0, x1, y1 in faces:
        X0, Y0, X1, Y1 = x0 * W, y0 * H, x1 * W, y1 * H
        fw, fh = X1 - X0, Y1 - Y0
        if tight:
            out.append((X0 - 0.25 * fw, Y0 - 0.25 * fh, X1 + 0.25 * fw, Y1 + 0.25 * fh))
        else:
            out.append((X0 - 0.45 * fw, Y0 - 0.45 * fh, X1 + 0.45 * fw, Y1 + 0.35 * fh))   # head
            out.append((X0 - 0.9 * fw, Y1, X1 + 0.9 * fw, Y1 + 2.4 * fh))                  # shoulders/torso
    return out


def build_captions(segs: list[Seg], W: int, H: int, fpath: str, base_video: Path,
                   pattern: dict | None = None) -> list[dict]:
    if pattern is not None and not pattern.get("classic"):
        return build_captions_pattern(segs, W, H, fpath, base_video, pattern)
    return build_captions_classic(segs, W, H, fpath, base_video)


def caption_chunks(segs: list[Seg], max_words: int = 3):
    """Yield (seg, chunk_words, s, e) for every caption — words only while someone really speaks."""
    for seg in segs:
        if seg.kind != "dialogue":
            continue
        dur = seg.t1 - seg.t0
        words = transcribe(Path(seg.src), seg.start, dur + 0.3)
        ws = []
        for w in words:
            raw = w["w"]
            key = re.sub(r"[^a-z']", "", raw.lower())
            if key in seg.fix:
                raw = seg.fix[key]
            if key in seg.drop or not clean_word(raw):
                continue
            ts = seg.t0 + (w["s"] - seg.start)
            te = seg.t0 + (w["e"] - seg.start)
            if ts < seg.t0 or te > seg.t1 + 0.05:
                continue
            ws.append({"raw": raw, "s": ts, "e": min(te, seg.t1)})
        ws = apply_phrase_fixes(ws, seg.fix_phrases)
        chunks = chunk_words(ws, max_words)
        for ci, ch in enumerate(chunks):
            s = ch[0]["s"]
            e = ch[-1]["e"] + 0.18
            if ci + 1 < len(chunks):
                e = min(e, chunks[ci + 1][0]["s"])
            e = min(max(e, s + 0.25), seg.t1)
            yield seg, ch, s, e


def read_frames(video: Path, idxs: set[int]) -> dict:
    """Sequentially decode only the needed frames (RGB)."""
    import cv2
    out, cap = {}, cv2.VideoCapture(str(video))
    last = max(idxs) if idxs else -1
    fi = 0
    while fi <= last:
        if not cap.grab():
            break
        if fi in idxs:
            ok, fr = cap.retrieve()
            if ok:
                out[fi] = cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)
        fi += 1
    cap.release()
    return out


def _clear_spot(img, faces: list[tuple], W: int, H: int, top: int, bot: int, margin: int, pref: tuple):
    """Fallback when every beside-speaker candidate touches a face: scan the picture area for the
    zero-overlap spot nearest the preferred position, shrinking the block (keeps colours/pop style)
    up to 3 times. Returns (img, x, y, overlap_px) or None."""
    pbs = person_boxes(faces, W, H, tight=True)
    for _ in range(4):
        best = None
        xs = range(margin // 2, max(margin // 2 + 1, W - img.width - margin // 2 + 1), max(8, W // 40))
        ys = range(top, max(top + 1, bot - img.height + 1), max(8, H // 60))
        for y in ys:
            for x in xs:
                box = (x + 12, y + 12, x + img.width - 12, y + img.height - 12)
                if any(_inter(box, pb) for pb in pbs):
                    continue
                d = (x - pref[0]) ** 2 + (y - pref[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, x, y)
        if best:
            return img, best[1], best[2], 0
        img = img.resize((max(1, int(img.width * 0.85)), max(1, int(img.height * 0.85))))
    return None


def place_beside(items: list[tuple], pat: dict, step: int, faces: list[tuple], main, W: int, H: int,
                 fpath: str, alt: int):
    """Place one caption chunk beside the speaker (approved rule) with the pattern's rhythm/offsets.
    Returns (img, x, y, overlap_px, zone).  Never covers a face: tries mirror side and vertical shifts."""
    bar = int(round((H - W * 9 / 21) / 2))
    margin = int(W * 0.05)
    top, bot = bar + 8, H - bar - 8
    dy_list = pat["y"]
    dy = dy_list[step % len(dy_list)]
    cands = []
    if main:
        fx = (main[0] + main[2]) / 2 * W
        fw = (main[2] - main[0]) * W
        fh = (main[3] - main[1]) * H
        fy = (main[1] + main[3]) / 2 * H
        side = "right" if fx < W / 2 else "left"
        for sd in (side, "left" if side == "right" else "right"):
            img = render_block(items, fpath, "left" if sd == "right" else "right")
            for ddy in (dy, dy - 0.7, dy + 0.7, 0.0):
                if sd == "right":
                    x = int(min(fx + fw * pat["gap"], W - margin - img.width))
                else:
                    x = int(max(fx - fw * pat["gap"] - img.width, margin))
                cx = x + img.width / 2
                x += int(pat["nudge"] * W * (1 if cx < W / 2 else -1))   # slight nudge toward centre
                y = int(fy + ddy * fh - img.height * 0.5)
                cands.append((img, x, y, "beside" if sd == side else "beside-mirror"))
    else:
        sd = "left" if alt % 2 == 0 else "right"
        for s2 in (sd, "right" if sd == "left" else "left"):
            img = render_block(items, fpath, "left" if s2 == "right" else "right")
            x = margin if s2 == "left" else W - margin - img.width
            cands.append((img, x, int(H * pat["no_face_y"]), f"noface-{s2}"))
    best = None
    for img, x, y, zone in cands:
        y = max(top, min(y, bot - img.height))
        x = max(margin // 2, min(x, W - img.width - margin // 2))
        box = (x + 12, y + 12, x + img.width - 12, y + img.height - 12)
        ov = int(sum(_inter(box, pb) for pb in person_boxes(faces, W, H, tight=True)))
        if best is None or ov < best[3]:
            best = (img, x, y, ov, zone)
        if ov == 0:
            break
    if best and best[3] > 0:   # all candidates touch a face -> search a clear spot (QA never fails on faces)
        img, x, y = best[0], best[1], best[2]
        got = _clear_spot(img, faces, W, H, top, bot, margin, (x, y))
        if got:
            best = (got[0], got[1], got[2], 0, best[4] + "-clear")
    return best


def style_items(ch: list[dict], pat: dict, H: int, color_i: int):
    items = []
    for w in ch:
        t = clean_word(w["raw"])
        base = re.sub(r"[^a-z']", "", t.lower())
        if base in FILLER or len(base) <= 2:
            items.append((t, (255, 255, 255), int(H * pat["filler"]), True))
        else:
            col = tuple(pat["keys"][color_i % len(pat["keys"])])
            color_i += 1
            items.append((t, col, int(H * (pat["key"] if len(base) <= 8 else pat["key_long"])), False))
    return items, color_i


def build_captions_pattern(segs: list[Seg], W: int, H: int, fpath: str, base_video: Path,
                           pat: dict) -> list[dict]:
    """Captions in one of the 15 beside-speaker variations (caption_patterns.py)."""
    chunks = list(caption_chunks(segs, max_words=pat["words"]))
    need = set()
    for _, _, s, e in chunks:
        f0, f1 = int(round(s * FPS)), int(round(e * FPS))
        need |= {f0, (f0 + f1) // 2, max(f0, f1 - 1)}
    frames = read_frames(base_video, need)
    faces_at = {fi: find_faces(fr) for fi, fr in frames.items()}
    events, color_i, alt = [], 0, 0
    for n, (seg, ch, s, e) in enumerate(chunks):
        f0, f1 = int(round(s * FPS)), int(round(e * FPS))
        fset = []
        for fi in (f0, (f0 + f1) // 2, max(f0, f1 - 1)):
            fset += faces_at.get(fi, [])
        mid_faces = faces_at.get((f0 + f1) // 2, [])
        main = max(mid_faces, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if mid_faces else None
        items, color_i = style_items(ch, pat, H, color_i)
        img, x, y, ov, zone = place_beside(items, pat, n, fset, main, W, H, fpath, alt)
        if not main:
            alt += 1
        events.append({"f0": f0, "f1": f1, "img": img, "x": x, "y": y, "pop": True, "anim": "pop",
                       "zone": zone, "overlap_px": ov, "text": " ".join(i[0] for i in items)})
    return events


def build_captions_classic(segs: list[Seg], W: int, H: int, fpath: str, base_video: Path) -> list[dict]:
    """Return caption events: dict(f0, f1, img, x, y, pop)."""
    import cv2
    events = []
    cap = cv2.VideoCapture(str(base_video))
    color_i = 0
    alt = 0
    bar = int(round((H - W * 9 / 21) / 2))
    for seg in segs:
        if seg.kind != "dialogue":
            continue
        dur = seg.t1 - seg.t0
        words = transcribe(Path(seg.src), seg.start, dur + 0.3)
        ws = []
        for w in words:
            raw = w["w"]
            key = re.sub(r"[^a-z']", "", raw.lower())
            if key in seg.fix:
                raw = seg.fix[key]
            if key in seg.drop or not clean_word(raw):
                continue
            ts = seg.t0 + (w["s"] - seg.start)
            te = seg.t0 + (w["e"] - seg.start)
            if ts < seg.t0 or te > seg.t1 + 0.05:
                continue
            ws.append({"raw": raw, "s": ts, "e": min(te, seg.t1)})
        ws = apply_phrase_fixes(ws, seg.fix_phrases)
        chunks = chunk_words(ws)
        for ci, ch in enumerate(chunks):
            s = ch[0]["s"]
            e = ch[-1]["e"] + 0.18
            if ci + 1 < len(chunks):
                e = min(e, chunks[ci + 1][0]["s"])
            e = min(max(e, s + 0.25), seg.t1)
            items = []
            for w in ch:
                t = clean_word(w["raw"])
                col, size, color_i = style_word(t, color_i, H)
                if t.rstrip("?!") in ("LUCIA",):
                    col = NEON["purple"]
                if t.rstrip("?!") in ("JASON",):
                    col = NEON["cyan"]
                items.append((t, col, size))
            f0, f1 = int(round(s * FPS)), int(round(e * FPS))
            cap.set(cv2.CAP_PROP_POS_FRAMES, (f0 + f1) // 2)
            ok, fr = cap.read()
            face = find_face(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB)) if ok else None
            side = seg.side
            if face and not side:
                side = "right" if face[0] < 0.5 else "left"
            if not side:
                side = "left" if alt % 2 == 0 else "right"
                alt += 1
            img = render_block(items, fpath, "left" if side == "right" else "right")
            margin = int(W * 0.05)
            if face:
                fx = face[0] * W
                fw = face[2] * W
                if side == "right":
                    x = int(min(fx + fw * 0.9, W - margin - img.width))
                else:
                    x = int(max(fx - fw * 0.9 - img.width, margin))
                y = int(face[1] * H - img.height * 0.5)
            else:
                x = margin if side == "left" else W - margin - img.width
                y = int(H * 0.30)
            y = max(bar + 8, min(y, H - bar - img.height - 8))
            x = max(margin // 2, min(x, W - img.width - margin // 2))
            ok2, fr2 = (True, fr) if ok else (False, None)
            ov = 0
            if ok2:
                fl = find_faces(cv2.cvtColor(fr2, cv2.COLOR_BGR2RGB))
                box = (x + 12, y + 12, x + img.width - 12, y + img.height - 12)
                ov = int(sum(_inter(box, pb) for pb in person_boxes(fl, W, H, tight=True)))
                if ov:  # never cover a face: mirror to the other side if that is clear
                    x2 = W - x - img.width
                    box2 = (x2 + 12, y + 12, x2 + img.width - 12, y + img.height - 12)
                    ov2 = int(sum(_inter(box2, pb) for pb in person_boxes(fl, W, H, tight=True)))
                    if ov2 < ov:
                        x, ov = x2, ov2
                if ov:
                    got = _clear_spot(img, fl, W, H, bar + 8, H - bar - 8, margin, (x, y))
                    if got:
                        img, x, y, ov = got
            events.append({"f0": f0, "f1": f1, "img": img, "x": x, "y": y, "pop": True, "anim": "pop",
                           "zone": "beside" if face else side, "overlap_px": ov,
                           "text": " ".join(i[0] for i in items)})
    cap.release()
    return events


def build_name_pops(segs: list[Seg], W: int, H: int, fpath: str) -> list[dict]:
    ev = []
    for seg in segs:
        if seg.name:
            img = neon_name(seg.name.upper(), seg.name_color, H, fpath)
            x = int(W * 0.60 - img.width / 2)
            y = int(H * 0.50 - img.height / 2)
            ev.append({"f0": seg.f0, "f1": seg.f1, "img": img, "x": max(0, min(x, W - img.width)),
                       "y": y, "pop": True, "text": seg.name})
    return ev


def _fade(img: Image.Image, a: float) -> Image.Image:
    if a >= 0.999:
        return img
    img = img.copy()
    img.putalpha(img.getchannel("A").point(lambda v: int(v * max(0.0, a))))
    return img


def _scaled(img, sx, sy, x, y):
    nw, nh = max(1, int(img.width * sx)), max(1, int(img.height * sy))
    return img.resize((nw, nh), Image.LANCZOS), x + (img.width - nw) // 2, y + (img.height - nh) // 2


def animate(e: dict, k: int, W: int, H: int):
    """Entry animations for caption patterns (first few frames only, then static)."""
    img, x, y, anim = e["img"], e["x"], e["y"], e["anim"]
    ease = lambda t: 1 - (1 - min(1.0, t)) ** 3
    if anim == "punch" and k < 5:
        sc = 1.32 - 0.32 * ease((k + 1) / 5)
        return _scaled(img, sc, sc, x, y)
    if anim == "slide_up" and k < 6:
        t = ease((k + 1) / 6)
        return _fade(img, t), x, y + int(H * 0.05 * (1 - t))
    if anim == "drop" and k < 7:
        t = (k + 1) / 7
        off = -H * 0.07 * (1 - ease(t)) + (H * 0.008 * math.sin(t * math.pi) if t > 0.55 else 0)
        return _fade(img, min(1, t * 2)), x, y + int(off)
    if anim == "slide_side" and k < 6:
        t = ease((k + 1) / 6)
        d = -1 if e.get("side") == "left" else 1
        return _fade(img, t), x + int(d * W * 0.07 * (1 - t)), y
    if anim == "fade" and k < 6:
        return _fade(img, (k + 1) / 6), x, y
    if anim == "stretch" and k < 5:
        t = ease((k + 1) / 5)
        return _scaled(img, 1.4 - 0.4 * t, 0.7 + 0.3 * t, x, y)
    if anim == "bounce" and k < 9:
        sc = 1 - 0.45 * math.exp(-0.55 * k) * math.cos(1.7 * k)
        return _scaled(img, sc, sc, x, y)
    if anim == "glitch" and k < 5:
        dx = int(W * 0.014 * (1 if k % 2 else -1) * (1 - k / 5))
        return _fade(img, 0.55 if k % 2 == 0 else 1.0), x + dx, y
    return img, x, y


def composite(base: Path, events: list[dict], wm: Image.Image | None, W: int, H: int,
              audio: Path, out: Path, nframes: int, crf: int):
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(base), "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", str(audio),
                            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "medium",
                            "-crf", str(crf), "-maxrate", "60M", "-bufsize", "120M", "-pix_fmt", "yuv420p", "-profile:v", "high",
                            "-c:a", "aac", "-b:a", "320k", "-shortest", "-movflags", "+faststart",
                            str(out)], stdin=subprocess.PIPE)
    fsz = W * H * 3
    wm_text, wm_font = (wm if isinstance(wm, tuple) else (None, None))
    if isinstance(wm, str):
        wm_text, wm_font = wm, font_path()
    for fi in range(nframes):
        buf = dec.stdout.read(fsz)  # decoder may hold a few extra frames; we stop at nframes
        if len(buf) < fsz:
            buf = bytes(fsz)
        act = [e for e in events if e["f0"] <= fi < e["f1"]]
        if act or wm_text:
            im = Image.frombuffer("RGB", (W, H), buf, "raw", "RGB", 0, 1).convert("RGBA")
            for e in act:
                img = e["img"]
                k = fi - e["f0"]
                anim = e.get("anim", "pop" if e["pop"] else "none")
                if anim == "pop" and e["pop"] and k < 4:
                    sc = 0.8 + 0.2 * (1 - (1 - (k + 1) / 4) ** 2)
                    nw, nh = max(1, int(img.width * sc)), max(1, int(img.height * sc))
                    img2 = img.resize((nw, nh), Image.LANCZOS)
                    x = e["x"] + (img.width - nw) // 2
                    y = e["y"] + (img.height - nh) // 2
                elif anim not in ("pop", "none", "reveal"):
                    img2, x, y = animate(e, k, W, H)
                else:
                    img2, x, y = img, e["x"], e["y"]
                if x < 0 or y < 0:
                    img2 = img2.crop((max(0, -x), max(0, -y), img2.width, img2.height))
                im.alpha_composite(img2, (max(0, x), max(0, y)))
            if wm_text:
                im = apply_glass_logo(im, wm_text, wm_font)
            buf = im.convert("RGB").tobytes()
        enc.stdin.write(buf)
    enc.stdin.close()
    enc.wait()
    dec.kill()
    dec.wait()
    if enc.returncode:
        raise RuntimeError("encode failed")


# ---------------------------------------------------------------- audio
def mix_audio(song: Path, song_start: float, total: float, segs: list[Seg], out: Path,
              duck: float = 0.28, ramp: float = 0.25, tail_fade: float = 1.8):
    sr = 48000
    n = int(round(total * sr))
    music = load_audio(song, sr, song_start, total)
    env = np.ones(n, np.float32)
    voice = np.zeros((n, 2), np.float32)
    for seg in segs:
        a, b = int(seg.t0 * sr), int(seg.t1 * sr)
        if seg.kind == "dialogue":
            env[a:b] = duck
            v = load_audio(Path(seg.src), sr, seg.start, seg.t1 - seg.t0)
            # normalise dialogue to ~ -16 dBFS RMS, gentle limiter
            rms = np.sqrt(np.mean(v ** 2)) + 1e-9
            v = v * min(6.0, 0.16 / rms)
            fl = int(0.04 * sr)
            v[:fl] *= np.linspace(0, 1, fl)[:, None]
            v[-fl:] *= np.linspace(1, 0, fl)[:, None]
            voice[a:a + len(v)] += v[: n - a]
        elif seg.sfx > 0:
            v = load_audio(Path(seg.src), sr, seg.start, seg.t1 - seg.t0) * seg.sfx
            voice[a:a + len(v)] += v[: n - a]
    # smooth the duck envelope (ramps)
    k = int(ramp * sr)
    if k > 1:
        kern = np.ones(k, np.float32) / k
        env = np.convolve(np.pad(env, (k // 2, k - k // 2 - 1), mode="edge"), kern, mode="valid")
    mixed = music * env[:, None] + voice
    peak = np.max(np.abs(mixed)) + 1e-9
    mixed = np.tanh(mixed * (0.98 / max(peak, 0.98)) * 1.1) / np.tanh(1.1)
    ft = min(len(mixed), int(tail_fade * sr))
    if ft > 0:
        mixed[-ft:] *= np.linspace(1, 0, ft)[:, None]
    import soundfile as sf
    sf.write(str(out), mixed, sr, subtype="PCM_24")


# ---------------------------------------------------------------- timeline + render
def layout(segs: list[Seg], binfo: dict, song_start: float) -> float:
    grid = [b for b in binfo["beats"] if b >= song_start - 1e-3]
    if abs(grid[0] - song_start) > 0.02:
        grid = [song_start] + grid
    bi = 0
    for s in segs:
        s.t0 = grid[bi] - song_start
        s.t1 = grid[bi + s.beats] - song_start
        bi += s.beats
    # frame-quantise contiguously so cuts land on the beat frame
    for s in segs:
        s.f0, s.f1 = int(round(s.t0 * FPS)), int(round(s.t1 * FPS))
        s.t0, s.t1 = s.f0 / FPS, s.f1 / FPS
    return segs[-1].t1


def render(segs: list[Seg], song: Path, song_start: float | None, out: Path, mode: str,
           res: str, hook: str | None, watermark: str | None, font: str | None,
           black_tail: float = 0.6, workers: int = 4, crf: int = 15,
           name_pops: bool = False, pattern: str | None = None) -> dict:
    W, H = map(int, res.lower().split("x"))
    fpath = font_path(font)
    binfo = detect_beats(song)
    if song_start is None:
        song_start = pick_song_start(binfo, sum(s.beats for s in segs))
    # snap to grid
    song_start = min(binfo["beats"], key=lambda b: abs(b - song_start))
    total_cut = layout(segs, binfo, song_start)
    tmp = cache_dir() / f"tmp_{out.stem}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    log(f"song_start={song_start:.3f}s tempo={binfo['tempo']:.1f} shots={len(segs)} cut_len={total_cut:.2f}s")
    with ThreadPoolExecutor(workers) as ex:
        parts = list(ex.map(lambda a: cut_segment(a[1], a[0], W, H, tmp), enumerate(segs)))
    tail_fr = int(round(black_tail * FPS))
    if tail_fr:
        blk = tmp / "black.mp4"
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={FPS}",
             "-frames:v", str(tail_fr), "-c:v", "libx264", "-crf", "13", "-pix_fmt", "yuv420p", str(blk)])
        parts.append(blk)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    base = tmp / "base.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(base)])
    nframes = segs[-1].f1 + tail_fr
    total = nframes / FPS
    wav = tmp / "mix.wav"
    mix_audio(song, song_start, total_cut, segs, wav)
    # black tail = silence (hard cut on the beat)
    import soundfile as sf
    a, sr = sf.read(str(wav))
    a = np.vstack([a, np.zeros((int(round((total - total_cut) * sr)), a.shape[1]))])
    sf.write(str(wav), a, sr, subtype="PCM_24")
    pat = CP.get(pattern) if pattern else None
    events = build_captions(segs, W, H, fpath, base, pat)
    if name_pops:  # off by default: big character-name pops are opt-in only
        events += build_name_pops(segs, W, H, fpath)
    wm = (watermark, fpath) if watermark else None
    wide = out if mode == "wide" else tmp / "wide.mp4"
    composite(base, events, wm, W, H, wav, wide, nframes, crf)
    if mode == "shorts":
        make_shorts(wide, out, hook or "", fpath)
    meta = {
        "out": str(out), "mode": mode, "song": str(song), "song_start": song_start,
        "duration": total, "shots": len(segs), "tempo": binfo["tempo"],
        "segments": [{k: v for k, v in asdict(s).items() if k in
                      ("kind", "src", "start", "beats", "name", "t0", "t1")} for s in segs],
        "captions": [{"t": round(e["f0"] / FPS, 2), "text": e["text"]} for e in events if not e.get("sub")],
        "pattern": (pat or CP.PATTERNS[0])["id"], "res": [W, H],
        "caption_boxes": [{"t0": round(e["f0"] / FPS, 3), "t1": round(e["f1"] / FPS, 3), "text": e["text"],
                           "box": [int(e["x"]), int(e["y"]), int(e["img"].width), int(e["img"].height)],
                           "zone": e.get("zone"), "overlap_px": e.get("overlap_px", 0)}
                          for e in events if not e.get("sub")],
        "letterbox_bar": int(round((H - W * 9 / 21) / 2)),
        "segment_times": [{"kind": s.kind, "t0": s.t0, "t1": s.t1} for s in segs],
    }
    Path(str(out) + ".json").write_text(json.dumps(meta, indent=1))
    shutil.rmtree(tmp, ignore_errors=True)
    log("wrote", out)
    return meta


def make_4k_wide(wide: Path, out: Path):
    """3840x2160 4K master of a wide (YouTube) render: Lanczos upscale + light luma sharpen."""
    run(["ffmpeg", "-y", "-v", "error", "-i", str(wide), "-vf",
         "scale=3840:2160:flags=lanczos,unsharp=5:5:0.35:5:5:0.0,format=yuv420p",
         "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "17", "-maxrate", "50M",
         "-bufsize", "100M", "-profile:v", "high", "-c:a", "copy", "-movflags", "+faststart", str(out)])


def make_shorts(wide: Path, out: Path, hook: str, fpath: str):
    """2160x3840: blurred fill background, full 16:9 video (no crop), white hook bar above."""
    W, H = 2160, 3840
    BW, BH = 2160, 1215
    hook = "".join(c for c in hook if ord(c) < 0x2000).strip()  # bold fonts have no emoji glyphs
    vy = (H - BH) // 2 + 120
    bar_h = 300
    bar = Image.new("RGBA", (W, bar_h), (255, 255, 255, 255))
    d = ImageDraw.Draw(bar)
    size = 130
    while size > 60:
        f = ImageFont.truetype(fpath, size)
        bb = d.textbbox((0, 0), hook, font=f)
        if bb[2] - bb[0] <= W - 100:
            break
        size -= 4
    d.text(((W - (bb[2] - bb[0])) / 2 - bb[0], (bar_h - (bb[3] - bb[1])) / 2 - bb[1]), hook,
           font=f, fill=(0, 0, 0, 255))
    bp = out.parent / f".{out.stem}_hook.png"
    bar.save(bp)
    vf = (f"[0:v]split[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
          f"boxblur=10:3,eq=brightness=-0.08,scale={W}:{H}[bg];"
          f"[b]scale={BW}:{BH}:flags=lanczos[fg];[bg][fg]overlay=0:{vy}[v1];"
          f"[v1][1:v]overlay=0:{vy - bar_h},format=yuv420p")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(wide), "-i", str(bp), "-filter_complex", vf,
         "-map", "0:a", "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-maxrate", "50M",
         "-bufsize", "100M", "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "copy",
         "-movflags", "+faststart", str(out)])
    bp.unlink(missing_ok=True)


# ---------------------------------------------------------------- auto mode
@dataclass
class Shot:
    src: str
    s: float
    e: float
    motion: float = 0.0
    color: float = 0.0
    speech: float = 0.0
    bad: str = ""


def scan_clip(path: Path) -> list[Shot]:
    """Scene-cut + per-shot stats from a 10 fps 320px proxy (cached)."""
    ck = cache_dir() / f"shots_ov4_{file_key(path)}.json"
    if ck.exists():
        # file_key follows the resolved source, but cached Shot.src may be a
        # deleted work-dir symlink from an earlier video — always use this path.
        here = str(path)
        return [Shot(**{**d, "src": here}) for d in json.loads(ck.read_text())]
    w, h, fps = 320, 180, 10
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                          f"fps={fps},scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    frames = []
    while True:
        b = p.stdout.read(w * h * 3)
        if len(b) < w * h * 3:
            break
        frames.append(np.frombuffer(b, np.uint8).reshape(h, w, 3))
    p.wait()
    import cv2
    stats, cuts = [], [0]
    prev_hist = None
    for i, fr in enumerate(frames):
        hsv = cv2.cvtColor(fr, cv2.COLOR_RGB2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).flatten()
        hist /= hist.sum() + 1e-9
        g = cv2.cvtColor(fr, cv2.COLOR_RGB2GRAY).astype(np.float32)
        dark = float((g < 28).mean())
        sat = float(hsv[:, :, 1].mean() / 255)
        mot = float(np.abs(g - stats[-1]["g"]).mean()) if stats else 0.0
        # pillar-boxed social-media/blurred side panels: sides much flatter than centre
        lap = cv2.Laplacian(g, cv2.CV_32F)
        side = float(np.abs(lap[:, : w // 6]).mean() + np.abs(lap[:, -w // 6:]).mean()) / 2
        ctr = float(np.abs(lap[:, w // 3: 2 * w // 3]).mean())
        if prev_hist is not None:
            d = 0.5 * np.abs(hist - prev_hist).sum()
            if d > 0.42 and i - cuts[-1] >= 3:
                cuts.append(i)
        prev_hist = hist
        stats.append({"g": g, "dark": dark, "sat": sat, "mot": mot, "pill": side / (ctr + 1e-6)})
    cuts.append(len(frames))
    clip_len = len(frames) / fps
    shots = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        if b - a < 6:
            continue
        st = stats[a + 1:b]
        sh = Shot(str(path), a / fps + 0.12, b / fps - 0.08,
                  motion=float(np.mean([x["mot"] for x in st])),
                  color=float(np.mean([x["sat"] for x in st])))
        dark = np.mean([x["dark"] for x in st])
        pill = np.median([x["pill"] for x in st])
        from pipeline_lib.overlays import head_tail_bad, sample_indices, shot_overlay_bad
        ht = head_tail_bad(sh.s, sh.e, clip_len)
        if ht:
            sh.bad = ht
        elif dark > 0.82 or (dark > 0.60 and sh.motion < 1.2):
            sh.bad = "dark/title card"
        elif pill < 0.22:
            sh.bad = "pillarbox/social overlay"
        else:
            idxs = sample_indices(a + 1, b, 3)
            sh.bad = shot_overlay_bad([frames[i] for i in idxs]) or ""
        shots.append(sh)
    ck.write_text(json.dumps([asdict(s) for s in shots]))
    return shots


def dialogue_windows(path: Path, shots: list[Shot]) -> list[dict]:
    dur = probe_duration(path)
    words = transcribe(path, 0.0, dur)
    # group into sentences by whisper segment id
    sents = {}
    for w in words:
        sents.setdefault(w["seg"], []).append(w)
    sents = [v for _, v in sorted(sents.items()) if len(v) >= 2]
    wins = []
    for i in range(len(sents)):
        for j in range(i, min(i + 6, len(sents))):
            group = sents[i:j + 1]
            if j > i and group[-1][0]["s"] - group[-2][-1]["e"] > 1.6:
                break
            s, e = group[0][0]["s"], group[-1][-1]["e"]
            if e - s > 8.5:
                break
            if e - s < 3.5:
                continue
            nw = sum(len(g) for g in group)
            p = float(np.mean([w["p"] for g in group for w in g]))
            wins.append({"src": str(path), "s": s, "e": e, "score": nw * p * (1 + 0.04 * (e - s)),
                         "p": p, "nw": nw,
                         "spans": [(w["s"], w["e"]) for g in group for w in g],
                         "text": " ".join(w["w"] for g in group for w in g)})
    return wins


DLG_MIN_P = 0.55          # whisper word confidence: sung / foreign lyrics forced to English score lower
DLG_MAX_GAP_RATIO = 0.6   # loudness between words vs during words: a music bed keeps the gaps loud
_DLG_CACHE: dict = {}


def _frame_at(src: str, t: float, w: int = 640, h: int = 360) -> np.ndarray | None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", src, "-frames:v", "1",
                        "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, np.uint8)
    return a.reshape(h, w, 3) if len(a) == w * h * 3 else None


def speech_not_music(win: dict, audio: np.ndarray | None = None, sr: int = 16000) -> tuple[bool, str]:
    """Reject sung lyrics / songs in the source (e.g. a Spanish track forced through English whisper)."""
    if win.get("p", 1.0) < DLG_MIN_P:
        return False, f"low word confidence {win.get('p', 0):.2f}"
    dur = max(0.1, win["e"] - win["s"])
    rate = win.get("nw", 0) / dur
    if win.get("nw") and not (1.2 <= rate <= 5.5):
        return False, f"word rate {rate:.1f}/s"
    spans = win.get("spans") or []
    if audio is None:
        try:
            audio = load_audio(Path(win["src"]), sr, win["s"], dur).mean(axis=1)
        except Exception:
            return True, "no audio check"
    if spans and len(audio):
        mask = np.zeros(len(audio), bool)
        for a, b in spans:
            mask[max(0, int((a - win["s"]) * sr)):max(0, int((b - win["s"]) * sr))] = True
        if mask.sum() > sr * 0.3 and (~mask).sum() > sr * 0.4:
            on = float(np.sqrt(np.mean(audio[mask] ** 2)) + 1e-9)
            off = float(np.sqrt(np.mean(audio[~mask] ** 2)) + 1e-9)
            if off / on > DLG_MAX_GAP_RATIO:
                return False, f"music bed between words ({off / on:.2f})"
    return True, "ok"


def is_real_dialogue(win: dict) -> bool:
    """A dialogue scene (21:9 bars + ducked music) must be speech, not a song, with a face on screen."""
    key = (win["src"], round(win["s"], 2), round(win["e"], 2))
    if key in _DLG_CACHE:
        return _DLG_CACHE[key]
    ok, why = speech_not_music(win)
    if ok:
        hits = 0
        for t in (win["s"] + 0.3, (win["s"] + win["e"]) / 2, win["e"] - 0.3):
            fr = _frame_at(win["src"], t)
            try:
                hits += fr is not None and find_face(fr) is not None
            except Exception:
                hits += 1      # no face model available: do not block dialogue on it
        ok = hits >= 2
        why = f"faces in {hits}/3 frames"
    if not ok:
        log(f"not a dialogue scene ({why}): {Path(win['src']).name} {win['s']:.1f}-{win['e']:.1f}")
    _DLG_CACHE[key] = ok
    return ok


def clip_signature(path: Path) -> tuple[float, np.ndarray | None]:
    dur = probe_duration(path)
    frames = []
    for frac in (0.2, 0.45, 0.7):
        f = _frame_at(str(path), dur * frac, 32, 18)
        if f is None:
            return dur, None
        frames.append(f.astype(np.float32).mean(axis=2))
    return dur, np.stack(frames)


def dedupe_clips(files: list[Path]) -> list[Path]:
    """Drop files that are the same video under another id (re-uploads); they made the edit loop footage."""
    keep, sigs = [], []
    for f in files:
        try:
            d, sig = clip_signature(f)
        except Exception:
            keep.append(f)
            continue
        dup = sig is not None and any(abs(d - d2) < 1.5 and s2 is not None and float(np.abs(sig - s2).mean()) < 10
                                      for d2, s2 in sigs)
        if dup:
            log("duplicate source footage skipped:", f.name)
            continue
        keep.append(f)
        sigs.append((d, sig))
    return keep


def auto_plan(song: Path, clips: Path, target: float, binfo: dict, exclude: dict | None = None,
              seed: int | None = None, avoid: list | None = None,
              open_with_dialogue: bool = False,
              prefer_dialogue_from: list | None = None,
              min_seconds: float | None = None) -> tuple[list[Seg], float]:
    """exclude: {file name: [[s, e], ...]} ranges to never use (e.g. title cards / text Gemini found).
    seed: varies shot / dialogue choice between videos (None = deterministic approved behaviour).
    avoid: [(source_id, start, end), ...] ranges used in earlier videos (long overlap is skipped).
    open_with_dialogue: sports-style layout — spoken interview/press first, then the action montage.
    prefer_dialogue_from: source paths whose spoken windows should be preferred for the opening.
    min_seconds: when set and fresh footage runs out, extend the beat-cut timeline BEFORE render until it
      fills `target` seconds of the song (at least min_seconds): first with shots used in older videos
      (still no repeat inside this video), then by looping this video's own montage shots. None keeps the
      strict no-loop behaviour (ends early)."""
    import random
    rng = random.Random(seed) if seed is not None else None
    exclude = exclude or {}
    period = binfo["period"]
    b1, b2 = hold_beats(period, 1), hold_beats(period, 2)
    hook_pat = [b2, b2, b2, b1, b1, b2]
    mid_pat = [b2, b1, b1, b2]
    end_pat = [b1, b1, b2, b1]
    fill_pat = [b2, b1, b1, b2, b1, b1]
    files = dedupe_clips(sorted(p for p in clips.iterdir() if p.suffix.lower() in VIDEO_EXT))
    log("scanning", len(files), "clips")
    allshots, wins = [], []
    for f in files:
        sh = scan_clip(f)
        allshots += sh
        wins += dialogue_windows(f, sh)
    # mark speech in shots
    spoken = []
    for wdw in wins:
        spoken.append((wdw["src"], wdw["s"] - 0.5, wdw["e"] + 0.5))
    def is_spoken(sh):
        return any(s == sh.src and not (sh.e < a or sh.s > b) for s, a, b in spoken)
    def excluded(src, a, b):
        return any(not (b < x0 or a > x1) for x0, x1 in exclude.get(Path(src).name, []))
    from pipeline_lib.edit_ranges import overlaps_long, source_id
    wins = [w for w in wins if not excluded(w["src"], w["s"] - 0.3, w["e"] + 0.3)]
    prefer = {Path(p).name for p in (prefer_dialogue_from or [])}
    # recent videos: treat used long ranges as already taken (short <1.5s overlap is ok)
    taken = list(avoid or [])
    here = []   # ranges used in THIS video: no reuse at all (any overlap > 0.05s is a repeat)
    for w in wins:
        if overlaps_long(source_id(w["src"]), w["s"], w["e"], taken):
            w["score"] *= 0.02
        if Path(w["src"]).name in prefer:
            w["score"] *= 8.0
        if rng:
            w["score"] *= rng.uniform(0.8, 1.2)
    def _ok(s, spoken):
        return (not s.bad and s.e - s.s >= period * b1 + 0.1 and is_spoken(s) is spoken
                and not excluded(s.src, s.s, s.e)
                and not overlaps_long(source_id(s.src), s.s, s.e, taken))
    silent = [s for s in allshots if _ok(s, False)]
    spoken_ok = [s for s in allshots if _ok(s, True)]
    # Fast-cut sources (official game update videos) rarely hold a shot for the full +1.5s.
    # Only when there are too few long shots, fall back to a half extra hold (still on the beat).
    need_shots = int(round(target / period)) // max(1, b1)
    if len(silent) + len(spoken_ok) < need_shots:
        fb1, fb2 = hold_beats(period, 1, HOLD_EXTRA_S / 2), hold_beats(period, 2, HOLD_EXTRA_S / 2)
        if fb1 < b1:
            log(f"only {len(silent) + len(spoken_ok)} long shots (< {need_shots}); short-hold fallback {b1}->{fb1} beats")
            b1, b2 = fb1, fb2
            hook_pat = [b2, b2, b2, b1, b1, b2]
            mid_pat = [b2, b1, b1, b2]
            end_pat = [b1, b1, b2, b1]
            fill_pat = [b2, b1, b1, b2, b1, b1]
            silent = [s for s in allshots if _ok(s, False)]
            spoken_ok = [s for s in allshots if _ok(s, True)]
    # Official Rockstar updates are often faster than a 3-beat hold. Last resort: 1/2-beat
    # cuts still on the grid, still no in-video reuse, rather than ending an 8s edit.
    if len(silent) + len(spoken_ok) < need_shots and b1 > 1:
        log(f"only {len(silent) + len(spoken_ok)} mid-length shots (< {need_shots}); last-resort 1-beat holds")
        b1, b2 = 1, 2
        hook_pat = [b2, b2, b2, b1, b1, b2]
        mid_pat = [b2, b1, b1, b2]
        end_pat = [b1, b1, b2, b1]
        fill_pat = [b2, b1, b1, b2, b1, b1]
        silent = [s for s in allshots if _ok(s, False)]
        spoken_ok = [s for s in allshots if _ok(s, True)]
    montage = silent
    # A single-title trailer is often dialogue-heavy; keep silent shots first, then
    # fill from spoken shots so the edit can still be built. Chosen dialogue windows
    # are reserved in `taken` before montage_block runs, so they are not reused.
    if len(montage) < 36 and spoken_ok:
        log(f"only {len(silent)} silent montage shots; allowing {len(spoken_ok)} spoken shots as fill")
        montage = silent + spoken_ok
    for s in montage:
        s.score = s.motion * 0.08 + s.color * 2.0
        # prefer silent shots when both are available
        if is_spoken(s):
            s.score *= 0.55
        if rng:
            s.score *= rng.uniform(0.7, 1.3)
    log(f"{len(allshots)} shots, {len(montage)} montage candidates ({len(silent)} silent), {len(wins)} dialogue windows")
    # two best non-overlapping dialogue windows (no repeated lines / ranges)
    def _text_overlap(t1: str, t2: str) -> float:
        w1 = set(re.findall(r"[a-z]{3,}", t1.lower()))
        w2 = set(re.findall(r"[a-z]{3,}", t2.lower()))
        if not w1 or not w2:
            return 0.0
        return len(w1 & w2) / min(len(w1), len(w2))

    # Dialogue 1: best window (3.5-8.5s). Dialogue 2: punchy 4-5s (3.5-5.5s) window.
    wins.sort(key=lambda w: -w["score"])
    chosen = []

    def _can_pick(w):
        if overlaps_long(source_id(w["src"]), w["s"], w["e"], taken):
            return False
        if any(_text_overlap(w["text"], c["text"]) >= 0.40 for c in chosen):
            return False
        if not all(w["src"] != c["src"] or w["e"] < c["s"] - 5 or w["s"] > c["e"] + 5 for c in chosen):
            return False
        return is_real_dialogue(w)

    for w in wins:
        if _can_pick(w):
            chosen.append(w)
            taken.append((source_id(w["src"]), w["s"], w["e"]))
            break

    if chosen:
        # Prefer 4-5s (3.5-5.5s) for Dialogue 2; fallback to any valid window if none exists
        for max_dur in (5.5, 99.0):
            picked_second = False
            for w in wins:
                if (w["e"] - w["s"]) <= max_dur and _can_pick(w):
                    chosen.append(w)
                    taken.append((source_id(w["src"]), w["s"], w["e"]))
                    picked_second = True
                    break
            if picked_second:
                break

    total_beats = int(round(target / period))
    hook_b = min(20, max(12, total_beats // 4))
    mid_b = min(10, max(6, total_beats // 8))
    # +0.95s so the last spoken word is not cut off after the 0.25s early in-point
    dlg_b = [int(math.ceil((c["e"] - c["s"] + 0.95) / period)) for c in chosen]
    end_b = max(8, total_beats - hook_b - mid_b - sum(dlg_b))
    for c, nb in zip(chosen, dlg_b):   # the full on-screen dialogue range, not just the spoken words
        here.append((source_id(c["src"]), max(0.0, c["s"] - 0.25), max(0.0, c["s"] - 0.25) + nb * period))
    short = []
    # interleave sources round-robin, best first
    by_src = {}
    for s in sorted(montage, key=lambda s: -s.score):
        by_src.setdefault(s.src, []).append(s)
    order = []
    while any(by_src.values()):
        for k in list(by_src):
            if by_src[k]:
                order.append(by_src[k].pop(0))
    leftover = list(order)

    def montage_block(nbeats, pattern):
        out, left, k = [], nbeats, 0
        while left > 0:
            b = min(pattern[k % len(pattern)], left)
            k += 1
            dur = b * period
            sh = None
            keep = []
            def pick(need):
                sh = None
                keep = []
                for cand in leftover:
                    mid = cand.s + 0.15
                    if cand.e - cand.s >= need + 0.1 and not overlaps_long(source_id(cand.src), mid, mid + need, taken) \
                            and not overlaps_long(source_id(cand.src), mid, mid + need, here, 0.05):
                        if sh is None:
                            sh = cand
                        else:
                            keep.append(cand)
                    else:
                        keep.append(cand)
                return sh, keep
            sh, keep = pick(dur)
            if sh is None and b > b1:
                b = b1
                dur = b * period
                sh, keep = pick(dur)
            leftover[:] = keep
            if sh is None:
                # never loop footage: end cleanly on the last beat instead
                log(f"out of fresh montage footage; ending {left} beats early")
                short.append(left)
                break
            mid = sh.s + 0.15
            taken.append((source_id(sh.src), mid, mid + dur))
            here.append((source_id(sh.src), mid, mid + dur))
            out.append(Seg("montage", sh.src, mid, b, zoom=1.04))
            rest_s = mid + dur + 0.04
            if sh.e - rest_s >= period + 0.05:
                leftover.append(Shot(sh.src, rest_s, sh.e, motion=sh.motion, color=sh.color))
            left -= b
        return out

    if open_with_dialogue:
        if not chosen:
            raise RuntimeError("need a spoken interview/press window to open the edit")
        segs = [Seg("dialogue", chosen[0]["src"], max(0.0, chosen[0]["s"] - 0.25), dlg_b[0])]
        rest = max(10, total_beats - dlg_b[0])
        segs += montage_block(rest, hook_pat)
    else:
        segs = montage_block(hook_b, hook_pat)
        for i, c in enumerate(chosen):
            segs.append(Seg("dialogue", c["src"], max(0.0, c["s"] - 0.25), dlg_b[i]))
            segs += montage_block(mid_b if i == 0 else end_b, mid_pat if i == 0 else end_pat)
    have = sum(s.beats for s in segs)
    if have < total_beats - 4 and not short:  # fewer than 2 dialogue scenes found: finish with montage
        segs += montage_block(total_beats - have, fill_pat)
    if min_seconds and sum(s.beats for s in segs) * period < max(min_seconds, target - 0.5):
        goal = max(total_beats, int(math.ceil(min_seconds / period)))
        dlg_keep = [(source_id(c["src"]), c["s"], c["e"]) for c in chosen]
        dlg_here = [(source_id(c["src"]), max(0.0, c["s"] - 0.25), max(0.0, c["s"] - 0.25) + nb * period)
                    for c, nb in zip(chosen, dlg_b)]
        pool = [s for s in allshots if not s.bad and s.e - s.s >= period * b1 + 0.1
                and not excluded(s.src, s.s, s.e)]
        for s in pool:
            if not getattr(s, "score", 0):
                s.score = s.motion * 0.08 + s.color * 2.0
        pool.sort(key=lambda s: -s.score)
        # tier 1: footage used in OLDER videos is allowed again; nothing repeats inside this video
        # tier 2+: loop this video's own montage shots (beat grid continues; dialogue never repeats)
        for tier in range(5):
            have = sum(s.beats for s in segs)
            if have * period >= max(min_seconds, target - 0.5) or have >= goal:
                break
            taken[:] = list(dlg_keep)
            if tier >= 1:
                here[:] = list(dlg_here)
            leftover[:] = [Shot(s.src, s.s, s.e, motion=s.motion, color=s.color) for s in pool]
            for s, s0 in zip(leftover, pool):
                s.score = s0.score
            short.clear()
            log(f"timeline {have * period:.1f}s < {min_seconds:.0f}s: extending with "
                f"{'older-video footage' if tier == 0 else 'looped beat-cut shots'} to {goal * period:.1f}s")
            segs += montage_block(goal - have, fill_pat)
    if not segs or segs[0].kind != "montage" and not open_with_dialogue:
        raise RuntimeError("not enough distinct montage shots in --clips")
    return segs, None


# ---------------------------------------------------------------- cli
def load_plan(plan: Path) -> tuple[list[Seg], dict]:
    d = json.loads(plan.read_text())
    root = plan.parent
    segs = []
    for s in d["segments"]:
        s = dict(s)
        src = Path(s["src"])
        if not src.is_absolute():
            src = (root / src).resolve()
        s["src"] = str(src)
        segs.append(Seg(**s))
    return segs, d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("auto", "render"):
        p = sub.add_parser(name)
        p.add_argument("--song", required=True, type=Path)
        p.add_argument("--out", required=True, type=Path)
        p.add_argument("--mode", choices=["wide", "shorts"], default="wide")
        p.add_argument("--res", default="2560x1440")
        p.add_argument("--hook", default=None, help="hook-bar text (shorts mode)")
        p.add_argument("--watermark", default="VICE CARTEL")
        p.add_argument("--font", default=None)
        p.add_argument("--song-start", type=float, default=None)
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--crf", type=int, default=18)
        p.add_argument("--pattern", default=None, help="caption pattern P01..P15 (default P01 = approved placement)")
        p.add_argument("--name-pops", action="store_true",
                       help="draw big neon character-name pops from the plan's 'name' fields (default: off)")
        if name == "auto":
            p.add_argument("--clips", required=True, type=Path)
            p.add_argument("--duration", type=float, default=38.0)
            p.add_argument("--plan-out", type=Path, default=None)
        else:
            p.add_argument("--plan", required=True, type=Path)
    a = ap.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if a.cmd == "auto":
        binfo = detect_beats(a.song)
        segs, _ = auto_plan(a.song, a.clips, a.duration, binfo)
        if a.plan_out:
            a.plan_out.write_text(json.dumps({"segments": [
                {k: v for k, v in asdict(s).items() if k not in ("t0", "t1", "f0", "f1")} for s in segs]}, indent=1))
        render(segs, a.song, a.song_start, a.out, a.mode, a.res, a.hook, a.watermark, a.font,
               workers=a.workers, crf=a.crf, name_pops=a.name_pops, pattern=a.pattern)
    else:
        segs, d = load_plan(a.plan)
        st = a.song_start if a.song_start is not None else d.get("song_start")
        render(segs, a.song, st, a.out, a.mode, a.res, a.hook or d.get("hook"),
               a.watermark if a.watermark else d.get("watermark"), a.font, workers=a.workers, crf=a.crf,
               name_pops=a.name_pops, pattern=a.pattern or d.get("pattern"))


if __name__ == "__main__":
    main()
