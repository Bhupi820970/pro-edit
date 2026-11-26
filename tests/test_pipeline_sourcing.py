#!/usr/bin/env python3
"""Stdlib tests for anti-repeat ranges, Gemini quota/keyframes/vet cache, attach names, seed, pool."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.edit_ranges import (  # noqa: E402
    source_id, overlap, merge_ranges, overlaps_long, LONG_REUSE, seg_source_end,
)
from pipeline_lib.gemini import next_quota_reset, Gemini, GeminiError  # noqa: E402
from box_fetch import safe_query, POOL_TARGET  # noqa: E402


IST = timezone(timedelta(hours=5, minutes=30))


class RangeTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(source_id("trailer1.mp4"), "QdBZY2fkU-0")
        self.assertEqual(source_id("/x/GTAVI_Trailer_1.mp4"), "QdBZY2fkU-0")
        self.assertEqual(source_id("trailer2.mkv"), "VQRLujxTm3c")
        self.assertEqual(source_id("launch_trailer.mkv"), "rz1B4B2yBZY")
        self.assertEqual(source_id("QdBZY2fkU-0_c01.mkv"), "QdBZY2fkU-0")

    def test_merge_adjacent_slices(self):
        merged = merge_ranges([[59.77, 62.0], [60.77, 64.0], [80.0, 82.0]])
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0][0], 59.77)
        self.assertAlmostEqual(merged[0][1], 64.0)

    def test_short_reuse_ok_long_blocked(self):
        used = [("QdBZY2fkU-0", 10.0, 20.0)]
        self.assertFalse(overlaps_long("QdBZY2fkU-0", 19.0, 20.2, used))  # 1.2s < 1.5
        self.assertTrue(overlaps_long("QdBZY2fkU-0", 18.0, 22.0, used))   # 2s
        self.assertFalse(overlaps_long("other", 10.0, 20.0, used))

    def test_seg_end_from_t0_t1(self):
        self.assertAlmostEqual(seg_source_end({"start": 5, "t0": 1, "t1": 3.5}), 7.5)


class QuotaTests(unittest.TestCase):
    def test_keys_include_5_and_6(self):
        from pipeline_lib.gemini import _keys
        env = {k: v for k, v in os.environ.items() if not k.startswith("GEMINI_API_KEY")}
        env.update({f"GEMINI_API_KEY_{i}": f"dummy{i}" for i in range(1, 7)})
        with patch.dict(os.environ, env, clear=True):
            labels = [lab for lab, _ in _keys()]
        self.assertEqual(labels, [f"key{i}" for i in range(1, 7)])

    def test_reset_is_1330_ist_before_cutoff(self):
        now = datetime(2026, 11, 19, 11, 0, tzinfo=IST)
        nxt = next_quota_reset(now)
        self.assertEqual(nxt.astimezone(IST).strftime("%Y-%m-%d %H:%M"), "2026-11-19 13:30")

    def test_reset_is_next_day_after_cutoff(self):
        now = datetime(2026, 11, 19, 13, 31, tzinfo=IST)
        nxt = next_quota_reset(now)
        self.assertEqual(nxt.astimezone(IST).strftime("%Y-%m-%d %H:%M"), "2026-11-20 13:30")

    def test_mark_parks_key_until_reset(self):
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if not k.startswith("GEMINI_API_KEY")}
            env.update({"VICE_PIPELINE_HOME": td, "GEMINI_API_KEY_1": "k1", "GEMINI_API_KEY_2": "k2"})
            with patch.dict(os.environ, env, clear=True):
                g = Gemini(log=lambda *a: None)
                g._mark("key1", "You exceeded your current quota")
                until = g.dead_until["key1"]
                self.assertGreater(until, next_quota_reset().timestamp() - 2)
                self.assertLessEqual(until, next_quota_reset().timestamp() + 2)
                self.assertFalse(g.quota_exhausted())  # key2 still live
                g._mark("key2", "429 RESOURCE_EXHAUSTED quota")
                self.assertTrue(g.quota_exhausted())
                # persisted across new client, even around IST midnight because we store timestamps
                g2 = Gemini(log=lambda *a: None)
                self.assertTrue(g2.quota_exhausted())
                live = g2._key_order()
                self.assertEqual(live, [])

    def test_per_minute_is_short(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": td, "GEMINI_API_KEY": "k"}, clear=False):
                g = Gemini(log=lambda *a: None, cooldown_s=120)
                g._mark("key", "Resource has been exhausted (Rate Limit) per minute")
                self.assertLess(g.dead_until["key"] - __import__("time").time(), 180)


class SeedAndPoolTests(unittest.TestCase):
    def test_seed_loads_trailer_scenes(self):
        seed = json.loads((HERE / "config" / "used_segments_seed.json").read_text())
        segs = seed["segments"]
        ids = {s["id"] for s in segs}
        self.assertIn("QdBZY2fkU-0", ids)
        self.assertIn("VQRLujxTm3c", ids)
        self.assertGreaterEqual(len(segs), 100)
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": td}):
                from pipeline_lib.state import State
                st = State()
                used = [s for s in st.d["segments_used"] if s["id"] in {"QdBZY2fkU-0", "VQRLujxTm3c"}]
                self.assertGreaterEqual(len(used), 10)
                avoided = st.avoid_windows(days=30)
                keys = {a[0] for a in avoided}
                self.assertIn("QdBZY2fkU-0", keys)

    def test_pool_target_40_and_queries_safe(self):
        self.assertTrue(all(v >= 15 for v in POOL_TARGET.values()))
        cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
        self.assertEqual(cfg["pool_target"]["gta"], 40)
        # GTA title filter includes V / Online, not only VI trailers
        tre = cfg["categories"]["gta"]["channels"][0]["title_re"]
        self.assertIn("GTA Online", tre)
        self.assertIn("GTA V", tre)
        for cat, c in cfg["categories"].items():
            self.assertGreaterEqual(len(c.get("search") or []), 3, cat)
            for q in c["search"]:
                self.assertIsNotNone(safe_query(q), f"unsafe query in {cat}: {q!r}")


class VetCacheAndKeyframes(unittest.TestCase):
    def _clip(self, folder: Path) -> Path:
        out = folder / "clip.mp4"
        # 4s 720p is below 1080 so height reject is easy; make 1080 for keyframe path
        import subprocess
        subprocess.check_call([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1920x1080:d=4",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart", str(out),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out

    def test_keyframes_and_never_vet_twice(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(td), "GEMINI_API_KEY": "k"}):
                from pipeline_lib.state import State
                from pipeline_lib.sourcing import Sourcer
                clip = self._clip(td)
                st = State()
                gem = MagicMock()
                gem.quota_exhausted.return_value = False
                gem.generate.return_value = {
                    "accept": True, "official_quality": True, "watermark": False,
                    "channel_logo": False, "burned_subtitles": False, "hud_overlay": False,
                    "editable": True, "has_dialogue": False, "mood": "test",
                    "summary": "red", "reject_reasons": [], "bad_ranges": [],
                    "characters": [],
                }
                cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
                src = Sourcer(cfg, st, gem)
                e = {"id": "srcTEST01ab", "title": "Official", "channel": "Rockstar Games",
                     "credit": "Rockstar Games", "url": "https://www.youtube.com/watch?v=srcTEST01ab",
                     "via": "official-channel"}
                a = src.vet(e, clip, "gta")
                b = src.vet(e, clip, "gta")
                self.assertEqual(gem.generate.call_count, 1)
                self.assertEqual(a["accept"], True)
                self.assertIs(a, b)
                self.assertEqual(a.get("vet_via"), "keyframes")
                self.assertIn(len(a.get("keyframe_times") or []), (8, 9))   # 8 proxies (+ corner sheet)
                # generate was called with images, not a video file
                kwargs = gem.generate.call_args.kwargs
                self.assertIn("images", kwargs)
                self.assertIn(len(kwargs["images"]), (8, 9))
                self.assertIsNone(kwargs.get("video"))
                # cache persisted
                st2 = State()
                self.assertIn("srcTEST01ab", st2.d["vet"])

    def test_skip_new_vet_when_quota_exhausted(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(td), "GEMINI_API_KEY": "k"}):
                from pipeline_lib.state import State
                from pipeline_lib.sourcing import Sourcer
                clip = self._clip(td)
                st = State()
                gem = MagicMock()
                gem.quota_exhausted.return_value = True
                cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
                src = Sourcer(cfg, st, gem)
                e = {"id": "srcTEST02cd", "title": "X", "channel": "Rockstar Games",
                     "url": "https://www.youtube.com/watch?v=srcTEST02cd", "via": "official-channel"}
                with self.assertRaises(GeminiError):
                    src.vet(e, clip, "gta")
                gem.generate.assert_not_called()
                self.assertNotIn("srcTEST02cd", st.d["vet"])


class AttachTests(unittest.TestCase):
    def test_unique_preview_name(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ready = td / "yo-gta-20261119-104157.mp4"
            prevdir = td / "prev"
            prevdir.mkdir()
            preview = prevdir / "yo-gta-20261119-104157.mp4"
            ready.write_bytes(b"ready")
            preview.write_bytes(b"prev")
            (td / "yo-gta-20261119-104157.mp4.json").write_text("{}")
            uploaded = []

            def fake_run(args, **kw):
                uploaded.append(Path(args[4]).name)
                return MagicMock(returncode=0, stdout="", stderr="")

            import pipeline
            with patch.object(pipeline, "run", fake_run):
                v = {"path": str(ready), "preview": str(preview)}
                pipeline.attach_video_assets("videos-youtube-20261119", "Bhupi820970/pro-edit", v)
            self.assertEqual(uploaded, [
                "yo-gta-20261119-104157.mp4",
                "yo-gta-20261119-104157.preview.mp4",
                "yo-gta-20261119-104157.mp4.json",
            ])
            self.assertTrue(
                (td / "yo-gta-20261119-104157.preview.mp4").exists()
                or (prevdir / "yo-gta-20261119-104157.preview.mp4").exists()
            )



class PreviewVetAndKeyframes(unittest.TestCase):
    def test_vet_prompt_asks_for_four_corners(self):
        from pipeline_lib.sourcing import VET_PROMPT
        self.assertIn("ALL FOUR CORNERS", VET_PROMPT)
        self.assertIn("CLIP KINGS", VET_PROMPT)
        self.assertIn("CLIP EMPIRE", VET_PROMPT)
        self.assertIn("NETFLIX", VET_PROMPT)

    def test_ytimg_url_allowlist(self):
        from pipeline_lib.sourcing import ytimg_url, youtube_video_id, safe_media_url, fetch_ytimg
        vid = "dQw4w9WgXcQ"
        self.assertEqual(ytimg_url(vid, "maxresdefault"),
                         "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg")
        self.assertIsNone(ytimg_url("../evil", "maxresdefault"))
        self.assertIsNone(ytimg_url(vid, "other"))
        self.assertIsNone(ytimg_url("short", "hqdefault"))
        self.assertEqual(youtube_video_id({"id": vid, "url": "https://www.youtube.com/watch?v=" + vid}), vid)
        self.assertIsNone(youtube_video_id({"id": "srcTEST01abcd"}))
        self.assertIsNone(safe_media_url("http://example.com/video.mp4"))
        self.assertIsNone(safe_media_url("https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"))
        self.assertTrue(safe_media_url("https://www.youtube.com/watch?v=" + vid))
        self.assertIsNone(safe_media_url("https://www.youtube.com/watch?v=" + vid + "; rm -rf /"))
        # fetch must refuse non-allowlisted hosts without opening them
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "x.jpg"
            self.assertFalse(fetch_ytimg("https://evil.example/vi/dQw4w9WgXcQ/maxresdefault.jpg", dest))
            self.assertFalse(dest.exists())
            self.assertFalse(fetch_ytimg("https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg?x=1", dest))
            self.assertFalse(fetch_ytimg("https://i.ytimg.com/vi/dQw4w9WgXcQ/../secret.jpg", dest))

    def test_keyframes_are_480p_low_quality_proxies(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(td)}):
                from pipeline_lib.state import State
                from pipeline_lib.sourcing import Sourcer
                cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
                src = Sourcer(cfg, State(), MagicMock())
                calls = []

                def fake_run(cmd, timeout=None, check=True, **kw):
                    calls.append([str(c) for c in cmd])
                    out = Path(cmd[-1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(b"\xff\xd8" + b"x" * 500)
                    return MagicMock(returncode=0, stdout="", stderr="")

                clip = td / "c.mp4"
                clip.write_bytes(b"x")
                import pipeline_lib.sourcing as S
                with patch.object(S, "run", fake_run):
                    frames = src._keyframes(clip, "k", 10.0)
                self.assertGreaterEqual(len(frames), 4)
                joined = " ".join(calls[0])
                self.assertIn("scale=854:-2", joined)
                self.assertIn("-q:v 6", " ".join(calls[0][i] + " " + calls[0][i + 1] for i in range(len(calls[0]) - 1)))

    def test_pre_vet_reject_skips_full_download(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(td), "GEMINI_API_KEY": "k"}):
                from pipeline_lib.state import State
                from pipeline_lib.sourcing import Sourcer
                st = State()
                gem = MagicMock()
                gem.quota_exhausted.return_value = False
                gem.generate.return_value = {
                    "accept": False, "watermark": True, "channel_logo": False,
                    "reject_reasons": ["corner logo"],
                }
                cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
                src = Sourcer(cfg, st, gem)
                thumb = td / "t.jpg"
                thumb.write_bytes(b"\xff\xd8" + b"y" * 3000)
                e = {"id": "dQw4w9WgXcQ", "title": "Clip", "channel": "Fan",
                     "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "via": "youtube-search"}
                with patch.object(src, "_preview_frames", return_value=[thumb]), \
                     patch.object(src, "download") as dl, \
                     patch.object(src, "candidates", return_value=[e]), \
                     patch.object(src, "local_file", return_value=None):
                    out = src.gather("gta", seed=1, want=1, need_dialogue=0)
                    dl.assert_not_called()
                self.assertEqual(out, [])
                self.assertFalse(st.d["vet"]["dQw4w9WgXcQ"]["accept"])
                self.assertEqual(st.d["vet"]["dQw4w9WgXcQ"].get("vet_via"), "preview")
                gem.generate.assert_called_once()
                self.assertIn("images", gem.generate.call_args.kwargs)

    def test_pre_vet_pass_then_post_vet_still_runs(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(td), "GEMINI_API_KEY": "k"}):
                from pipeline_lib.state import State
                from pipeline_lib.sourcing import Sourcer
                clip = VetCacheAndKeyframes()._clip(td)
                st = State()
                gem = MagicMock()
                gem.quota_exhausted.return_value = False
                gem.generate.side_effect = [
                    {"accept": True, "watermark": False, "channel_logo": False, "reject_reasons": []},
                    {"accept": True, "official_quality": True, "watermark": False,
                     "channel_logo": False, "burned_subtitles": False, "hud_overlay": False,
                     "editable": True, "has_dialogue": False, "mood": "test",
                     "summary": "red", "reject_reasons": [], "bad_ranges": [],
                     "characters": []},
                ]
                cfg = json.loads((HERE / "config" / "pipeline.json").read_text())
                src = Sourcer(cfg, st, gem)
                thumb = td / "t.jpg"
                thumb.write_bytes(b"\xff\xd8" + b"y" * 3000)
                e = {"id": "dQw4w9WgXcQ", "title": "Official", "channel": "Rockstar Games",
                     "credit": "Rockstar Games",
                     "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "via": "official-channel"}
                with patch.object(src, "_preview_frames", return_value=[thumb]):
                    self.assertTrue(src.pre_vet(e, "gta"))
                # pre-vet accept is not a cached full vet
                self.assertNotIn("dQw4w9WgXcQ", st.d.get("vet") or {})
                a = src.vet(e, clip, "gta")
                self.assertTrue(a["accept"])
                self.assertEqual(a.get("vet_via"), "keyframes")
                self.assertEqual(gem.generate.call_count, 2)

if __name__ == "__main__":
    unittest.main()

