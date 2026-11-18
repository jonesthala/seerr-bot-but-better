import unittest

from bot.core import (
    AVAILABLE,
    GB,
    MAX_CHOICES,
    PARTIAL,
    PENDING,
    PROCESSING,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    Season,
    SpaceWatch,
    deleted_text,
    format_numbers,
    no_space_text,
    open_requests,
    pick_server,
    request_done_text,
    root_folder_free,
    search_results,
    shorten,
    size_text,
    title_from,
)

MOVIE = {
    "id": 45745,
    "mediaType": "movie",
    "title": "Sintel",
    "originalTitle": "Sintel",
    "releaseDate": "2010-09-27",
    "overview": "Ein Mädchen sucht ihren Drachen.",
    "posterPath": "/abc.jpg",
}
SHOW = {
    "id": 30010,
    "mediaType": "tv",
    "name": "Pioneer One",
    "firstAirDate": "2010-06-15",
    "overview": "",
    "posterPath": None,
    "mediaInfo": {"status": PARTIAL},
}
SHOW_DETAILS = {
    "id": 30010,
    "name": "Pioneer One",
    "firstAirDate": "2010-06-15",
    "seasons": [
        {"seasonNumber": 0, "episodeCount": 2},
        {"seasonNumber": 1, "episodeCount": 6},
        {"seasonNumber": 2, "episodeCount": 6},
        {"seasonNumber": 3, "episodeCount": 6},
    ],
    "mediaInfo": {"status": PARTIAL, "seasons": [{"seasonNumber": 1, "status": AVAILABLE}]},
}


class TitleTest(unittest.TestCase):
    def test_movie(self):
        title = title_from(MOVIE)
        self.assertEqual((title.kind, title.tmdb_id, title.label), ("movie", 45745, "Sintel (2010)"))
        self.assertEqual(title.poster, "https://image.tmdb.org/t/p/w342/abc.jpg")
        self.assertTrue(title.requestable)
        self.assertIn("kann angefragt werden", title.status_text())
        self.assertEqual(title.kind_text, "Film")

    def test_show_without_seasons_yet(self):
        title = title_from(SHOW)
        self.assertEqual(title.label, "Pioneer One (2010)")
        self.assertIsNone(title.poster)
        self.assertEqual(title.seasons, ())
        self.assertTrue(title.requestable)  # partly there, details decide later

    def test_show_details_with_seasons(self):
        title = title_from(SHOW_DETAILS, "tv")
        self.assertEqual([s.number for s in title.seasons], [0, 1, 2, 3])
        self.assertEqual(title.requestable_seasons(), [2, 3])  # no specials, season 1 is there
        self.assertEqual(title.status_text(), "🟡 Teilweise da, es fehlen noch Staffel 2–3")
        self.assertEqual(title.seasons_text(), "Staffel 1–3 · noch nicht da: 2–3")

    def test_states_that_block_a_request(self):
        for status in (PENDING, PROCESSING, AVAILABLE):
            title = title_from({**MOVIE, "mediaInfo": {"status": status}})
            self.assertFalse(title.requestable, status)
        everything_there = {**SHOW_DETAILS, "mediaInfo": {"status": AVAILABLE, "seasons": [
            {"seasonNumber": n, "status": AVAILABLE} for n in (1, 2, 3)]}}
        self.assertFalse(title_from(everything_there, "tv").requestable)

    def test_garbage(self):
        for bad in (None, [], {}, {"id": "1", "mediaType": "movie", "title": "x"},
                    {"id": 1, "mediaType": "person", "name": "Someone"}, {"id": 1, "mediaType": "movie", "title": " "}):
            self.assertIsNone(title_from(bad), bad)

    def test_season_rules(self):
        self.assertFalse(Season(0, 3).requestable)
        self.assertTrue(Season(1, 3).requestable)
        self.assertFalse(Season(1, 3, PROCESSING).requestable)


class SearchTest(unittest.TestCase):
    def test_filters_kind_and_limits(self):
        data = {"results": [SHOW, MOVIE, {"id": 9, "mediaType": "person", "name": "X"}] +
                [{**MOVIE, "id": 100 + n} for n in range(10)]}
        movies = search_results(data, "movie")
        self.assertEqual(len(movies), MAX_CHOICES)
        self.assertEqual(movies[0].tmdb_id, 45745)
        self.assertEqual([t.kind for t in search_results(data, "tv")], ["tv"])
        self.assertEqual(search_results({"results": "nope"}, "movie"), [])
        self.assertEqual(search_results(None, "tv"), [])


