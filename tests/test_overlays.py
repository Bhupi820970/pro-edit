#!/usr/bin/env python3
"""OCR + persistent-corner-logo + trailer head/tail rejection."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.overlays import (  # noqa: E402
    head_tail_bad, head_tail_margin, has_burned_text, has_persistent_corner_logo,
    ocr_available, sample_indices, shot_overlay_bad,
)


def _font(size: int):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        str(HERE / "fonts" / "Anton-Regular.ttf"),
    ):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _moving_block(n=3, w=320, h=180):
    frames = []
    for i in range(n):
        f = np.zeros((h, w, 3), np.uint8)
        f[:, :] = (20, 70, 30)
        x = 30 + i * 50
        f[40:130, x:x + 70] = (200, 40, 40)
        frames.append(f)
    return frames


def _with_logo(frames):
    out = [f.copy() for f in frames]
    for f in out:
        f[8:38, -46:-8] = (0, 210, 255)
        f[12:16, -42:-12] = (255, 255, 255)
        f[10:36, -30:-26] = (255, 255, 255)
    return out


def _credit_frames():
    frames = []
    font = _font(42)
    for _ in range(3):
        im = Image.new("RGB", (640, 360), (8, 8, 8))
        d = ImageDraw.Draw(im)
        d.text((70, 150), "UNIVERSAL PICTURES", fill=(255, 255, 255), font=font)
        frames.append(np.array(im))
    return frames


class HeadTail(unittest.TestCase):
    def test_eight_seconds_on_a_two_minute_trailer(self):
        self.assertEqual(head_tail_margin(120), 8.0)
        self.assertEqual(head_tail_bad(1.0, 6.0, 120), "clip head/tail (ratings card, logos, end slate)")
        self.assertEqual(head_tail_bad(114.0, 119.0, 120), "clip head/tail (ratings card, logos, end slate)")
        self.assertEqual(head_tail_bad(10.0, 14.0, 120), "")

    def test_short_clip_shrinks_margin(self):
        # 25s clip: 12% = 3s, floored at 2.5
        self.assertAlmostEqual(head_tail_margin(25), 3.0)
        self.assertEqual(head_tail_bad(4.0, 20.0, 25), "")


class CornerLogo(unittest.TestCase):
    def test_clean_motion_is_accepted(self):
        self.assertFalse(has_persistent_corner_logo(_moving_block()))

    def test_static_top_right_bug_is_rejected(self):
        self.assertTrue(has_persistent_corner_logo(_with_logo(_moving_block())))

    def test_netflix_bug_in_corner_is_rejected(self):
        font = _font(22)
        frames = []
        for i in range(3):
            im = Image.fromarray(_moving_block(n=3)[i]).resize((640, 360))
            d = ImageDraw.Draw(im)
            d.text((520, 12), "NETFLIX", fill=(255, 255, 255), font=font)
            frames.append(np.array(im))
        self.assertTrue(has_persistent_corner_logo(frames))

    def test_locked_off_textured_corner_is_not_a_logo(self):

        rng = np.random.default_rng(0)
        f = np.zeros((180, 320, 3), np.uint8)
        f[:, :] = (30, 80, 40)
        f[0:36, -50:] = rng.integers(20, 200, (36, 50, 3), dtype=np.uint8)
        frames = [f.copy() for _ in range(3)]
        self.assertFalse(has_persistent_corner_logo(frames))

    def test_shot_overlay_bad_prefers_logo_reason(self):

        self.assertEqual(shot_overlay_bad(_with_logo(_moving_block())), "persistent corner logo")


class BurnedText(unittest.TestCase):
    def test_credit_card_is_rejected(self):
        self.assertTrue(has_burned_text(_credit_frames()))
        self.assertEqual(shot_overlay_bad(_credit_frames()), "burned-in text/credits")

    def test_clean_action_has_no_text(self):
        self.assertFalse(has_burned_text(_moving_block()))

    @unittest.skipUnless(ocr_available(), "tesseract not installed")
    def test_ocr_reads_studio_name(self):
        from pipeline_lib.overlays import ocr_text, CREDIT_RE
        txt = ocr_text(_credit_frames()[0])
        self.assertTrue(CREDIT_RE.search(txt) or "UNIVERSAL" in txt.upper(), txt)


class TitleCardPhrases(unittest.TestCase):
    def test_caps_phrase_and_not_crumbs(self):
        from pipeline_lib.overlays import _title_card_phrases
        self.assertIn("JUMANJI OPEN WORLD", _title_card_phrases("now JUMANJI OPEN WORLD starring"))
        self.assertFalse(_title_card_phrases("the a to of"))  # OCR junk
        self.assertFalse(_title_card_phrases("OPEN"))  # single word sign


class Samples(unittest.TestCase):

    def test_three_spread_indexes(self):
        self.assertEqual(sample_indices(10, 40, 3), [10 + int(29 * 0.2), 10 + int(29 * 0.5), 10 + int(29 * 0.8)])


if __name__ == "__main__":
    unittest.main()

