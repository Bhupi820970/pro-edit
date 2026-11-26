import json
import os
import tempfile
import unittest
from unittest.mock import patch

from pipeline_lib import gemini as G


def resp(st, body):
    return st, {}, json.dumps(body).encode()


OK = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
QUOTA = {"error": {"message": "You exceeded your current quota, please check your plan and billing details"}}
INVALID = {"error": {"message": "API key not valid. Please pass a valid API key."}}


class Keys(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GEMINI")}
        env.update({"VICE_PIPELINE_HOME": self.td.name, "GEMINI_API_KEY_1": "KEYAAA", "GEMINI_API_KEY_2": "KEYBBB",
                    "GEMINI_API_KEY_3": "KEYCCC"})
        self.p = patch.dict(os.environ, env, clear=True)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.td.cleanup()

    def client(self, handler):
        g = G.Gemini(models=["main-lite", "backup-lite"], log=lambda *a: None)
        g._available = {"main-lite", "backup-lite"}
        g._req = lambda key, method, url, *a, **k: handler(key, url)
        return g

    def test_invalid_key_skipped_others_continue(self):
        seen = []

        def h(key, url):
            seen.append((key, url.rsplit("/", 1)[-1]))
            if key == "KEYAAA":
                return resp(400, INVALID)
            return resp(200, OK)
        g = self.client(h)
        self.assertEqual(g.generate([{"text": "x"}]), "hi")
        self.assertIn("key1", g.invalid)
        self.assertEqual(seen[1][0], "KEYBBB")
        # next call never touches the invalid key
        seen.clear()
        g.generate([{"text": "x"}])
        self.assertNotIn("KEYAAA", [k for k, _ in seen])

    def test_quota_on_main_model_falls_back_to_backup_on_same_keys(self):
        calls = []

        def h(key, url):
            model = url.rsplit("/", 1)[-1].split(":")[0]
            calls.append((key, model))
            return resp(429, QUOTA) if model == "main-lite" else resp(200, OK)
        g = self.client(h)
        self.assertEqual(g.generate([{"text": "x"}]), "hi")
        self.assertEqual([m for _, m in calls].count("main-lite"), 3)   # every key tried on the main model
        self.assertEqual(calls[-1], ("KEYAAA", "backup-lite"))               # then the backup on the same key
        self.assertFalse(g.quota_exhausted())                            # backup still has quota
        self.assertTrue(g._combo_dead("key1", "main-lite"))
        self.assertFalse(g._combo_dead("key1", "backup-lite"))

    def test_everything_out_reports_exhausted(self):
        g = self.client(lambda key, url: resp(429, QUOTA))
        with self.assertRaises(G.GeminiError):
            g.generate([{"text": "x"}])
        self.assertTrue(g.quota_exhausted())
        with self.assertRaises(G.GeminiError):   # no more HTTP calls once everything is parked
            g.generate([{"text": "x"}])
        st = g.status()
        self.assertTrue(st["exhausted"])
        self.assertNotIn("KEYAAA", json.dumps(st))


if __name__ == "__main__":
    unittest.main()


class MissingSlots(unittest.TestCase):
    def test_missing_slot_11_is_skipped_and_12_keys_work(self):
        import os
        from unittest.mock import patch
        from pipeline_lib import gemini as G
        env = {f"GEMINI_API_KEY_{i}": f"k{i}x" for i in range(1, 14) if i != 11}
        env["GEMINI_API_KEY_11"] = ""          # deleted secret -> empty env value in Actions
        with patch.dict(os.environ, env, clear=True):
            labels = [k for k, _ in G._keys()]
        self.assertEqual(len(labels), 12)
        self.assertNotIn("key11", labels)
        self.assertIn("key12", labels)
