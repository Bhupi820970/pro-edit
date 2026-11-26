#!/usr/bin/env python3
"""Box fetcher: 2-3 extra official YouTube videos of the same title, grouped by "work"; disk guard; prune."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import box_fetch as B  # noqa: E402

_CLEAN = patch("pipeline_lib.overlay_check.check_clip", return_value=(True, "clean", {}))


def _e(i, title, ch="Movieclips", verified=True, dur=180):
    vid = f"{i:011d}"[-11:]
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": title, "channel": ch,
            "verified": verified, "duration": dur}


class TitleGroups(unittest.TestCase):
    def setUp(self):
        p = patch.object(B, "video_size", return_value=(1920, 1080))   # fake files: treat as 1080p
        p.start()
        self.addCleanup(p.stop)

    def _box(self, td):
        a = SimpleNamespace(repo="x/y", dir=Path(td), cookies_from_browser=None, no_gemini=True, dry_run=True)
        return B.Box(a)

    def _fake_ytdlp(self, td):
        def f(args, timeout=1800):
            out = args[args.index("-o") + 1]
            Path(out.replace("%(ext)s", "mkv")).write_bytes(b"x")
            return True, ""
        return f

    def test_siblings_of_same_movie_are_fetched_and_grouped(self):
        with tempfile.TemporaryDirectory() as td:
            box = self._box(td)
            main = _e(1, "Heat (1995) - Bank Robbery Scene | Movieclips")
            sibs = [_e(2, "Heat (1995) - Coffee Shop Scene | Movieclips"),
                    _e(3, "Heat (1995) - Final Chase Scene | Movieclips"),
                    _e(4, "Heat (1995) - Official Trailer"),              # trailer: never
                    _e(5, "Heat (1995) - Street scene", ch="RandomFan", verified=False),  # not official
                    _e(6, "Collateral - Club Scene | Movieclips"),       # other title
                    _e(7, "Heat (1995) - Airport Scene | Movieclips")]
            work = B.title_key("movie", main["title"])
            self.assertTrue(work)

            def search(q, n=15):
                return [main] if "Heat" not in q else [s for s in sibs if True]
            with _CLEAN, patch.object(B, "CFG", {**B.CFG}), \
                    patch.object(B.S, "list_channel", return_value=[]), \
                    patch.object(B.S, "search_youtube", side_effect=search), \
                    patch.object(B.S, "ytdlp", side_effect=self._fake_ytdlp(td)), \
                    patch.object(B.Box, "manifest", return_value={"clips": []}), \
                    patch.object(B, "free_gb", return_value=50.0):
                recs = box.fetch("movie", 10, use_gemini=False)
            ids = [r["id"] for r in recs]
            self.assertEqual(ids[0], main["id"])
            self.assertEqual(set(ids[1:]), {sibs[0]["id"], sibs[1]["id"]})  # 2 extra -> 3 clips of ONE title
            self.assertTrue(all(r.get("work") == work for r in recs))
            self.assertTrue(all(r["url"].startswith("https://www.youtube.com/") for r in recs))
            self.assertTrue(all(r["genre"] == "movie" and r["resolution"] == "1920x1080" and r["vetted"] is False
                                and "has_dialogue" in r for r in recs))
            self.assertFalse(list(Path(td).glob("*.mkv")))                 # freed after each batch

    def test_overlay_reject_at_download_deletes_and_takes_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            box = self._box(td)
            bad = _e(1, "Heat (1995) - Bank Robbery Scene | Movieclips")
            good = _e(2, "Collateral (2004) - Club Scene | Movieclips")

            def chk(path):
                return (False, "static overlay: bottom_right=0.05", {}) if bad["id"] in str(path) else \
                    (True, "clean", {})
            with patch("pipeline_lib.overlay_check.check_clip", side_effect=chk), \
                    patch.dict(os.environ, {"BOX_FETCH_SIBLINGS": "0"}), \
                    patch.object(B.S, "list_channel", return_value=[]), \
                    patch.object(B.S, "search_youtube", return_value=[bad, good]), \
                    patch.object(B.S, "ytdlp", side_effect=self._fake_ytdlp(td)), \
                    patch.object(B.Box, "manifest", return_value={"clips": []}), \
                    patch.object(B, "free_gb", return_value=50.0):
                recs = box.fetch("movie", 1, use_gemini=False)
            self.assertEqual([r["id"] for r in recs], [good["id"]])        # replacement keeps the target
            self.assertFalse((Path(td) / f"{bad['id']}.mkv").exists())     # bad clip deleted
            self.assertIn(bad["id"], box.box_rejects)
            box2 = self._box(td)                                           # remembered: never retried
            self.assertIn(bad["id"], box2.box_rejects)

    def test_disk_guard_stops_downloads(self):
        with tempfile.TemporaryDirectory() as td:
            box = self._box(td)
            with patch.object(B.S, "list_channel", return_value=[]), \
                    patch.object(B.S, "search_youtube", return_value=[_e(1, "Heat (1995) - Scene | Movieclips")]), \
                    patch.object(B.S, "ytdlp", side_effect=self._fake_ytdlp(td)) as y, \
                    patch.object(B.Box, "manifest", return_value={"clips": []}), \
                    patch.object(B, "free_gb", return_value=3.0):
                recs = box.fetch("movie", 10, use_gemini=False)
            self.assertEqual(recs, [])
            self.assertFalse(y.called)
            self.assertTrue(box.disk_low)

    def test_prune_only_own_old_media(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            old = d / "a.mkv"; old.write_bytes(b"x")
            new = d / "b.mkv"; new.write_bytes(b"x")
            keep = d / "manifest.json"; keep.write_text("{}")
            sub = d / "sub"; sub.mkdir(); (sub / "c.mkv").write_bytes(b"x")
            t = time.time() - 7 * 3600
            os.utime(old, (t, t)); os.utime(keep, (t, t)); os.utime(sub / "c.mkv", (t, t))
            self.assertEqual(B.prune_own_media(d, 6 * 3600), 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists() and keep.exists() and (sub / "c.mkv").exists())


if __name__ == "__main__":
    unittest.main()
