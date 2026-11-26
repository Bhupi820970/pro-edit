#!/usr/bin/env python3
"""Plain-script watermark / logo detector: synthetic clips with and without a burned-in corner logo."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.overlay_check import check_clip  # noqa: E402


def _clip(out: Path, logo: bool, letterbox: bool = False):
    # moving, changing footage: random-noise-ish test source with hue rotation and scene changes
    vf = "scale=960:540"
    if letterbox:
        vf += ",scale=960:400,pad=960:540:0:70"
    if logo:
        vf += ",drawbox=x=850:y=480:w=90:h=40:color=white@0.85:t=fill,drawbox=x=862:y=490:w=66:h=20:color=black@0.9:t=fill"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=480x270:r=25,geq=lum='128+90*sin(X/9+T*5)*cos(Y/7-T*3)+30*random(1)':cb=128:cr=128,scale=960:540", "-t", "30", "-vf", vf,
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed")
class OverlayCheck(unittest.TestCase):
    def test_corner_logo_rejected_clean_and_letterbox_pass(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, c = Path(td) / "logo.mp4", Path(td) / "clean.mp4", Path(td) / "lb.mp4"
            _clip(a, True)
            _clip(b, False)
            _clip(c, False, letterbox=True)
            ok_a, why_a, _ = check_clip(a)
            ok_b, why_b, d_b = check_clip(b)
            ok_c, why_c, d_c = check_clip(c)
        self.assertFalse(ok_a, why_a)
        self.assertIn("bottom_right", why_a)
        self.assertTrue(ok_b, (why_b, d_b))
        self.assertTrue(ok_c, (why_c, d_c))

    def test_unreadable_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.mkv"
            f.write_bytes(b"x")
            ok, why, _ = check_clip(f)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
