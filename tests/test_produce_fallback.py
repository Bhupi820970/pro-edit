#!/usr/bin/env python3
"""Box-first sourcing, runner bot-check circuit breaker, category / star-player fallback,
Gemini-quota manual-pick queue, and the box pool's usable-clip rules. Stdlib + mocks only."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

CFG = json.loads((HERE / "config" / "pipeline.json").read_text())


def _env(td):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GEMINI_API_KEY", "VICE_"))}
    env["VICE_PIPELINE_HOME"] = str(td)
    return env


class PickCategoryTests(unittest.TestCase):
    def _state(self, videos):
        return SimpleNamespace(produced_today=lambda platform: videos)

    def test_qa_failed_renders_do_not_use_up_the_daily_mix(self):
        import pipeline as P
        # 6 QA-failed GTA renders today (the Nov 26 bug forced "gaming" for every attempt)
        vids = [{"category": "gta", "state": "qa_failed"} for _ in range(6)]
        picks = {P.pick_category(self._state(vids), "youtube") for _ in range(30)}
        self.assertIn("gta", picks)

    def test_skip_falls_back_to_another_category(self):
        import pipeline as P
        vids = [{"category": "gta", "state": "ready"} for _ in range(2)] + \
               [{"category": "movie", "state": "ready"} for _ in range(2)] + [{"category": "romance", "state": "ready"}]
        st = self._state(vids)
        self.assertEqual(P.pick_category(st, "instagram"), "sports")      # sports minimum unfilled
        for _ in range(20):
            self.assertNotEqual(P.pick_category(st, "instagram", {"sports"}), "sports")

    def test_all_skipped_returns_none(self):
        import pipeline as P
        allc = {"gta", "gaming", "movie", "series", "marvel", "dc", "anime", "sports", "celebrity", "comedy", "music", "podcast"}
        self.assertIsNone(P.pick_category(self._state([]), "youtube", allc))


class CmdProduceFallbackTests(unittest.TestCase):
    def test_dead_category_is_skipped_and_run_moves_on(self):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            calls = []

            def fake_produce(platform, cat, state, gem, sourcer, seed, timer=None):
                calls.append(cat)
                if cat == "sports":
                    raise P.CategoryUnavailable("no clips of a single star player for sports", cat)
                return {"id": f"in-{cat}-{len(calls)}", "category": cat}

            gem = MagicMock()
            gem.quota_exhausted.return_value = False
            gem.calls = []
            src = MagicMock()
            src.stats = {"thin": []}
            src.manual_pick_doc.return_value = {"platform": "instagram", "categories": {}}
            a = SimpleNamespace(platform="instagram", category="sports", max_minutes=0, target=7)
            with patch.object(P, "produce_one", side_effect=fake_produce), \
                    patch("pipeline_lib.gemini.Gemini", return_value=gem), \
                    patch("pipeline_lib.sourcing.Sourcer", return_value=src), \
                    patch.object(P, "queue_fetch"), patch("pipeline_lib.cleanup.sweep"):
                rc = P.cmd_produce(a, count=2)
            self.assertEqual(rc, 0)
            self.assertEqual(calls[:1], ["sports"])          # one miss, then straight to another topic
            self.assertTrue(all(c != "sports" for c in calls[1:]))
            self.assertEqual(len([c for c in calls if c != "sports"]), 2)
            doc = json.loads((Path(td) / "queue" / "needs_manual_pick.json").read_text())
            self.assertEqual(doc["categories"], {})

    def test_quota_out_skips_after_one_miss_and_writes_manual_pick(self):
        import pipeline as P
        # manual override (ignore_quota_gate): cached vets + manual picks are still tried
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {**_env(td), "VICE_IGNORE_QUOTA_GATE": "true"}, clear=True):
            calls = []

            def fake_produce(platform, cat, state, gem, sourcer, seed, timer=None):
                calls.append(cat)
                raise P.CategoryUnavailable(f"only 0 clean sources for {cat}", cat)

            gem = MagicMock()
            gem.quota_exhausted.return_value = True
            gem.calls = []
            src = MagicMock()
            src.stats = {"thin": []}
            src.manual_pick_doc.return_value = {
                "platform": "youtube", "categories": {"gaming": [{"id": "abcdefghijk", "url": "u"}]}}
            a = SimpleNamespace(platform="youtube", category=None, max_minutes=0, target=7)
            with patch.object(P, "produce_one", side_effect=fake_produce), \
                    patch("pipeline_lib.gemini.Gemini", return_value=gem), \
                    patch("pipeline_lib.sourcing.Sourcer", return_value=src), \
                    patch.object(P, "queue_fetch"), patch("pipeline_lib.cleanup.sweep"):
                P.cmd_produce(a, until_target=True)
            # each topic tried once (mix first, then the fallback topics), then stop
            self.assertEqual(sorted(calls[:2]), ["gaming", "gta"])
            self.assertEqual(len(calls), len(set(calls)))
            doc = json.loads((Path(td) / "queue" / "needs_manual_pick.json").read_text())
            self.assertEqual(doc["categories"]["gaming"][0]["id"], "abcdefghijk")

    def test_quota_out_ends_run_fast_without_attempts(self):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            gem = MagicMock()
            gem.quota_exhausted.return_value = True
            gem.status.return_value = {"exhausted": True, "reset_at": "2026-11-27 13:30 IST"}
            gem.calls = []
            a = SimpleNamespace(platform="youtube", category=None, max_minutes=60, target=7)
            with patch.object(P, "produce_one") as po, \
                    patch("pipeline_lib.gemini.Gemini", return_value=gem), \
                    patch("pipeline_lib.sourcing.Sourcer", return_value=MagicMock()):
                rc = P.cmd_produce(a, until_target=True)
            po.assert_not_called()
            self.assertEqual(rc, 0)
            st = json.loads((Path(td) / "status.json").read_text())
            self.assertTrue(st["last_run"]["quota_blocked"])
            self.assertEqual(st["last_run"]["gemini"]["reset_at"], "2026-11-27 13:30 IST")

    def test_each_finished_video_goes_to_stock_right_away(self):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td, \
                patch.dict(os.environ, {**_env(td), "VICE_ATTACH_EACH_TAG": "vc-ready"}, clear=True):
            gem = MagicMock()
            gem.quota_exhausted.return_value = False
            gem.calls = []
            src = MagicMock()
            src.stats = {"thin": []}
            src.manual_pick_doc.return_value = {"platform": "youtube", "categories": {}}
            a = SimpleNamespace(platform="youtube", category="gta", max_minutes=0, target=7)
            with patch.object(P, "produce_one", side_effect=lambda *x: {"id": "v", "category": "gta"}), \
                    patch("pipeline_lib.gemini.Gemini", return_value=gem), \
                    patch("pipeline_lib.sourcing.Sourcer", return_value=src), \
                    patch.object(P, "attach_release") as att, \
                    patch.object(P, "queue_fetch"), patch("pipeline_lib.cleanup.sweep"):
                P.cmd_produce(a, count=2)
            self.assertEqual(att.call_count, 2)
            self.assertEqual(att.call_args.args[1], "vc-ready")

    def test_merge_manual_pick_per_platform(self):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td:
            a, b, out = Path(td) / "a.json", Path(td) / "b.json", Path(td) / "q" / "n.json"
            a.write_text(json.dumps({"platform": "youtube", "categories": {"gta": [{"id": "x"}]}}))
            b.write_text(json.dumps({"platform": "instagram", "categories": {}}))
            P.merge_manual_pick([str(a)], out)
            m = P.merge_manual_pick([str(b)], out)
            self.assertEqual(set(m["platforms"]), {"youtube", "instagram"})
            self.assertEqual(m["waiting"], 1)


class SourcerTests(unittest.TestCase):
    def _sourcer(self, td, quota=False):
        from pipeline_lib.state import State
        from pipeline_lib.sourcing import Sourcer
        gem = MagicMock()
        gem.quota_exhausted.return_value = quota
        return Sourcer(CFG, State(), gem), gem

    def test_runner_stops_downloading_after_bot_checks(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {**_env(td), "VICE_RUNNER_BOT_LIMIT": "2"},
                                                            clear=True):
            from pipeline_lib import sourcing as S
            src, _ = self._sourcer(td)
            with patch.object(S, "ytdlp", return_value=(False, "Sign in to confirm you're not a bot")) as y:
                for i in range(5):
                    self.assertIsNone(src.download({"id": f"vid{i:08d}", "url": "https://www.youtube.com/watch?v=x"}))
            self.assertEqual(y.call_count, 2)
            self.assertTrue(src.runner_blocked)
            self.assertEqual(src.stats["runner_skipped"], 3)

    def test_box_clips_ranked_first_and_blocked_runner_skips_remote(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            src, gem = self._sourcer(td)
            (src.dir / "boxclip0001.mkv").write_bytes(b"0" * 200_000)
            remote = {"id": "remote00001", "title": "Official", "channel": "Rockstar Games",
                      "url": "https://www.youtube.com/watch?v=remote00001", "via": "official-channel"}
            box = {"id": "boxclip0001", "title": "Official", "channel": "Rockstar Games",
                   "url": "https://www.youtube.com/watch?v=boxclip0001", "via": "box-fetch"}
            src.runner_blocked = True
            seen = []
            src.candidates = lambda cat, fb=False: [remote, box]
            src.pre_vet = MagicMock(return_value=True)

            def vet(e, path, cat):
                seen.append(e["id"])
                return {"accept": True, "duration": 60.0, "has_dialogue": True}
            src.vet = vet
            src.chunk_long = lambda sid, path, dur: [(sid, path)]
            got = src.gather("gta", 1, 1)
            self.assertEqual([g["id"] for g in got], ["boxclip0001"])
            self.assertEqual(seen, ["boxclip0001"])
            src.pre_vet.assert_not_called()           # no Gemini spent on clips the runner can't fetch

    def test_sports_falls_back_to_next_star_player(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            src, _ = self._sourcer(td)
            for sid in ("lebron00001", "lebron00002", "messi000001"):
                (src.dir / f"{sid}.mkv").write_bytes(b"0" * 200_000)
            c = [{"id": "lebron00001", "title": "LeBron James highlights", "channel": "NBA", "via": "box-fetch",
                  "url": "https://www.youtube.com/watch?v=lebron00001"},
                 {"id": "lebron00002", "title": "LeBron James interview", "channel": "NBA", "via": "box-fetch",
                  "url": "https://www.youtube.com/watch?v=lebron00002"},
                 {"id": "messi000001", "title": "Lionel Messi interview", "channel": "Inter Miami", "via": "box-fetch",
                  "url": "https://www.youtube.com/watch?v=messi000001"}]
            src.candidates = lambda cat, fb=False: c
            # every LeBron clip has a logo (rejected) -> top group empty -> Messi
            src.vet = lambda e, p, cat: {"accept": e["id"].startswith("messi"), "duration": 60.0,
                                         "has_dialogue": True}
            src.chunk_long = lambda sid, path, dur: [(sid, path)]
            got = src.gather("sports", 3, 3)
            self.assertEqual([g["src_id"] for g in got], ["messi000001"])

    def test_quota_out_records_manual_pick_candidates(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            from pipeline_lib.gemini import GeminiError
            src, _ = self._sourcer(td, quota=True)
            e = {"id": "abcdefghijk", "title": "GTA Online official", "channel": "Rockstar Games",
                 "url": "https://www.youtube.com/watch?v=abcdefghijk", "via": "official-channel"}
            with self.assertRaises(GeminiError):
                src.pre_vet(e, "gta")
            doc = src.manual_pick_doc("youtube")
            self.assertEqual(doc["categories"]["gta"][0]["id"], "abcdefghijk")
            self.assertIn("manual_picks.json", doc["how_to_pick"])

    def test_manual_picks_validated_and_used_without_gemini(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            q = Path(td) / "queue"
            q.mkdir()
            (q / "manual_picks.json").write_text(json.dumps({"picks": [
                {"id": "goodclip001", "category": "gta", "accept": True, "has_dialogue": True},
                {"id": "bad;rm -rf", "category": "gta", "accept": True},
                {"id": "badcat00001", "category": "nope", "accept": True},
                {"id": "notbool0001", "category": "gta", "accept": "yes"},
            ]}))
            from pipeline_lib.sourcing import load_manual_picks
            picks = load_manual_picks(CFG)
            self.assertEqual(list(picks), ["goodclip001"])
            src, gem = self._sourcer(td, quota=True)
            self.assertTrue(src.pre_vet({"id": "goodclip001"}, "gta"))
            gem.generate.assert_not_called()

    def test_js_runtime_only_on_box_and_validated(self):
        from pipeline_lib.sourcing import ytdlp_js_args
        with patch.dict(os.environ, {"VICE_YTDLP_JS_RUNTIME": "deno:/home/box/.deno/bin/deno"}, clear=True):
            self.assertEqual(ytdlp_js_args(), ["--js-runtimes", "deno:/home/box/.deno/bin/deno"])
        with patch.dict(os.environ, {"VICE_YTDLP_JS_RUNTIME": "deno", "GITHUB_ACTIONS": "true"}, clear=True):
            self.assertEqual(ytdlp_js_args(), [])
        with patch.dict(os.environ, {"VICE_YTDLP_JS_RUNTIME": "--exec rm"}, clear=True):
            self.assertEqual(ytdlp_js_args(), [])


class BoxPoolRuleTests(unittest.TestCase):
    def test_usable_for_matches_runner_rules(self):
        from box_fetch import usable_for
        self.assertFalse(usable_for("gaming", "Popucom - Launch Trailer | PS5 Games"))
        self.assertFalse(usable_for("gaming", "Grand Theft Auto VI gameplay"))
        self.assertTrue(usable_for("gaming", "Marvel's Spider-Man 2 official gameplay"))
        self.assertTrue(usable_for("gaming", "Elden Ring - Official Gameplay Reveal Trailer"))   # top-10 + trailer ok
        self.assertFalse(usable_for("gaming", "Halo Infinite official gameplay"))                 # not a top-10 game
        self.assertFalse(usable_for("sports", "ROCKETS AT NUGGETS | FULL GAME HIGHLIGHTS", "NBA"))
        self.assertTrue(usable_for("sports", "LeBron James postgame interview", "Lakers"))

    def test_pool_counts_only_usable_unrejected_release_clips(self):
        from box_fetch import rejected_from_states, usable_pool
        man = {"clips": [
            {"id": "aaaaaaaaaaa", "file": "aaaaaaaaaaa.mkv", "category": "gaming", "title": "Elden Ring official gameplay"},
            {"id": "bbbbbbbbbbb", "file": "bbbbbbbbbbb.mkv", "category": "gaming", "title": "Elden Ring Teaser"},
            {"id": "ccccccccccc", "file": "ccccccccccc.mkv", "category": "gaming", "title": "Elden Ring cinematic gameplay"},
            {"id": "ddddddddddd", "file": "ddddddddddd.mkv", "category": "gaming", "title": "Elden Ring gameplay part 2"},
        ]}
        assets = {"aaaaaaaaaaa.mkv", "bbbbbbbbbbb.mkv", "ccccccccccc.mkv"}      # ddd not uploaded
        rej = rejected_from_states([{"vet": {"ccccccccccc": {"accept": False}, "aaaaaaaaaaa": {"accept": True}}},
                                    "garbage", {"vet": []}])
        self.assertEqual(rej, {"ccccccccccc"})
        self.assertEqual(usable_pool(man, assets, rej, "gaming"), {"aaaaaaaaaaa"})


if __name__ == "__main__":
    unittest.main()


class QuotaOutFallbackTests(unittest.TestCase):
    def test_scheduled_run_quota_out_still_falls_back_to_other_category(self):
        import pipeline as P
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, _env(td), clear=True):
            calls = []

            def fake_produce(platform, cat, state, gem, sourcer, seed, timer=None):
                calls.append(cat)
                if cat == "gta":
                    raise P.CategoryUnavailable("only 0 clean sources for gta", cat)
                return {"id": f"in-{cat}-{len(calls)}", "category": cat}

            gem = MagicMock()
            qcalls = []
            gem.quota_exhausted.side_effect = lambda: (qcalls.append(1) or len(qcalls) > 1)   # runs out mid-run
            gem.calls = []
            src = MagicMock()
            src.stats = {"thin": []}
            src.manual_pick_doc.return_value = {"platform": "instagram", "categories": {}}
            a = SimpleNamespace(platform="instagram", category="gta", max_minutes=0, target=7)
            with patch.object(P, "produce_one", side_effect=fake_produce), \
                    patch("pipeline_lib.gemini.Gemini", return_value=gem), \
                    patch("pipeline_lib.sourcing.Sourcer", return_value=src), \
                    patch.object(P, "queue_fetch"), patch("pipeline_lib.cleanup.sweep"):
                P.cmd_produce(a, count=1)
            self.assertEqual(calls[0], "gta")
            self.assertTrue(len(calls) >= 2 and calls[1] != "gta")


class PlatformFallbackTopicTests(unittest.TestCase):
    def test_youtube_uses_fallback_topics_only_when_mix_is_dead(self):
        import pipeline as P
        st = MagicMock()
        st.produced_today.return_value = []
        self.assertIn(P.pick_category(st, "youtube"), ("gta", "gaming"))
        self.assertEqual(P.pick_category(st, "youtube", {"gta", "gaming"}), "movie")
        self.assertEqual(P.pick_category(st, "youtube", {"gta", "gaming", "movie"}), "series")
