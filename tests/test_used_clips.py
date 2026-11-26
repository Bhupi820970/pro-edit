import json, tempfile, unittest
from pathlib import Path
from pipeline_lib import used_clips as UC


class UsedClipsTest(unittest.TestCase):
    def test_roundtrip_merge_and_chunk_ids(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.json", Path(d) / "b.json"
            UC.write_ledger({"HqZXw5M6qQY": {"id": "HqZXw5M6qQY", "videos": ["v1"], "url": "u"}}, paths=(a,))
            UC.write_ledger({"HqZXw5M6qQY": {"id": "HqZXw5M6qQY", "videos": ["v2"]},
                             "bad id!": {"id": "bad id!", "videos": []}}, paths=(b,))
            e = UC.load_entries((a, b))
            self.assertEqual(e["HqZXw5M6qQY"]["videos"], ["v1", "v2"])
            self.assertNotIn("bad id!", e)
            used = UC.used_ids((a, b))
            self.assertTrue(UC.is_used("HqZXw5M6qQY_c3", used))
            self.assertFalse(UC.is_used("zzzzzzzzzzz", used))

    def test_missing_or_garbage_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("{nope")
            self.assertEqual(UC.used_ids((p, Path(d) / "none.json")), set())


if __name__ == "__main__":
    unittest.main()
