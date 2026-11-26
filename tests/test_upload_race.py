#!/usr/bin/env python3
"""Two upload runs for one slot (18:33 kick + another dispatch 19 s later) must post once per platform."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish import ledger  # noqa: E402


class LedgerRace(unittest.TestCase):
    def test_merge_is_union_without_conflict(self):
        a = {"posted": [{"id": "v1", "platform": "youtube", "slot": "S", "posted_at": "1"}]}
        b = {"posted": [{"id": "v2", "platform": "instagram", "slot": "S", "posted_at": "2"},
                        {"id": "v1", "platform": "youtube", "youtube_id": "x", "posted_at": "1"}]}
        m = ledger.merge(a, b)
        self.assertEqual([e["id"] for e in m["posted"]], ["v1", "v2"])
        self.assertEqual(m["posted"][0]["youtube_id"], "x")
        self.assertEqual(m["posted"][0]["slot"], "S")

    def test_slot_guard(self):
        d = {"posted": [{"id": "v1", "platform": "youtube", "slot": "2026-11-26T18:30"}]}
        self.assertTrue(ledger.slot_taken(d, "2026-11-26T18:30", "youtube"))
        self.assertFalse(ledger.slot_taken(d, "2026-11-26T18:30", "instagram"))
        self.assertFalse(ledger.slot_taken(d, "2026-11-26T21:30", "youtube"))
        self.assertFalse(ledger.slot_taken(d, None, "youtube"))

    def test_merge_file_cli(self):
        with tempfile.TemporaryDirectory() as td:
            ours, theirs = Path(td) / "o.json", Path(td) / "t.json"
            ours.write_text(json.dumps({"version": 1, "posted": [{"id": "a", "posted_at": "1"}]}))
            theirs.write_text(json.dumps({"version": 1, "posted": [{"id": "b", "posted_at": "2"}]}))
            ledger._main(["merge-file", "--ours", str(ours), "--theirs", str(theirs)])
            self.assertEqual({e["id"] for e in json.loads(ours.read_text())["posted"]}, {"a", "b"})

    def _run(self, remote):
        from unittest.mock import patch
        from pipeline_lib.publish import runner
        items = [{"id": "yo-2", "platform": "youtube"}, {"id": "in-2", "platform": "instagram"}]
        uploads = []

        def up(kind):
            def f(item, *a, **k):
                uploads.append((kind, item["id"]))
                return {"ok": True, "status": "posted", "platform": {"yt": "youtube", "ig": "instagram"}[kind]}
            return f
        with tempfile.TemporaryDirectory() as td, \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "VC_REMOTE_LEDGER": "1", "GITHUB_STEP_SUMMARY": str(Path(td) / "s"),
                                          "GITHUB_OUTPUT": str(Path(td) / "o")}), \
                patch.object(runner.ledger, "fetch_remote", return_value=remote), \
                patch.object(runner.ledger, "cas_update", side_effect=lambda repo, mut, msg, **k: mut(json.loads(json.dumps(remote)))), \
                patch.object(runner.live_check, "yt_live_copy", return_value=None), \
                patch.object(runner.live_check, "ig_live_copy", return_value=None), \
                patch.object(runner.youtube, "access_token_from_env", return_value="t"), \
                patch.object(runner.ledger, "due_slot", return_value="2026-11-26T18:30"), \
                patch.object(runner.stock, "load_stock_dir", return_value=items), \
                patch.object(runner.stock, "pick_next",
                             side_effect=lambda its, plat, posted: next((i for i in its if i["platform"] == plat
                                                                         and i["id"] not in posted), None)), \
                patch.object(runner.transfer, "cleanup_stale"), patch.object(runner, "_gemini", return_value=None), \
                patch.object(runner, "_ensure_path", side_effect=lambda item, *a: Path(td) / "v.mp4"), \
                patch.object(runner.captions, "build", return_value={"title": "t"}), \
                patch.object(runner, "_upload_youtube", side_effect=up("yt")), \
                patch.object(runner, "_upload_instagram", side_effect=up("ig")):
            lp = Path(td) / "posted.json"
            lp.write_text(json.dumps({"posted": []}))   # stale checkout: empty ledger
            runner.run_pass(mode="slot", dry_run=False, repo="x/y", ledger_path=lp, stock_dir=td, work_dir=td)
        return uploads

    def test_second_run_with_stale_checkout_does_not_repost(self):
        remote = {"posted": [
            {"id": "yo-1", "platform": "youtube", "slot": "2026-11-26T18:30", "run_id": "A"},
            {"id": "in-1", "platform": "instagram", "slot": "2026-11-26T18:30", "run_id": "A"}]}
        self.assertEqual(self._run(remote), [])

    def test_first_run_posts_once_per_platform(self):
        self.assertEqual(self._run({"posted": []}), [("yt", "yo-2"), ("ig", "in-2")])

    def test_unreadable_remote_ledger_blocks_slot_posts(self):
        self.assertEqual(self._run(None), [])


if __name__ == "__main__":
    unittest.main()
