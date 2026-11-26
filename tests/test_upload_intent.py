"""Intent-before-post + reconcile: no failure path may lead to a double post."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_lib.publish import runner

SLOT = "2026-11-26T21:30"


class Intent(unittest.TestCase):
    def run_pass(self, remote, yt_live=None, ig_live=None, yt_raises=None, items=None):
        items = items or [{"id": "yo-2", "platform": "youtube", "title": "T2"},
                          {"id": "in-2", "platform": "instagram", "title": "I2"}]
        uploads = []
        state = {"remote": remote}

        def cas(repo, mut, msg, **k):
            new = mut(json.loads(json.dumps(state["remote"])))
            if new is not None:
                state["remote"] = new
            return state["remote"] if new is None else new

        def up_yt(item, *a, **k):
            uploads.append(("yt", item["id"]))
            if yt_raises:
                raise yt_raises
            return {"ok": True, "status": "posted", "platform": "youtube", "youtube_id": "NEW", "url": "u"}

        def up_ig(item, *a, **k):
            uploads.append(("ig", item["id"]))
            return {"ok": True, "status": "posted", "platform": "instagram", "ig_media_id": "M", "url": "u"}
        with tempfile.TemporaryDirectory() as td, \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "VC_REMOTE_LEDGER": "1", "GITHUB_RUN_ID": "R2",
                                          "GITHUB_STEP_SUMMARY": str(Path(td) / "s"), "GITHUB_OUTPUT": str(Path(td) / "o")}), \
                patch.object(runner.ledger, "fetch_remote", side_effect=lambda repo: json.loads(json.dumps(state["remote"]))), \
                patch.object(runner.ledger, "cas_update", side_effect=cas), \
                patch.object(runner.ledger, "due_slot", return_value=SLOT), \
                patch.object(runner.live_check, "yt_live_copy", side_effect=lambda *a, **k: yt_live() if yt_live else None), \
                patch.object(runner.live_check, "ig_live_copy", side_effect=lambda *a, **k: ig_live() if ig_live else None), \
                patch.object(runner.youtube, "access_token_from_env", return_value="t"), \
                patch.object(runner.stock, "load_stock_dir", return_value=items), \
                patch.object(runner.stock, "pick_next",
                             side_effect=lambda its, plat, posted: next((i for i in its if i["platform"] == plat
                                                                         and i["id"] not in posted), None)), \
                patch.object(runner.transfer, "cleanup_stale"), patch.object(runner, "_gemini", return_value=None), \
                patch.object(runner, "_ensure_path", side_effect=lambda item, *a: Path(td) / "v.mp4"), \
                patch.object(runner.captions, "build", return_value={"title": "t", "tags": []}), \
                patch.object(runner, "_upload_youtube", side_effect=up_yt), \
                patch.object(runner, "_upload_instagram", side_effect=up_ig):
            lp = Path(td) / "posted.json"
            lp.write_text(json.dumps({"posted": []}))
            out = runner.run_pass(mode="slot", dry_run=False, repo="x/y", ledger_path=lp, stock_dir=td, work_dir=td)
        return uploads, state["remote"], out

    def test_intent_written_then_posted(self):
        ups, remote, _ = self.run_pass({"posted": []})
        self.assertEqual(ups, [("yt", "yo-2"), ("ig", "in-2")])
        rows = {r["id"]: r for r in remote["posted"]}
        self.assertEqual(rows["yo-2"]["status"], "posted")
        self.assertEqual(rows["yo-2"]["slot"], SLOT)

    def test_fresh_intent_of_other_run_blocks(self):
        from pipeline_lib.util import now_local
        remote = {"posted": [{"id": "yo-1", "platform": "youtube", "status": "posting", "run_id": "R1",
                              "intent_at": now_local().isoformat(), "slot": SLOT}]}
        ups, _, out = self.run_pass(remote)
        self.assertNotIn(("yt", "yo-2"), ups)

    def test_stale_intent_that_went_live_is_reconciled_not_reposted(self):
        remote = {"posted": [{"id": "yo-2", "platform": "youtube", "status": "posting", "run_id": "R1",
                              "intent_at": "2026-11-26T10:00:00+05:30", "slot": "2026-11-26T08:00"}]}
        ups, remote, _ = self.run_pass(remote, yt_live=lambda: {"id": "LIVE1"})
        self.assertNotIn(("yt", "yo-2"), ups)
        row = next(r for r in remote["posted"] if r["id"] == "yo-2")
        self.assertEqual(row["status"], "posted")
        self.assertEqual(row["youtube_id"], "LIVE1")

    def test_crash_after_upload_but_live_records_posted(self):
        calls = {"n": 0}

        def live():
            calls["n"] += 1
            return None if calls["n"] == 1 else {"id": "LIVE2"}   # pre-check: not live; after crash: live
        ups, remote, _ = self.run_pass({"posted": []}, yt_live=live, yt_raises=RuntimeError("lost response"))
        self.assertEqual(ups.count(("yt", "yo-2")), 1)
        row = next(r for r in remote["posted"] if r["id"] == "yo-2")
        self.assertEqual(row["status"], "posted")
        self.assertTrue(row.get("reconciled"))

    def test_failure_not_live_frees_video_and_slot(self):
        ups, remote, _ = self.run_pass({"posted": []}, yt_raises=RuntimeError("quota"))
        row = next(r for r in remote["posted"] if r["id"] == "yo-2")
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row.get("slot"))
        self.assertNotIn("yo-2", runner.ledger.posted_ids(remote))

    def test_already_live_render_is_recorded_and_skipped(self):
        ups, remote, _ = self.run_pass({"posted": []}, ig_live=lambda: {"id": "IGX", "permalink": "p"},
                                       items=[{"id": "in-2", "platform": "instagram", "title": "I2"}])
        self.assertEqual(ups, [])
        row = next(r for r in remote["posted"] if r["id"] == "in-2")
        self.assertTrue(row["already_live"])

    def test_merge_never_downgrades_posted(self):
        a = {"posted": [{"id": "x", "status": "posting", "slot": "s"}]}
        b = {"posted": [{"id": "x", "status": "posted", "slot": "s", "youtube_id": "Y"}]}
        m = runner.ledger.merge(a, b)
        self.assertEqual(m["posted"][0]["status"], "posted")


if __name__ == "__main__":
    unittest.main()
