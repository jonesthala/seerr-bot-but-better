import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bot.arr import ArrClient
from bot.config import ConfigError, Settings
from bot.core import GB, help_text
from bot.extras import (
    BRAKE_MARGIN,
    EpisodePlan,
    Span,
    brake_decision,
    brake_pulled_text,
    brake_released_text,
    episodes_text,
    event_description,
    event_end,
    parse_episodes,
    parse_when,
    per_episode_estimate,
    pick_candidates,
    pick_facts,
    plan_episodes,
    plan_lines,
    runtime_text,
    select_episodes,
)
from bot.jellyfin import JellyfinClient
from bot.store import Store
from bot.torrents import QbitClient

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
PAST, FUTURE = "2026-11-01T10:00:00Z", "2026-12-24T10:00:00Z"


def ep(eid, season, number, absolute=None, has_file=False, aired=True):
    e = {"id": eid, "seasonNumber": season, "episodeNumber": number, "hasFile": has_file,
         "airDateUtc": PAST if aired else FUTURE}
    if absolute is not None:
        e["absoluteEpisodeNumber"] = absolute
    return e


ONE_PIECE = [ep(1, 0, 1)] + [ep(100 + n, 23, n, 1155 + n, has_file=n <= 12, aired=n <= 30) for n in range(1, 26)]


class BrakeTest(unittest.TestCase):
    def test_decision(self):
        limit = 3 * GB
        self.assertEqual(brake_decision(2 * GB, False, limit), "pull")
        self.assertEqual(brake_decision(2 * GB, True, limit), "pull")  # keeps pausing new downloads
        self.assertIsNone(brake_decision(4 * GB, True, limit))  # not yet: limit plus margin
        self.assertEqual(brake_decision(limit + BRAKE_MARGIN, True, limit), "release")
        self.assertIsNone(brake_decision(10 * GB, False, limit))
        self.assertIsNone(brake_decision(None, True, limit))  # unknown space: do nothing
        self.assertIsNone(brake_decision(1 * GB, False, 0))  # switched off

    def test_texts(self):
        text = brake_pulled_text(int(2.5 * GB), ["Dune", "One Piece 1168", "Loki", "Sintel"], 3 * GB)
        self.assertIn("Nur noch 2.5 GB frei", text)
        self.assertIn("4 Downloads", text)
        self.assertIn("und 1 weitere", text)
        self.assertIn("sobald wieder 5.0 GB frei", text)
        self.assertEqual(brake_released_text(6 * GB, 1), "✅ **Notbremse gelöst:** Wieder 6.0 GB frei, 1 Download läuft weiter.")
        self.assertEqual(brake_released_text(6 * GB, 0), "✅ **Notbremse gelöst:** Wieder 6.0 GB frei.")

    def test_store_remembers_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            self.assertTrue(store.brake_pull(["aaa", "bbb"]))
            self.assertFalse(store.brake_pull(["bbb", "ccc"]))  # already on: no second message
            again = Store(folder)
            self.assertTrue(again.brake_active)
            self.assertEqual(again.brake_hashes, ["aaa", "bbb", "ccc"])
            self.assertEqual(again.brake_release(), ["aaa", "bbb", "ccc"])
            self.assertFalse(Store(folder).brake_active)


