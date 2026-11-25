#!/usr/bin/env python3
"""Allowlist, junk/duration filters, and CI fallback order for song_fetch. No network."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib import song_fetch as F  # noqa: E402
from pipeline_lib.songs import download_official  # noqa: E402


class Allowlist(unittest.TestCase):
    def test_https_allowlisted_hosts(self):
        self.assertTrue(F.allowed_url("https://soundcloud.com/artist/track"))
        self.assertTrue(F.allowed_url("https://cf-media.sndcdn.com/abc.mp3"))
        self.assertTrue(F.allowed_url("https://archive.org/download/id/file.mp3"))
        self.assertTrue(F.allowed_url("https://ia800201.us.archive.org/1/items/x/y.mp3"))
        self.assertTrue(F.allowed_url("https://www.youtube.com/watch?v=dQw4w9wgwxc"))
        self.assertTrue(F.allowed_url("https://inv.nadeko.net/api/v1/search?q=x"))
        self.assertTrue(F.allowed_url("https://pipedapi.kavin.rocks/streams/dQw4w9wgwxc"))
        self.assertTrue(F.allowed_url("https://cdn.pixabay.com/audio/2023/x.mp3"))

    def test_rejects_unsafe_urls(self):
        self.assertFalse(F.allowed_url("http://soundcloud.com/x"))          # not https
        self.assertFalse(F.allowed_url("https://evil.example/steal"))
        self.assertFalse(F.allowed_url("https://user:pass@soundcloud.com/x"))
        self.assertFalse(F.allowed_url("https://soundcloud.com:8080/x"))
        self.assertFalse(F.allowed_url("https://1.2.3.4/x"))
        self.assertFalse(F.allowed_url("file:///etc/passwd"))
        self.assertFalse(F.allowed_url("https://archive.org.evil.com/x"))
        self.assertFalse(F.allowed_url(""))


class Quality(unittest.TestCase):
    def test_junk_titles_rejected(self):
        self.assertTrue(F.is_junk("Midnight City (Remix)"))
        self.assertTrue(F.is_junk("Starboy nightcore sped up"))
        self.assertTrue(F.is_junk("Lyrics video - official"))
        self.assertTrue(F.is_junk("cover by some channel"))
        self.assertFalse(F.is_junk("Midnight City"))
        self.assertFalse(F.is_junk("Starboy Official Audio"))

    def test_title_artist_and_duration(self):
        self.assertTrue(F.title_hit("M83 - Midnight City (Official Video)", "Midnight City"))
        self.assertFalse(F.title_hit("Nightcall", "Midnight City"))
        self.assertTrue(F.artist_hit("M83 - Topic", "M83"))
        self.assertTrue(F.artist_hit("The Weeknd", "The Weeknd"))
        self.assertFalse(F.artist_hit("Fake Covers", "The Weeknd"))
        self.assertTrue(F.duration_ok(180))
        self.assertTrue(F.duration_ok(90))
        self.assertFalse(F.duration_ok(30))
        self.assertFalse(F.duration_ok(900))
        self.assertFalse(F.duration_ok(None))

    def test_archive_rejects_path_traversal_names(self):
        ident = "good-id"
        meta = {"files": [{"name": "../etc/passwd", "format": "VBR MP3"},
                          {"name": "ok.mp3", "format": "VBR MP3"}]}
        with patch.object(F, "http_json", return_value=meta):
            url = F._archive_file_url(ident)
        self.assertEqual(url, "https://archive.org/download/good-id/ok.mp3")
        self.assertTrue(F.allowed_url(url))


class Order(unittest.TestCase):
    def test_ci_skips_youtube_first(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(F.source_order(), F.CI_ORDER)
            self.assertEqual(F.source_order()[0], "soundcloud")
            self.assertNotIn("youtube", F.source_order())
            self.assertEqual(F.source_order()[-1], "pixabay")

    def test_box_tries_youtube_first(self):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(F.source_order()[0], "youtube")

    def test_fetch_tries_sources_in_order_and_stops(self):
        calls = []

        def sc(t, a, e):
            calls.append("soundcloud")
            return {"id": "sc_ok", "url": "https://soundcloud.com/m83/midnight-city",
                    "channel": "M83", "source": "soundcloud"}

        def boom(name):
            def f(t, a, e):
                calls.append(name)
                raise AssertionError(f"{name} should not be called")
            return f

        sdir = Path(tempfile.mkdtemp())
        audio = sdir / "sc_ok.m4a"
        audio.write_bytes(b"0" * 2000)
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
             patch.dict(F.FINDERS, {"soundcloud": sc, "yt_mirrors": boom("yt_mirrors"),
                                    "archive": boom("archive"), "pixabay": boom("pixabay")}), \
             patch.object(F, "_ytdlp_audio", return_value=audio), \
             patch.object(F, "_probe_dur", return_value=180.0):
            rec = F.fetch_original({"title": "Midnight City", "artist": "M83"}, sdir, ["gta"], "test")
        self.assertEqual(calls, ["soundcloud"])
        self.assertEqual(rec["source"], "soundcloud")
        self.assertEqual(rec["title"], "Midnight City")

    def test_unsafe_terms_never_search(self):
        called = []
        with patch.dict(F.FINDERS, {k: (lambda *a, **k: called.append(1)) for k in F.FINDERS}):
            self.assertIsNone(F.fetch_original({"title": "--exec rm", "artist": "x"}, Path("/tmp"), ["gta"], "t"))
        self.assertEqual(called, [])

    def test_download_official_uses_fetch_original(self):
        with patch("pipeline_lib.song_fetch.fetch_original", return_value={"id": "x"}) as m:
            rec = download_official({"title": "A", "artist": "B"}, Path("/tmp"), ["gta"], "t")
        self.assertEqual(rec["id"], "x")
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
