#!/usr/bin/env python3
"""Edits must be 28-50s: a short beat-cut plan adds more box clips / extends the timeline BEFORE render,
and a plan that still can't reach the window is skipped without rendering. Every attempt logs step timings."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _segs(n_beats):
    return [SimpleNamespace(beats=n_beats, kind="montage")]


class PlanLengthGate(unittest.TestCase):
    def _run(self, plans):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"VICE_PIPELINE_HOME": td}):
            clip = Path(td) / "a.mkv"
            clip.write_bytes(b"x")
            song = Path(td) / "s.wav"
            song.write_bytes(b"x")
            c1 = {"id": "a", "src_id": "a", "path": str(clip), "has_dialogue": True, "bad_ranges": []}
            c2 = {"id": "b", "src_id": "b", "path": str(clip), "has_dialogue": False, "bad_ranges": []}
            src = MagicMock()
            src.gather.side_effect = [[c1, dict(c1, id="a2")], [c1, dict(c1, id="a2"), c2]]
            state = MagicMock()
            state.pick_pattern.return_value = "P01"
            state.avoid_windows.return_value = []
            calls = []

            def fake_plan(*a, **k):
                calls.append(k.get("min_seconds"))
                return plans[len(calls) - 1], None
            render = MagicMock(side_effect=RuntimeError("render reached"))
            with patch.object(P.V, "auto_plan", side_effect=fake_plan), \
                    patch.object(P.V, "detect_beats", return_value={"period": 0.5, "beats": []}), \
                    patch.object(P.V, "render", render), \
                    patch("pipeline_lib.songs.pick_song", return_value={"title": "t", "artist": "x", "url": "",
                                                                        "path": str(song)}):
                timer = P.StepTimer()
                try:
                    P.produce_one("youtube", "gta", state, MagicMock(), src, 1, timer)
                    err = None
                except Exception as ex:
                    err = ex
            return err, calls, src, render, timer.done()

    def test_short_plan_gets_more_clips_then_extended_before_render(self):
        import pipeline as P
        err, calls, src, render, tm = self._run([_segs(30), _segs(76)])   # 15.6s -> 38.6s
        self.assertEqual(calls, [None, 29.5])                     # 2nd plan asks for the 28-50 window
        self.assertEqual(src.gather.call_args_list[1].kwargs.get("chunks_per_source"), 4)
        self.assertTrue(render.called)                             # only now does it render
        self.assertIn("render reached", str(err))
        self.assertNotIsInstance(err, P.ShortPlan)
        for k in ("gather", "song", "scan_plan", "more_clips", "render", "total"):
            self.assertIn(k, tm)

    def test_hopeless_plan_skips_before_render(self):
        import pipeline as P
        err, calls, src, render, tm = self._run([_segs(20), _segs(30)])
        self.assertIsInstance(err, P.ShortPlan)
        self.assertFalse(render.called)

    def test_good_plan_renders_without_extension(self):
        err, calls, src, render, tm = self._run([_segs(76)])
        self.assertEqual(calls, [None])
        self.assertEqual(src.gather.call_count, 1)
        self.assertTrue(render.called)

    def test_attempt_cap_raises_timeout(self):
        import time
        import pipeline as P
        P._arm_budget(0.5 / 60, "test")   # 0.5 s
        with self.assertRaises(P.AttemptTimeout):
            time.sleep(3)
        P._disarm_budget()


if __name__ == "__main__":
    unittest.main()
