#!/usr/bin/env python3
"""Sports edits open on spoken words, then montage; other genres keep the hook-first layout."""
from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
import numpy as np
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import vicestyle as V  # noqa: E402


def _shots(src: str, start: float = 24.0, n: int = 24):
    # hook_b scales with target (min 12); provide plenty of body shots
    out = []
    t = start
    for _ in range(n):
        out.append(V.Shot(src, t, t + 4.0, motion=5.0, color=0.5))
        t += 4.2
    return out



_PATCHES = []


def setUpModule():
    # these tests use fake clips: skip the real ffmpeg face / duplicate-footage probes
    _PATCHES.extend([patch.object(V, "is_real_dialogue", return_value=True),
                     patch.object(V, "dedupe_clips", side_effect=lambda fs: fs)])
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in _PATCHES:
        p.stop()

class OpenWithDialogue(unittest.TestCase):
    def _plan(self, open_with_dialogue: bool):
        with tempfile.TemporaryDirectory() as td:
            clips = Path(td)
            interview = clips / "interview.mp4"
            action = clips / "action.mp4"
            interview.write_bytes(b"x")
            action.write_bytes(b"x")
            binfo = {"period": 0.5, "tempo": 120}
            wins = [{"src": str(interview), "s": 12.0, "e": 20.0, "score": 10.0,
                     "text": "I just want to be great"}]

            def fake_scan(path):
                path = Path(path)
                if path.name == "interview.mp4":
                    return _shots(str(path), start=24.0, n=12)
                return _shots(str(path), start=10.0, n=20)

            def fake_dlg(path, shots):
                return wins if Path(path).name == "interview.mp4" else []

            with patch.object(V, "scan_clip", side_effect=fake_scan), \
                 patch.object(V, "dialogue_windows", side_effect=fake_dlg):
                segs, _ = V.auto_plan(interview, clips, 20.0, binfo,
                                      open_with_dialogue=open_with_dialogue)
            return segs

    def test_sports_starts_with_dialogue(self):
        segs = self._plan(True)
        self.assertTrue(segs)
        self.assertEqual(segs[0].kind, "dialogue")
        self.assertIn("montage", {s.kind for s in segs[1:]})

    def test_default_still_starts_with_montage_hook(self):
        segs = self._plan(False)
        self.assertTrue(segs)
        self.assertEqual(segs[0].kind, "montage")



def _plan_with_windows(wins, target=38.0, period=0.5, open_with_dialogue=False):
    with tempfile.TemporaryDirectory() as td:
        clips = Path(td)
        sources = []
        for w in wins:
            sources.append(Path(w["src"]).name)
        sources = list(dict.fromkeys(sources)) + ["action.mp4"]
        for name in sources:
            (clips / name).write_bytes(b"x")

        binfo = {"period": period, "tempo": 120}

        def fake_scan(path):
            path = Path(path)
            start = 40.0 if "dlg" in path.name or "interview" in path.name else 10.0
            n = 28 if path.name == "action.mp4" else 14
            return _shots(str(clips / path.name), start=start, n=n)

        def fake_dlg(path, shots):
            path = Path(path)
            out = []
            for w in wins:
                if Path(w["src"]).name == path.name:
                    ww = dict(w)
                    ww["src"] = str(clips / path.name)
                    out.append(ww)
            return out

        with patch.object(V, "scan_clip", side_effect=fake_scan), patch.object(V, "dialogue_windows", side_effect=fake_dlg):
            segs, _ = V.auto_plan(clips / sources[0], clips, target, binfo,
                                  open_with_dialogue=open_with_dialogue)
        return segs, period


class DialoguePickAndPacing(unittest.TestCase):
    def test_rejects_duplicate_dialogue_text(self):
        wins = [
            {"src": "dlg_a.mp4", "s": 10.0, "e": 15.0, "score": 20.0,
             "text": "Lying to yourself is the only way you will ever be happy"},
            {"src": "dlg_b.mp4", "s": 8.0, "e": 13.0, "score": 19.0,
             "text": "Lying to yourself is the only way you will be happy"},
            {"src": "dlg_c.mp4", "s": 6.0, "e": 10.5, "score": 8.0,
             "text": "I will help you find a way out of this city"},
        ]
        segs, _ = _plan_with_windows(wins)
        dlg = [s for s in segs if s.kind == "dialogue"]
        self.assertEqual(len(dlg), 2)
        names = {Path(s.src).name for s in dlg}
        self.assertIn("dlg_a.mp4", names)
        self.assertIn("dlg_c.mp4", names)
        self.assertNotIn("dlg_b.mp4", names)

    def test_rejects_overlapping_source_ranges(self):
        wins = [
            {"src": "dlg_a.mp4", "s": 10.0, "e": 16.0, "score": 20.0,
             "text": "We need to leave tonight before they find us"},
            {"src": "dlg_a.mp4", "s": 14.0, "e": 19.0, "score": 19.0,
             "text": "The package is already on the boat waiting"},
            {"src": "dlg_b.mp4", "s": 7.0, "e": 11.5, "score": 8.0,
             "text": "Stay low and keep your head down now"},
        ]
        segs, _ = _plan_with_windows(wins)
        dlg = [s for s in segs if s.kind == "dialogue"]
        self.assertEqual(len(dlg), 2)
        names = {Path(s.src).name for s in dlg}
        self.assertEqual(names, {"dlg_a.mp4", "dlg_b.mp4"})
        self.assertEqual(sum(1 for s in dlg if Path(s.src).name == "dlg_a.mp4"), 1)

    def test_dialogue_two_prefers_short_window(self):
        wins = [
            {"src": "dlg_a.mp4", "s": 10.0, "e": 17.5, "score": 30.0,
             "text": "This city does not forgive men like us anymore"},
            {"src": "dlg_b.mp4", "s": 8.0, "e": 15.0, "score": 20.0,
             "text": "Put the gun down and walk away slowly"},
            {"src": "dlg_c.mp4", "s": 6.0, "e": 10.5, "score": 9.0,
             "text": "Come with me if you want to live"},
        ]
        segs, period = _plan_with_windows(wins)
        dlg = [s for s in segs if s.kind == "dialogue"]
        self.assertEqual(len(dlg), 2)
        names = [Path(s.src).name for s in dlg]
        self.assertEqual(names[0], "dlg_a.mp4")
        self.assertEqual(names[1], "dlg_c.mp4")

    def test_last_spoken_word_is_not_cut_off(self):
        wins = [
            {"src": "interview.mp4", "s": 12.0, "e": 20.0, "score": 10.0,
             "text": "I just want to be great"},
        ]
        segs, period = _plan_with_windows(wins, target=20.0)
        dlg = [s for s in segs if s.kind == "dialogue"]
        self.assertTrue(dlg)
        for s in dlg:
            self.assertGreaterEqual(s.start + s.beats * period, 20.0)


