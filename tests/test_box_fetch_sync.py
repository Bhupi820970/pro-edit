#!/usr/bin/env python3
"""Stdlib unit tests for box_fetch commit parsing and release-asset sync helpers (no network)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from box_fetch import (  # noqa: E402
    AUDIO_EXT, VIDEO_EXT, asset_covers, local_media, parse_commits_list,
)


class TestParseCommitsList(unittest.TestCase):
    def test_valid_list(self):
        commit, reason = parse_commits_list(0, json.dumps([{"sha": "a" * 40, "commit": {"message": "state: youtube run 1"}}]))
        self.assertEqual(reason, "ok")
        self.assertEqual(commit["sha"], "a" * 40)

    def test_empty_list_is_missing_queue_not_a_parse_error(self):
        commit, reason = parse_commits_list(0, "[]")
        self.assertIsNone(commit)
        self.assertEqual(reason, "no queue file")
        self.assertNotIn("could not read last commit", reason)

    def test_suspended_or_not_found_dict(self):
        body = json.dumps({"message": "Sorry. Your account was suspended", "documentation_url": "https://docs.github.com"})
        commit, reason = parse_commits_list(1, body)
        self.assertIsNone(commit)
        self.assertIn("could not read last commit", reason)
        self.assertIn("suspended", reason)

    def test_invalid_json_and_empty_stdout(self):
        commit, reason = parse_commits_list(0, "<html>oops</html>")
        self.assertIsNone(commit)
        self.assertIn("invalid JSON", reason)
        commit, reason = parse_commits_list(22, "", "HTTP 403: Sorry. Your account was suspended")
        self.assertIsNone(commit)
        self.assertIn("gh exit 22", reason)
        self.assertIn("suspended", reason)

    def test_malformed_entry(self):
        commit, reason = parse_commits_list(0, json.dumps([{"commit": {"message": "no sha"}}]))
        self.assertIsNone(commit)
        self.assertIn("malformed entry", reason)


class TestAssetCoversAndLocalMedia(unittest.TestCase):
    def test_asset_covers_filename_and_stem(self):
        assets = {"EiQEBYDox_k.mp4", "manifest.json"}
        self.assertTrue(asset_covers("EiQEBYDox_k", "EiQEBYDox_k.mkv", assets))
        self.assertTrue(asset_covers("EiQEBYDox_k", "EiQEBYDox_k.mp4", assets))
        self.assertFalse(asset_covers("eOrb93UZfPU", "eOrb93UZfPU.mkv", assets))
        self.assertFalse(asset_covers("", "", assets))

    def test_local_media_matches_any_extension(self):
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            (d / "EiQEBYDox_k.mp4").write_bytes(b"x" * 2000)
            (d / "tiny.mp4").write_bytes(b"no")
            got = local_media(d, "EiQEBYDox_k", VIDEO_EXT, min_size=1000)
            self.assertIsNotNone(got)
            self.assertEqual(got.name, "EiQEBYDox_k.mp4")
            self.assertIsNone(local_media(d, "EiQEBYDox_k", AUDIO_EXT, min_size=1000))
            self.assertIsNone(local_media(d, "tiny", VIDEO_EXT, min_size=1000))
            self.assertIsNone(local_media(d, "missing", VIDEO_EXT, min_size=1000))


if __name__ == "__main__":
    unittest.main()
