"""Gemini quota out on every key + backup model -> runs end fast, watchdog waits; hard job caps (26 Nov:
a produce job spent 2 h making 0 videos while every key/model was out until 13:30 IST)."""
import time
import unittest
from pathlib import Path

import yaml

from pipeline_lib import quota_gate as Q

ROOT = Path(__file__).resolve().parent.parent
MODELS = ["m1", "m2"]
NOW = 1_000_000.0
LATER = NOW + 3600


class Gate(unittest.TestCase):
    def test_all_pairs_dead_is_blocked_until_first_reset(self):
        doc = {"dead_until": {"key1|m1": LATER, "key1|m2": LATER, "key2": LATER + 60}}
        self.assertEqual(Q.blocked_until([doc], ["key1", "key2"], MODELS, NOW), LATER)

    def test_one_live_pair_is_not_blocked(self):
        doc = {"dead_until": {"key1|m1": LATER, "key1|m2": LATER}}
        self.assertIsNone(Q.blocked_until([doc], ["key1", "key2"], MODELS, NOW))   # key2 untouched
        doc2 = {"dead_until": {"key1|m1": LATER}}
        self.assertIsNone(Q.blocked_until([doc2], ["key1"], MODELS, NOW))          # m2 still free

    def test_expired_and_invalid_and_merge_across_platform_files(self):
        yt = {"dead_until": {"key1|m1": NOW - 1, "key1|m2": LATER}}
        ig = {"dead_until": {"key1|m1": LATER}, "invalid": {"key2": LATER}}
        self.assertIsNone(Q.blocked_until([yt], ["key1"], MODELS, NOW))            # m1 reset already
        self.assertEqual(Q.blocked_until([yt, ig], ["key1", "key2"], MODELS, NOW), LATER)

    def test_watchdog_without_key_env_uses_labels_in_files(self):
        doc = {"dead_until": {"key1|m1": LATER, "key1|m2": LATER}}
        self.assertEqual(Q.blocked_until([doc], None, MODELS, NOW), LATER)
        self.assertIsNone(Q.blocked_until([], None, MODELS, NOW))

    def test_check_formats_ist(self):
        from pipeline_lib.gemini import DEFAULT_MODELS
        until = time.time() + 7200
        doc = {"dead_until": {f"key1|{m}": until for m in DEFAULT_MODELS}}
        blocked, when = Q.check(["key1"], [doc])
        self.assertTrue(blocked)
        self.assertTrue(when.endswith(" IST"))


class Caps(unittest.TestCase):
    def test_attempt_cap_is_not_swallowed_by_except_exception(self):
        import pipeline
        self.assertFalse(issubclass(pipeline.AttemptTimeout, Exception))
        try:
            try:
                raise pipeline.AttemptTimeout("cap")
            except Exception:          # what sourcing / vetting code does
                self.fail("cap swallowed")
        except pipeline.AttemptTimeout:
            pass

    def test_produce_fails_fast_and_never_starts_a_late_attempt(self):
        src = (ROOT / "pipeline.py").read_text()
        self.assertIn("if gem.quota_exhausted() and os.environ.get(\"VICE_IGNORE_QUOTA_GATE\"", src)
        self.assertIn("quota_blocked", src)
        self.assertIn("left < attempt_cap", src)
        self.assertIn("except (Exception, AttemptTimeout) as ex", src)

    def test_workflow_gate_and_hard_caps(self):
        wf = yaml.safe_load((ROOT / ".github/workflows/produce.yml").read_text())
        plan, prod = wf["jobs"]["plan"], wf["jobs"]["produce"]
        self.assertIn("quota_gate check", " ".join(s.get("run", "") for s in plan["steps"]))
        self.assertIn("ignore_quota_gate", wf[True]["workflow_dispatch"]["inputs"])
        self.assertIn("needs.plan.outputs.blocked", prod["if"])
        self.assertLessEqual(prod["timeout-minutes"], 100)
        steps = {s.get("name"): s for s in prod["steps"]}
        self.assertLessEqual(steps["Produce"]["timeout-minutes"], 75)
        self.assertLessEqual(steps["Restore pipeline state (branch pipeline-state)"]["timeout-minutes"], 20)
        self.assertNotIn("max-parallel", prod.get("strategy", {}))      # Instagram + YouTube in parallel
        self.assertIn('["instagram","youtube"]', " ".join(s.get("run", "") for s in plan["steps"]))
        self.assertIn("'60'", steps["Produce"]["run"])


if __name__ == "__main__":
    unittest.main()
