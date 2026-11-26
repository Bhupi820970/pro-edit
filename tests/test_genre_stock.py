import json
import unittest
from pathlib import Path

import box_fetch as B
from pipeline_lib import songs
from pipeline_lib.titles import SINGLE_WORK_CATEGORIES

ROOT = Path(__file__).resolve().parents[1]


class PoolRule(unittest.TestCase):
    T = {"gta": 12, "marvel": 9, "comedy": 9}
    TOT = {"target": 25, "refill_below": 10}

    def test_refill_starts_below_threshold_and_fills_to_targets(self):
        plan, on = B.pool_refill_plan({"gta": 5, "marvel": 2, "comedy": 2}, self.T, False, self.TOT)
        self.assertTrue(on)
        self.assertEqual(plan, {"gta": 7, "marvel": 7, "comedy": 7})

    def test_between_thresholds_only_empty_genres(self):
        plan, on = B.pool_refill_plan({"gta": 12, "marvel": 9, "comedy": 0}, self.T, False, self.TOT)
        self.assertFalse(on)
        self.assertEqual(plan, {"comedy": 3})

    def test_refill_stops_at_target(self):
        plan, on = B.pool_refill_plan({"gta": 12, "marvel": 9, "comedy": 9}, self.T, True, self.TOT)
        self.assertFalse(on)
        self.assertEqual(plan, {})

    def test_refill_continues_until_target(self):
        plan, on = B.pool_refill_plan({"gta": 12, "marvel": 5, "comedy": 4}, self.T, True, self.TOT)
        self.assertTrue(on)
        self.assertEqual(plan, {"marvel": 4, "comedy": 5})


class AssetsAndRes(unittest.TestCase):
    def test_cleanup_only_dead_oldest_first(self):
        man = {"clips": [{"id": "a", "file": "a.mkv", "added": "2026-01-02"},
                         {"id": "b", "file": "b.mkv", "added": "2026-01-01"},
                         {"id": "c", "file": "c.mkv", "added": "2026-01-01"}]}
        assets = {f"x{i}" for i in range(9)} | {"a.mkv", "b.mkv", "c.mkv"}
        out = B.assets_to_clean(man, assets, {"a", "b"}, limit_at=10, keep_below=10)
        self.assertEqual(out, [("b", "b.mkv"), ("a", "a.mkv")])        # c is usable: never deleted
        self.assertEqual(B.assets_to_clean(man, assets, {"a"}, limit_at=50, keep_below=40), [])

    def test_720p_rule(self):
        self.assertTrue(B.meets_min_res((1280, 720)))
        self.assertTrue(B.meets_min_res((1080, 1920)))
        self.assertFalse(B.meets_min_res((854, 480)))
        self.assertFalse(B.meets_min_res(None))


class Songs(unittest.TestCase):
    def test_every_genre_has_30_original_seeds_with_mood(self):
        cfg = json.loads((ROOT / "config" / "songs.json").read_text())
        pipe = json.loads((ROOT / "config" / "pipeline.json").read_text())
        for g in songs.GENRES:
            seeds = cfg["seeds"][g]
            self.assertGreaterEqual(len(seeds), 30, g)
            keys = {(s["title"].lower(), s["artist"].lower()) for s in seeds}
            self.assertEqual(len(keys), len(seeds), f"repeat in {g}")
            for s in seeds:
                self.assertTrue(s.get("mood"))
                self.assertNotRegex(s["title"].lower(), r"cover|tribute|nightcore|sped up|remix|\(live\)")
            self.assertIn(g, pipe["categories"])
        for g in ("marvel", "dc", "anime", "celebrity", "comedy"):
            self.assertIn(g, SINGLE_WORK_CATEGORIES)
            self.assertIn(g, B.SIBLING_CATS)
            self.assertTrue(pipe["categories"][g]["channels"])

    def test_song_fit_prefers_matching_mood_and_bpm(self):
        cfg = {"categories": {"romance": {"mood": ["love"]}, "comedy": {"mood": ["fun"]}}}
        love = {"mood": "love", "bpm": 80}
        hype = {"mood": "high-energy", "bpm": 150}
        self.assertGreater(songs.song_fit_score(love, "romance", cfg), songs.song_fit_score(hype, "romance", cfg))
        fun = {"mood": "fun", "bpm": 118}
        self.assertGreater(songs.song_fit_score(fun, "comedy", cfg), songs.song_fit_score(love, "comedy", cfg))
        # clip mood words pull an action edit toward high-energy
        self.assertGreater(songs.song_fit_score(hype, "series", {"categories": {"series": {"mood": []}}}, "intense chase"),
                           songs.song_fit_score(love, "series", {"categories": {"series": {"mood": []}}}, "intense chase"))


if __name__ == "__main__":
    unittest.main()


class GamingAndCaptions(unittest.TestCase):
    def test_gaming_allows_official_trailers_not_teasers(self):
        from pipeline_lib.titles import trailer_blocked
        self.assertFalse(trailer_blocked("Elden Ring - Official Gameplay Reveal Trailer", "gaming"))
        self.assertFalse(trailer_blocked("Cyberpunk 2077 — Official Trailer", "gaming"))
        self.assertTrue(trailer_blocked("Fortnite Chapter 6 Teaser", "gaming"))
        self.assertTrue(trailer_blocked("GTA 6 Trailer Reaction", "gaming"))
        self.assertTrue(trailer_blocked("Heat (1995) Official Trailer", "movie"))
        self.assertTrue(B.usable_for("gaming", "God of War Ragnarok - Official Gameplay Reveal"))
        self.assertFalse(B.usable_for("gaming", "Grand Theft Auto VI Trailer 2"))
        cfg = json.loads((ROOT / "config" / "pipeline.json").read_text())
        g = cfg["categories"]["gaming"]
        self.assertEqual(len(g["top_games"]), 10)
        self.assertTrue(all("gta" in ch["skip_re"].lower() for ch in g["channels"]))
        self.assertIn("gaming", SINGLE_WORK_CATEGORIES)

    def test_caption_rows_detected_plain_numpy(self):
        import numpy as np
        from pipeline_lib import overlay_check as O
        rng = np.random.default_rng(1)

        def frame(caption: bool, seed: int):
            f = rng.uniform(60, 150, (O.H, O.W)).astype(np.float32)
            f[100 + seed * 7:140 + seed * 7, 200 + seed * 13:300 + seed * 13] = 230     # moving bright object
            if caption:
                y = 495
                for i in range(30):                                                 # glyph strokes + outline
                    x = 300 + i * 12
                    f[y - 2:y + 18, x - 2:x + 6] = 20
                    f[y:y + 16, x:x + 4] = 245
            return f
        clean = O.caption_scores([frame(False, i) for i in range(20)])
        subs = O.caption_scores([frame(i % 4 != 0, i) for i in range(20)])
        self.assertLess(clean["share"], O.CAPTION_SHARE)
        self.assertGreaterEqual(subs["share"], O.CAPTION_SHARE)
        self.assertGreaterEqual(subs["text_frames"], O.CAPTION_MIN_FRAMES)
