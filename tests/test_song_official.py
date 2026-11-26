import unittest
from pipeline_lib.song_official import check


class Official(unittest.TestCase):
    def ok(self, rec, artist="Daft Punk", title="Get Lucky"):
        return check(rec, artist, title)[0]

    def test_rejects_fan_and_tribute_rips(self):
        self.assertFalse(self.ok({"source": "soundcloud", "url": "https://soundcloud.com/danilo-lima-64/get-lucky"}))
        self.assertFalse(self.ok({"source": "soundcloud", "url": "https://soundcloud.com/user-55123/get-lucky"}))
        self.assertFalse(check({"source": "soundcloud", "url": "https://soundcloud.com/80s-tribute/dont-you-want-me"},
                               "The Human League", "Don't You Want Me")[0])
        self.assertFalse(self.ok({"source": "youtube", "channel": "Daft Punk Covers", "url": "https://youtu.be/x"}))
        self.assertFalse(self.ok({"source": "archive", "url": "https://archive.org/details/x"}))
        self.assertFalse(self.ok({"source": "youtube", "channel": "Random Music Uploads"}))

    def test_accepts_artist_topic_vevo_label(self):
        self.assertTrue(self.ok({"source": "youtube", "channel": "Daft Punk - Topic"}))
        self.assertTrue(self.ok({"source": "youtube", "channel": "DaftPunkVEVO"}))
        self.assertTrue(self.ok({"source": "youtube", "channel": "Daft Punk", "verified": True}))
        self.assertTrue(check({"source": "youtube", "channel": "Fueled By Ramen and twenty one pilots"},
                              "twenty one pilots", "Heathens")[0])
        self.assertTrue(check({"source": "soundcloud", "url": "https://soundcloud.com/recordmakers/kavinsky-nightcall"},
                              "Kavinsky", "Nightcall")[0])

    def test_unverified_artist_named_channel_rejected(self):
        self.assertFalse(self.ok({"source": "youtube", "channel": "Daft Punk", "verified": False}))


if __name__ == "__main__":
    unittest.main()