class DialogueWindowBounds(unittest.TestCase):
    def test_skips_short_and_breaks_long(self):
        words = [
            {"seg": 0, "s": 1.0, "e": 1.4, "p": 0.9, "w": "hey"},
            {"seg": 0, "s": 1.5, "e": 2.0, "p": 0.9, "w": "there"},
            {"seg": 1, "s": 2.1, "e": 3.0, "p": 0.9, "w": "friend"},
            {"seg": 1, "s": 3.1, "e": 3.8, "p": 0.9, "w": "now"},
            {"seg": 2, "s": 4.0, "e": 5.0, "p": 0.9, "w": "please"},
            {"seg": 2, "s": 5.1, "e": 6.0, "p": 0.9, "w": "listen"},
            {"seg": 3, "s": 6.1, "e": 7.2, "p": 0.9, "w": "carefully"},
            {"seg": 3, "s": 7.3, "e": 8.0, "p": 0.9, "w": "ok"},
            {"seg": 4, "s": 8.2, "e": 10.0, "p": 0.9, "w": "because"},
            {"seg": 4, "s": 10.1, "e": 13.0, "p": 0.9, "w": "this"},
        ]
        with patch.object(V, "probe_duration", return_value=20.0), patch.object(V, "transcribe", return_value=words):
            wins = V.dialogue_windows(Path("/tmp/x.mp4"), [])
        durs = [round(w["e"] - w["s"], 2) for w in wins]
        self.assertTrue(durs)
        self.assertTrue(all(3.5 <= d <= 8.5 for d in durs))
        self.assertTrue(any(abs(d - 4.0) < 0.05 for d in durs))
        self.assertFalse(any(d > 8.5 for d in durs))
        self.assertFalse(any(d < 3.5 for d in durs))


class MixAudioAndDuration(unittest.TestCase):
    def test_mix_audio_defaults(self):
        sig = inspect.signature(V.mix_audio)
        self.assertEqual(sig.parameters["duck"].default, 0.28)  # duck to ~28%, never mute
        self.assertEqual(sig.parameters["ramp"].default, 0.25)
        self.assertEqual(sig.parameters["tail_fade"].default, 1.8)

    def test_zero_length_fade_does_not_wipe_audio(self):
        with tempfile.TemporaryDirectory() as td:
            song = Path(td) / "s.wav"
            out = Path(td) / "o.wav"
            song.write_bytes(b"x")
            captured = {}

            def fake_load(path, sr, start, dur):
                n = int(round(dur * sr))
                return np.ones((n, 2), np.float32) * 0.5

            def fake_write(path, data, sr, subtype=None):
                captured["data"] = np.array(data, copy=True)

            with patch.object(V, "load_audio", fake_load), patch("soundfile.write", fake_write):
                V.mix_audio(song, 0.0, 0.4, [], out, tail_fade=0.0)
            self.assertIn("data", captured)
            self.assertGreater(float(np.max(np.abs(captured["data"]))), 0.1)

    def test_default_duration_is_38(self):
        self.assertIn("default=38.0", inspect.getsource(V.main))

if __name__ == "__main__":
    unittest.main()



class NoReuseInVideo(unittest.TestCase):
    def test_no_segment_overlaps_and_short_footage_ends_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            clips = Path(td)
            a = clips / "a.mp4"
            a.write_bytes(b"x")
            wins = [{"src": str(a), "s": 12.0, "e": 17.0, "score": 10.0, "text": "we run this city tonight"}]
            with patch.object(V, "scan_clip", side_effect=lambda p: _shots(str(p), start=0.0, n=9)), \
                 patch.object(V, "dialogue_windows", side_effect=lambda p, sh: wins):
                segs, _ = V.auto_plan(a, clips, 38.0, {"period": 0.5, "tempo": 120})
        rng = []
        for s in segs:
            dur = s.beats * 0.5
            for r0, r1 in rng:
                self.assertLessEqual(min(r1, s.start + dur) - max(r0, s.start), 0.05, "footage reused")
            rng.append((s.start, s.start + dur))
        self.assertLess(sum(s.beats for s in segs) * 0.5, 38.0)   # ran out: ended early, no loop
