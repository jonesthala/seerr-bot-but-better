import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from bot.arr import ArrClient
from bot.core import help_text
from bot.extras import calendar_lines, early_search_ids, follow_lines, parse_start, start_episode, when_text
from bot.store import Follow, Store
from test_seerr import FakeSeerr, client

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
ZURICH = timezone(timedelta(hours=1))


def ep(eid, season, number, air, has_file=False, monitored=False, absolute=None):
    e = {"id": eid, "seasonNumber": season, "episodeNumber": number, "airDateUtc": air, "hasFile": has_file,
         "monitored": monitored}
    if absolute is not None:
        e["absoluteEpisodeNumber"] = absolute
    return e


DEXTER = [ep(1, 0, 1, "2026-02-17T01:00:00Z"), ep(2, 1, 1, "2025-07-13T01:00:00Z", monitored=True)] + [
    ep(10 + n, 2, n, (datetime(2026, 11, 2, 1, tzinfo=timezone.utc) + timedelta(weeks=n - 1)).isoformat(),
       monitored=True) for n in range(1, 11)]


class FakeSonarr:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        self.calls.append((method, parts.path, json.loads(body) if body else None, parse_qs(parts.query)))
        if parts.path.endswith("/series/lookup"):
            return 200, [{"title": "Dexter: Resurrection", "tvdbId": 452595, "seasons": [{"seasonNumber": 1, "monitored": True}]}]
        if parts.path.endswith("/series") and method == "POST":
            return 201, {**json.loads(body), "id": 10}
        if parts.path.endswith("/episode"):
            return 200, DEXTER
        if parts.path.endswith("/queue"):
            return 200, {"records": [{"episodeId": 14}, {"movieId": 3}]}
        return 200, None

    def sent(self, method, path_end):
        return [c for c in self.calls if c[0] == method and c[1].endswith(path_end)]


class SonarrFollowTest(unittest.TestCase):
    def test_add_without_episodes(self):
        fake = FakeSonarr()
        sonarr = ArrClient("http://s", "k", "tv", fetch=fake)
        lookup = sonarr.lookup_tvdb(452595)
        added = sonarr.add_series(lookup, 6, "/mnt/shared/shows", anime=False, new_seasons=True)
        body = fake.sent("POST", "/series")[0][2]
        self.assertEqual(added["id"], 10)
        self.assertEqual(body["addOptions"], {"monitor": "none", "searchForMissingEpisodes": False,
                                              "searchForCutoffUnmetEpisodes": False})
        self.assertEqual(body["seasons"], [{"seasonNumber": 1, "monitored": False}])  # Sonarr's lookup said True
        self.assertEqual((body["qualityProfileId"], body["seriesType"], body["monitorNewItems"]), (6, "standard", "all"))
        self.assertEqual(sonarr.lookup_tvdb(1), None)

    def test_follow_from_an_episode(self):
        fake = FakeSonarr()
        sonarr = ArrClient("http://s", "k", "tv", fetch=fake)
        series = {"id": 10, "monitored": True, "monitorNewItems": "none"}
        get, later, have = sonarr.follow(series, DEXTER, (2, 3), NOW, new_seasons=True, only_these=True)
        self.assertEqual([e["episodeNumber"] for e in get], [3])  # out on 16.11., missing: searched now
        self.assertEqual([e["episodeNumber"] for e in later], list(range(4, 11)))
        self.assertEqual(have, [])
        off = fake.sent("PUT", "/episode/monitor")[0][2]
        self.assertEqual(off, {"episodeIds": [2, 11, 12], "monitored": False})  # what Sonarr monitored by itself
        on = fake.sent("PUT", "/episode/monitor")[1][2]
        self.assertEqual(on["episodeIds"], list(range(13, 21)))
        self.assertEqual(fake.sent("PUT", "/series/10")[0][2]["monitorNewItems"], "all")
        self.assertEqual(fake.sent("POST", "/command")[0][2], {"name": "EpisodeSearch", "episodeIds": [13]})

    def test_follow_what_is_to_come(self):
        fake = FakeSonarr()
        sonarr = ArrClient("http://s", "k", "tv", fetch=fake)
        get, later, _ = sonarr.follow({"id": 10, "monitored": True, "monitorNewItems": "all"}, DEXTER, None, NOW, True)
        self.assertEqual((get, [e["episodeNumber"] for e in later]), ([], list(range(4, 11))))
        self.assertEqual(fake.sent("PUT", "/series/10"), [])  # nothing to change on the show
        self.assertEqual(fake.sent("POST", "/command"), [])  # nothing out yet

    def test_unfollow_keeps_files(self):
        fake = FakeSonarr()
        sonarr = ArrClient("http://s", "k", "tv", fetch=fake)
        self.assertEqual(sonarr.unfollow({"id": 10, "monitorNewItems": "all"}, NOW), 7)  # E4-E10
        self.assertEqual(fake.sent("PUT", "/series/10")[0][2]["monitorNewItems"], "none")

    def test_wait_for_episodes(self):
        answers = [[], [], DEXTER]
        sonarr = ArrClient("http://s", "k", "tv", fetch=lambda m, u, h, b: (200, answers.pop(0)))
        slept = []
        self.assertEqual(len(sonarr.wait_for_episodes(10, sleep=slept.append)), len(DEXTER))
        self.assertEqual(len(slept), 2)

    def test_queued_ids(self):
        self.assertEqual(ArrClient("http://s", "k", "tv", fetch=FakeSonarr()).queued_ids(), {14})
        self.assertEqual(ArrClient("http://r", "k", "movie", fetch=FakeSonarr()).queued_ids(), {3})