class QbitSwitchTest(unittest.TestCase):
    def fake(self, version5=False):
        calls = []

        def fetch(method, url, headers, body):
            path = urlsplit(url).path
            calls.append((path, parse_qs((body or b"").decode())))
            if path.endswith("/auth/login"):
                return 200, {"set-cookie": "SID=x; path=/"}, b"Ok."
            if path.endswith("/torrents/info"):
                return 200, {}, json.dumps([{"hash": "a", "state": "downloading"}, {"hash": "b", "state": "pausedDL"}]).encode()
            if version5 and path.endswith(("/torrents/pause", "/torrents/resume")):
                return 404, {}, b""
            return 200, {}, b""
        return QbitClient("http://q", "admin", "pw", fetch=fetch), calls

    def test_pause_and_resume(self):
        client, calls = self.fake()
        self.assertEqual([t["hash"] for t in client.all_torrents()], ["a", "b"])
        client.pause(["a", "c"])
        client.resume(["a"])
        self.assertEqual([(p, f) for p, f in calls if "/torrents/" in p and "info" not in p],
                         [("/api/v2/torrents/pause", {"hashes": ["a|c"]}), ("/api/v2/torrents/resume", {"hashes": ["a"]})])

    def test_qbittorrent_5(self):
        client, calls = self.fake(version5=True)
        client.pause(["a"])
        client.resume(["a"])
        self.assertIn("/api/v2/torrents/stop", [p for p, _ in calls])
        self.assertIn("/api/v2/torrents/start", [p for p, _ in calls])

    def test_nothing_to_do(self):
        client, calls = self.fake()
        client.pause([])
        self.assertEqual(calls, [])


class ParseEpisodesTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_episodes("S23E18-E25"), [Span((23, 18), (23, 25))])
        self.assertEqual(parse_episodes("s23e18 – 25"), [Span((23, 18), (23, 25))])
        self.assertEqual(parse_episodes("S23E18-S24E02"), [Span((23, 18), (24, 2))])
        self.assertEqual(parse_episodes("S23"), [Span((23, 1), (23, 9999))])
        self.assertEqual(parse_episodes("S01E05"), [Span((1, 5), (1, 5))])
        self.assertEqual(parse_episodes("1155-1170, 1180"), [Span((None, 1155), (None, 1170)), Span((None, 1180), (None, 1180))])
        self.assertEqual(parse_episodes("1155 bis 1160"), [Span((None, 1155), (None, 1160))])

    def test_errors(self):
        for text in ("", "Elbaph", "S23E18-E10", "1170-1155", "S23-E05", "S24E01-S23E01", "1-2-3"):
            with self.assertRaises(ValueError, msg=text):
                parse_episodes(text)


class SelectEpisodesTest(unittest.TestCase):
    def test_absolute_and_season(self):
        by_absolute = select_episodes(ONE_PIECE, parse_episodes("1173-1180"))
        self.assertEqual([e["episodeNumber"] for e in by_absolute], list(range(18, 26)))
        by_season = select_episodes(ONE_PIECE, parse_episodes("S23E18-E25"))
        self.assertEqual(by_season, by_absolute)
        self.assertEqual(len(select_episodes(ONE_PIECE, parse_episodes("S23"))), 25)  # no special
        self.assertEqual(len(select_episodes(ONE_PIECE, parse_episodes("S00E01"))), 1)  # only when asked for

    def test_show_without_absolute_numbers(self):
        with self.assertRaises(ValueError):
            select_episodes([ep(1, 1, 1)], parse_episodes("5-8"))

    def test_plan_and_text(self):
        chosen = select_episodes(ONE_PIECE, parse_episodes("S23E10-E25"))
        future = [e if e["episodeNumber"] < 24 else {**e, "airDateUtc": FUTURE} for e in chosen]
        plan = plan_episodes(future, NOW, int(1.45 * GB))
        self.assertEqual(len(plan.have), 3)  # 10-12
        self.assertEqual([e["episodeNumber"] for e in plan.later], [24, 25])
        self.assertEqual(episodes_text(plan.get), "S23E13–E23 (Folge 1168–1178)")
        self.assertEqual(plan.size, 11 * int(1.45 * GB))
        lines = plan_lines(plan, 14 * GB, 5 * GB)
        self.assertTrue(lines[0].startswith("🔎 **11 Folgen holen:** S23E13–E23"))
        self.assertIn("Noch nicht erschienen", lines[1])
        self.assertIn("Schon da", lines[2])
        self.assertIn("unter der Grenze von 5.0 GB", lines[3])  # 14 - 16 GB

    def test_text_for_gaps(self):
        eps = [ep(1, 1, 1), ep(2, 1, 2), ep(3, 1, 5), ep(4, 2, 1)]
        self.assertEqual(episodes_text(eps), "S01E01–E02, S01E05, S02E01")

    def test_size_estimate(self):
        self.assertEqual(per_episode_estimate({"statistics": {"episodeFileCount": 12, "sizeOnDisk": 12 * GB}}, 1.8), GB)
        self.assertEqual(per_episode_estimate({"runtime": 60, "statistics": {"episodeFileCount": 0}}, 2.0), 2 * GB)
        self.assertEqual(per_episode_estimate({}, 1.8), int(0.4 * 1.8 * GB))  # 24 minutes if unknown

    def test_empty_plan(self):
        self.assertEqual(plan_lines(EpisodePlan([], [], [], GB), 10 * GB, 5 * GB), [])


