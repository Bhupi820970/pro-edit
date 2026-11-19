import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fanedit_stark as F  # noqa: E402


def test_drop_found_at_quiet_to_loud_jump():
    sr = 22050
    t = np.arange(int(sr * 60)) / sr
    y = 0.03 * np.sin(2 * np.pi * 220 * t)
    hit = 41.3
    y[t >= hit] = 0.5 * np.sign(np.sin(2 * np.pi * 55 * t[t >= hit]))
    assert abs(F.drop_from_signal(y, sr, []) - hit) < 0.05


def test_layout_budget_never_clips_speech():
    beat = 60 / 112
    bb, slots, sb = F.layout_budget([19.85, 2.05, 0.7], beat, 7, 2, 2)
    assert all(n * beat >= d - 0.15 * beat for n, d in zip(bb, [19.85, 2.05, 0.7]))
    assert slots == 14 and sb == 2


def test_stark_pattern_is_separate_from_gta_patterns():
    import caption_patterns as CP
    assert F.STARK_PATTERN["id"] not in [p["id"] for p in CP.PATTERNS]
    assert CP.get("P01")["classic"] is True        # approved GTA default untouched


def test_seg_filter_full_frame_no_letterbox():
    import vicestyle as VS
    s = VS.Seg(kind="dialogue", src="x.mp4", start=0, beats=1, gamma=1.0)
    f = F.stark_seg_filter(s, 1080, 1350)
    assert "drawbox" not in f and "crop=1080:1350" in f
    s2 = VS.Seg(kind="montage", src="x.mp4", start=0, beats=1, gamma=1.0)
    assert F.GRADE_WARM in F.stark_seg_filter(s2, 1080, 1350)


def test_demo_plan_is_valid():
    d = json.loads((Path(__file__).resolve().parents[1] / "plans" / "stark_daenerys.json").read_text())
    spec = F.Spec(**{k: v for k, v in d.items() if not k.startswith("_")})
    assert spec.min_height == 1080 and spec.loops == 2 and len(spec.build) >= 1


def test_logo_asset_present():
    assert F.LOGO_ASSET.exists() and F.LOGO_ASSET.stat().st_size > 100_000

