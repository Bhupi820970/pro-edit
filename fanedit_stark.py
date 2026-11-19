#!/usr/bin/env python3
"""fanedit_stark — "The North Remembers" (Silence 21) fan-edit structure on top of vicestyle.

Structure (reference: Silence 21 House Stark edit):
  1. BUILD   : speech scene(s) over the quiet song intro. Dialogue segments always play their OWN
               shot's audio (lip-safe), vocals isolated with demucs, word-synced captions from vicestyle.
  2. DROP    : motion-blur ramp -> glass cracks -> white flash + shards breaking outward (+ glass SFX),
               grade switches cold -> warm exactly on the song drop.
  3. CLIMAX  : 1-slot (2 beats) slow-mo shots, zoom-punch + RGB split on every cut, sequence looped.
  4. ENDING  : real storm-cloud footage plate + pre-rendered 3D metallic logo, song echoes out.
  Throughout : film grain, dust, feathered letterbox bars, vignette.

vicestyle.py (the approved GTA style) is imported, never modified: this module only swaps the
per-shot filter / caption pattern for its own render call.

  python3 fanedit_stark.py --spec plans/stark_daenerys.json --out out/daenerys.mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import caption_patterns as CP  # noqa: E402
import vicestyle as VS  # noqa: E402

W, H, FPS = 1080, 1350, 60
LOGO_ASSET = HERE / "assets" / "fanedit_stark" / "logo_vice_cartel.webm"
# third-party assets are downloaded on demand into the cache (never committed); see CREDITS.md
ASSETS = {
    "plate": ("https://upload.wikimedia.org/wikipedia/commons/1/1b/"
              "Storm_clouds_moving_in_Yantarny%2C_Kaliningrad_Oblast.webm", "storm_clouds.webm"),
    "glass": ("https://upload.wikimedia.org/wikipedia/commons/a/af/Glass_breaking_%28Gravity_Sound%29.wav",
              "glass_breaking.wav"),
}
# cold build grade / warm fire climax grade (ffmpeg)
GRADE_COLD = ("eq=contrast=1.12:saturation=0.62:gamma=0.98,"
              "colorbalance=rs=-0.04:gs=0.0:bs=0.05:rm=-0.03:bm=0.04:rh=-0.02:bh=0.03")
GRADE_WARM = ("eq=contrast=1.18:saturation=1.25:gamma=0.95,"
              "colorbalance=rs=0.10:gs=0.02:bs=-0.10:rm=0.08:gm=0.0:bm=-0.08:rh=0.05:bh=-0.06")
RED, GOLD, FIRE, PALE = (226, 34, 28), (240, 188, 92), (255, 122, 34), (255, 226, 170)
STARK_PATTERN = {"id": "S01", "name": "Stark build: beside-lower, gold/fire", "y": [0.85], "gap": 0.95,
                 "nudge": 0.0, "words": 3, "key": .050, "key_long": .044, "filler": .030,
                 "keys": [GOLD, FIRE, RED, PALE], "no_face_y": .64, "classic": False}


def log(*a):
    print("[fanedit_stark]", *a, file=sys.stderr, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def cache() -> Path:
    d = VS.cache_dir() / "fanedit_stark"
    d.mkdir(parents=True, exist_ok=True)
    return d


def key(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def asset(name: str) -> Path:
    url, fn = ASSETS[name]
    p = cache() / fn
    if not p.exists():
        log("downloading", fn)
        import time
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "pro-edit/1.0 (fan-edit tool)"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    p.write_bytes(r.read())
                break
            except Exception as e:  # 429 from Wikimedia: back off
                if attempt == 3:
                    raise
                log(f"download failed ({e}); retrying")
                time.sleep(20 * (attempt + 1))
    return p


def probe_height(src: str) -> int:
    o = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=height",
                        "-of", "csv=p=0", src], capture_output=True, text=True).stdout.strip()
    return int(o.split()[0]) if o else 0


# ---------------------------------------------------------------- music structure
def find_drop(song: Path, beats: list[float], lo: float = 20.0) -> float:
    """Biggest quiet->loud energy jump (2 s windows), snapped to the nearest beat."""
    y = VS.load_audio(song, 22050)
    y = y.mean(axis=1) if y.ndim > 1 else y
    return drop_from_signal(y, 22050, beats, lo)


def drop_from_signal(y: np.ndarray, sr: int, beats: list[float], lo: float = 20.0) -> float:
    hop = sr // 10
    n = len(y) // hop
    rms = np.sqrt(np.mean(y[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    w = 20  # 2 s
    best, bt = 0.0, None
    for i in range(max(w, int(lo * 10)), n - w):
        before, after = rms[i - w:i].mean(), rms[i:i + w].mean()
        score = after / (before + 1e-6) * after
        if score > best:
            best, bt = score, i / 10
    if bt is None:
        raise ValueError("no drop found")
    # refine on 20 ms hops: sharpest onset within +-0.8 s
    fh = sr // 50
    a0 = max(0, int((bt - 0.8) * sr))
    seg = y[a0: a0 + int(1.6 * sr) + 10 * fh]
    m = len(seg) // fh
    e = np.sqrt(np.mean(seg[: m * fh].reshape(m, fh) ** 2, axis=1) + 1e-12)
    jump = [e[i:i + 5].mean() / (e[max(0, i - 5):i].mean() + 1e-6) if i >= 5 else 0 for i in range(m - 5)]
    bt = a0 / sr + int(np.argmax(jump)) * fh / sr
    return bt


def layout_budget(build_durs: list[float], beat: float, n_climax: int, loops: int, slot_beats: int):
    """Beats per build segment (rounded up so speech is never clipped) and climax slot count."""
    bb = [max(1, int(math.ceil(d / beat - 0.15))) for d in build_durs]
    return bb, n_climax * loops, slot_beats


# ---------------------------------------------------------------- sources
def clean_dialogue(src: str, start: float, end: float, use_demucs: bool = True) -> tuple[str, float]:
    """Cut [start-0.5, end+0.5] of src into a cached clip whose audio is demucs vocals only.
    Returns (clip, offset of `start` inside the clip)."""
    pre = min(0.5, start)
    s0, dur = start - pre, end - start + pre + 0.5
    k = key(src, round(s0, 3), round(dur, 3), use_demucs)
    out = cache() / f"dlg_{k}.mp4"
    if out.exists():
        return str(out), pre
    tmp = cache() / f"dlg_{k}"
    tmp.mkdir(exist_ok=True)
    wav = tmp / "a.wav"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", src, "-vn",
         "-ac", "2", "-ar", "44100", str(wav)])
    voc = wav
    if use_demucs:
        try:
            run([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals", "-o", str(tmp), str(wav)],
                capture_output=True)
            voc = next(tmp.rglob("vocals.wav"))
        except Exception as e:  # demucs missing -> keep original audio
            log("demucs unavailable, keeping original dialogue audio:", e)
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.3f}", "-t", f"{dur:.3f}", "-i", src, "-i", str(voc),
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-c:a", "pcm_s16le",
         str(out).replace(".mp4", ".mov")])
    os.replace(str(out).replace(".mp4", ".mov"), out)
    return str(out), pre


def slowmo(src: str, start: float, out_dur: float, speed: float, crop: dict | None) -> str:
    """Motion-interpolated slow-mo clip (60 fps), cropped to 4:5 around crop['cx'], crop['cy']."""
    k = key(src, start, round(out_dur, 3), speed, json.dumps(crop or {}, sort_keys=True))
    out = cache() / f"slow_{k}.mp4"
    if out.exists():
        return str(out)
    c = crop or {}
    cx, cy, z = c.get("cx", 0.5), c.get("cy", 0.5), c.get("zoom", 1.0)
    vf = (f"crop='ih/{z}*{W}/{H}':'ih/{z}':'max(0,min(iw-ow,{cx}*iw-ow/2))':'max(0,min(ih-oh,{cy}*ih-oh/2))',"
          f"scale=720:900:flags=lanczos,setpts={1 / speed:.4f}*PTS,"
          f"minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
          f"scale={W}:{H}:flags=lanczos,trim=duration={out_dur + 0.2:.3f}")
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{out_dur * speed + 0.3:.3f}", "-i", src,
         "-vf", vf, "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-pix_fmt", "yuv420p", str(out)])
    return str(out)


# ---------------------------------------------------------------- lip-sync QA (mediapipe)
def mouth_speech_corr(src: str, start: float, dur: float, fps: int = 12) -> float | None:
    """Correlation between the on-screen mouth opening and the speech envelope.
    High (>0.2) = the visible face is the one talking. None = no face / mediapipe unavailable."""
    try:
        import mediapipe as mp
        from mediapipe.tasks.python import vision, BaseOptions
    except Exception:
        return None
    model = cache() / "face_landmarker.task"
    if not model.exists():
        try:
            urllib.request.urlretrieve("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                                       "face_landmarker/float16/1/face_landmarker.task", model)
        except Exception:
            return None
    lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model)), num_faces=1))
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
                        "-vf", f"fps={fps},scale=480:-2", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True).stdout
    hh = probe_height(src)
    ww = 480
    fh = int(round(hh * 480 / max(1, int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
            "-show_entries", "stream=width", "-of", "csv=p=0", src], capture_output=True, text=True).stdout or 1))))
    fh += fh % 2
    n = len(p) // (ww * fh * 3)
    mouth = []
    for i in range(n):
        fr = np.frombuffer(p, np.uint8, ww * fh * 3, i * ww * fh * 3).reshape(fh, ww, 3)
        r = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(fr)))
        if not r.face_landmarks:
            mouth.append(np.nan)
            continue
        L = r.face_landmarks[0]
        face_h = abs(L[152].y - L[10].y) + 1e-6
        mouth.append(abs(L[14].y - L[13].y) / face_h)
    m = np.array(mouth)
    if n == 0 or np.mean(np.isnan(m)) > 0.5:
        return None
    a = VS.load_audio(Path(src), 16000, start, dur)
    a = a.mean(axis=1) if a.ndim > 1 else a
    hop = 16000 // fps
    env = np.array([np.sqrt(np.mean(a[i * hop:(i + 1) * hop] ** 2)) for i in range(n)])
    ok = ~np.isnan(m)
    if ok.sum() < 6 or np.std(m[ok]) < 1e-4:
        return 0.0
    # mouth moves slightly ahead of sound: allow +-2 frame lag
    best = max(np.corrcoef(np.roll(m[ok], s), env[ok])[0, 1] for s in (-2, -1, 0, 1, 2))
    return float(best)


# ---------------------------------------------------------------- vicestyle hooks
def stark_seg_filter(seg, w: int, h: int) -> str:
    """Full-frame 4:5 crop (no 21:9 bars: letterbox is feathered in post), cold/warm grade."""
    z = max(1.0, seg.zoom)
    cx = getattr(seg, "cx", 0.5)
    f = [f"scale=-2:{int(h * z)}:flags=lanczos",
         f"crop={w}:{h}:'max(0,min(iw-{w},{cx}*iw-{w}/2))':(ih-{h})/2", "setsar=1"]
    if seg.gamma and abs(seg.gamma - 1) > 0.01:
        f.append(f"eq=gamma={seg.gamma}")
    f.append(GRADE_COLD if seg.kind == "dialogue" else GRADE_WARM)
    f.append(f"fps={VS.FPS},format=yuv420p")
    return ",".join(f)


# ---------------------------------------------------------------- post pass (drop, punch, finish, outro)
class Post:
    def __init__(self, seed: int = 7):
        import cv2
        self.cv2 = cv2
        self.rng = np.random.default_rng(seed)
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        self.yy, self.xx = yy, xx
        bar, fe = 70, 60
        lb = np.clip(np.minimum((yy - bar) / fe, (H - bar - yy) / fe), 0, 1)
        lb = lb * lb * (3 - 2 * lb)
        r = np.sqrt(((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H / 2) / (H * 0.6)) ** 2)
        vig = np.clip(1 - 0.55 * np.clip(r - 0.45, 0, 1) ** 1.4 * 1.8, 0, 1)
        self.finish_mask = (lb * vig)[..., None].astype(np.float32)
        self.grains = [cv2.GaussianBlur(self.rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 0.8)
                       for _ in range(6)]
        self.shards = None

    # -- helpers
    def zoom(self, img, s):
        if abs(s - 1) < 1e-3:
            return img
        M = np.float32([[s, 0, W / 2 - s * W / 2], [0, s, H / 2 - s * H / 2]])
        return self.cv2.warpAffine(img, M, (W, H), flags=self.cv2.INTER_LINEAR, borderMode=self.cv2.BORDER_REFLECT)

    @staticmethod
    def rgbsplit(x, px):
        p = int(round(px))
        if p < 1:
            return x
        o = x.copy()
        o[:, :, 2] = np.roll(x[:, :, 2], p, axis=1)
        o[:, :, 0] = np.roll(x[:, :, 0], -p, axis=1)
        return o

    def hblur(self, x, k):
        k = int(k)
        if k < 3:
            return x
        return self.cv2.filter2D(x, -1, np.ones((1, k), np.float32) / k, borderType=self.cv2.BORDER_REFLECT)

    def bloom(self, x, amt, th):
        cv2 = self.cv2
        b = np.clip(cv2.resize(x, (W // 4, H // 4), interpolation=cv2.INTER_AREA) - th, 0, 1)
        b = cv2.resize(cv2.GaussianBlur(b, (0, 0), 4.5), (W, H))
        return np.clip(x + b * amt * 3, 0, 1)

    def finish(self, x, fi):
        rng = np.random.default_rng(1000 + fi)
        g = self.grains[fi % 6] * 0.035
        x = x + g[..., None] * (0.6 + 0.4 * (1 - x))
        if rng.random() < 0.35:
            for _ in range(rng.integers(1, 4)):
                c = float(rng.choice([0.9, 0.05]))
                self.cv2.circle(x, (int(rng.integers(0, W)), int(rng.integers(0, H))), int(rng.integers(1, 3)),
                                (c, c, c), -1, self.cv2.LINE_AA)
        return np.clip(x * self.finish_mask, 0, 1)

    # -- glass
    def make_shards(self, ix, iy):
        cv2, rng = self.cv2, self.rng
        pts = []
        for _ in range(150):
            rr = abs(rng.normal(0, 1)) * 330 + 8
            th = rng.uniform(0, 2 * np.pi)
            pts.append((ix + rr * np.cos(th), iy + rr * np.sin(th) * 1.1))
        for _ in range(45):
            pts.append((rng.uniform(-80, W + 80), rng.uniform(-80, H + 80)))
        pts = np.array(pts, np.float32)
        gy, gx = np.mgrid[0:H // 3, 0:W // 3].astype(np.float32) * 3
        lab = np.argmin((gx[..., None] - pts[:, 0]) ** 2 + (gy[..., None] - pts[:, 1]) ** 2, axis=2)
        lab = cv2.resize(lab.astype(np.float32), (W, H), interpolation=cv2.INTER_NEAREST).astype(np.int32)
        edges = (cv2.morphologyEx(lab.astype(np.float32), cv2.MORPH_GRADIENT, np.ones((3, 3))) > 0).astype(np.float32)
        shards = []
        for i in range(len(pts)):
            m = lab == i
            if m.sum() < 40:
                continue
            ys, xs = np.nonzero(m)
            x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
            cx, cy = xs.mean(), ys.mean()
            v = np.array([cx - ix, cy - iy])
            d = np.linalg.norm(v) + 1
            v = v / d * rng.uniform(1100, 1900) * (0.8 + 260 / (d + 60))
            mm = m[y0:y1, x0:x1].astype(np.float32)
            # refraction: each shard samples the image slightly displaced + its own normal tilt
            shards.append(dict(box=(x0, x1, y0, y1), m=mm, e=cv2.morphologyEx(mm, cv2.MORPH_GRADIENT,
                               np.ones((3, 3), np.uint8)), c=(cx, cy), v=v, d=d,
                               rot=rng.uniform(-260, 260) * (1 + 150 / (d + 50)),
                               zs=rng.uniform(1.5, 4.0) * (1 + 200 / (d + 80)), tint=rng.uniform(0.9, 1.15),
                               ga=rng.uniform(0, 2 * np.pi), gs=rng.uniform(0.05, 0.18),
                               refr=(rng.uniform(-9, 9), rng.uniform(-9, 9))))
        cracks = np.zeros((H, W), np.float32)

        def walk(x0, y0, th, L, w, depth):
            l = 0
            while l < L:
                th += rng.normal(0, 0.22)
                st = rng.uniform(8, 22)
                x1, y1 = x0 + st * np.cos(th), y0 + st * np.sin(th)
                fall = max(0.25, 1 - np.hypot(x1 - ix, y1 - iy) / 900)
                cv2.line(cracks, (int(x0), int(y0)), (int(x1), int(y1)), fall, w, cv2.LINE_AA)
                if depth < 2 and rng.random() < 0.06:
                    walk(x1, y1, th + rng.choice([-1, 1]) * rng.uniform(0.4, 0.9), L * 0.4, 1, depth + 1)
                x0, y0, l = x1, y1, l + st
        for i in range(22):
            walk(ix, iy, i / 22 * 2 * np.pi + rng.uniform(-0.12, 0.12), rng.uniform(500, 1100), 1 if i % 3 else 2, 0)
        rd = np.hypot(self.xx - ix, self.yy - iy)
        cracks = np.maximum(cracks, edges * np.clip(1 - rd / 320, 0, 1) * 0.8)
        cv2.circle(cracks, (int(ix), int(iy)), 10, 1.0, -1, cv2.LINE_AA)
        self.shards = (shards, cracks, (ix, iy))

    def cracks(self, x, k):
        shards, cracks, (ix, iy) = self.shards
        rd = np.hypot(self.xx - ix, self.yy - iy)
        cm = cracks * np.clip((k * 1150 - rd) / 60, 0, 1)
        g = self.cv2.GaussianBlur(cm, (0, 0), 2.5)
        return np.clip(x * (1 - cm[..., None] * 0.3) + (cm * 0.85 + g * 0.5)[..., None], 0, 1)

    def shatter(self, still, bg, tt):
        cv2 = self.cv2
        shards, _, _ = self.shards
        out = bg.copy()
        for s in sorted(shards, key=lambda s: -s["d"]):
            x0, x1, y0, y1 = s["box"]
            cx, cy = s["c"]
            life = np.clip(1 - tt / 0.36, 0, 1) ** 0.6
            if life <= 0:
                continue
            rx, ry = s["refr"]
            sx0, sy0 = int(np.clip(x0 + rx, 0, W - (x1 - x0))), int(np.clip(y0 + ry, 0, H - (y1 - y0)))
            patch = still[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
            hh, ww = patch.shape[:2]
            acc = None
            for sub in (0.0, 0.006, 0.012):  # motion blur
                p = max(0.0, tt - sub)
                sc = 1 + s["zs"] * p * p * 4 + 0.3 * p
                M = cv2.getRotationMatrix2D((cx - x0, cy - y0), s["rot"] * p, sc)
                M[0, 2] += x0 + s["v"][0] * p * (1 + 2 * p)
                M[1, 2] += y0 + s["v"][1] * p * (1 + 2 * p) + 500 * p * p
                gx = (np.linspace(-1, 1, ww)[None, :] * np.cos(s["ga"]) +
                      np.linspace(-1, 1, hh)[:, None] * np.sin(s["ga"]))
                glint = (np.clip(gx + (p * 6 - 1), -1, 1) * 0.5 + 0.5) ** 6 * s["gs"] * 2
                col = patch * s["tint"] + glint[..., None] + s["e"][..., None] * 0.45
                img = np.dstack([col, s["m"]]).astype(np.float32)
                wv = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=0)
                acc = wv if acc is None else acc + wv
            acc /= 3
            a = acc[:, :, 3:4] * life
            out = out * (1 - a) + acc[:, :, :3] / np.maximum(acc[:, :, 3:4], 1e-3) * a
        return np.clip(out, 0, 1)

    # -- outro
    def outro_frames(self, n, plate_src, plate_start, logo):
        """Real storm-cloud plate (graded monochrome, slow push) + 3D logo with flash flicker."""
        cv2 = self.cv2
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{plate_start:.2f}", "-i", str(plate_src),
                              "-frames:v", str(n), "-vf",
                              f"fps={FPS},crop=ih*0.62*{W}/{H}:ih*0.62:iw*0.04:0,"
                              f"scale={W}:{H}:flags=lanczos,format=gray,eq=contrast=1.35:brightness=-0.06",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
        fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.float32) / 255.
        rr = np.sqrt(((self.xx - W / 2) / (W * 0.58)) ** 2 + ((self.yy - H * 0.47) / (H * 0.46)) ** 2)
        vig = np.clip(1.15 - rr, 0, 1) ** 1.2
        for i in range(n):
            tt = i / FPS
            c = fr[min(i, len(fr) - 1)]
            c = self.zoom(np.dstack([c] * 3), 1.0 + 0.04 * tt / (n / FPS))
            flick = 1 + 0.35 * math.exp(-((tt - 1.45) / 0.12) ** 2) + 0.2 * math.exp(-((tt - 1.7) / 0.08) ** 2)
            x = c * vig[..., None] * 0.95 * flick
            L = logo[min(len(logo) - 1, int(tt * 30))]
            lh = L.shape[0]
            y0 = int(H * 0.47 - lh / 2)
            a = L[:, :, 3:4] * np.clip((tt - 0.12) / 0.45, 0, 1)
            sh = cv2.GaussianBlur(L[:, :, 3], (0, 0), 12)[..., None] * a * 0.7
            reg = x[y0:y0 + lh] * (1 - sh)
            reg = reg * (1 - a) + L[:, :, :3] * (0.9 + 0.25 * (flick - 1)) * a
            x[y0:y0 + lh] = reg
            fade = np.clip((n / FPS - 0.05 - tt) / 0.7, 0, 1)
            x = np.clip(x * fade, 0, 1)
            if tt < 0.12:
                x = np.clip(x + (1 - tt / 0.12) * 0.6, 0, 1)
            yield x


def load_logo(path: Path) -> list[np.ndarray]:
    import cv2
    tmp = cache() / f"logo_{key(path.stat().st_size, path.name)}"
    if not tmp.exists():
        tmp.mkdir()
        run(["ffmpeg", "-v", "error", "-c:v", "libvpx-vp9", "-i", str(path), str(tmp / "l_%04d.png")])
    out = []
    for p in sorted(tmp.glob("l_*.png")):
        L = cv2.imread(str(p), cv2.IMREAD_UNCHANGED).astype(np.float32) / 255.
        pw = W - L.shape[1]
        L = np.pad(L, ((0, 0), (pw // 2, pw - pw // 2), (0, 0))) if pw > 0 else L[:, (-pw) // 2:(-pw) // 2 + W]
        out.append(L)
    return out


# ---------------------------------------------------------------- main build
@dataclass
class Spec:
    song: str
    build: list
    climax: list
    drop: float | None = None
    loops: int = 2
    slot_beats: int = 2
    slowmo: float = 0.5
    min_height: int = 1080
    outro: float = 3.75
    plate: dict | None = None
    demucs: bool = True
    font: str | None = None
    lip_min_corr: float = 0.15
    pattern: dict | None = None


def stark_font() -> str:
    """Cinzel Bold static instance (cached); falls back to vicestyle's Anton."""
    var = Path("/usr/share/fonts/truetype/sand-box/google/Cinzel/Cinzel-VariableFont_wght.ttf")
    out = cache() / "Cinzel-Bold.ttf"
    if not out.exists() and var.exists():
        try:
            run([sys.executable, "-m", "fontTools.varLib.instancer", str(var), "wght=700", "-q", "-o", str(out)])
        except Exception:
            pass
    return str(out) if out.exists() else VS.font_path(None)