class SeerrShowTest(unittest.TestCase):
    def test_ids_and_defaults(self):
        fake = FakeSeerr({
            ("GET", "/tv/259909"): (200, {"name": "Dexter: Resurrection", "externalIds": {"tvdbId": 452595},
                                          "keywords": [{"id": 1, "name": "police"}]}),
            ("GET", "/tv/37854"): (200, {"name": "One Piece", "externalIds": {}, "keywords": [{"id": 210024}]}),
            ("GET", "/service/sonarr"): (200, [{"id": 0, "is4k": False, "isDefault": True, "activeProfileId": 6,
                                               "activeDirectory": "/mnt/shared/shows", "activeAnimeProfileId": 7}]),
        })
        seerr = client(fake)
        self.assertEqual(seerr.show_ids(259909), (452595, False, "Dexter: Resurrection"))
        self.assertEqual(seerr.show_ids(37854), (None, True, "One Piece"))
        self.assertEqual(seerr.sonarr_defaults(), (6, "/mnt/shared/shows", 7, "/mnt/shared/shows"))


class FollowTextTest(unittest.TestCase):
    def test_start(self):
        self.assertIsNone(parse_start(""))
        self.assertIsNone(parse_start("jetzt"))
        self.assertEqual(start_episode(parse_start("S02E04"), DEXTER), (2, 4))
        self.assertEqual(start_episode(parse_start("S02"), DEXTER), (2, 1))
        anime = [ep(1, 23, 26, "2026-11-23T01:00:00Z", absolute=1181)]
        self.assertEqual(start_episode(parse_start("1181"), anime), (23, 26))
        with self.assertRaises(ValueError):
            start_episode(parse_start("1181"), DEXTER)
        for text in ("S02E04-E06", "1-5", "irgendwann"):
            with self.assertRaises(ValueError, msg=text):
                parse_start(text)

    def test_lines(self):
        later = [e for e in DEXTER if e["seasonNumber"] == 2 and e["episodeNumber"] >= 4]
        lines = follow_lines("Dexter: Resurrection", (2, 4), [], later, [], True, ZURICH)
        self.assertEqual(lines[0], "📺 **Dexter: Resurrection** wird verfolgt, ab S02E04.")
        self.assertTrue(lines[1].startswith("📅 **Nächste Folge: Mo 23.11. um 02:00** (S02E04), danach 6 weitere bis Mo 04.01."))
        self.assertEqual(lines[-1], "🆕 Neue Staffeln werden auch geholt.")
        self.assertIn("keine neue Folge", follow_lines("X", None, [], [], [], False, ZURICH)[1])
        self.assertEqual(when_text(datetime(2026, 11, 23, 1, tzinfo=timezone.utc), ZURICH), "Mo 23.11. um 02:00")

    def test_calendar(self):
        show = {"title": "Dexter: Resurrection", "seriesType": "standard"}
        anime = {"title": "One Piece", "seriesType": "anime"}
        episodes = [
            {**ep(14, 2, 4, "2026-11-23T01:00:00Z"), "series": show},
            {**ep(30, 23, 26, "2026-11-22T00:15:00Z", absolute=1181), "series": anime},
            {**ep(31, 23, 25, "2026-11-19T10:00:00Z", has_file=True), "series": anime},
            {**ep(32, 23, 27, "2026-12-30T00:00:00Z"), "series": anime},  # after the end
        ]
        movies = [{"id": 3, "title": "Dune: Part Two", "year": 2024, "digitalRelease": "2026-11-21T00:00:00Z"}]
        start, end = datetime(2026, 11, 18, 23, tzinfo=timezone.utc), datetime(2026, 11, 25, 23, tzinfo=timezone.utc)
        lines = calendar_lines(episodes, movies, {14}, {3}, start, end, NOW, ZURICH)
        self.assertEqual(lines, [
            "**Do 19.11.**", "`11:00` 📺 One Piece · S23E25 · ✅ da", "",
            "**Sa 21.11.**", "🎬 Dune: Part Two (2024) · erscheint digital · ⬇️ lädt", "",
            "**So 22.11.**", "`01:15` 📺 One Piece · S23E26 (Folge 1181) · ⏳ wird geholt", "",
            "**Mo 23.11.**", "`02:00` 📺 Dexter: Resurrection · S02E04 · ⬇️ lädt",
        ])
        self.assertEqual(calendar_lines([], [], set(), set(), start, end, NOW, ZURICH), [])


