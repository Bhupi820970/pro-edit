#!/usr/bin/env python3
"""Stdlib tests for Instagram Graph API uploader + runner wiring (mock HTTP, no network)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.publish import instagram_api as IG  # noqa: E402
from pipeline_lib.publish import ledger, runner  # noqa: E402
from pipeline_lib.publish import secrets as S  # noqa: E402
import scripts.upload as upload_cli  # noqa: E402


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_err(code: int, payload: dict) -> urllib.error.HTTPError:
    raw = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        "https://graph.facebook.com/v26.0/test",
        code,
        "Err",
        {},
        io.BytesIO(raw),
    )


class InstagramApiTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {
                "IG_ACCESS_TOKEN": "EAAB_super_secret_test_token_123456",
                "IG_USER_ID": "17841412696987950",
            },
        )
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()

    def test_check_token_reads_username_and_quota_without_leaking_token(self):
        seen = []

        def fake_urlopen(req, timeout=60):
            seen.append((req.get_method(), req.full_url, dict(req.header_items())))
            self.assertNotIn("EAAB_super_secret_test_token_123456", req.full_url)
            auth = req.get_header("Authorization") or ""
            self.assertEqual(auth, "Bearer EAAB_super_secret_test_token_123456")
            if "content_publishing_limit" in req.full_url:
                return _FakeResp({
                    "data": [{
                        "quota_usage": 3,
                        "config": {"quota_total": 100, "quota_duration": 86400},
                    }]
                })
            return _FakeResp({"id": "17841412696987950", "username": "benreelr"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = IG.check_token(expected_username="benreelr")

        self.assertTrue(res["ok"])
        self.assertEqual(res["username"], "benreelr")
        self.assertEqual(res["id"], "17841412696987950")
        self.assertEqual(res["quota_usage"], 3)
        self.assertEqual(res["quota_total"], 100)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(m == "GET" for m, _, _ in seen))

    def test_wrong_username_refuses_and_redacts_token_on_error(self):
        def fake_urlopen(req, timeout=60):
            return _FakeResp({"id": "17841412696987950", "username": "wronguser"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(IG.InstagramApiError) as ctx:
                IG.assert_account("benreelr")
        self.assertIn("wronguser", str(ctx.exception))

        msg = S.redact("Error with Bearer EAAB_super_secret_test_token_123456 and EAAB_super_secret_test_token_123456")
        self.assertNotIn("EAAB_super_secret_test_token_123456", msg)

    def test_quota_limit_blocks_publish(self):
        def fake_urlopen(req, timeout=60):
            if "content_publishing_limit" in req.full_url:
                return _FakeResp({
                    "data": [{
                        "quota_usage": 100,
                        "config": {"quota_total": 100, "quota_duration": 86400},
                    }]
                })
            return _FakeResp({"id": "17841412696987950", "username": "benreelr"})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(IG.InstagramApiError) as ctx:
                IG.publish_reel("https://example.com/v.mp4", "caption")
        self.assertIn("publishing limit reached", str(ctx.exception))

    def test_publish_reel_happy_path_polls_and_publishes(self):
        calls = []
        poll_n = {"n": 0}

        def fake_urlopen(req, timeout=60):
            body = req.data.decode() if req.data else ""
            calls.append((req.get_method(), req.full_url, body))
            url = req.full_url
            if url.endswith("/17841412696987950?fields=username%2Cid"):
                return _FakeResp({"id": "17841412696987950", "username": "benreelr"})
            if "content_publishing_limit" in url:
                return _FakeResp({"data": [{"quota_usage": 1, "config": {"quota_total": 100, "quota_duration": 86400}}]})
            if url.endswith("/17841412696987950/media") and req.get_method() == "POST":
                self.assertIn("media_type=REELS", body)
                self.assertIn("share_to_feed=true", body)
                self.assertIn("video_url=https%3A%2F%2Fcdn.example.com%2Fclip.mp4", body)
                return _FakeResp({"id": "cont_1"})
            if "/cont_1?" in url:
                poll_n["n"] += 1
                st = "IN_PROGRESS" if poll_n["n"] == 1 else "FINISHED"
                return _FakeResp({"id": "cont_1", "status_code": st})
            if url.endswith("/17841412696987950/media_publish"):
                self.assertIn("creation_id=cont_1", body)
                return _FakeResp({"id": "media_99"})
            if "/media_99?" in url:
                return _FakeResp({"id": "media_99", "permalink": "https://www.instagram.com/reel/XYZ/"})
            raise AssertionError(f"unexpected request: {req.get_method()} {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("pipeline_lib.publish.instagram_api.time.sleep"):
            res = IG.publish_reel("https://cdn.example.com/clip.mp4", "Night drive\n\n#GTA6")

        self.assertEqual(res["container_id"], "cont_1")
        self.assertEqual(res["media_id"], "media_99")
        self.assertEqual(res["url"], "https://www.instagram.com/reel/XYZ/")
        self.assertFalse(res["already_published"])
        self.assertEqual(poll_n["n"], 2)

    def test_container_error_never_calls_media_publish(self):
        publish_calls = {"n": 0}

        def fake_urlopen(req, timeout=60):
            url = req.full_url
            if "fields=username" in url:
                return _FakeResp({"id": "17841412696987950", "username": "benreelr"})
            if "content_publishing_limit" in url:
                return _FakeResp({"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
            if url.endswith("/media") and req.get_method() == "POST":
                return _FakeResp({"id": "cont_bad"})
            if "/cont_bad?" in url:
                return _FakeResp({"id": "cont_bad", "status_code": "ERROR", "status": "video unreachable"})
            if "media_publish" in url:
                publish_calls["n"] += 1
                return _FakeResp({"id": "should_not_happen"})
            raise AssertionError(url)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("pipeline_lib.publish.instagram_api.time.sleep"):
            with self.assertRaises(IG.InstagramApiError):
                IG.publish_reel("https://cdn.example.com/bad.mp4", "cap", retry_once=True)
        self.assertEqual(publish_calls["n"], 0)

    def test_never_double_publish_when_first_publish_succeeded_on_server(self):
        publish_calls = {"n": 0}
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

        def fake_urlopen(req, timeout=60):
            url = req.full_url
            if "fields=username" in url:
                return _FakeResp({"id": "17841412696987950", "username": "benreelr"})
            if "content_publishing_limit" in url:
                return _FakeResp({"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
            if url.endswith("/media") and req.get_method() == "POST":
                return _FakeResp({"id": "cont_ok"})
            if "/cont_ok?" in url:
                # Before media_publish, container is FINISHED; after uncertain media_publish error, it's PUBLISHED!
                st = "PUBLISHED" if publish_calls["n"] >= 1 else "FINISHED"
                return _FakeResp({"id": "cont_ok", "status_code": st})
            if "media_publish" in url:
                publish_calls["n"] += 1
                raise urllib.error.URLError("timed out waiting for response")
            if "/17841412696987950/media?" in url:
                return _FakeResp({
                    "data": [{
                        "id": "media_already",
                        "caption": "Night drive in Vice City #GTA6",
                        "permalink": "https://www.instagram.com/reel/RECOVERED/",
                        "timestamp": now_iso,
                    }]
                })
            raise AssertionError(url)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("pipeline_lib.publish.instagram_api.time.sleep"):
            res = IG.publish_reel("https://cdn.example.com/v.mp4", "Night drive in Vice City #GTA6", retry_once=True)

        self.assertEqual(publish_calls["n"], 1)
        self.assertTrue(res["already_published"])
        self.assertEqual(res["media_id"], "media_already")
        self.assertEqual(res["url"], "https://www.instagram.com/reel/RECOVERED/")

    def test_retry_media_publish_only_when_container_still_finished(self):
        publish_calls = {"n": 0}

        def fake_urlopen(req, timeout=60):
            url = req.full_url
            if "fields=username" in url:
                return _FakeResp({"id": "17841412696987950", "username": "benreelr"})
            if "content_publishing_limit" in url:
                return _FakeResp({"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
            if url.endswith("/media") and req.get_method() == "POST":
                return _FakeResp({"id": "cont_retry"})
            if "/cont_retry?" in url:
                return _FakeResp({"id": "cont_retry", "status_code": "FINISHED"})
            if "/17841412696987950/media?" in url:
                return _FakeResp({"data": []})
            if "media_publish" in url:
                publish_calls["n"] += 1
                if publish_calls["n"] == 1:
                    raise _http_err(500, {"error": {"message": "transient error", "code": 2}})
                return _FakeResp({"id": "media_2nd"})
            if "/media_2nd?" in url:
                return _FakeResp({"id": "media_2nd", "permalink": "https://www.instagram.com/reel/SECOND/"})
            raise AssertionError(url)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("pipeline_lib.publish.instagram_api.time.sleep"):
            res = IG.publish_reel("https://cdn.example.com/v.mp4", "Night drive #GTA6", retry_once=True)

        self.assertEqual(publish_calls["n"], 2)
        self.assertEqual(res["media_id"], "media_2nd")
        self.assertFalse(res["already_published"])


class RunnerInstagramWiringTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.stock = self.root / "stock"
        self.stock.mkdir()
        self.ledger = self.root / "posted.json"
        item = {
            "id": "in-gta-1",
            "platform": "instagram",
            "category": "gta",
            "title": "Vice City never sleeps",
            "hook": "NIGHT DRIVE",
            "hashtags": ["GTA6"],
            "song": {"title": "Give It Up", "artist": "KC and the Sunshine Band"},
            "sources": [{"credit": "Rockstar Games", "title": "GTA VI Trailer 2"}],
            "created": "2026-11-20T08:00:00+05:30",
            "state": "ready",
        }
        mp4 = self.stock / "in-gta-1.mp4"
        mp4.write_bytes(b"fake-mp4")
        item["path"] = str(mp4)
        (self.stock / "in-gta-1.mp4.json").write_text(json.dumps(item))

    def tearDown(self):
        self.td.cleanup()

    def test_runner_uses_ig_graph_api_by_default_and_cleans_transfer(self):
        deleted = []
        with patch("pipeline_lib.publish.runner.transfer.create",
                   return_value={"tag": "vc-xfer-1-abc", "filename": "x.mp4", "url": "https://github.com/x/y.mp4"}), \
             patch("pipeline_lib.publish.runner.transfer.delete", side_effect=lambda r, t: deleted.append(t)), \
             patch("pipeline_lib.publish.runner.instagram_api.publish_reel",
                   return_value={"ig_user_id": "1784", "username": "benreelr",
                                 "container_id": "c1", "media_id": "m1",
                                 "url": "https://www.instagram.com/reel/ABC/"}) as pub_mock, \
             patch("pipeline_lib.publish.runner.buffer_ig.find_instagram_channel") as buf_mock, \
             patch("pipeline_lib.publish.runner._gemini", return_value=None):
            out = runner.run_pass(
                "slot",
                dry_run=False,
                repo="Bhupi820970/pro-edit",
                stock_dir=self.stock,
                work_dir=self.root / "work",
                ledger_path=self.ledger,
            )

        pub_mock.assert_called_once()
        buf_mock.assert_not_called()
        self.assertEqual(deleted, ["vc-xfer-1-abc"])
        ig = next(r for r in out["results"] if r["platform"] == "instagram")
        self.assertTrue(ig["ok"])
        self.assertEqual(ig["ig_media_id"], "m1")
        self.assertEqual(ig["ig_container_id"], "c1")
        d = ledger.load(self.ledger)
        row = ledger.entry_for(d, "in-gta-1")
        self.assertEqual(row["ig_media_id"], "m1")
        self.assertEqual(row["instagram_url"], "https://www.instagram.com/reel/ABC/")

    def test_buffer_fallback_off_by_default_and_optional_when_enabled(self):
        deleted = []
        with patch("pipeline_lib.publish.runner.transfer.create",
                   return_value={"tag": "vc-xfer-2-abc", "filename": "x.mp4", "url": "https://github.com/x/y.mp4"}), \
             patch("pipeline_lib.publish.runner.transfer.delete", side_effect=lambda r, t: deleted.append(t)), \
             patch("pipeline_lib.publish.runner.instagram_api.publish_reel",
                   side_effect=IG.InstagramApiError("boom")), \
             patch("pipeline_lib.publish.runner.buffer_ig.find_instagram_channel") as buf_mock, \
             patch("pipeline_lib.publish.runner._gemini", return_value=None):
            out = runner.run_pass(
                "slot",
                dry_run=False,
                repo="Bhupi820970/pro-edit",
                stock_dir=self.stock,
                work_dir=self.root / "work",
                ledger_path=self.ledger,
            )
        buf_mock.assert_not_called()
        ig = next(r for r in out["results"] if r["platform"] == "instagram")
        self.assertEqual(ig["status"], "failed")
        self.assertEqual(deleted, ["vc-xfer-2-abc"])

        # Now enable fallback flag via env
        with patch.dict(os.environ, {"IG_BUFFER_FALLBACK": "1"}), \
             patch("pipeline_lib.publish.runner.transfer.create",
                   return_value={"tag": "vc-xfer-3-abc", "filename": "x.mp4", "url": "https://github.com/x/y.mp4"}), \
             patch("pipeline_lib.publish.runner.transfer.delete"), \
             patch("pipeline_lib.publish.runner.instagram_api.publish_reel",
                   side_effect=IG.InstagramApiError("boom")), \
             patch("pipeline_lib.publish.runner._upload_instagram_buffer",
                   return_value={"platform": "instagram", "video_id": "in-gta-1", "ok": True,
                                 "status": "posted", "buffer_post_id": "b1",
                                 "url": "https://instagram.com/p/b1", "title": "t"}) as buf_up, \
             patch("pipeline_lib.publish.runner._gemini", return_value=None):
            out2 = runner.run_pass(
                "slot",
                dry_run=False,
                repo="Bhupi820970/pro-edit",
                stock_dir=self.stock,
                work_dir=self.root / "work",
                ledger_path=self.ledger,
            )
        buf_up.assert_called_once()
        ig2 = next(r for r in out2["results"] if r["platform"] == "instagram")
        self.assertEqual(ig2["buffer_post_id"], "b1")

    def test_cli_ig_token_check_invokes_check(self):
        with patch("scripts.upload.run_ig_token_check",
                   return_value={"ok": True, "username": "benreelr", "id": "1784",
                                 "quota_usage": 0, "quota_total": 100}) as chk:
            rc = upload_cli.main(["--ig-token-check"])
        self.assertEqual(rc, 0)
        chk.assert_called_once()

    def test_upload_workflow_has_ig_token_check_and_secrets(self):
        wf = (HERE / ".github" / "workflows" / "upload.yml").read_text()
        self.assertIn("ig-token-check:", wf)
        self.assertIn("ig_token_check:", wf)
        self.assertIn("IG_ACCESS_TOKEN", wf)
        self.assertIn("IG_USER_ID", wf)
        self.assertIn("ig_token_check != 'true'", wf)


if __name__ == "__main__":
    unittest.main()
