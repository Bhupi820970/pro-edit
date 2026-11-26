"""26 Nov: every YouTube (wide) video failed QA on resolution (QA wanted 2160x3840 for wide too), and
parallel workers cutting the same looped segment raced on one shared .part file."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RenderFixes(unittest.TestCase):
    def test_qa_wants_4k_per_format(self):
        src = (ROOT / "pipeline_lib" / "qa.py").read_text()
        self.assertIn('want = (2160, 3840) if fmt == "shorts" else (3840, 2160)', src)

    def test_wide_gets_a_4k_master(self):
        src = (ROOT / "pipeline.py").read_text()
        self.assertIn("V.make_4k_wide(wide, out)", src)
        self.assertIn("scale=3840:2160:flags=lanczos", (ROOT / "vicestyle.py").read_text())

    def test_cut_temp_file_is_unique_per_worker(self):
        src = (ROOT / "vicestyle.py").read_text()
        self.assertIn('part = f"{out}.{os.getpid()}-{threading.get_ident()}.part.mp4"', src)
        self.assertNotIn('str(out) + ".part.mp4"', src)


if __name__ == "__main__":
    unittest.main()