def build(spec: Spec, out: Path, workers: int = 4) -> dict:
    import cv2
    song = Path(spec.song)
    binfo = VS.detect_beats(song)
    beats = binfo["beats"]
    beat = 60.0 / binfo["tempo"]
    drop = spec.drop if spec.drop else find_drop(song, beats)
    # vicestyle's beat grid is tempo-uniform: shift its phase so a grid line sits exactly on the drop
    near = min(beats, key=lambda b: abs(b - drop))
    beats = [b + (drop - near) for b in beats]
    binfo = dict(binfo, beats=beats)
    di = beats.index(min(beats, key=lambda b: abs(b - drop)))
    log(f"tempo={binfo['tempo']:.1f} drop={drop:.3f}s")

    # BUILD: lip-safe dialogue (own shot audio), vocals isolated, lip/mouth QA
    segs, qa = [], []
    for b in spec.build:
        clip, off = clean_dialogue(b["src"], b["start"], b["end"], spec.demucs)
        dur = b["end"] - b["start"]
        corr = mouth_speech_corr(b["src"], b["start"], dur) if b.get("lip_check", True) else None
        qa.append({"src": Path(b["src"]).name, "start": b["start"], "mouth_speech_corr": corr,
                   "offscreen_ok": b.get("offscreen", False)})
        if corr is not None and corr < spec.lip_min_corr and not b.get("offscreen", False):
            log(f"LIP-SYNC WARNING {Path(b['src']).name}@{b['start']}: visible mouth does not follow the "
                f"voice (r={corr:.2f}); mark as offscreen narration or pick the speaker's own shot")
        s = VS.Seg(kind="dialogue", src=clip, start=off, beats=0, fix=b.get("fix", {}),
                   drop=b.get("drop", []), zoom=b.get("zoom", 1.0), gamma=b.get("gamma"),
                   fix_phrases=b.get("fix_phrases", []))
        s.cx = b.get("cx", 0.5)
        s._dur = dur
        segs.append(s)
    bb, nslots, sb = layout_budget([s._dur for s in segs], beat, len(spec.climax), spec.loops, spec.slot_beats)
    for s, n in zip(segs, bb):
        s.beats = n
    song_start = beats[di - sum(bb)]

    # CLIMAX: >=1080p only, slow-mo, looped
    shots = []
    for c in spec.climax:
        h = probe_height(c["src"])
        if h < spec.min_height:
            log(f"reject climax shot {Path(c['src']).name}@{c['start']}: {h}p < {spec.min_height}p")
            continue
        shots.append(c)
    if not shots:
        raise SystemExit("no climax shots >= min_height")
    slot = beat * sb
    slow = [slowmo(c["src"], c["start"], slot + 0.1, spec.slowmo, c.get("crop")) for c in shots]
    for _ in range(spec.loops):
        for p in slow:
            s = VS.Seg(kind="montage", src=p, start=0.0, beats=sb, gamma=1.0)
            s.cx = 0.5
            segs.append(s)

    # vicestyle render with the Stark hooks (no GTA defaults touched)
    VS.FPS = FPS
    orig_filter, orig_beats = VS.seg_filter, VS.detect_beats
    VS.seg_filter = stark_seg_filter
    VS.detect_beats = lambda _song: binfo
    pat = dict(spec.pattern or STARK_PATTERN)
    CP.BY_ID[pat["id"]] = pat
    main = out.with_name(out.stem + "_main.mp4")
    try:
        meta = VS.render(segs, song, song_start, main, "wide", f"{W}x{H}", None, None,
                         spec.font or stark_font(), black_tail=0.0, workers=workers, crf=12, pattern=pat["id"])
    finally:
        VS.seg_filter, VS.detect_beats = orig_filter, orig_beats
    times = meta["segment_times"]
    t_drop = next(t["t0"] for t in times if t["kind"] == "montage")
    mont = [t for t in times if t["kind"] == "montage"]
    total = meta["duration"]

    # POST: blur ramp, cracks, flash + shatter, punch/RGB on cuts, grain/letterbox, outro
    post = Post()
    post.make_shards(W * 0.5, H * 0.44)
    cap = cv2.VideoCapture(str(main))
    nmain = int(round(total * FPS))
    n_out = int(round(spec.outro * FPS))
    plate = spec.plate or {"start": 2.0}
    plate_src = plate.get("src") or asset("plate")
    logo = load_logo(LOGO_ASSET)
    vid = out.with_name(out.stem + "_v.mp4")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                            "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "15",
                            "-pix_fmt", "yuv420p", str(vid)], stdin=subprocess.PIPE)
    still = None
    for fi in range(nmain):
        ok, fr = cap.read()
        if not ok:
            break
        t = fi / FPS
        x = fr.astype(np.float32) / 255.
        if t < t_drop:
            x = post.bloom(x, 0.12, 0.78)
            if t > t_drop - 0.32:
                k = (t - (t_drop - 0.32)) / 0.32
                if still is None or t < t_drop - 1.5 / FPS:
                    still = np.clip(x * 1.08 + 0.03, 0, 1)
                x = np.clip(post.hblur(x, 3 + k * k * 90) + k ** 3 * 0.35, 0, 1)
            if t > t_drop - 0.12:
                x = post.cracks(x, (t - (t_drop - 0.12)) / 0.12)
        else:
            m = next((m for m in mont if m["t0"] - 1e-6 <= t < m["t1"]), mont[-1])
            lt = t - m["t0"]
            x = post.bloom(x, 0.3, 0.65)
            x = post.zoom(x, 1 + 0.13 * math.exp(-lt / 0.11))
            x = post.rgbsplit(x, 16 * math.exp(-lt / 0.07) + 1.5)
            x = np.clip(x + 0.18 * math.exp(-lt / 0.05), 0, 1)
            tt = t - t_drop
            if tt < 1.5 / FPS:
                x = np.ones_like(x)
            elif tt < 0.45 and still is not None:
                wf = math.exp(-tt / 0.13)
                bg = np.clip(post.hblur(x, int(60 * wf) + 1) * (1 - wf) + wf, 0, 1)
                x = post.shatter(still, bg, tt)
            if t > total - 0.1:
                k2 = (t - (total - 0.1)) / 0.1
                x = np.clip(post.hblur(x, int(k2 * 60) + 1) + k2 * 0.5, 0, 1)
        enc.stdin.write((post.finish(x, fi) * 255 + 0.5).astype(np.uint8).tobytes())
        if fi % 300 == 0:
            log(f"post {fi}/{nmain}")
    for j, x in enumerate(post.outro_frames(n_out, plate_src, plate.get("start", 2.0), logo)):
        enc.stdin.write((post.finish(x, nmain + j) * 255 + 0.5).astype(np.uint8).tobytes())
    enc.stdin.close()
    enc.wait()

    # AUDIO: vicestyle mix + glass SFX on the drop + song tail echoing out over the logo
    glass = asset("glass")
    end_song = song_start + total
    run(["ffmpeg", "-v", "error", "-y", "-i", str(main), "-i", str(song), "-i", str(glass), "-filter_complex",
         f"[0:a]aresample=48000[m];"
         f"[1:a]atrim={end_song:.3f}:{end_song + 3.0:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
         f"aecho=0.8:0.85:350|700|1050:0.45|0.3|0.2,lowpass=f=3500,afade=t=out:st=0.2:d=3.3,"
         f"apad=whole_dur={spec.outro + 0.1:.2f}[tail];[m][tail]concat=n=2:v=0:a=1[mt];"
         f"[2:a]aresample=48000,aformat=channel_layouts=stereo,volume=0.55,adelay={int(t_drop * 1000) - 60}|"
         f"{int(t_drop * 1000) - 60}[g];"
         f"[mt][g]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.2:LRA=11,aresample=48000[a]",
         "-map", "[a]", "-c:a", "pcm_s16le", str(out.with_suffix(".wav"))])
    run(["ffmpeg", "-v", "error", "-y", "-i", str(vid), "-i", str(out.with_suffix(".wav")), "-map", "0:v",
         "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart",
         str(out)])
    for p in (vid, out.with_suffix(".wav")):
        p.unlink(missing_ok=True)
    info = {"out": str(out), "song_start": song_start, "drop_song": drop, "drop_t": t_drop,
            "duration": total + spec.outro, "lip_qa": qa, "climax_shots": len(shots),
            "captions": meta.get("captions", [])}
    Path(str(out) + ".json").write_text(json.dumps(info, indent=1))
    log("wrote", out)
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    d = {k: v for k, v in json.loads(Path(a.spec).read_text()).items() if not k.startswith("_")}
    base = Path(a.spec).resolve().parent
    rel = lambda p: str((base / p).resolve()) if p and not os.path.isabs(p) else p  # noqa: E731
    d["song"] = rel(d["song"])
    for item in d.get("build", []) + d.get("climax", []):
        item["src"] = rel(item["src"])
    if d.get("plate", {}).get("src"):
        d["plate"]["src"] = rel(d["plate"]["src"])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    build(Spec(**d), out, a.workers)


if __name__ == "__main__":
    main()

