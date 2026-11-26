#!/usr/bin/env python3
"""Stdlib tests for Vice Cartel upload: ledger, stock, Buffer channel pick, YouTube
guards, transfer safety, runner retry, workflow security. No network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish import (  # noqa: E402
    buffer_ig, captions, cfg as cfgmod, ledger, stock, transfer, youtube,
)
from pipeline_lib.publish.runner import run_pass, _gemini  # noqa: E402
from pipeline_lib import util as pipeline_util  # noqa: E402
from pipeline_lib.gemini import Gemini  # noqa: E402
import vicestyle as V  # noqa: E402


def _item(vid, platform="youtube", category="gta", **kw):
    d = {
        "id": vid,
        "platform": platform,
        "category": category,
        "title": "Vice City never sleeps",
        "hook": "NIGHT DRIVE",
        "hashtags": ["GTA6"],
        "song": {"title": "Give It Up", "artist": "KC and the Sunshine Band"},
        "sources": [{"credit": "Rockstar Games", "title": "GTA VI Trailer 2"}],
        "created": kw.pop("created", "2026-11-20T08:00:00+05:30"),
        "state": "ready",
    }
    d.update(kw)
    return d


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "posted.json"

    def tearDown(self):
        self.td.cleanup()

    def test_record_and_never_twice(self):
        d = ledger.load(self.path)
        self.assertEqual(ledger.posted_ids(d), set())
        ledger.record(d, {"id": "yo-gta-1", "platform": "youtube", "youtube_id": "abc"})
        ledger.save(d, self.path)
        d2 = ledger.load(self.path)
        self.assertTrue(ledger.is_posted(d2, "yo-gta-1"))
        self.assertTrue(ledger.is_posted(d2, "yo-gta-1", "youtube"))
        self.assertFalse(ledger.is_posted(d2, "in-gta-1"))
        ledger.record(d2, {"id": "yo-gta-1", "platform": "youtube", "youtube_url": "https://youtu.be/abc"})
        self.assertEqual(sum(1 for e in d2["posted"] if e["id"] == "yo-gta-1"), 1)
        self.assertEqual(ledger.entry_for(d2, "yo-gta-1")["youtube_id"], "abc")
        self.assertTrue(ledger.entry_for(d2, "yo-gta-1")["youtube_url"].endswith("abc"))


class StockTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, item, with_mp4=True):
        side = self.root / f"{item['id']}.mp4.json"
        if with_mp4:
            mp4 = self.root / f"{item['id']}.mp4"
            mp4.write_bytes(b"fake")
            item = {**item, "path": str(mp4)}
        side.write_text(json.dumps(item))
        return item

    def test_load_pick_and_counts_skip_posted(self):
        a = self._write(_item("yo-gta-1", "youtube", created="2026-11-20T08:00:00+05:30"))
        b = self._write(_item("yo-gta-2", "youtube", created="2026-11-20T09:00:00+05:30"))
        c = self._write(_item("in-gta-1", "instagram", created="2026-11-20T08:30:00+05:30"))
        items = stock.load_stock_dir(self.root)
        self.assertEqual([x["id"] for x in items], ["yo-gta-1", "in-gta-1", "yo-gta-2"])
        posted = {"yo-gta-1"}
        self.assertEqual(stock.pick_next(items, "youtube", posted)["id"], "yo-gta-2")
        self.assertEqual(stock.counts(items, posted), {"youtube": 1, "instagram": 1})
        self.assertEqual(stock.counts(items, {"yo-gta-1", "yo-gta-2", "in-gta-1"}),
                         {"youtube": 0, "instagram": 0})
        rec = stock.video_record(a)
        self.assertEqual(rec["file"], "yo-gta-1.mp4")
        man = stock.upsert_manifest(self.root / "manifest.json", [stock.video_record(x) for x in (a, b, c)])
        self.assertEqual(len(man["videos"]), 3)

    def test_manifest_merge_keeps_older_and_adds_new(self):
        p = self.root / "manifest.json"
        stock.upsert_manifest(p, [stock.video_record(_item("yo-gta-1", path="yo-gta-1.mp4"))])
        stock.upsert_manifest(p, [stock.video_record(_item("yo-gta-2", path="yo-gta-2.mp4"))])
        man = json.loads(p.read_text())
        self.assertEqual({v["id"] for v in man["videos"]}, {"yo-gta-1", "yo-gta-2"})


class BufferChannelTests(unittest.TestCase):
    def test_matches_username_and_url_only_instagram(self):
        ig = {"id": "1", "name": "benreelr", "service": "instagram"}
        ig2 = {"id": "2", "displayName": "@BenReelr", "service": "instagramBusiness",
               "externalLink": "https://instagram.com/benreelr"}
        yt = {"id": "3", "name": "vicecartelgta", "service": "youtube"}
        other = {"id": "4", "name": "someoneelse", "service": "instagram"}
        self.assertTrue(buffer_ig.channel_matches(ig, "benreelr"))
        self.assertTrue(buffer_ig.channel_matches(ig2, "benreelr"))
        self.assertFalse(buffer_ig.channel_matches(yt, "benreelr"))
        self.assertFalse(buffer_ig.channel_matches(other, "benreelr"))

    def test_find_picks_benreelr_not_other_channels(self):
        orgs = [{"id": "org1", "name": "Main"}]
        chans = [
            {"id": "yt", "name": "Skywalker", "service": "youtube"},
            {"id": "ig", "name": "benreelr", "displayName": "Ben", "service": "instagram",
             "isLocked": False, "isDisconnected": False},
        ]
        with patch.object(buffer_ig, "organizations", return_value=orgs), \
             patch.object(buffer_ig, "list_channels", return_value=chans):
            ch = buffer_ig.find_instagram_channel("benreelr")
        self.assertEqual(ch["id"], "ig")
        self.assertEqual(ch["organizationId"], "org1")

    def test_missing_or_locked_raises(self):
        with patch.object(buffer_ig, "organizations", return_value=[{"id": "o", "name": "x"}]), \
             patch.object(buffer_ig, "list_channels", return_value=[]):
            with self.assertRaises(buffer_ig.BufferError):
                buffer_ig.find_instagram_channel("benreelr")
        locked = [{"id": "ig", "name": "benreelr", "service": "instagram", "isLocked": True}]
        with patch.object(buffer_ig, "organizations", return_value=[{"id": "o", "name": "x"}]), \
             patch.object(buffer_ig, "list_channels", return_value=locked):
            with self.assertRaises(buffer_ig.BufferError):
                buffer_ig.find_instagram_channel("benreelr")


class YouTubeGuardTests(unittest.TestCase):
    def test_wrong_channel_refuses(self):
        ident = {"id": "UC123", "title": "Other", "handle": "@someoneelse", "custom_url": "@someoneelse"}
        with patch.object(youtube, "channel_identity", return_value=ident):
            with self.assertRaises(youtube.YouTubeError) as ctx:
                youtube.assert_channel("tok", "@vicecartelgta")
        self.assertIn("@someoneelse", str(ctx.exception))
        self.assertIn("@vicecartelgta", str(ctx.exception))

    def test_expected_channel_passes(self):
        ident = {"id": "UC999", "title": "Vice Cartel", "handle": "@vicecartelgta", "custom_url": "vicecartelgta"}
        with patch.object(youtube, "channel_identity", return_value=ident):
            got = youtube.assert_channel("tok", "@vicecartelgta")
        self.assertEqual(got["id"], "UC999")

    def test_missing_handle_refuses(self):
        ident = {"id": "UC", "title": "Mystery", "handle": "", "custom_url": ""}
        with patch.object(youtube, "channel_identity", return_value=ident):
            with self.assertRaises(youtube.YouTubeError):
                youtube.assert_channel("tok", "@vicecartelgta")

    def test_redact_and_mask_token(self):
        os.environ["YT_ACCESS_TOKEN"] = "ya29.super-secret-token-value"
        try:
            from pipeline_lib.publish import secrets as S
            S.remember("ya29.super-secret-token-value")
            msg = S.redact("Authorization: Bearer ya29.super-secret-token-value failed")
            self.assertNotIn("super-secret-token-value", msg)
            self.assertIn("***", msg)
        finally:
            os.environ.pop("YT_ACCESS_TOKEN", None)


class TransferSafetyTests(unittest.TestCase):
    def test_protected_tags_never_deleted(self):
        for tag in ("vc-ready", "sources", "songs", "test-renders"):
            self.assertFalse(transfer.is_transfer_tag(tag), tag)
            with self.assertRaises(transfer.TransferError):
                transfer.delete("Bhupi820970/pro-edit", tag)

    def test_only_xfer_prefix_is_transfer(self):
        self.assertTrue(transfer.is_transfer_tag("vc-xfer-123-ab"))
        self.assertFalse(transfer.is_transfer_tag("videos-youtube-20261120"))
        self.assertFalse(transfer.is_transfer_tag(""))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.stock = self.root / "stock"
        self.stock.mkdir()
        self.ledger = self.root / "posted.json"
        for vid, plat, created in (
            ("yo-gta-1", "youtube", "2026-11-20T08:00:00+05:30"),
            ("in-gta-1", "instagram", "2026-11-20T08:00:00+05:30"),
            ("yo-gta-2", "youtube", "2026-11-20T09:00:00+05:30"),
        ):
            item = _item(vid, plat, created=created)
            mp4 = self.stock / f"{vid}.mp4"
            mp4.write_bytes(b"fake-mp4")
            item["path"] = str(mp4)
            (self.stock / f"{vid}.mp4.json").write_text(json.dumps(item))

    def tearDown(self):
        self.td.cleanup()

    def test_dry_run_picks_one_each_and_does_not_write_ledger(self):
        out = run_pass("test", dry_run=True, repo="Bhupi820970/pro-edit",
                       stock_dir=self.stock, work_dir=self.root / "work",
                       ledger_path=self.ledger)
        self.assertTrue(out["ok"])
        plats = {r["platform"]: r for r in out["results"]}
        self.assertEqual(plats["youtube"]["status"], "dry_run")
        self.assertEqual(plats["instagram"]["status"], "dry_run")
        self.assertEqual(plats["youtube"]["video_id"], "yo-gta-1")
        self.assertEqual(plats["instagram"]["video_id"], "in-gta-1")
        self.assertFalse(self.ledger.exists())

    def test_skips_already_posted_and_posts_next(self):
        d = ledger.load(self.ledger)
        ledger.record(d, {"id": "yo-gta-1", "platform": "youtube"})
        ledger.save(d, self.ledger)
        out = run_pass("slot", dry_run=True, repo="Bhupi820970/pro-edit",
                       stock_dir=self.stock, work_dir=self.root / "work",
                       ledger_path=self.ledger)
        yt = next(r for r in out["results"] if r["platform"] == "youtube")
        self.assertEqual(yt["video_id"], "yo-gta-2")

    def test_retry_once_then_continues_other_platform(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise youtube.YouTubeError("net")

        ig_ok = {"platform": "instagram", "video_id": "in-gta-1", "ok": True, "status": "posted",
                 "buffer_post_id": "b1", "url": "https://instagram.com/p/x", "title": "t"}
        with patch("pipeline_lib.publish.runner.youtube.upload_video", side_effect=boom), \
             patch("pipeline_lib.publish.runner._upload_instagram", return_value=ig_ok), \
             patch("pipeline_lib.publish.runner._gemini", return_value=None), \
             patch("pipeline_lib.publish.runner.time.sleep"):
            out = run_pass("test", dry_run=False, repo="Bhupi820970/pro-edit",
                           stock_dir=self.stock, work_dir=self.root / "work",
                           ledger_path=self.ledger)
        self.assertEqual(calls["n"], 2)
        by = {r["platform"]: r for r in out["results"]}
        self.assertEqual(by["youtube"]["status"], "failed")
        self.assertEqual(by["instagram"]["status"], "posted")  # other platform still ran
        self.assertFalse(out["ok"])

    def test_successful_youtube_records_ledger_once(self):
        def yt_ok(path, title, description, tags, **kw):
            self.assertNotIn("AI video", title)
            self.assertIn("Song: Give It Up", description)
            return {"id": "vidX", "url": "https://www.youtube.com/watch?v=vidX", "title": title}

        with patch("pipeline_lib.publish.runner.youtube.upload_video", side_effect=yt_ok), \
             patch("pipeline_lib.publish.runner._upload_instagram",
                   return_value={"platform": "instagram", "video_id": "in-gta-1",
                                 "ok": True, "status": "posted", "buffer_post_id": "b1",
                                 "url": "https://instagram.com/p/x", "title": "t"}), \
             patch("pipeline_lib.publish.runner._gemini", return_value=None):
            out = run_pass("test", dry_run=False, repo="Bhupi820970/pro-edit",
                           stock_dir=self.stock, work_dir=self.root / "work",
                           ledger_path=self.ledger)
        self.assertTrue(out["ok"])
        d = ledger.load(self.ledger)
        self.assertTrue(ledger.is_posted(d, "yo-gta-1", "youtube"))
        self.assertTrue(ledger.is_posted(d, "in-gta-1", "instagram"))
        # second pass must not pick the same videos
        out2 = run_pass("test", dry_run=True, repo="Bhupi820970/pro-edit",
                        stock_dir=self.stock, work_dir=self.root / "work",
                        ledger_path=self.ledger)
        yt = next(r for r in out2["results"] if r["platform"] == "youtube")
        ig = next(r for r in out2["results"] if r["platform"] == "instagram")
        self.assertEqual(yt["video_id"], "yo-gta-2")
        self.assertEqual(ig["status"], "no_stock")


class StyleAndSecurityTests(unittest.TestCase):
    def test_approved_look_unchanged(self):
        pipe = json.loads((HERE / "config" / "pipeline.json").read_text())
        self.assertEqual(pipe["duration"], 38)
        self.assertEqual(V.WM_BOTTOM, 0.14)
        src = (HERE / "pipeline.py").read_text()
        self.assertIn('"VICE CARTEL"', src)
        # produce_one still renders the glass logo, not a new watermark
        self.assertIn('V.render(', src)

    def test_publish_config_slots(self):
        c = cfgmod.load()
        self.assertEqual(c["slots_ist"], ["08:00", "11:30", "15:00", "18:30", "21:30"])
        self.assertEqual(c["stock_release"], "vc-ready")
        self.assertEqual(c["youtube_channel"], "@vicecartelgta")
        self.assertEqual(c["instagram_username"], "benreelr")
        self.assertEqual(c["stock_warn_below"], 3)

    def test_workflows_are_locked_down(self):
        wf_dir = HERE / ".github" / "workflows"
        for p in wf_dir.glob("*.yml"):
            text = p.read_text()
            # comments may mention the forbidden triggers; only the `on:` keys matter
            self.assertNotRegex(text, r"(?m)^\s+pull_request_target\s*:")
            self.assertNotRegex(text, r"(?m)^\s+pull_request\s*:")
            # third-party actions must be pinned to a 40-char SHA
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("- uses:") or s.startswith("uses:"):
                    action = s.split(":", 1)[1].strip()
                    if action.startswith("./"):
                        continue
                    self.assertRegex(
                        action,
                        r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}(?:\s+#.*)?$",
                        f"{p.name}: unpinned action {action!r}",
                    )
            # never write the schedule flag from a workflow (the == 'true' gate is required)
            self.assertNotRegex(text, r"gh\s+variable\s+set\s+SCHEDULE_ENABLED")
            self.assertNotRegex(text, r"gh\s+api\s+.*SCHEDULE_ENABLED")

    def test_upload_workflow_has_test_dispatch_and_slots(self):
        text = (HERE / ".github" / "workflows" / "upload.yml").read_text()
        self.assertIn("workflow_dispatch", text)
        self.assertIn("schedule:", text)
        for cron in ('"30 2 * * *"', '"0 6 * * *"', '"30 9 * * *"', '"0 13 * * *"', '"0 16 * * *"'):
            self.assertIn(cron, text)
        self.assertIn("vars.SCHEDULE_ENABLED == 'true'", text)
        self.assertIn("options: [test, slot]", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn('"7,27,47 * * * *"', text)
        self.assertIn("repository_dispatch:", text)
        self.assertIn("upload-slot", text)
        self.assertIn("github.event.action == 'upload-slot'", text)
        self.assertIn("github.actor == github.repository_owner", text)
        self.assertIn("VICE_PIPELINE_HOME: /tmp/vice-ph", text)


class DueSlotTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.slots = {"slots_ist": ["08:00", "11:30", "15:00", "18:30", "21:30"]}
        self.tz = ZoneInfo("Asia/Kolkata")

    def _now(self, h, m):
        from datetime import datetime
        return datetime(2026, 11, 25, h, m, tzinfo=self.tz)

    def test_before_first_slot(self):
        self.assertIsNone(ledger.due_slot(self.slots, {"posted": []}, self._now(7, 0)))

    def test_first_slot_due(self):
        self.assertEqual(
            ledger.due_slot(self.slots, {"posted": []}, self._now(8, 0)),
            "2026-11-25T08:00",
        )

    def test_midday_catches_earliest_unposted(self):
        self.assertEqual(
            ledger.due_slot(self.slots, {"posted": []}, self._now(16, 10)),
            "2026-11-25T08:00",
        )

    def test_named_slot_consumed(self):
        led = {"posted": [{"id": "a", "platform": "youtube", "slot": "2026-11-25T08:00"}]}
        self.assertEqual(
            ledger.due_slot(self.slots, led, self._now(16, 10)),
            "2026-11-25T11:30",
        )

    def test_unslotted_today_posts_consume_earliest(self):
        led = {"posted": [
            {"id": "a", "platform": "youtube", "posted_at": "2026-11-25T16:20:00+05:30"},
            {"id": "b", "platform": "instagram", "posted_at": "2026-11-25T16:21:00+05:30"},
        ]}
        self.assertEqual(
            ledger.due_slot(self.slots, led, self._now(16, 30)),
            "2026-11-25T11:30",
        )

    def test_all_elapsed_slots_filled(self):
        led = {"posted": [
            {"id": "a", "platform": "youtube", "slot": "2026-11-25T08:00"},
            {"id": "b", "platform": "youtube", "slot": "2026-11-25T11:30"},
            {"id": "c", "platform": "youtube", "slot": "2026-11-25T15:00"},
        ]}
        self.assertIsNone(ledger.due_slot(self.slots, led, self._now(16, 10)))


class ScanClipCacheTests(unittest.TestCase):
    def test_cached_shots_rewrite_src_to_current_path(self):
        td = tempfile.TemporaryDirectory()
        prev = os.environ.get("VICESTYLE_CACHE")
        try:
            root = Path(td.name)
            os.environ["VICESTYLE_CACHE"] = str(root / "cache")
            clip = root / "clip.mp4"
            clip.write_bytes(b"x" * 100)
            ck = V.cache_dir() / f"shots_ov4_{V.file_key(clip)}.json"
            ck.write_text(json.dumps([{
                "src": "/mnt/ph/instagram/work/old/clips/T7aaNEW7zA0.mkv",
                "s": 0.0, "e": 2.0, "motion": 0.1, "color": 0.4, "speech": 0.0, "bad": "",
            }]))
            shots = V.scan_clip(clip)
            self.assertEqual(len(shots), 1)
            self.assertEqual(shots[0].src, str(clip))
            self.assertEqual(shots[0].s, 0.0)
            self.assertEqual(shots[0].e, 2.0)
        finally:
            td.cleanup()
            if prev is None:
                os.environ.pop("VICESTYLE_CACHE", None)
            else:
                os.environ["VICESTYLE_CACHE"] = prev



class PipelineHomeTests(unittest.TestCase):
    def tearDown(self):
        pipeline_util._home_fallback_noted = False

    def test_unwritable_home_falls_back_so_gemini_can_init(self):
        td = tempfile.TemporaryDirectory()
        try:
            blocked = Path(td.name) / "blocked"
            blocked.mkdir()
            blocked.chmod(0o500)
            dest = blocked / "pipeline"
            with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(dest), "TMPDIR": td.name,
                                         "GEMINI_API_KEY_1": "dummy-key"}, clear=False):
                # drop other numbered keys for a deterministic key list
                env_del = {k: "" for k in list(os.environ) if k.startswith("GEMINI_API_KEY") and k != "GEMINI_API_KEY_1"}
                with patch.dict(os.environ, env_del, clear=False):
                    pipeline_util._home_fallback_noted = False
                    h = pipeline_util.home()
                    self.assertEqual(h, Path(td.name) / "vice-ph")
                    self.assertTrue(h.is_dir())
                    g = Gemini(log=lambda *a: None)
                    self.assertTrue(g.keys)
            blocked.chmod(0o700)
        finally:
            td.cleanup()

    def test_gemini_helper_logs_permission_error_and_keeps_template_fallback(self):
        logs = []
        with patch("pipeline_lib.gemini.Gemini.__init__",
                   side_effect=PermissionError("[Errno 13] Permission denied: '/workspace/gta6/pipeline'")),              patch("pipeline_lib.publish.runner._log", side_effect=lambda m: logs.append(m)):
            self.assertIsNone(_gemini())
        self.assertTrue(any("PermissionError" in m and "Permission denied" in m and "template captions" in m for m in logs))
        self.assertFalse(any("dummy-key" in m or "AIza" in m for m in logs))



if __name__ == "__main__":
    unittest.main()
