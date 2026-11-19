import unittest

from bot.core import GB, RULE_OF_THUMB, Season, SizeRate, Title, rate_from_library, space_text, title_from
from bot.jellyfin import JellyfinClient, library_usage

RATE = SizeRate(2.0, 1.0, 3.0, 5)
SHOW = Title("tv", 37854, "One Piece", "1999", "", None, 1,
             seasons=(Season(22, 70), Season(23, 25)), runtime=24)
MOVIE = Title("movie", 1, "Film", "2020", "", None, 1, runtime=120)


class RuntimeTest(unittest.TestCase):
    def test_from_details(self):
        movie = title_from({"id": 1, "title": "Film", "runtime": 142}, "movie")
        self.assertEqual(movie.runtime, 142)
        show = title_from({"id": 2, "name": "One Piece", "episodeRunTime": [24]}, "tv")
        self.assertEqual(show.runtime, 24)
        self.assertIsNone(title_from({"id": 3, "name": "Loki", "episodeRunTime": []}, "tv").runtime)
        self.assertIsNone(title_from({"id": 4, "title": "Suche"}, "movie").runtime)  # search results have none


class RateTest(unittest.TestCase):
    def test_from_library(self):
        rate = rate_from_library([2.6, 2.0, 1.8, 1.2, 0.9])
        self.assertEqual((rate.typical, rate.titles), (1.8, 5))
        self.assertAlmostEqual(rate.low, 0.9)
        self.assertAlmostEqual(rate.high, 2.88)  # at least 1.6 × typical
        self.assertEqual(rate_from_library([2.0, 1.0]), RULE_OF_THUMB)  # too few titles


class SpaceTextTest(unittest.TestCase):
    def test_one_season(self):
        text = space_text(SHOW, [23], RATE, 100 * GB, 15 * GB)  # 25 × 24 min = 10 h
        self.assertIn("≈ **20 GB** (10 GB–30 GB) für 25 Folgen à 24 Min.", text)
        self.assertIn("Frei: 100 GB", text)
        self.assertNotIn("⚠️", text)
        self.assertNotIn("❌", text)
        self.assertIn("2.0 GB pro Stunde", text)

    def test_tight_and_too_little(self):
        tight = space_text(SHOW, [23], RATE, 30 * GB, 15 * GB)  # 30 − 20 < 15, but 30 − 10 ≥ 15
        self.assertIn("⚠️ Könnte knapp werden", tight)
        too_little = space_text(SHOW, [22, 23], RATE, 30 * GB, 15 * GB)  # 95 episodes ≈ 76 GB
        self.assertIn("❌ Reicht vermutlich nicht", too_little)

    def test_movie_and_unknown_length(self):
        self.assertIn("≈ **4.0 GB** (2.0 GB–6.0 GB) für 120 Min.", space_text(MOVIE, None, RATE, None, 15 * GB))
        loki = Title("tv", 2, "Loki", "2021", "", None, 1, seasons=(Season(1, 6),))
        self.assertIn("6 Folgen à 45 Min. (Länge unbekannt, geschätzt)", space_text(loki, [1], RATE, None, 15 * GB))
        self.assertIsNone(space_text(Title("movie", 3, "X", "", "", None, 1), None, RATE, None, 15 * GB))
        self.assertIn("Faustregel", space_text(MOVIE, None, RULE_OF_THUMB, None, 15 * GB))


class LibraryRateTest(unittest.TestCase):
    def test_hours_and_rates(self):
        hour = 36_000_000_000  # ticks
        items = [{"Id": "m1", "Type": "Movie", "Name": "A", "RunTimeTicks": 2 * hour, "MediaSources": [{"Size": 4 * 1024 ** 3}]},
                 {"Id": "e1", "Type": "Episode", "SeriesId": "s", "SeriesName": "B", "RunTimeTicks": hour // 2,
                  "MediaSources": [{"Size": 1024 ** 3}]},
                 {"Id": "e2", "Type": "Episode", "SeriesId": "s", "SeriesName": "B", "RunTimeTicks": hour // 2,
                  "MediaSources": [{"Size": 1024 ** 3}]},
                 {"Id": "x", "Type": "Movie", "Name": "kurz", "RunTimeTicks": hour // 10, "MediaSources": [{"Size": 10}]}]
        rates = {u.name: u.gb_per_hour for u in library_usage(items, [], {})}
        self.assertEqual(rates["A"], 2.0)
        self.assertEqual(rates["B"], 2.0)
        self.assertIsNone(rates["kurz"])  # too short to count

        def fake(method, url, headers, body):
            return 200, {"Items": items}
        self.assertEqual(sorted(JellyfinClient("http://j", "k", fetch=fake).size_rates()), [2.0, 2.0])


if __name__ == "__main__":
    unittest.main()
