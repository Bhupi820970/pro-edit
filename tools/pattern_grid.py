#!/usr/bin/env python3
"""QA contact sheet of all caption patterns on real graded/letterboxed dialogue frames.

python3 tools/pattern_grid.py --src CLIP.mp4 --times 12.3,40.1 --out patterns_grid.jpg
Each tile shows the pattern's first caption chunk placed for that frame (chunks never overlap in a video).
"""
import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vicestyle as V  # noqa: E402
import caption_patterns as CP  # noqa: E402

LINES = [["FIXING", "SOME", "LEAKS"], ["SERIOUSLY", "DUDE"]]


def frame(src, t, W, H):
    seg = V.Seg("dialogue", src, t, 1, gamma=1.0)
    seg.f0, seg.f1 = 0, 1
    vf = V.seg_filter(seg, W, H).replace("noise=alls=5:allf=t,", "")
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", src, "-frames:v", "1", "-vf", vf,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W, 3)


def tile(fr, pat, W, H, fpath):
    im = Image.fromarray(fr).convert("RGBA")
    faces = V.find_faces(fr)
    main = max(faces, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])) if faces else None
    ci = 0
    groups = []
    flat = [w for ln in LINES for w in ln]
    k = pat["words"]
    i = 0
    while i < len(flat) and len(groups) < 1:
        groups.append(flat[i:i + k])
        i += k
    for step, g in enumerate(groups):
        items, ci = V.style_items([{"raw": w} for w in g], pat, H, ci)
        if pat.get("classic"):
            img, x, y, ov, zone = V.place_beside(items, pat, 0, faces, main, W, H, fpath, step)
        else:
            img, x, y, ov, zone = V.place_beside(items, pat, step, faces, main, W, H, fpath, step)
        if step == 1:
            img = V._fade(img, 0.55)
        im.alpha_composite(img, (max(0, x), max(0, y)))
    return im.convert("RGB"), faces


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", action="append", required=True)
    ap.add_argument("--times", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    W, H = 1280, 720
    fpath = V.font_path()
    frames = []
    for src, ts in zip(a.src, a.times):
        for t in ts.split(","):
            frames.append(frame(src, float(t), W, H))
    tiles = []
    for i, p in enumerate(CP.PATTERNS):
        t, faces = tile(frames[i % len(frames)], p, W, H, fpath)
        d = ImageDraw.Draw(t)
        f = ImageFont.truetype(fpath, 34)
        lab = f"{p['id']}  {p['name']}"
        d.rectangle([0, H - 56, W, H], fill=(0, 0, 0))
        d.text((16, H - 50), lab, font=f, fill=(255, 255, 255))
        x = 16 + d.textlength(lab, font=f) + 20
        for c in p["keys"]:
            d.rectangle([x, H - 44, x + 28, H - 16], fill=tuple(c))
            x += 36
        tiles.append(t.resize((640, 360), Image.LANCZOS))
    cols = 3
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 640 + (cols + 1) * 8, rows * 360 + (rows + 1) * 8), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, (8 + (i % cols) * 648, 8 + (i // cols) * 368))
    sheet.save(a.out, quality=90)
    print("wrote", a.out, sheet.size)


if __name__ == "__main__":
    main()

