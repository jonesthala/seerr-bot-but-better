import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bot.arr import ArrClient
from bot.config import Settings
from bot.core import GB
from bot.extras import episodes_deleted_text, watched_questions, watched_text
from bot.jellyfin import JellyfinClient
from bot.store import Store, WatchQuestion
from bot.torrents import QbitClient, TorrentCleaner

SINCE = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def played(item_id, kind, when, is_played=True, **extra):
    return {"Id": item_id, "Type": kind, "UserData": {"Played": is_played, "LastPlayedDate": when}, **extra}


def episode(item_id, season, number, when="2026-11-19T20:00:00Z"):
    return played(item_id, "Episode", when, SeriesId="dex", SeriesName="Dexter: Wiedererwachen", ParentIndexNumber=season,
                  IndexNumber=number, MediaSources=[{"Size": 2 * GB}])


class QuestionsTest(unittest.TestCase):
    def test_grouped_per_show_and_movie(self):
        data = {
            "u1": ("jones", [episode("e2", 2, 2), episode("e1", 2, 1),
                             played("m1", "Movie", "2026-11-19T19:00:00Z", Name="Dune", ProductionYear=2021,
                                    ProviderIds={"Tmdb": "438631"}, MediaSources=[{"Size": 5 * GB}]),
                             played("old", "Movie", "2026-11-18T19:00:00Z", Name="Alt"),  # before the last look
                             played("half", "Movie", "2026-11-19T19:00:00Z", is_played=False, Name="Halb")]),
            "u2": ("freund", [episode("e1", 2, 1), episode("asked", 2, 3)]),
        }
        questions = watched_questions(data, SINCE, {"asked"}, {"dex": 259909})
        self.assertEqual(len(questions), 2)
        show = next(q for q in questions if q.kind == "tv")
        movie = next(q for q in questions if q.kind == "movie")
        self.assertEqual((show.tmdb_id, show.episodes, show.size), (259909, [[2, 1], [2, 2]], 4 * GB))
        self.assertEqual(show.viewers, ["jones", "freund"])
        self.assertEqual((movie.tmdb_id, movie.label, movie.size), (438631, "Dune (2021)", 5 * GB))

    def test_texts(self):
        show = WatchQuestion("tv", 259909, "Dexter", "dex", [[2, 1], [2, 2]], ["e1", "e2"], ["jones"], 4 * GB)
        text = watched_text(show, ["freund"])
        self.assertIn("👀 **Geschaut:** **Dexter** · S02E01–E02 · 4.0 GB", text)
        self.assertIn("Noch nicht fertig geschaut von: freund", text)
        self.assertIn("Sonarr holt diese Folgen danach nicht wieder", text)
        self.assertIn("/film", watched_text(WatchQuestion("movie", 1, "Dune (2021)", viewers=["jones"]), []))
        self.assertEqual(episodes_deleted_text(2, 4 * GB, (1, 4 * GB, 0), None),
                         "🗑️ 2 Folgen gelöscht (≈ 4.0 GB), Sonarr holt sie nicht wieder. 1 Torrent entfernt.")
        self.assertIn("später frei", episodes_deleted_text(1, GB, (0, 0, 1), None))
        self.assertIn("erst frei", episodes_deleted_text(1, GB, None, None))


class StoreTest(unittest.TestCase):
    def test_questions_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            question = store.add_watch_question(WatchQuestion("tv", 1, "Dexter", "dex", [[2, 1]], ["e1"], ["jones"], GB))
            self.assertEqual(question.qid, 1)
            question.messages = [[10, 20]]
            store.set_watch_since("2026-11-19T12:00:00+00:00")
            again = Store(folder)
            self.assertEqual(again.watch_questions[1].messages, [[10, 20]])
            self.assertEqual(again.watch_questions[1].episodes, [[2, 1]])
            self.assertEqual(again.watch_asked, ["e1"])
            self.assertEqual(again.watch_since, "2026-11-19T12:00:00+00:00")
            self.assertEqual(again.add_watch_question(WatchQuestion("movie", 2, "Dune")).qid, 2)


