#!/usr/bin/env python3
"""Approved glass-text logo: one mark, bottom-center, blurs the live frame under the letters."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import vicestyle as V  # noqa: E402


class GlassLogo(unittest.TestCase):
    def setUp(self):
        self.font = V.font_path()
        self.W, self.H = 2560, 1440
        self.text = "VICE CARTEL"
        self.box = V.glass_logo_box(self.text, self.W, self.H, self.font)

    def test_font_is_about_3_4_percent_of_height(self):
        x, y, bw, bh, f, bb, pad = self.box
        # requested size is H*0.034; glyph box is a bit smaller, padded by WM_PAD
        self.assertEqual(f.size, int(self.H * V.WM_HEIGHT))
        self.assertAlmostEqual(V.WM_HEIGHT, 0.034, places=3)
        self.assertGreater(bh, int(self.H * 0.034))
        self.assertLess(bh, int(self.H * 0.08))

    def test_bottom_center_at_frac_0_14_not_top_right(self):
        x, y, bw, bh, *_ = self.box
        self.assertAlmostEqual(x + bw / 2, self.W / 2, delta=2)
        self.assertEqual(self.H - (y + bh), int(self.H * V.WM_BOTTOM))
        self.assertEqual(V.WM_BOTTOM, 0.14)
        self.assertGreater(y, self.H * 0.7)
        # must not sit in a top-right bug slot
        self.assertLess(x, self.W * 0.5)
        self.assertGreater(y, self.H * 0.5)
        pos = V.watermark_pos(self.text, self.W, self.H, self.font)
        self.assertEqual(pos, (x, y))

    def test_blurs_the_actual_frame_under_the_letters(self):
        def frame(color):
            im = Image.new("RGB", (self.W, self.H), color)
            return V.apply_glass_logo(im, self.text, self.font)

        red = frame((180, 20, 20))
        blue = frame((20, 20, 180))
        x, y, bw, bh, *_ = self.box
        # sample the centre of the logo box — should carry the underlying colour
        rx, gx, bx, *_ = red.getpixel((x + bw // 2, y + bh // 2))
        ry, gy, by, *_ = blue.getpixel((x + bw // 2, y + bh // 2))
        self.assertGreater(rx, bx)
        self.assertGreater(by, ry)
        self.assertNotEqual(red.getpixel((x + bw // 2, y + bh // 2)),
                            blue.getpixel((x + bw // 2, y + bh // 2)))
        # a region far from the logo is unchanged (single logo only)
        self.assertEqual(red.getpixel((40, 40)), (180, 20, 20, 255))
        self.assertEqual(red.getpixel((self.W - 40, 40)), (180, 20, 20, 255))

    def test_hold_beats_adds_about_one_and_a_half_seconds(self):
        # 125 BPM ~ 0.48s/beat: +1.5s rounds to 3 beats
        self.assertEqual(V.hold_beats(0.48, 1), 4)
        self.assertEqual(V.hold_beats(0.48, 2), 5)
        # 85 BPM ~ 0.70s/beat: +1.5s rounds to 2 beats
        self.assertEqual(V.hold_beats(0.70, 1), 3)
        self.assertEqual(V.hold_beats(0.70, 2), 4)
        # never shorter than +2 beats
        self.assertEqual(V.hold_beats(2.0, 1), 3)


if __name__ == "__main__":
    unittest.main()