class SonarrWriteTest(unittest.TestCase):
    def test_want_episodes(self):
        calls = []

        def fetch(method, url, headers, body):
            calls.append((method, urlsplit(url).path, json.loads(body) if body else None))
            return 200, {}
        sonarr = ArrClient("http://s", "k", "tv", fetch=fetch)
        sonarr.want_episodes({"id": 9, "monitored": False, "title": "One Piece"}, [1, 2, 3], [1, 2])
        self.assertEqual([(m, p) for m, p, _ in calls],
                         [("PUT", "/api/v3/series/9"), ("PUT", "/api/v3/episode/monitor"), ("POST", "/api/v3/command")])
        self.assertTrue(calls[0][2]["monitored"])
        self.assertEqual(calls[1][2], {"episodeIds": [1, 2, 3], "monitored": True})
        self.assertEqual(calls[2][2], {"name": "EpisodeSearch", "episodeIds": [1, 2]})
        calls.clear()
        sonarr.want_episodes({"id": 9, "monitored": True}, [3], [])  # monitored show, nothing out yet
        self.assertEqual([(m, p) for m, p, _ in calls], [("PUT", "/api/v3/episode/monitor")])

    def test_queued_titles(self):
        records = [{"series": {"tmdbId": 37854, "title": "One Piece", "year": 1999}},
                   {"series": {"tmdbId": 37854, "title": "One Piece", "year": 1999}}, {"series": {"tmdbId": 0}}, {}]
        sonarr = ArrClient("http://s", "k", "tv", fetch=lambda m, u, h, b: (200, {"records": records}))
        self.assertEqual(sonarr.queued_titles(), {37854: "One Piece (1999)"})


ITEMS = [
    {"Id": "m1", "Type": "Movie", "Name": "Dune", "ProductionYear": 2021, "Genres": ["Science Fiction", "Abenteuer"],
     "RunTimeTicks": 155 * 600_000_000, "CommunityRating": 7.8},
    {"Id": "m2", "Type": "Movie", "Name": "One Piece Film Red", "Genres": ["Animation"]},
    {"Id": "s1", "Type": "Series", "Name": "Loki", "Genres": ["Science Fiction"], "ChildCount": 2},
]


class RandomTest(unittest.TestCase):
    def test_candidates(self):
        self.assertEqual([i["Id"] for i in pick_candidates(ITEMS, {"m2"}, None, None)], ["m1", "s1"])
        self.assertEqual([i["Id"] for i in pick_candidates(ITEMS, set(), "tv", None)], ["s1"])
        self.assertEqual([i["Id"] for i in pick_candidates(ITEMS, set(), None, "science fiction")], ["m1", "s1"])
        self.assertEqual(pick_candidates(ITEMS, {"m1", "m2", "s1"}, None, None), [])

    def test_facts(self):
        self.assertEqual(pick_facts(ITEMS[0]), "🎬 Film · 2 Std. 35 Min. · ⭐ 7.8 · Science Fiction, Abenteuer")
        self.assertEqual(pick_facts(ITEMS[2]), "📺 Serie · 2 Staffeln · Science Fiction")
        self.assertEqual(runtime_text(45 * 600_000_000), "45 Min.")
        self.assertEqual(runtime_text(None), "")

    def test_played_by_anyone(self):
        def fetch(method, url, headers, body):
            query = parse_qs(urlsplit(url).query)
            if urlsplit(url).path == "/Users":
                return 200, [{"Id": "u1"}, {"Id": "u2"}]
            return 200, {"Items": [{"Id": "m1"}] if query["userId"] == ["u1"] else [{"Id": "s1"}]}
        jellyfin = JellyfinClient("http://jf", "k", fetch=fetch)
        self.assertEqual(jellyfin.played_ids(), {"m1", "s1"})
        self.assertEqual(jellyfin.played_ids("u2"), {"s1"})


