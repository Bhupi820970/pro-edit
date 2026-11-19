#!/usr/bin/env python3
"""One video = one movie / one series / one star player."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from pipeline_lib.titles import (  # noqa: E402
    SINGLE_WORK_CATEGORIES, is_full_scene_upload, is_gta_title, is_trailer_title, player_name,
    restrict_to_single_player, restrict_to_single_work, work_title,
)


class WorkTitle(unittest.TestCase):
    def test_strips_trailer_and_channel_junk(self):
        self.assertEqual(work_title('The Love Hypothesis - Official Trailer | Prime Video', "romance"),
                         "The Love Hypothesis")
        self.assertEqual(work_title("The Love Hypothesis | Official Trailer", "romance"),
                         "The Love Hypothesis")
        self.assertEqual(work_title('"Wuthering Heights" | Official Trailer', "romance"),
                         "Wuthering Heights")
        self.assertEqual(work_title("Wish You Were Here (2025) Official Trailer - A Julia Stiles Film", "romance"),
                         "Wish You Were Here")
        self.assertEqual(work_title("The Holiday 20th Anniversary Trailer", "movie"),
                         "The Holiday")
        self.assertEqual(work_title("DIE MY LOVE | Official Trailer | Now Streaming | With Jennifer Lawrence & Robert Pattinson", "romance"),
                         "DIE MY LOVE")
        self.assertEqual(work_title("Abigail | Official Trailer", "movie"), "Abigail")
        self.assertEqual(work_title("Violent Night 2 | Official Trailer", "movie"), "Violent Night 2")
        self.assertEqual(work_title("Violent Night - Official Trailer", "movie"), "Violent Night")
        self.assertEqual(work_title("JUMANJI: OPEN WORLD - Official Trailer (4K)", "movie"),
                         "JUMANJI: OPEN WORLD")

    def test_series_seasons_collapse(self):
        self.assertEqual(work_title("Stranger Things 5 | Official Trailer | Netflix", "series"),
                         "Stranger Things")
        self.assertEqual(work_title("Stranger Things 2 | Official Final Trailer", "series"),
                         "Stranger Things")
        self.assertEqual(work_title("The Last of Us Season 2 | Official Trailer | Max", "series"),
                         "The Last of Us")
        self.assertEqual(work_title("Fallout - Teaser Trailer | Prime Video", "series"), "Fallout")
        self.assertEqual(work_title("The 100 | Official Trailer", "series"), "The 100")

    def test_restrict_keeps_one_movie(self):
        clips = [
            {"id": "a", "title": "Violent Night - Official Trailer"},
            {"id": "b", "title": "Violent Night 2 | Official Trailer"},
            {"id": "c", "title": "Abigail | Official Trailer"},
            {"id": "d", "title": "JUMANJI: OPEN WORLD - New Trailer (4K)"},
            {"id": "e", "title": "JUMANJI: OPEN WORLD - Official Trailer (4K)"},
        ]
        got = restrict_to_single_work(clips, "movie")
        titles = {work_title(c["title"], "movie") for c in got}
        self.assertEqual(titles, {"JUMANJI: OPEN WORLD"})
        self.assertEqual(len(got), 2)
        # other categories are left mixed
        self.assertEqual(len(restrict_to_single_work(clips, "gta")), 5)

    def test_equal_counts_pick_the_longer_title(self):
        clips = [
            {"id": "a", "title": "Abigail | Official Trailer", "duration": 90},
            {"id": "b", "title": "The Holiday 20th Anniversary Trailer", "duration": 160},
        ]
        got = restrict_to_single_work(clips, "movie")
        self.assertEqual({work_title(c["title"], "movie") for c in got}, {"The Holiday"})

    def test_restrict_keeps_one_series(self):
        clips = [
            {"id": "1", "title": "Stranger Things 5 | Official Trailer | Netflix"},
            {"id": "2", "title": "Stranger Things 2 | Official Final Trailer"},
            {"id": "3", "title": "Wednesday Addams | Official Teaser | Netflix"},
        ]
        got = restrict_to_single_work(clips, "series")
        self.assertEqual({work_title(c["title"], "series") for c in got}, {"Stranger Things"})
        self.assertEqual(len(got), 2)


class TrailerTitles(unittest.TestCase):
    def test_rejects_trailer_teaser_promo_tv_spot(self):
        self.assertTrue(is_trailer_title('Abigail | Official Trailer'))
        self.assertTrue(is_trailer_title("Wuthering Heights Official Teaser"))
        self.assertTrue(is_trailer_title("NFL official cinematic trailer"))
        self.assertTrue(is_trailer_title("Movie promo"))
        self.assertTrue(is_trailer_title("Official TV Spot"))
        self.assertFalse(is_trailer_title("Jumanji official clip — motorcycle chase scene"))
        self.assertFalse(is_trailer_title("Stranger Things official scene"))
        self.assertFalse(is_trailer_title("LeBron James official highlights"))
        self.assertFalse(is_trailer_title("The Last of Us full episode scene"))


class Players(unittest.TestCase):

    def test_star_aliases(self):
        self.assertEqual(player_name("LeBron James | Official Interview"), "lebron james")
        self.assertEqual(player_name("KING JAMES hype film", "NBA"), "lebron james")
        self.assertEqual(player_name("Lionel Messi best goals", "Inter Miami"), "lionel messi")
        self.assertEqual(player_name("Cristiano Ronaldo official highlights"), "cristiano ronaldo")
        self.assertEqual(player_name("CR7 training", "Al Nassr"), "cristiano ronaldo")
        self.assertIsNone(player_name("AMAZING Assists From This Premier League Season"))
        self.assertIsNone(player_name("Lake Show Energy | Lakers Hype Video", "Los Angeles Lakers"))

    def test_restrict_drops_non_player_and_keeps_one_star(self):
        clips = [
            {"id": "pl", "title": "Premier League best assists"},
            {"id": "m1", "title": "Lionel Messi official interview", "has_dialogue": True},
            {"id": "m2", "title": "Messi official highlights", "has_dialogue": False},
            {"id": "l1", "title": "LeBron James official highlights", "has_dialogue": False},
        ]
        got = restrict_to_single_player(clips)
        self.assertTrue(got)
        self.assertEqual({player_name(c["title"]) for c in got}, {"lionel messi"})
        self.assertGreaterEqual(sum(1 for c in got if c.get("has_dialogue")), 1)

    def test_gta_flag(self):
        self.assertTrue(is_gta_title("Grand Theft Auto VI Trailer 2"))
        self.assertTrue(is_gta_title("GTA Online: A Safehouse in the Hills"))
        self.assertFalse(is_gta_title("Red Dead Redemption 2 Official Trailer"))
        self.assertTrue("movie" in SINGLE_WORK_CATEGORIES)
        self.assertNotIn("sports", SINGLE_WORK_CATEGORIES)
        self.assertNotIn("gaming", SINGLE_WORK_CATEGORIES)
        self.assertNotIn("gta", SINGLE_WORK_CATEGORIES)


    def test_full_scene_upload_allows_fan_channels_not_trailers(self):
        self.assertTrue(is_full_scene_upload(
            'Clip Kings “You Stole John Wick’s Car... and Killed His Dog” (Full Scene) | John Wick'))
        self.assertTrue(is_full_scene_upload(
            "They Call Him Baba Yaga | John Wick (Michael Nyqvist, Alfie Allen Scene)"))
        self.assertTrue(is_full_scene_upload("John Wick official movie clip | Movieclips"))
        self.assertFalse(is_full_scene_upload("Abigail | Official Trailer"))
        self.assertFalse(is_full_scene_upload("Stranger Things 5 | Official Trailer | Netflix"))
        self.assertFalse(is_full_scene_upload("The Love Hypothesis - Official Teaser"))

    def test_work_title_prefers_movie_name_after_pipe(self):
        self.assertEqual(work_title(
            '“You Stole John Wick’s Car... and Killed His Dog” (Full Scene) | John Wick', "movie"),
            "John Wick")
        self.assertEqual(work_title(
            "They Call Him Baba Yaga | John Wick (Michael Nyqvist, Alfie Allen Scene)", "movie"),
            "John Wick")



if __name__ == "__main__":
    unittest.main()