class EarlySearchTest(unittest.TestCase):
    def test_streaming_before_the_tv_date(self):
        friday = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        episodes = [
            ep(13, 2, 4, "2026-11-23T01:00:00Z", monitored=True),  # TVDB Monday, out on Friday: searched
            ep(14, 2, 5, "2026-11-30T01:00:00Z", monitored=True),  # too far ahead
            ep(15, 2, 3, "2026-11-16T01:00:00Z", monitored=True),  # out 4 days ago: RSS had its chance
            ep(16, 2, 6, "2026-11-21T01:00:00Z", has_file=True, monitored=True),  # already there
            ep(17, 2, 7, "2026-11-22T01:00:00Z", monitored=False),  # not wanted
            ep(18, 2, 8, "2026-11-19T20:00:00Z", monitored=True),  # yesterday, still missing
            ep(19, 2, 9, "2026-11-21T01:00:00Z", monitored=True),  # downloading
            ep(20, 0, 1, "2026-11-21T01:00:00Z", monitored=True),  # special
        ]
        self.assertEqual(early_search_ids(episodes, {19}, friday, ahead_days=4), [13, 18])
        self.assertEqual(early_search_ids(episodes, set(), friday, ahead_days=0), [18])  # only what is already out


class SonarrSearchTest(unittest.TestCase):
    def test_search(self):
        fake = FakeSonarr()
        sonarr = ArrClient("http://s", "k", "tv", fetch=fake)
        sonarr.search_episodes([13])
        sonarr.search_episodes([])
        self.assertEqual([c[2] for c in fake.sent("POST", "/command")], [{"name": "EpisodeSearch", "episodeIds": [13]}])


class FollowStoreTest(unittest.TestCase):
    def test_follows(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            first = store.add_follow(Follow(11, "jones", 259909, "Dexter (2025)", "S02E04", state="active"))
            store.add_follow(Follow(12, "freund", 259909, "Dexter (2025)"))  # pending
            again = Store(folder)
            self.assertEqual([f.user_id for f in again.active_follows(tmdb_id=259909)], [11])
            self.assertEqual(again.follows[first.fid].start_text, "S02E04")
            again.drop_follow(first.fid)
            self.assertEqual(Store(folder).active_follows(), [])

    def test_help(self):
        text = help_text([("verfolgen", "Serie verfolgen"), ("kalender", "Kalender")], admin=False, may_request=True)
        self.assertIn("`/verfolgen Dexter ab S02E04`", text)
        self.assertIn("`/kalender`", text)


if __name__ == "__main__":
    unittest.main()