class OpenRequestsTest(unittest.TestCase):
    def test_parse(self):
        data = {"results": [
            {"status": REQUEST_PENDING, "type": "movie", "media": {"tmdbId": 1, "status": PENDING},
             "requestedBy": {"displayName": "alex", "email": "x@example.com"}},
            {"status": REQUEST_APPROVED, "type": "tv", "media": {"tmdbId": 2, "status": PROCESSING,
             "downloadStatus": [{"size": 1000, "sizeLeft": 250}, {"size": 1000, "sizeLeft": 750}]},
             "requestedBy": {"email": "only@example.com"}},
            {"status": REQUEST_APPROVED, "type": "movie", "media": {"tmdbId": 3, "status": AVAILABLE}},
            {"status": 3, "type": "movie", "media": {"tmdbId": 4, "status": 1}},
            {"status": 2, "type": "movie", "media": "broken"},
        ]}
        found = open_requests(data)
        self.assertEqual([(r.kind, r.tmdb_id) for r in found], [("movie", 1), ("tv", 2)])
        self.assertEqual(found[0].requested_by, "alex")
        self.assertEqual(found[1].requested_by, "")  # never the e-mail address
        self.assertEqual(found[1].progress, 50)
        self.assertEqual(found[0].line("Sintel (2010)"), "**Sintel (2010)** · ⏳ wartet auf Freigabe · von alex")
        self.assertEqual(found[1].line("Pioneer One"), "**Pioneer One** · ⬇️ wird geladen, 50 %")

    def test_searching_without_download(self):
        data = {"results": [{"status": 2, "type": "movie", "media": {"tmdbId": 5, "status": PROCESSING}}]}
        self.assertIn("wird gesucht", open_requests(data)[0].line("X"))

    def test_partly_there(self):
        data = {"results": [
            {"status": REQUEST_APPROVED, "type": "tv", "media": {"tmdbId": 6, "status": PARTIAL}},
            {"status": REQUEST_APPROVED, "type": "tv", "media": {"tmdbId": 7, "status": PARTIAL,
             "downloadStatus": [{"size": 1000, "sizeLeft": 500}]}},
        ]}
        searching, loading = open_requests(data)
        self.assertEqual(searching.line("X"), "**X** · 🟡 teilweise da, der Rest wird gesucht")
        self.assertEqual(loading.line("Y"), "**Y** · 🟡 teilweise da · ⬇️ wird geladen, 50 %")


class LibraryTest(unittest.TestCase):
    def test_media_id_and_library(self):
        there = title_from({**MOVIE, "mediaInfo": {"id": 12, "status": AVAILABLE}})
        self.assertEqual(there.media_id, 12)
        self.assertTrue(there.in_library)
        self.assertFalse(title_from({**MOVIE, "mediaInfo": {"id": 12, "status": PENDING}}).in_library)
        self.assertFalse(title_from(MOVIE).in_library)
        self.assertIn("allen Staffeln", deleted_text(title_from(SHOW)))

    def test_both_kinds(self):
        data = {"results": [SHOW, MOVIE]}
        self.assertEqual([t.kind for t in search_results(data, None)], ["tv", "movie"])
        self.assertEqual(search_results(data, None, library_only=True), [])


class SpaceTest(unittest.TestCase):
    def test_pick_server(self):
        servers = [{"id": 1, "is4k": True, "isDefault": True}, {"id": 2}, {"id": 3, "isDefault": True}]
        self.assertEqual(pick_server(servers)["id"], 3)
        self.assertEqual(pick_server([{"id": 2}])["id"], 2)
        for bad in (None, [], [{"id": "x"}], [{"id": 1, "is4k": True}]):
            self.assertIsNone(pick_server(bad), bad)

    def test_root_folder_free(self):
        details = {"rootFolders": [{"path": "/mnt/shared/movies/", "freeSpace": 40 * GB},
                                   {"path": "/other", "freeSpace": 5 * GB}]}
        self.assertEqual(root_folder_free(details, "/mnt/shared/movies"), 40 * GB)
        self.assertEqual(root_folder_free(details, "/elsewhere"), 5 * GB)  # unknown folder: be careful
        self.assertIsNone(root_folder_free({"rootFolders": []}, "/x"))
        self.assertIsNone(root_folder_free(None, "/x"))

    def test_size_text(self):
        self.assertEqual(size_text(37 * GB), "37 GB")
        self.assertEqual(size_text(int(2.5 * GB)), "2.5 GB")

    def test_watch_posts_once_each_way(self):
        watch = SpaceWatch(15 * GB)
        self.assertIsNone(watch.update(None))
        self.assertIsNone(watch.update(30 * GB))  # fine at start: no message
        low = watch.update(10 * GB)
        self.assertIn("fast voll", low)
        self.assertIn("10 GB", low)
        self.assertIsNone(watch.update(9 * GB))   # still low: quiet
        self.assertIsNone(watch.update(16 * GB))  # just above the minimum: not yet
        self.assertIn("Wieder genug Platz", watch.update(18 * GB))
        self.assertIsNone(watch.update(40 * GB))

    def test_watch_low_at_start(self):
        self.assertIn("fast voll", SpaceWatch(15 * GB).update(3 * GB))

    def test_no_space_text(self):
        text = no_space_text(12 * GB, 15 * GB)
        self.assertIn("12 GB", text)
        self.assertIn("15 GB", text)
        self.assertIn("/löschen", text)


class TextTest(unittest.TestCase):
    def test_format_numbers(self):
        self.assertEqual(format_numbers([1, 2, 3, 5]), "1–3, 5")
        self.assertEqual(format_numbers([4]), "4")
        self.assertEqual(format_numbers([3, 1, 2, 2]), "1–3")
        self.assertEqual(format_numbers([]), "")

    def test_shorten(self):
        self.assertEqual(shorten("kurz", 10), "kurz")
        text = shorten("eins zwei drei vier fünf sechs", 15)
        self.assertTrue(text.endswith("…") and len(text) <= 15, text)

    def test_done_texts(self):
        self.assertIn("wird jetzt gesucht", request_done_text("movie", REQUEST_APPROVED))
        self.assertIn("Freigabe", request_done_text("tv", REQUEST_PENDING))

    def test_swiss_spelling(self):
        import bot.core
        import bot.seerr
        for module in (bot.core, bot.seerr):
            with open(module.__file__, encoding="utf-8") as source:
                self.assertNotIn("ß", source.read(), module.__name__)


if __name__ == "__main__":
    unittest.main()
