"""Automatic QA for a rendered video."""
from __future__ import annotations

import json
import re

from .util import run, probe


def qa_video(path: str, meta: dict, fmt: str, cfg: dict) -> dict:
    checks = {}
    info = probe(path)
    dur = float(info["format"]["duration"])
    vs = [s for s in info["streams"] if s.get("codec_type") == "video"]
    aus = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    lo, hi = cfg.get("qa_duration", [40, 70])
    checks["duration"] = {"ok": lo <= dur <= hi, "value": round(dur, 2)}
    want = (2160, 3840) if fmt == "shorts" else tuple(int(x) for x in cfg["platform_res"].split("x"))
    got = (vs[0]["width"], vs[0]["height"]) if vs else (0, 0)
    checks["resolution"] = {"ok": got == want, "value": f"{got[0]}x{got[1]}", "want": f"{want[0]}x{want[1]}"}
    # audio present and not silent
    mv = -99.0
    if aus:
        r = run(["ffmpeg", "-v", "info", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"], check=False)
        m = re.search(r"mean_volume: (-?[\d.]+) dB", r.stderr)
        mv = float(m.group(1)) if m else -99.0
    checks["audio"] = {"ok": bool(aus) and mv > -35, "mean_db": mv}
    # black frames anywhere but the ending (cut to black on the beat)
    r = run(["ffmpeg", "-v", "info", "-i", path, "-vf", "blackdetect=d=0.25:pix_th=0.06", "-an", "-f", "null", "-"],
            check=False)
    blacks = [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", r.stderr)]
    mid_black = [b for b in blacks if b[1] < dur - 1.2]
    checks["black_frames"] = {"ok": not mid_black, "mid_video": mid_black[:5],
                              "ending_black": any(b[1] >= dur - 0.2 for b in blacks)}
    # captions: inside the safe area (picture area between letterbox bars, 2.5% side margin), no face overlap
    W, H = meta.get("res", [2560, 1440])
    bar = meta.get("letterbox_bar", 0)
    bad, face = [], []
    for c in meta.get("caption_boxes", []):
        x, y, w, h = c["box"]
        if x < W * 0.02 or x + w > W * 0.98 or y < bar or y + h > H - bar:
            bad.append(c["text"])
        if c.get("overlap_px", 0) > 0.02 * w * h:
            face.append(c["text"])
    checks["captions_safe_area"] = {"ok": not bad, "outside": bad[:5], "count": len(meta.get("caption_boxes", []))}
    checks["captions_clear_of_faces"] = {"ok": not face, "covering": face[:5]}
    # captions only during dialogue segments (never song lyrics over montage)
    dl = [(s["t0"], s["t1"]) for s in meta.get("segment_times", []) if s["kind"] == "dialogue"]
    stray = [c["text"] for c in meta.get("caption_boxes", [])
             if not any(a - 0.05 <= c["t0"] and c["t1"] <= b + 0.05 for a, b in dl)]
    checks["captions_only_in_dialogue"] = {"ok": not stray, "stray": stray[:5]}
    checks["no_name_pops"] = {"ok": not any(s.get("name") for s in meta.get("segments", []))}
    return {"pass": all(c["ok"] for c in checks.values()), "checks": checks}

