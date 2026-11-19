#!/usr/bin/env python3
"""Stdlib unit tests for queue trust + URL/query validation (no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.queue_trust import (  # noqa: E402
    OWNER, REPO, identity, is_owner_commit, is_workflow_bot_commit,
    parse_state_message, run_is_trusted, trust_queue_commit, on_pipeline_state,
)
from box_fetch import safe_url, safe_query, safe_request  # noqa: E402


def bot_commit(**kw):
    base = {
        "sha": "d15f9fd307937a19113a406df94014c8e167d8dc",
        "author": {"login": "actions"},
        "committer": {"login": "actions"},
        "commit": {
            "author": {
                "name": "vice-pipeline-bot",
                "email": "actions@users.noreply.github.com",
                "date": "2026-11-19T04:05:56Z",
            },
            "committer": {
                "name": "vice-pipeline-bot",
                "email": "actions@users.noreply.github.com",
                "date": "2026-11-19T04:05:56Z",
            },
            "message": "state: youtube run 44444329130",
        },
    }
    base.update(kw)
    return base


def good_run(**kw):
    base = {
        "id": 44444329130,
        "event": "workflow_dispatch",
        "path": ".github/workflows/produce.yml",
        "head_branch": "main",
        "triggering_actor": {"login": OWNER},
        "actor": {"login": OWNER},
        "repository": {"full_name": REPO},
        "created_at": "2026-11-19T02:26:28Z",
        "started_at": "2026-11-19T02:26:30Z",
        "updated_at": "2026-11-19T04:44:38Z",
        "completed_at": "2026-11-19T04:44:38Z",
    }
    base.update(kw)
    return base


def compare_ok():
    return {"status": "ahead", "ahead_by": 1, "behind_by": 0}


class TestIdentity(unittest.TestCase):
    def test_bot_identity(self):
        self.assertTrue(is_workflow_bot_commit(bot_commit()))
        self.assertFalse(is_owner_commit(bot_commit()))

    def test_standard_actions_bot(self):
        c = bot_commit()
        c["author"] = {"login": "github-actions[bot]"}
        c["committer"] = {"login": "github-actions[bot]"}
        c["commit"]["author"]["name"] = "github-actions[bot]"
        c["commit"]["author"]["email"] = "41898282+github-actions[bot]@users.noreply.github.com"
        c["commit"]["committer"] = dict(c["commit"]["author"])
        self.assertTrue(is_workflow_bot_commit(c))

    def test_login_actions_alone_is_not_enough(self):
        c = bot_commit()
        c["commit"]["author"]["name"] = "hacker"
        c["commit"]["committer"]["name"] = "hacker"
        c["commit"]["author"]["email"] = "hacker@example.com"
        c["commit"]["committer"]["email"] = "hacker@example.com"
        self.assertFalse(is_workflow_bot_commit(c))

    def test_spoofed_name_without_actions_email_fails(self):
        c = bot_commit()
        c["commit"]["author"]["email"] = "not-actions@example.com"
        c["commit"]["committer"]["email"] = "not-actions@example.com"
        self.assertFalse(is_workflow_bot_commit(c))

    def test_owner(self):
        c = bot_commit()
        c["author"] = {"login": OWNER}
        c["committer"] = {"login": OWNER}
        c["commit"]["author"]["name"] = "Bhupi820970"
        c["commit"]["author"]["email"] = "Bhupi820970@users.noreply.github.com"
        self.assertTrue(is_owner_commit(c))


class TestMessage(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parse_state_message("state: youtube run 44444329130", "youtube/fetch_queue.json"),
                         ("youtube", "44444329130"))

    def test_platform_mismatch(self):
        self.assertIsNone(parse_state_message("state: youtube run 44444329130", "instagram/fetch_queue.json"))

    def test_garbage(self):
        self.assertIsNone(parse_state_message("hack the box", "youtube/fetch_queue.json"))
        self.assertIsNone(parse_state_message("state: youtube run 12", "youtube/fetch_queue.json"))


class TestRunTrust(unittest.TestCase):
    def test_good(self):
        ok, _ = run_is_trusted(good_run(), bot_commit(), REPO, "44444329130")
        self.assertTrue(ok)

    def test_pr_event_rejected(self):
        ok, reason = run_is_trusted(good_run(event="pull_request"), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)
        self.assertIn("event", reason)

    def test_pull_request_target_rejected(self):
        ok, _ = run_is_trusted(good_run(event="pull_request_target"), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)

    def test_wrong_workflow(self):
        ok, _ = run_is_trusted(good_run(path=".github/workflows/evil.yml"), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)

    def test_wrong_actor(self):
        ok, _ = run_is_trusted(good_run(triggering_actor={"login": "someone-else"}), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)

    def test_wrong_repo(self):
        ok, _ = run_is_trusted(good_run(repository={"full_name": "evil/fork"}), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)

    def test_not_main(self):
        ok, _ = run_is_trusted(good_run(head_branch="attacker-branch"), bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)

    def test_outside_window(self):
        c = bot_commit()
        c["commit"]["author"]["date"] = "2026-11-18T00:00:00Z"
        ok, reason = run_is_trusted(good_run(), c, REPO, "44444329130")
        self.assertFalse(ok)
        self.assertIn("window", reason)

    def test_missing_run(self):
        ok, _ = run_is_trusted(None, bot_commit(), REPO, "44444329130")
        self.assertFalse(ok)


class TestTrustQueueCommit(unittest.TestCase):
    def _ok_deps(self, run=None):
        r = run or good_run()
        return dict(get_run=lambda repo, rid: r, get_compare=lambda repo, sha: compare_ok())

    def test_bot_with_matching_run_accepted(self):
        ok, reason = trust_queue_commit(bot_commit(), "youtube/fetch_queue.json", REPO, **self._ok_deps())
        self.assertTrue(ok, reason)

    def test_bot_without_run_rejected(self):
        ok, _ = trust_queue_commit(bot_commit(), "youtube/fetch_queue.json", REPO,
                                   get_run=lambda repo, rid: None,
                                   get_compare=lambda repo, sha: compare_ok())
        self.assertFalse(ok)

    def test_not_on_branch_rejected(self):
        ok, reason = trust_queue_commit(
            bot_commit(), "youtube/fetch_queue.json", REPO,
            get_run=lambda repo, rid: good_run(),
            get_compare=lambda repo, sha: {"status": "diverged", "behind_by": 3, "ahead_by": 1},
        )
        self.assertFalse(ok)
        self.assertIn("not on branch", reason)

    def test_owner_skips_run_lookup(self):
        c = bot_commit()
        c["author"] = {"login": OWNER}
        c["committer"] = {"login": OWNER}
        called = []

        def get_run(repo, rid):
            called.append(rid)
            return None

        ok, _ = trust_queue_commit(c, "youtube/fetch_queue.json", REPO,
                                   get_run=get_run, get_compare=lambda repo, sha: compare_ok())
        self.assertTrue(ok)
        self.assertEqual(called, [])

    def test_unknown_login_rejected(self):
        c = bot_commit()
        c["author"] = {"login": "actions"}
        c["commit"]["author"]["name"] = "not-the-bot"
        c["commit"]["committer"]["name"] = "not-the-bot"
        ok, reason = trust_queue_commit(c, "youtube/fetch_queue.json", REPO, **self._ok_deps())
        self.assertFalse(ok)
        self.assertIn("not a trusted", reason)

    def test_on_pipeline_state_helper(self):
        self.assertTrue(on_pipeline_state(compare_ok()))
        self.assertTrue(on_pipeline_state({"status": "identical", "behind_by": 0}))
        self.assertFalse(on_pipeline_state({"status": "behind", "behind_by": 2}))
        self.assertFalse(on_pipeline_state(None))


class TestSafeValidation(unittest.TestCase):
    def test_good_youtube(self):
        self.assertTrue(safe_url("https://www.youtube.com/watch?v=QdBZY2fkU-0"))
        self.assertTrue(safe_url("https://youtu.be/QdBZY2fkU-0"))

    def test_rejects_shell_and_other_hosts(self):
        self.assertIsNone(safe_url("https://evil.example/watch?v=QdBZY2fkU-0"))
        self.assertIsNone(safe_url("https://www.youtube.com/watch?v=QdBZY2fkU-0;rm -rf /"))
        self.assertIsNone(safe_url("--cookies-from-browser=chrome"))
        self.assertIsNone(safe_url("https://www.youtube.com/watch?v=$(id)"))

    def test_query_and_request(self):
        self.assertTrue(safe_query("Grand Theft Auto VI Trailer 1 Rockstar Games"))
        self.assertIsNone(safe_query("foo; rm -rf /"))
        req = safe_request({"id": "youtube-202611190935-gta", "category": "gta", "limit": 3,
                            "queries": ["Grand Theft Auto VI Trailer 1 Rockstar Games", "bad;query"],
                            "links": ["https://www.youtube.com/watch?v=QdBZY2fkU-0", "https://evil.test/x"]})
        self.assertEqual(req["limit"], 3)
        self.assertEqual(req["links"], ["https://www.youtube.com/watch?v=QdBZY2fkU-0"])
        self.assertEqual(req["queries"], ["Grand Theft Auto VI Trailer 1 Rockstar Games"])
        self.assertIsNone(safe_request({"id": "x", "category": "not-a-category"}))


if __name__ == "__main__":
    unittest.main()

