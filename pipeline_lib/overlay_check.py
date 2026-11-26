"""Plain-script (no AI) detector for burned-in watermarks, channel logos, corner bugs, burned channel
names AND burned-in captions / subtitles / on-screen text in a downloaded source clip. Used by the box fetcher at DOWNLOAD time, before upload.

Idea: sample frames spread over the whole clip (many different shots). Real footage changes between
shots; an overlay does not. Pixels that stay the same in every sample AND sit on a sharp edge (text /
logo outline) inside a corner or an edge band are an overlay. Letterbox / pillarbox borders (long
straight static lines across the frame) are ignored, and a static-camera clip (whole frame static) is
judged by how much MORE static the corner is than the centre.
ffmpeg is called with argument lists only."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

W, H = 960, 540


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def _frame(path: Path, t: float) -> np.ndarray | None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={W}:{H}:flags=area,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=60)
    if len(r.stdout) != W * H:
        return None
    return np.frombuffer(r.stdout, np.uint8).reshape(H, W).astype(np.float32)


def sample_frames(path: Path, n: int = 24) -> list | None:
    """Gray 960x540 frames spread over the clip, near-black frames (fades / black cards) dropped.
    None if the clip is too short to judge."""
    dur = _duration(path)
    if dur < 8:
        return None
    frames = [f for f in (_frame(path, t) for t in np.linspace(dur * 0.05, dur * 0.95, n)) if f is not None]
    return [f for f in frames if f.mean() > 18]


def static_overlay_scores(path: Path, n: int = 16, frames: list | None = None) -> dict:
    if frames is None:
        frames = sample_frames(path, n)
    if frames is None:
        return {"ok": False, "reason": "too short to check", "frames": 0}
    if len(frames) < 8:
        return {"ok": False, "reason": f"only {len(frames)} usable frames", "frames": len(frames)}
    F = np.stack(frames)
    # persistent edges: an overlay outline is a sharp edge at the SAME pixel in (nearly) every sampled
    # frame, even when it is semi-transparent; real footage edges move between shots
    gy, gx = np.gradient(F, axis=(1, 2))
    edges = np.hypot(gx, gy) > 12.0
    persist = edges.mean(0)
    mask = persist >= 0.85
    # letterbox / pillarbox borders and other full-width static lines are not logos
    for axis in (1, 0):
        lines = np.where(mask.mean(axis) > 0.2)[0]
        for d in (-2, -1, 0, 1, 2):
            idx = np.clip(lines + d, 0, mask.shape[1 - axis] - 1)
            if axis == 1:
                mask[idx, :] = False
            else:
                mask[:, idx] = False
    cw, ch, band = int(W * 0.24), int(H * 0.20), int(H * 0.13)
    regions = {
        "top_left": mask[:ch, :cw], "top_right": mask[:ch, -cw:],
        "bottom_left": mask[-ch:, :cw], "bottom_right": mask[-ch:, -cw:],
        "top_band": mask[:band, :], "bottom_band": mask[-band:, :],
    }
    centre = mask[int(H * 0.3):int(H * 0.7), int(W * 0.3):int(W * 0.7)].mean()
    scores = {k: round(float(v.mean()), 4) for k, v in regions.items()}
    return {"ok": True, "scores": scores, "centre": round(float(centre), 4), "frames": len(frames)}


def _local_min(a: np.ndarray, r: int) -> np.ndarray:
    """Min filter over a (2r+1)x(2r+1) window with plain numpy shifts."""
    m = a.copy()
    for d in range(1, r + 1):
        m[d:, :] = np.minimum(m[d:, :], a[:-d, :])
        m[:-d, :] = np.minimum(m[:-d, :], a[d:, :])
    b = m.copy()
    for d in range(1, r + 1):
        b[:, d:] = np.minimum(b[:, d:], m[:, :-d])
        b[:, :-d] = np.minimum(b[:, :-d], m[:, d:])
    return b


def text_lines(f: np.ndarray) -> list[tuple[float, int, float, int]]:
    """Caption-like text lines in one gray frame: rows of bright glyph pixels that touch a dark outline /
    shadow (white or yellow subtitles, boxed captions), 6-48 px tall at 540p, with many separate glyph
    strokes across the row. Returns (y centre, height, horizontal extent, stroke count)."""
    cand = (f > 190) & (_local_min(f, 3) < 70)
    out = []
    y0 = int(H * 0.03)
    sub = cand[y0:int(H * 0.97)]
    on = sub.sum(1) >= 12
    y = 0
    while y < len(on):
        if not on[y]:
            y += 1
            continue
        s = y
        while y < len(on) and on[y]:
            y += 1
        h = y - s
        if 6 <= h <= 48:
            band = sub[s:y].any(0)
            cols = np.flatnonzero(band)
            ext = (cols.max() - cols.min()) / W if len(cols) else 0.0
            strokes = int(np.count_nonzero(np.diff(band.astype(np.int8)) == 1))
            if 0.06 <= ext <= 0.92 and strokes >= 8 and sub[s:y].mean() > 0.02:
                out.append((y0 + s + h / 2, h, round(float(ext), 2), strokes))
    return out


def caption_scores(frames: list) -> dict:
    """Burned captions change words but stay on the same line height: count the sampled frames that have
    a text line within +-18 px of the most common text row. Real footage text (a sign, a plate) does not
    sit at the same row across many different shots."""
    per = [text_lines(f) for f in frames]
    best, best_y = 0, None
    for y in (h[0] for p in per for h in p):
        k = sum(1 for p in per if any(abs(h[0] - y) <= 18 for h in p))
        if k > best:
            best, best_y = k, y
    return {"text_frames": best, "frames": len(frames), "share": round(best / max(len(frames), 1), 3),
            "row": round(best_y / H, 2) if best_y is not None else None}


# captions: clean clips calibrated at <=13% same-row text frames, burned subtitles at >=62%
CAPTION_SHARE, CAPTION_MIN_FRAMES = 0.30, 5

# corner bug: >=0.4% of the corner box is persistent sharp edges (a small text/logo outline at 960x540);
# band: burned channel name / ticker / lower third across the top or bottom
CORNER_MIN, BAND_MIN, RATIO = 0.004, 0.004, 4.0


def check_clip(path: Path) -> tuple[bool, str, dict]:
    """(clean, reason, details). clean=False -> reject (overlay found, or the clip could not be checked)."""
    frames = sample_frames(Path(path), 24)
    d = static_overlay_scores(Path(path), frames=frames)
    if not d.get("ok"):
        return False, f"overlay check failed: {d.get('reason')}", d
    cap = caption_scores(frames)
    d["captions"] = cap
    if cap["text_frames"] >= CAPTION_MIN_FRAMES and cap["share"] >= CAPTION_SHARE:
        return False, (f"burned captions / on-screen text: text line at {cap['row']:.0%} height in "
                       f"{cap['text_frames']}/{cap['frames']} frames"), d
    c = max(d["centre"], 0.0005)
    hits = []
    for k, v in d["scores"].items():
        lim = BAND_MIN if k.endswith("band") else CORNER_MIN
        if v >= lim and v >= RATIO * c:
            hits.append(f"{k}={v:.3f}")
    if hits:
        return False, "static overlay (logo/watermark/burned text): " + ", ".join(hits), d
    return True, "clean", d
