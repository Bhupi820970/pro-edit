import unittest
from unittest.mock import patch
from pipeline_lib.publish import archive_posted as A


class Archive(unittest.TestCase):
    def test_status_counts_per_platform(self):
        names = {"yo-a.mp4": 1, "yo-a.preview.mp4": 1, "yo-b.mp4": 1, "in-c.mp4": 1, "manifest.json": 1}
        rows = [{"id": "yo-a", "platform": "youtube", "status": "posted"}]
        st = A.stock_status("r", names, rows)["platforms"]
        self.assertEqual(st["youtube"]["unposted_ids"], ["yo-b"])
        self.assertEqual(st["youtube"]["posted_still_in_stock"], 1)
        self.assertEqual(st["instagram"]["unposted"], 1)

    def test_move_only_posted_and_only_after_verified_copy(self):
        stock = {"yo-a.mp4": 5, "yo-a.preview.mp4": 3, "yo-a.mp4.json": 2, "yo-b.mp4": 5}
        calls = []

        def fake_gh(args, timeout=120, check=True):
            calls.append(args)
            class R: returncode = 0; stdout = "{}"; stderr = ""
            if args[0] == "release" and args[1] == "download":
                d = args[args.index("--dir") + 1]; n = args[args.index("--pattern") + 1]
                if n in stock:
                    open(f"{d}/{n}", "wb").write(b"x" * stock[n])
                else:
                    R.returncode = 1
            return R
        arch = {}
        with patch.object(A, "_gh", fake_gh), \
             patch.object(A, "posted_rows", lambda repo: [{"id": "yo-a", "platform": "youtube", "status": "posted"}]), \
             patch.object(A, "assets", lambda repo, tag: stock if tag == A.STOCK_TAG else arch):
            arch.update({"yo-a.mp4": 5, "yo-a.preview.mp4": 3, "yo-a.mp4.json": 2})
            moved = A.move("r")
        self.assertEqual(moved, ["yo-a"])
        deleted = [c[3] for c in calls if c[:2] == ["release", "delete-asset"]]
        self.assertEqual(sorted(deleted), ["yo-a.mp4", "yo-a.mp4.json", "yo-a.preview.mp4"])

    def test_unverified_copy_is_not_deleted(self):
        stock = {"yo-a.mp4": 5}
        calls = []

        def fake_gh(args, timeout=120, check=True):
            calls.append(args)
            class R: returncode = 0; stdout = "{}"; stderr = ""
            if args[:2] == ["release", "download"]:
                d = args[args.index("--dir") + 1]
                open(f"{d}/yo-a.mp4", "wb").write(b"x" * 5)
            return R
        with patch.object(A, "_gh", fake_gh), \
             patch.object(A, "posted_rows", lambda repo: [{"id": "yo-a", "platform": "youtube", "status": "posted"}]), \
             patch.object(A, "assets", lambda repo, tag: stock if tag == A.STOCK_TAG else {}):
            self.assertEqual(A.move("r"), [])
        self.assertFalse([c for c in calls if c[:2] == ["release", "delete-asset"]])


if __name__ == "__main__":
    unittest.main()