class SonarrDropTest(unittest.TestCase):
    def test_find_and_drop(self):
        calls = []

        def fetch(method, url, headers, body):
            path = urlsplit(url).path
            calls.append((method, path, json.loads(body) if body else None))
            if path.endswith("/history/series"):
                return 200, [{"episodeId": 1, "downloadId": "AAA"}, {"episodeId": 3, "downloadId": "CCC"}, {"episodeId": 2}]
            if path.endswith("/series"):
                return 200, [{"id": 9, "tmdbId": 259909}]
            if path.endswith("/episode"):
                return 200, [{"id": 1, "seasonNumber": 2, "episodeNumber": 1, "hasFile": True, "episodeFileId": 11},
                             {"id": 2, "seasonNumber": 2, "episodeNumber": 2, "hasFile": True, "episodeFileId": 12},
                             {"id": 3, "seasonNumber": 2, "episodeNumber": 3, "hasFile": False, "episodeFileId": 0}]
            if path.endswith("/history/series"):
                return 200, [{"episodeId": 1, "downloadId": "AAA"}, {"episodeId": 3, "downloadId": "CCC"}, {"episodeId": 2}]
            if "/episodefile/" in path and method == "GET":
                return 200, {"size": 2 * GB}
            if path.endswith("/queue"):
                return 200, {"records": [{"downloadId": "BUSY"}]}
            return 200, None
        sonarr = ArrClient("http://s", "k", "tv", fetch=fetch)
        show, eps = sonarr.find_episodes(259909, [(2, 1), (2, 2)])
        self.assertEqual((show["id"], [e["id"] for e in eps]), (9, [1, 2]))
        self.assertEqual(sonarr.find_episodes(1, [(1, 1)]), (None, []))
        self.assertEqual(sonarr.episode_hashes(9, [1, 2]), ["aaa"])
        self.assertEqual(sonarr.queue_hashes(), {"busy"})
        calls.clear()
        self.assertEqual(sonarr.drop_episodes(eps), 4 * GB)
        self.assertEqual(calls[0], ("PUT", "/api/v3/episode/monitor", {"episodeIds": [1, 2], "monitored": False}))
        self.assertEqual([(m, p) for m, p, _ in calls if m == "DELETE"],
                         [("DELETE", "/api/v3/episodefile/11"), ("DELETE", "/api/v3/episodefile/12")])


class TorrentFinishedTest(unittest.TestCase):
    def test_only_finished_and_not_busy(self):
        deleted = []

        def fetch(method, url, headers, body):
            path = urlsplit(url).path
            if path.endswith("/auth/login"):
                return 200, {"set-cookie": "SID=x"}, b"Ok."
            if path.endswith("/torrents/info"):
                return 200, {}, json.dumps([{"hash": "aaa", "progress": 1, "size": GB}, {"hash": "bbb", "progress": 0.4},
                                            {"hash": "busy", "progress": 1, "size": GB}]).encode()
            if path.endswith("/torrents/delete"):
                deleted.append(parse_qs(body.decode())["hashes"][0])
            return 200, {}, b""
        cleaner = TorrentCleaner(QbitClient("http://q", "a", "b", fetch=fetch), {})
        self.assertEqual(cleaner.remove_finished(["aaa", "bbb", "busy"], {"busy"}), (1, GB, 2))
        self.assertEqual(deleted, ["aaa"])


class JellyfinPlayedTest(unittest.TestCase):
    def test_recently_played_and_user_data(self):
        urls = []

        def fetch(method, url, headers, body):
            urls.append(url)
            if "Ids=" in url:
                return 200, {"Items": [{"Id": "e1", "UserData": {"Played": True}}]}
            return 200, {"Items": [{"Id": "e1"}, {"Name": "ohne Id"}]}
        jellyfin = JellyfinClient("http://jf", "k", fetch=fetch)
        self.assertEqual([i["Id"] for i in jellyfin.recently_played("u1")], ["e1"])
        query = parse_qs(urlsplit(urls[0]).query)
        self.assertEqual((query["IsPlayed"], query["SortBy"], query["userId"]), (["true"], ["DatePlayed"], ["u1"]))
        self.assertEqual(jellyfin.user_data("u1", ["e1", "dex"]), {"e1": {"Played": True}})
        self.assertEqual(jellyfin.user_data("u1", []), {})


class SettingTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}

    def test_watched_ask(self):
        with mock.patch.dict("os.environ", self.BASE, clear=True):
            self.assertTrue(Settings.from_env().watched_ask)
        with mock.patch.dict("os.environ", {**self.BASE, "WATCHED_ASK": "0"}, clear=True):
            self.assertFalse(Settings.from_env().watched_ask)


if __name__ == "__main__":
    unittest.main()
