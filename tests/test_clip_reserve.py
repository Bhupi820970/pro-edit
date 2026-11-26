import datetime as dt
import json
import multiprocessing as mp
import unittest
from pathlib import Path
import tempfile

from pipeline_lib import clip_reserve as cr


def _race(path, owner, q):
    r = cr.Reservations(owner, backend=cr.LocalBackend(Path(path)), log=lambda *a: None)
    q.put((owner, r.reserve("abcdefghijk")))


class ClipReserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "clip_reservations.json"

    def tearDown(self):
        self.tmp.cleanup()

    def R(self, owner):
        return cr.Reservations(owner, "youtube", backend=cr.LocalBackend(self.path), log=lambda *a: None)

    def test_second_run_cannot_take_a_reserved_clip(self):
        a, b = self.R("run1-youtube"), self.R("run1-instagram")
        self.assertTrue(a.reserve("abcdefghijk"))
        self.assertFalse(b.reserve("abcdefghijk"))
        self.assertFalse(b.reserve("abcdefghijk_c2"))          # chunks share the source id
        self.assertIn("abcdefghijk", b.taken_ids())
        self.assertNotIn("abcdefghijk", a.taken_ids())         # own live reservation is not "taken"

    def test_parallel_processes_only_one_wins(self):
        q = mp.Queue()
        ps = [mp.Process(target=_race, args=(str(self.path), f"o{i}", q)) for i in range(6)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        wins = [o for o, ok in (q.get() for _ in ps) if ok]
        self.assertEqual(len(wins), 1)

    def test_confirm_is_permanent_even_for_the_same_run(self):
        a, b = self.R("run1-youtube"), self.R("run2-youtube")
        a.reserve("abcdefghijk")
        a.confirm(["abcdefghijk_c1"], "yo-anime-1")
        self.assertIn("abcdefghijk", a.taken_ids())            # never again in a later video of this run
        self.assertFalse(a.reserve("abcdefghijk"))
        self.assertFalse(b.reserve("abcdefghijk"))
        b.release(["abcdefghijk"])                             # releasing does not undo "used"
        self.assertEqual(json.loads(self.path.read_text())["clips"]["abcdefghijk"]["state"], "used")

    def test_release_frees_only_own_reservations_after_a_failed_render(self):
        a, b = self.R("run1-youtube"), self.R("run1-instagram")
        a.reserve("aaaaaaaaaaa")
        b.reserve("bbbbbbbbbbb")
        a.release()                                            # render failed
        self.assertTrue(b.reserve("aaaaaaaaaaa"))
        self.assertFalse(a.reserve("bbbbbbbbbbb"))

    def test_stale_reservation_of_a_crashed_run_expires(self):
        a = self.R("crashed")
        a.reserve("abcdefghijk")
        doc = json.loads(self.path.read_text())
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cr.RESERVE_TTL_H + 1)
        doc["clips"]["abcdefghijk"]["at"] = old.isoformat()
        self.path.write_text(json.dumps(doc))
        self.assertTrue(self.R("next").reserve("abcdefghijk"))

    def test_conflict_is_retried_then_succeeds(self):
        class Flaky(cr.LocalBackend):
            n = 0

            def write(self, doc, sha, msg):
                Flaky.n += 1
                if Flaky.n == 1:
                    raise cr.Conflict()
                super().write(doc, sha, msg)
        r = cr.Reservations("o", backend=Flaky(self.path), log=lambda *a: None)
        cr.time.sleep, orig = (lambda s: None), cr.time.sleep
        try:
            self.assertTrue(r.reserve("abcdefghijk"))
        finally:
            cr.time.sleep = orig
        self.assertEqual(Flaky.n, 2)

    def test_unreachable_ledger_fails_closed(self):
        class Down(cr.LocalBackend):
            def read(self):
                raise OSError("down")
        r = cr.Reservations("o", backend=Down(self.path), log=lambda *a: None, tries=1)
        self.assertFalse(r.reserve("abcdefghijk"))             # never risk a repeat


if __name__ == "__main__":
    unittest.main()
