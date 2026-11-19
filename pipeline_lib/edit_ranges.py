"""Source-range helpers: stop the same long shot being reused in one video or across recent videos."""
from __future__ import annotations

import re
from pathlib import Path

# short re-use under this many seconds is allowed; anything longer is a repeat
LONG_REUSE = 1.5
# adjacent cuts from the same take are treated as one used scene
MERGE_GAP = 1.5

# filenames from the original hand / code GTA edits -> YouTube id
ALIASES = {
    "trailer1.mp4": "QdBZY2fkU-0", "GTAVI_Trailer_1": "QdBZY2fkU-0", "GTAVI_Trailer_1.mp4": "QdBZY2fkU-0",
    "trailer2.mkv": "VQRLujxTm3c", "launch_trailer.mkv": "rz1B4B2yBZY",
}


def source_id(src: str) -> str:
    name = Path(src or "").name
    stem = re.sub(r"_c\d+$", "", Path(name).stem)
    return ALIASES.get(name) or ALIASES.get(stem) or ALIASES.get(stem + Path(name).suffix) or stem


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(float(a1), float(b1)) - max(float(a0), float(b0)))


def merge_ranges(ranges: list, gap: float = MERGE_GAP) -> list[list[float]]:
    xs = sorted(([float(a), float(b)] for a, b in ranges if b > a), key=lambda r: r[0])
    if not xs:
        return []
    out = [xs[0][:]]
    for a, b in xs[1:]:
        if a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def overlaps_long(key: str, start: float, end: float, used: list, min_keep: float = LONG_REUSE) -> bool:
    """used: iterable of (id, start, end). True if this range reuses >= min_keep seconds of a used take."""
    for item in used:
        if len(item) >= 3 and item[0] == key and overlap(start, end, item[1], item[2]) >= min_keep:
            return True
    return False


def seg_source_end(seg: dict, period: float | None = None) -> float:
    start = float(seg.get("start") if seg.get("start") is not None else seg.get("s") or 0)
    if seg.get("t0") is not None and seg.get("t1") is not None:
        return start + max(0.2, float(seg["t1"]) - float(seg["t0"]))
    beats = float(seg.get("beats") or 1)
    step = float(period) if period and period > 0.2 else 0.55
    return start + beats * step