ZURICH = timezone(timedelta(hours=1))  # November: no summer time


class WhenTest(unittest.TestCase):
    NOW = datetime(2026, 11, 19, 18, 30, tzinfo=ZURICH)  # Thursday evening

    def at(self, text):
        return parse_when(text, self.NOW).strftime("%a %d.%m. %H:%M")

    def test_forms(self):
        self.assertEqual(self.at("20:00"), "Thu 19.11. 20:00")
        self.assertEqual(self.at("20 Uhr"), "Thu 19.11. 20:00")
        self.assertEqual(self.at("17:00"), "Fri 20.11. 17:00")  # passed today: tomorrow
        self.assertEqual(self.at("morgen 20:15"), "Fri 20.11. 20:15")
        self.assertEqual(self.at("Fr 20:00"), "Fri 20.11. 20:00")
        self.assertEqual(self.at("samstag 21.30"), "Sat 21.11. 21:30")
        self.assertEqual(self.at("do 17:00"), "Thu 26.11. 17:00")  # Thursday already passed: next week
        self.assertEqual(self.at("24.12. 20:00"), "Thu 24.12. 20:00")
        self.assertEqual(self.at("1.1. 20:00"), "Fri 01.01. 20:00")
        self.assertEqual(parse_when("1.1. 20:00", self.NOW).year, 2027)

    def test_errors(self):
        for text in ("heute 17:00", "25:00", "irgendwann", "31.2. 20:00", "1.1.2025 20:00", ""):
            with self.assertRaises(ValueError, msg=text):
                parse_when(text, self.NOW)

    def test_event(self):
        start = datetime(2026, 11, 20, 20, 0, tzinfo=ZURICH)
        self.assertEqual(event_end(start, 155 * 600_000_000), start + timedelta(minutes=170))
        self.assertEqual(event_end(start, None), start + timedelta(hours=2))
        text = event_description("Dune (2021)", "http://jf/web/index.html#!/details?id=m1")
        self.assertIn("SyncPlay", text)
        self.assertTrue(text.endswith("id=m1"))
        self.assertLessEqual(len(event_description("x" * 2000, None)), 1000)


class SettingsAndHelpTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}

    def test_brake_setting(self):
        with mock.patch.dict("os.environ", self.BASE, clear=True):
            self.assertEqual(Settings.from_env().brake_free_gb, 3)
        with mock.patch.dict("os.environ", {**self.BASE, "BRAKE_FREE_GB": "0"}, clear=True):
            self.assertEqual(Settings.from_env().brake_free_gb, 0)
        with mock.patch.dict("os.environ", {**self.BASE, "BRAKE_FREE_GB": "-1"}, clear=True):
            with self.assertRaises(ConfigError):
                Settings.from_env()

    def test_help(self):
        commands = [("zufall", "Vorschlag"), ("filmabend", "Filmabend"), ("folgen", "Folgen holen")]
        friend = help_text(commands, admin=False, may_request=True)
        self.assertIn("`/zufall Filme Comedy`", friend)
        self.assertIn("`/filmabend Dune morgen 20:00`", friend)
        self.assertNotIn("/folgen", friend)
        self.assertIn("`/folgen One Piece S23E18-E25`", help_text(commands, admin=True, may_request=True))


if __name__ == "__main__":
    unittest.main()
