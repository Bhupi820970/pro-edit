#!/usr/bin/env python3
"""Genre song stock: tags, seeds, refill planning, one genre-matched song per video with cooldown."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib import songs as S  # noqa: E402
from pipeline_lib import song_stock as ST  # noqa: E402

CFG = json.loads((HERE / "config" / "pipeline.json").read_text())
SONGS = json.loads((HERE / "config" / "songs.json").read_text())


class Tags(unittest.TestCase):
    def test_every_song_tagged_with_known_genre(self):
        for s in SONGS["songs"]:
            self.assertTrue(S.clean_genres(s.get("genres")), s["title"])
        self.assertEqual(set(S.GENRES), set(CFG["categories"]))

    def test_seeds_cover_every_genre_and_are_safe(self):
        for g in S.GENRES:
            seeds = SONGS["seeds"][g]
            self.assertGreaterEqual(len(seeds), 15, g)
            for x in seeds:
                self.assertTrue(S.safe_search_term(x["title"]) and S.safe_search_term(x["artist"]), x)

    def test_manifest_records_get_genres_from_catalogue(self):
        idx = S.genre_index(CFG)
        self.assertIn("romance", S.genres_of({"title": "Die With A Smile", "artist": "Lady Gaga"}, idx))
        self.assertEqual(S.genres_of({"title": "X", "artist": "Y", "genres": ["sports", "bogus"]}, idx), ["sports"])


class Refill(unittest.TestCase):
    def test_plan_refills_only_low_genres_to_target(self):
        man = {"songs": [{"title": f"t{i}", "artist": "a", "genres": ["gta"]} for i in range(20)]
                        + [{"title": "r1", "artist": "a", "genres": ["romance"]}]}
        need = ST.plan_refill(man, {}, {"stock": {"target": 30, "refill_at": 8}})
        self.assertNotIn("gta", need)
        self.assertEqual(need["romance"], 29)
        self.assertEqual(need["sports"], 30)

    def test_vet_drops_gemini_rejections(self):
        class G:
            def generate(self, *a, **k):
                return [{"i": 0, "fits": True}, {"i": 1, "fits": False}]
        kept = ST.vet(G(), "romance", [{"title": "Perfect", "artist": "Ed Sheeran"},
                                       {"title": "Till I Collapse", "artist": "Eminem"}])
        self.assertEqual([k["title"] for k in kept], ["Perfect"])

    def test_suggest_drops_unsafe_terms(self):
        class G:
            def generate(self, *a, **k):
                return [{"title": "--exec rm", "artist": "x"}, {"title": "Kesariya", "artist": "Arijit Singh"}]
        self.assertEqual([o["title"] for o in ST.suggest(G(), "romance", [], 5)], ["Kesariya"])


class FakeState:
    def __init__(self, recent):
        self.r = recent

    def recent_songs(self, days):
        return list(self.r)


class Pick(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sd = self.tmp / "songs"
        sd.mkdir()
        recs = []
        for i, (t, a, g) in enumerate([("Love A", "Art", "romance"), ("Love B", "Art", "romance"),
                                       ("Hype A", "Art", "sports")]):
            (sd / f"id{i}.wav").write_bytes(b"0" * 2000)
            recs.append({"id": f"id{i}", "file": f"id{i}.wav", "title": t, "artist": a, "genres": [g]})
        (sd / "manifest.json").write_text(json.dumps({"songs": recs}))
        self.p = patch.object(S, "home", lambda: self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_only_genre_songs_and_cooldown(self):
        st = FakeState(["Love A - Art"])
        for _ in range(5):
            r = S.pick_song(None, st, "romance", [], CFG, set())
            self.assertEqual(r["title"], "Love B")

    def test_gemini_new_song_is_fetched_and_added(self):
        class G:
            def generate(self, *a, **k):
                return [{"title": "Kesariya", "artist": "Arijit Singh", "why": "fits"}]
        def fake_dl(o, sdir, genres, by, extra=None):
            (sdir / "newid.wav").write_bytes(b"0" * 2000)
            return {"id": "newid", "file": "newid.wav", "url": "https://www.youtube.com/watch?v=newid000000",
                    "title": o["title"], "artist": o["artist"], "genres": genres, "by": by}
        with patch.object(S, "download_official", fake_dl):
            r = S.pick_song(G(), FakeState([]), "romance", [], CFG, set())
        self.assertEqual(r["title"], "Kesariya")
        new = json.loads((self.tmp / "songs" / "new_songs.json").read_text())["songs"]
        self.assertEqual(new[0]["genres"], ["romance"])


class Dialogue(unittest.TestCase):
    def test_low_confidence_or_music_bed_is_not_dialogue(self):
        import numpy as np
        import vicestyle as V
        ok, _ = V.speech_not_music({"src": "x", "s": 0, "e": 4, "p": 0.3, "nw": 10}, np.zeros(64000))
        self.assertFalse(ok)
        sr = 16000
        a = np.random.default_rng(0).normal(0, 0.3, 4 * sr)          # loud all the time = music bed
        win = {"src": "x", "s": 0, "e": 4, "p": 0.9, "nw": 8, "spans": [(0.2, 0.8), (2.0, 2.6)]}
        self.assertFalse(V.speech_not_music(win, a)[0])
        a2 = a.copy()
        a2[int(0.8 * sr):int(2.0 * sr)] *= 0.05
        a2[int(2.6 * sr):] *= 0.05
        a2[:int(0.2 * sr)] *= 0.05
        self.assertTrue(V.speech_not_music(win, a2)[0])

    def test_duck_is_not_mute(self):
        import inspect
        import vicestyle as V
        d = inspect.signature(V.mix_audio).parameters["duck"].default
        self.assertTrue(0.25 <= d <= 0.30)


if __name__ == "__main__":
    unittest.main()
