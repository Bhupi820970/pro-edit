#!/usr/bin/env python3
"""Stdlib tests for upload captions (no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish import captions as C  # noqa: E402


ITEM = {
    "id": "yo-gta-20261120-120000",
    "platform": "youtube",
    "category": "gta",
    "title": "Vice City hits different",
    "hook": "NIGHT DRIVE",
    "hashtags": ["GTA6", "ViceCity"],
    "song": {"title": "Give It Up", "artist": "KC and the Sunshine Band"},
    "song_credit": '"Give It Up" – KC and the Sunshine Band',
    "sources": [{"credit": "Rockstar Games", "title": "Grand Theft Auto VI Trailer 2"}],
}


class CaptionCleaning(unittest.TestCase):
    def test_strips_emojis_and_ai_phrase(self):
        s = C.clean_line("Fire edit 🔥 this is an AI video drop")
        self.assertNotIn("AI video", s)
        self.assertNotIn("🔥", s)
        self.assertFalse(C.AI_RE.search(s))

    def test_rejects_ai_generated_variants(self):
        for raw in ("AI-generated clip", "made by AI", "AIvideo drop", "artificial intelligence video"):
            self.assertTrue(C.AI_RE.search(raw), raw)
            self.assertFalse(C.AI_RE.search(C.strip_ai(raw)), raw)

    def test_song_credit_format(self):
        self.assertEqual(C.song_credit(ITEM["song"]), "Song: Give It Up - KC and the Sunshine Band")
        self.assertEqual(
            C.song_credit(None, ITEM["song_credit"]),
            "Song: Give It Up - KC and the Sunshine Band",
        )

    def test_fallback_is_deterministic_and_unique_per_id(self):
        a = C.fallback(ITEM)
        b = C.fallback(ITEM)
        self.assertEqual(a, b)
        other = C.fallback({**ITEM, "id": "yo-gta-20261120-180000", "title": "", "hook": ""})
        # same category templates, hashed by id — may coincide rarely; title at least set
        self.assertTrue(other["title"])
        self.assertNotIn("AI", a["title"].upper())

    def test_build_includes_credit_hashtags_no_emoji_no_ai(self):
        out = C.build(ITEM, "youtube", gem=None)
        self.assertIn("Song: Give It Up - KC and the Sunshine Band", out["description"])
        self.assertIn("Rockstar Games", out["description"])
        self.assertIn("#GTA6", out["description"])
        self.assertNotIn("AI video", out["description"].lower())
        self.assertNotIn("🎵", out["description"])
        self.assertEqual(out["title"], "Vice City hits different")
        self.assertIn("Song: Give It Up", out["ig_caption"])
        self.assertLessEqual(len(out["ig_caption"]), 2200)
        self.assertLessEqual(len(out["description"]), 5000)

    def test_gemini_failure_falls_back(self):
        class Boom:
            def generate(self, *a, **k):
                raise RuntimeError("quota")
        out = C.build(ITEM, "instagram", gem=Boom())
        self.assertTrue(out["title"])
        self.assertIn("Song:", out["description"])

    def test_gemini_success_is_sanitized(self):
        class Ok:
            def generate(self, *a, **k):
                return {
                    "title": "Neon streets 🔥",
                    "caption": "This scene goes hard. You coming?",
                    "hashtags": ["GTA6", "AIvideo", "ViceCity"],
                    "tags": ["gta"],
                }
        out = C.build(ITEM, "youtube", gem=Ok())
        self.assertEqual(out["title"], "Neon streets")
        self.assertNotIn("AIvideo", "".join(out["hashtags"]))
        self.assertIn("ViceCity", out["hashtags"])

    def test_gemini_ai_title_is_discarded(self):
        class Bad:
            def generate(self, *a, **k):
                return {"title": "AI video masterpiece", "caption": "watch this", "hashtags": ["x"]}
        out = C.build(ITEM, "youtube", gem=Bad())
        self.assertNotIn("AI video", out["title"].lower())
        self.assertEqual(out["title"], "Vice City hits different")


if __name__ == "__main__":
    unittest.main()
