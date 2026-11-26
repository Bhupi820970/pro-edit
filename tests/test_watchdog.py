import datetime as dt
import unittest
from zoneinfo import ZoneInfo

from pipeline_lib import autoproduce as A, health as H

IST = ZoneInfo("Asia/Kolkata")
NOW = dt.datetime(2026, 11, 26, 20, 0, tzinfo=IST)


class Auto(unittest.TestCase):
    def test_silent_when_stock_ok(self):
        self.assertFalse(A.decide({"youtube": 5, "instagram": 4}, [], (False, ""), NOW)["dispatch"])

    def test_instagram_low_dispatches_instagram(self):
        d = A.decide({"youtube": 5, "instagram": 0}, [], (False, ""), NOW)
        self.assertTrue(d["dispatch"])
        self.assertEqual(d["platform"], "instagram")

    def test_both_low_both_with_ig_first(self):
        d = A.decide({"youtube": 1, "instagram": 0}, [], (False, ""), NOW)
        # one run, both jobs in parallel; the matrix lists Instagram first, YouTube makes the smaller batch
        self.assertEqual((d["platform"], d["low"][0]), ("both", "instagram"))

    def test_never_stacks_and_respects_quota_and_cooldown(self):
        self.assertFalse(A.decide({"instagram": 0}, [{"status": "in_progress"}], (False, ""), NOW)["dispatch"])
        self.assertFalse(A.decide({"instagram": 0}, [], (True, "x"), NOW)["dispatch"])
        recent = [{"status": "completed", "conclusion": "success", "displayTitle": A.AUTO_TITLE,
                   "createdAt": "2026-11-26T14:10:00Z"}]   # 19:40 IST
        self.assertIn("cooldown", A.decide({"instagram": 0}, recent, (False, ""), NOW)["reason"])
        failed = [{"status": "completed", "conclusion": "failure", "displayTitle": A.AUTO_TITLE,
                   "createdAt": "2026-11-26T13:00:00Z"}] * 2   # 18:30 IST, 90 min ago
        self.assertIn("back-off", A.decide({"instagram": 0}, failed, (False, ""), NOW)["reason"])

    def test_counts(self):
        self.assertEqual(A.count_for("instagram", {"instagram": 0, "youtube": 3}), 4)
        self.assertEqual(A.count_for("youtube", {"instagram": 0, "youtube": 3}), 3)
        self.assertEqual(A.count_for("youtube", {"instagram": 7, "youtube": 7}), 0)


class Health(unittest.TestCase):
    def test_silent_when_fine(self):
        led = {"posted": [{"slot": f"2026-11-26T{s}", "platform": p, "status": "posted"}
                          for s in ("08:00", "11:30", "15:00", "18:30") for p in ("youtube", "instagram")]}
        self.assertEqual(H.problems({"youtube": 3, "instagram": 2}, led, ["08:00", "11:30", "15:00", "18:30", "21:30"],
                                    [], None, True, NOW), [])

    def test_alerts(self):
        runs = [{"status": "completed", "conclusion": "failure"}, {"status": "completed", "conclusion": "cancelled"},
                {"status": "completed", "conclusion": "failure"}]
        p = H.problems({"youtube": 3, "instagram": 0}, {"posted": []}, ["18:30"], runs, 3.0, False, NOW)
        text = " ".join(p)
        self.assertIn("18:30 IST", text)
        self.assertIn("stock is 0 on instagram", text)
        self.assertIn("2 produce runs failed", text)
        self.assertIn("IG_ACCESS_TOKEN", text)
        self.assertIn("YT_REFRESH_TOKEN", text)


if __name__ == "__main__":
    unittest.main()


class Disk(unittest.TestCase):
    def test_prune_used_clips_oldest_first_only_used(self):
        import os, tempfile, time
        from pathlib import Path
        from unittest.mock import patch
        import box_fetch as B
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for i, name in enumerate(["usedA.mp4", "usedB.mp4", "fresh.mp4"]):
                (d / name).write_bytes(b"x")
                os.utime(d / name, (time.time() - 100 + i, time.time() - 100 + i))
            free = iter([5, 5, 11, 11])
            with patch.object(B, "free_gb", side_effect=lambda p: next(free)), \
                 patch("pipeline_lib.used_clips.used_ids", return_value={"usedA", "usedB"}):
                n = B.prune_used_clips(d, 10)
            self.assertEqual(n, 1)
            self.assertFalse((d / "usedA.mp4").exists())
            self.assertTrue((d / "usedB.mp4").exists())
            self.assertTrue((d / "fresh.mp4").exists())
