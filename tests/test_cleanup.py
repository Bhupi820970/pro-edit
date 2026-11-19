#!/usr/bin/env python3
"""Stdlib tests for posted-video disk cleanup (no network, temp dirs only)."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.cleanup import (  # noqa: E402
    CleanupConfig, after_mark, cleanup_posted, has_keep_flag,
    is_inside, safe_delete, source_ids, sweep, CleanupResult,
)
from pipeline_lib.util import now_local  # noqa: E402
import pipeline as P  # noqa: E402


def _write(path: Path, size: int = 1000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class _FakeState:
    def __init__(self, videos):
        self.d = {"videos": videos}

    def save(self):
        pass


class CleanupHarness(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.home = root / "pipeline"
        self.box = root / "gta6"
        self.repo = root / "repo"
        for p in (
            self.home / "work", self.home / "sources", self.home / "sources" / "chunks",
            self.home / "prev", self.home / "ready" / "youtube", self.home / "songs",
            self.repo / "dl", self.repo / "cache", self.repo / "sources", self.repo / "song",
            self.box / "work", self.box / "demo_src" / "work", self.box / "demo_src" / "songs",
            self.box / "out" / "samples", self.box / "out" / "demos",
        ):
            p.mkdir(parents=True, exist_ok=True)
        self.cfg = CleanupConfig(
            home=self.home,
            allow=(
                self.home / "work", self.home / "sources", self.home / "prev",
                self.home / "ready", self.repo / "dl", self.repo / "cache",
                self.repo / "sources", self.box / "work", self.box / "demo_src" / "work",
                self.box / "out" / "demos",
            ),
            protected=(
                self.repo / "song", self.home / "songs",
                self.box / "demo_src" / "songs", self.box / "out" / "samples",
            ),
            box_root=self.box,
            final_keep=dt.timedelta(hours=24),
            temp_max_age=dt.timedelta(days=3),
        )

    def tearDown(self):
        self.td.cleanup()

    def _video(self, vid="yo-movie-20261119-120000", state="posted", keep=False,
               src_id="abcABC12345", **kw):
        src = self.home / "sources" / f"{src_id}.mkv"
        if not src.exists():
            _write(src, 4_000_000)
        final = self.home / "ready" / "youtube" / f"{vid}.mp4"
        if not final.exists():
            _write(final, 2_000_000)
        prev = self.home / "prev" / f"{vid}.mp4"
        if not prev.exists():
            _write(prev, 200_000)
        sidecar = Path(str(final) + ".json")
        if not sidecar.exists():
            sidecar.write_text("{}")
        v = {
            "id": vid, "platform": "youtube", "category": "movie", "state": state,
            "path": str(final), "preview": str(prev),
            "created": now_local().isoformat(),
            "posted_at": now_local().isoformat() if state == "posted" else None,
            "sources": [{"id": src_id, "src_id": src_id, "path": str(src), "title": "Scene"}],
            "keep": keep,
        }
        v.update(kw)
        return v

    def test_posted_deletes_sources_temp_and_preview_keeps_final(self):
        v = self._video()
        work = self.home / "work" / v["id"]
        _write(work / "clips" / "tmp.mp4", 50_000)
        _write(work / f"{v['id']}_wide.mp4", 80_000)
        state = _FakeState([v])
        result = cleanup_posted(v, state, cfg=self.cfg)
        self.assertFalse(Path(v["sources"][0]["path"]).exists())
        self.assertFalse((self.home / "prev" / f"{v['id']}.mp4").exists())
        self.assertFalse(work.exists())
        self.assertTrue(Path(v["path"]).exists(), "final render must stay for 24h")
        self.assertTrue(Path(str(v["path"]) + ".json").exists())
        self.assertGreater(result.freed_mb, 4.0)
        self.assertGreater(result.freed_bytes, 4_000_000)

    def test_source_shared_with_ready_backup_is_kept(self):
        posted = self._video("yo-movie-1", src_id="sharedClip01")
        ready = self._video("yo-movie-2", state="ready", src_id="sharedClip01")
        # same source path for both
        ready["sources"][0]["path"] = posted["sources"][0]["path"]
        state = _FakeState([posted, ready])
        cleanup_posted(posted, state, cfg=self.cfg)
        self.assertTrue(Path(posted["sources"][0]["path"]).exists(),
                        "backup-buffer source must not be deleted")
        self.assertTrue(Path(ready["path"]).exists())
        self.assertFalse(Path(posted["preview"]).exists())

    def test_path_outside_allowlist_is_not_deleted(self):
        v = self._video()
        secret = Path(self.td.name) / "outside" / "secret.mp4"
        _write(secret, 999)
        v["sources"][0]["path"] = str(secret)
        # also try to point work at it via a recorded path only
        state = _FakeState([v])
        cleanup_posted(v, state, cfg=self.cfg)
        self.assertTrue(secret.exists())
        # allowlisted source id file still goes away
        self.assertFalse((self.home / "sources" / "abcABC12345.mkv").exists())

    def test_symlink_to_outside_does_not_delete_target(self):
        v = self._video()
        secret = Path(self.td.name) / "outside" / "secret.mkv"
        _write(secret, 1234)
        link = self.home / "sources" / "abcABC12345.mkv"
        link.unlink()
        link.symlink_to(secret)
        v["sources"][0]["path"] = str(link)
        cleanup_posted(v, _FakeState([v]), cfg=self.cfg)
        self.assertTrue(secret.exists(), "symlink target outside allowlist must stay")
        self.assertFalse(link.exists() or link.is_symlink())

    def test_songs_and_approved_samples_are_never_deleted(self):
        song = _write(self.repo / "song" / "give_it_up.wav", 5000)
        cached_song = _write(self.home / "songs" / "give_it_up.m4a", 5000)
        demo_song = _write(self.box / "demo_src" / "songs" / "x.wav", 5000)
        sample = _write(self.box / "out" / "samples" / "logo_glass3.jpg", 5000)
        gta = _write(self.box / "out" / "demos" / "gta_give_it_up.mp4", 5000)
        gaming = _write(self.box / "out" / "demos" / "gaming_hook.mp4", 5000)
        # even if someone lists them as this video's "sources"
        v = self._video()
        v["sources"] = [
            {"id": "give_it_up", "path": str(song)},
            {"id": "logo_glass3", "path": str(sample)},
            {"id": "gta_give_it_up", "path": str(gta)},
            {"id": "gaming_hook", "path": str(gaming)},
        ]
        result = CleanupResult()
        for p in (song, cached_song, demo_song, sample, gta, gaming):
            safe_delete(p, self.cfg, result)
        cleanup_posted(v, _FakeState([v]), cfg=self.cfg)
        for p in (song, cached_song, demo_song, sample, gta, gaming):
            self.assertTrue(p.exists(), f"protected file vanished: {p}")

    def test_keep_flag_preserves_final_after_24h(self):
        old = now_local() - dt.timedelta(hours=30)
        v = self._video(keep=True)
        v["posted_at"] = old.isoformat()
        v["created"] = old.isoformat()
        state = _FakeState([v])
        self.assertTrue(has_keep_flag(v))
        sweep(state, cfg=self.cfg, now=now_local())
        self.assertTrue(Path(v["path"]).exists())

    def test_keep_sidecar_file_also_counts(self):
        old = now_local() - dt.timedelta(hours=30)
        v = self._video(keep=False)
        v["posted_at"] = old.isoformat()
        Path(v["path"]).with_suffix(".keep").write_text("1")
        sweep(_FakeState([v]), cfg=self.cfg, now=now_local())
        self.assertTrue(Path(v["path"]).exists())

    def test_posted_final_deleted_after_24h_without_keep(self):
        old = now_local() - dt.timedelta(hours=30)
        v = self._video(keep=False)
        v["posted_at"] = old.isoformat()
        v["created"] = old.isoformat()
        # preview already gone; final should be swept
        Path(v["preview"]).unlink()
        result = sweep(_FakeState([v]), cfg=self.cfg, now=now_local())
        self.assertFalse(Path(v["path"]).exists())
        self.assertFalse(Path(str(v["path"]) + ".json").exists())
        self.assertGreater(result.freed_mb, 1.0)

    def test_ready_backup_final_is_never_swept(self):
        old = now_local() - dt.timedelta(days=10)
        ready = self._video("yo-ready-1", state="ready")
        ready["created"] = old.isoformat()
        sweep(_FakeState([ready]), cfg=self.cfg, now=now_local())
        self.assertTrue(Path(ready["path"]).exists())

    def test_stale_work_older_than_3_days_is_swept(self):
        stale = _write(self.home / "work" / "oldjob" / "clip.mp4", 80_000)
        old = now_local().timestamp() - 4 * 24 * 3600
        os.utime(stale, (old, old))
        fresh = _write(self.home / "work" / "newjob" / "clip.mp4", 80_000)
        result = sweep(_FakeState([]), cfg=self.cfg, now=now_local())
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), "recent work must stay (in-progress)")
        self.assertGreater(result.freed_bytes, 70_000)

    def test_stale_work_referenced_by_pending_queue_is_kept(self):
        src = _write(self.home / "work" / "shared" / "clip01.mkv", 90_000)
        old = now_local().timestamp() - 5 * 24 * 3600
        os.utime(src, (old, old))
        ready = self._video("yo-ready-2", state="ready", src_id="clip01")
        ready["sources"][0]["path"] = str(src)
        sweep(_FakeState([ready]), cfg=self.cfg, now=now_local())
        self.assertTrue(src.exists())

    def test_allowlist_root_itself_is_not_deleted(self):
        result = CleanupResult()
        safe_delete(self.home / "sources", self.cfg, result)
        self.assertTrue((self.home / "sources").exists())

    def test_manifest_and_heartbeat_are_skipped(self):
        man = _write(self.home / "sources" / "manifest.json", 100)
        hb = _write(self.home / "sources" / "box_heartbeat.json", 100)
        v = self._video()
        cleanup_posted(v, _FakeState([v]), cfg=self.cfg)
        self.assertTrue(man.exists())
        self.assertTrue(hb.exists())

    def test_cache_and_dl_files_for_that_source_are_removed(self):
        v = self._video(src_id="clipZX00001")
        dl = _write(self.repo / "dl" / "clipZX00001.mkv", 300_000)
        wav = _write(self.repo / "cache" / "clipZX00001.wav", 50_000)
        tx = _write(self.repo / "cache" / "clipZX00001.tx.json", 200)
        other = _write(self.repo / "dl" / "unrelated99.mkv", 300_000)
        cleanup_posted(v, _FakeState([v]), cfg=self.cfg)
        self.assertFalse(dl.exists())
        self.assertFalse(wav.exists())
        self.assertFalse(tx.exists())
        self.assertTrue(other.exists())

    def test_source_ids_reject_path_traversal(self):
        self.assertEqual(source_ids({"sources": [{"id": "../etc/passwd"}, {"id": "okid"}]}), {"okid"})

    def test_from_env_tmp_home_does_not_allow_box_paths(self):
        with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(self.home)}):
            cfg = CleanupConfig.from_env()
        allow = {str(p) for p in cfg.allow}
        self.assertIn(str((self.home / "work").resolve()), allow)
        self.assertFalse(any(str(p).startswith("/workspace/gta6/work") for p in cfg.allow))
        self.assertFalse(any("/vicestyle/dl" in str(p) for p in cfg.allow))

    def test_is_inside_does_not_follow_escape_symlink_location(self):
        root = self.home / "sources"
        outside = Path(self.td.name) / "outside_dir"
        outside.mkdir()
        self.assertTrue(is_inside(root / "a.mkv", root))
        self.assertFalse(is_inside(outside / "a.mkv", root))

    def test_mark_posted_hook_and_keep(self):
        v = self._video("yo-movie-hook", state="ready")
        state_path = self.home / "state.json"
        state_path.write_text(json.dumps({"videos": [v], "patterns": [], "songs": [],
                                          "windows": [], "sources_used": [], "segments_used": [],
                                          "vet": {}, "listing_cache": {}, "song_fail": {}}))
        env = {"VICE_PIPELINE_HOME": str(self.home)}
        with patch.dict(os.environ, env):
            rc = P.main(["mark", "--id", "yo-movie-hook", "--state", "posted"])
        self.assertEqual(rc, 0)
        self.assertFalse(Path(v["sources"][0]["path"]).exists())
        self.assertFalse(Path(v["preview"]).exists())
        self.assertTrue(Path(v["path"]).exists())
        data = json.loads(state_path.read_text())
        rec = next(x for x in data["videos"] if x["id"] == "yo-movie-hook")
        self.assertEqual(rec["state"], "posted")
        self.assertIn("cleanup", rec)
        self.assertGreater(rec["cleanup"]["freed_mb"], 0)

    def test_mark_posted_keep_sets_flag(self):
        v = self._video("yo-movie-keep", state="ready", keep=False)
        state_path = self.home / "state.json"
        state_path.write_text(json.dumps({"videos": [v], "patterns": [], "songs": [],
                                          "windows": [], "sources_used": [], "segments_used": [],
                                          "vet": {}, "listing_cache": {}, "song_fail": {}}))
        with patch.dict(os.environ, {"VICE_PIPELINE_HOME": str(self.home)}):
            rc = P.main(["mark", "--id", "yo-movie-keep", "--state", "posted", "--keep"])
        self.assertEqual(rc, 0)
        rec = next(x for x in json.loads(state_path.read_text())["videos"] if x["id"] == "yo-movie-keep")
        self.assertTrue(rec.get("keep"))
        self.assertTrue(Path(v["path"]).exists())

    def test_after_mark_ignores_non_posted_states(self):
        v = self._video(state="approved")
        src = Path(v["sources"][0]["path"])
        result = after_mark(v, _FakeState([v]), cfg=self.cfg)
        self.assertTrue(src.exists())
        self.assertEqual(result.deleted, [])


if __name__ == "__main__":
    unittest.main()

