import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from bot.arr import Download, Progress
from bot.config import ConfigError, Settings
from bot.core import AVAILABLE, GB, PARTIAL, UNKNOWN, Season, Title
from bot.panel import (
    LoginLimiter,
    PanelError,
    calendar_days,
    episode_rows,
    follow_data,
    jellyfin_login,
    load_key,
    number_list,
    progress_data,
    read_session,
    season_rows,
    sign_session,
    title_data,
    torrent_data,
)
from bot.store import Follow

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}


class SessionTest(unittest.TestCase):
    def test_round_trip(self):
        key = b"k" * 32
        cookie = sign_session("jono", key, 1000.0)
        self.assertEqual(read_session(cookie, key, 1000.0), "jono")
        self.assertEqual(read_session(cookie, key, 1000.0 + 29 * 86400), "jono")

    def test_refused(self):
        key = b"k" * 32
        cookie = sign_session("jono", key, 1000.0)
        self.assertIsNone(read_session(cookie, key, 1000.0 + 31 * 86400))  # expired
        self.assertIsNone(read_session(cookie, b"x" * 32, 1000.0))  # other key
        payload, mac = cookie.split(".")
        forged = sign_session("someone", b"x" * 32, 1000.0).split(".")[0]
        self.assertIsNone(read_session(f"{forged}.{mac}", key, 1000.0))
        for value in (None, "", "abc", "a.b.c", f"{payload}.", "!!!.???"):
            self.assertIsNone(read_session(value, key, 1000.0), value)

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = load_key(tmp)
            self.assertEqual(len(first), 32)
            self.assertEqual(load_key(tmp), first)
            if os.name == "posix":
                mode = stat.S_IMODE(os.stat(os.path.join(tmp, "panel.key")).st_mode)
                self.assertEqual(mode, 0o600)
            with open(os.path.join(tmp, "panel.key"), "w", encoding="ascii") as file:
                file.write("broken")
            self.assertNotEqual(load_key(tmp), first)  # a new key replaces a broken file
        self.assertEqual(len(load_key(None)), 32)


class LimiterTest(unittest.TestCase):
    def test_limit_and_window(self):
        limiter = LoginLimiter(tries=3, window=600)
        for second in range(3):
            self.assertEqual(limiter.wait("a", 100.0 + second), 0)
            limiter.failed("a", 100.0 + second)
        self.assertGreater(limiter.wait("a", 103.0), 590)
        self.assertEqual(limiter.wait("b", 103.0), 0)  # other address
        self.assertEqual(limiter.wait("a", 701.0), 0)  # the window has passed
        limiter.failed("a", 702.0)
        limiter.succeeded("a")
        self.assertEqual(limiter.failures.get("a"), None)


class FakeJellyfin:
    def __init__(self, status, data):
        self.status, self.data, self.calls = status, data, []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if url.endswith("/Sessions/Logout"):
            return 204, None
        if isinstance(self.status, Exception):
            raise self.status
        return self.status, self.data


class JellyfinLoginTest(unittest.TestCase):
    def test_admin(self):
        fake = FakeJellyfin(200, {"User": {"Name": "Jono", "Policy": {"IsAdministrator": True}}, "AccessToken": "t1"})
        self.assertEqual(jellyfin_login("http://jf:8096/", "jono", "pw", fetch=fake), ("Jono", ""))
        method, url, headers, body = fake.calls[0]
        self.assertEqual((method, url), ("POST", "http://jf:8096/Users/AuthenticateByName"))
        self.assertIn(b'"Pw": "pw"', body)
        self.assertTrue(headers["Authorization"].startswith("MediaBrowser "))
        logout = fake.calls[1]  # the session Jellyfin opened is closed again
        self.assertTrue(logout[1].endswith("/Sessions/Logout"))
        self.assertIn('Token="t1"', logout[2]["Authorization"])

    def test_refused(self):
        friend = FakeJellyfin(200, {"User": {"Name": "Max", "Policy": {"IsAdministrator": False}}, "AccessToken": "t"})
        name, problem = jellyfin_login("http://jf", "max", "pw", fetch=friend)
        self.assertIsNone(name)
        self.assertIn("Administratoren", problem)
        self.assertEqual(len(friend.calls), 2)  # logged out again all the same
        name, problem = jellyfin_login("http://jf", "x", "y", fetch=FakeJellyfin(401, None))
        self.assertIsNone(name)
        self.assertIn("stimmt nicht", problem)
        name, problem = jellyfin_login("http://jf", "x", "y", fetch=FakeJellyfin(OSError("down"), None))
        self.assertIn("erreichbar", problem)
        name, problem = jellyfin_login("http://jf", "x", "y", fetch=FakeJellyfin(500, None))
        self.assertIsNone(name)


class DataTest(unittest.TestCase):
    def test_title(self):
        show = Title("tv", 7, "Dexter: Resurrection", "2025", "…", "https://image.tmdb.org/p.jpg", PARTIAL,
                     (Season(0, 3, UNKNOWN), Season(1, 10, AVAILABLE), Season(2, 10, UNKNOWN)), media_id=4, runtime=55)
        data = title_data(show)
        self.assertEqual(data["label"], "Dexter: Resurrection (2025)")
        self.assertEqual([s["number"] for s in data["seasons"]], [1, 2])  # no specials
        self.assertEqual(data["requestable_seasons"], [2])
        self.assertTrue(data["in_library"])
        self.assertTrue(data["requestable"])
        self.assertIn("Staffel 2", data["status_text"])

    def test_progress(self):
        eta = NOW + timedelta(minutes=30)
        downloads = (
            Download("S02E05", 4 * GB, 1 * GB, eta, "downloading", hash="aa", episodes=1),
            Download("S02E06", 4 * GB, 4 * GB, None, "paused", hash="bb", episodes=1),
        )
        progress = Progress(have=4, total=8, missing={2: [7, 8]}, upcoming=2, downloads=downloads, searching=True)
        data = progress_data("tv", progress, {"aa": {"dlspeed": 5_000_000}}, NOW)
        self.assertEqual(data["percent"], round(100 * (4 + 0.75) / 8))
        self.assertEqual((data["size"], data["loaded"], data["speed"]), (4 * GB, 3 * GB, 5_000_000))
        self.assertEqual(data["eta_text"], "noch ca. 30 Min.")
        self.assertEqual(data["missing"], [{"season": 2, "text": "7–8", "count": 2}])
        self.assertEqual([d["state"] for d in data["downloads"]], ["downloading", "paused"])
        self.assertEqual(data["downloads"][1]["eta_text"], "")
        movie = progress_data("movie", Progress(1, 1, {}, 0, (), False), {}, NOW)
        self.assertEqual(movie["percent"], 100)

    def test_torrent(self):
        data = torrent_data({"hash": "ab", "name": "Show.S01E01", "progress": 0.456, "size": 100, "dlspeed": 2048,
                             "eta": 120, "state": "stalledDL", "category": "tv-sonarr", "num_seeds": 3, "added_on": 5})
        self.assertEqual((data["percent"], data["state"], data["eta"], data["seeds"]), (45.6, "stalled", 120, 3))
        self.assertIsNone(torrent_data({"eta": 8640000, "state": "uploading"})["eta"])
        self.assertEqual(torrent_data({"state": "stoppedUP"})["state"], "done")
        self.assertEqual(torrent_data({"state": "somethingNew"})["state"], "other")

    def test_episodes(self):
        def ep(id_, season, number, aired, has=False, title="Titel"):
            return {"id": id_, "seasonNumber": season, "episodeNumber": number, "hasFile": has, "monitored": False,
                    "title": title, "airDateUtc": (NOW + timedelta(days=aired)).isoformat() if aired is not None else None}
        episodes = [ep(1, 1, 1, -30, has=True), ep(2, 1, 2, -20), ep(3, 1, 3, 5, title="TBA"), ep(4, 0, 1, -100),
                    ep(5, 1, 4, None), ep(6, 1, 5, -1)]
        rows = episode_rows(episodes, {6}, NOW, timezone.utc, anime=False)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 5, 6, 4])  # specials last
        self.assertEqual([r["state"] for r in rows], ["have", "missing", "upcoming", "upcoming", "loading", "missing"])
        self.assertEqual(rows[2]["label"], "E03")
        self.assertEqual(rows[0]["label"], "E01 · Titel")
        seasons = season_rows(rows)
        self.assertEqual(seasons[0], {"number": 1, "total": 5, "have": 1, "open": 3})
        self.assertEqual(seasons[1]["number"], 0)

    def test_calendar(self):
        start, end = NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=3)
        episodes = [
            {"id": 1, "seasonNumber": 2, "episodeNumber": 5, "title": "Kapitel", "airDateUtc": "2026-11-25T08:00:00Z",
             "series": {"title": "Dexter"}},
            {"id": 2, "seasonNumber": 1, "episodeNumber": 1, "airDateUtc": "2026-11-24T10:00:00Z", "hasFile": True,
             "series": {"title": "Neu"}},
            {"id": 3, "seasonNumber": 1, "episodeNumber": 2, "airDateUtc": "2026-12-30T10:00:00Z", "series": {}},
        ]
        movies = [{"id": 9, "title": "Film", "year": 2026, "digitalRelease": "2026-11-26T00:00:00Z"}]
        days = calendar_days(episodes, movies, {1}, set(), start, end, NOW, timezone.utc)
        self.assertEqual([d["label"] for d in days], ["Heute 24.11.", "Morgen 25.11.", "Do 26.11."])
        self.assertEqual(days[0]["items"][0]["state"], "have")
        self.assertEqual(days[1]["items"][0], {"kind": "tv", "title": "Dexter", "what": "S02E05", "episode_title": "Kapitel",
                                               "time": "08:00", "state": "loading"})
        self.assertEqual(days[2]["items"][0]["title"], "Film (2026)")
        self.assertIsNone(days[2]["items"][0]["time"])

    def test_follow(self):
        follow = Follow(1, "Admin-Panel", 7, "Dexter (2025)", "S02E04", True, state="active", fid=3, series_id=11)
        nxt = {"seasonNumber": 2, "episodeNumber": 5, "title": "Kapitel", "airDateUtc": "2026-11-27T08:00:00Z"}
        data = follow_data(follow, nxt, timezone.utc)
        self.assertEqual(data["next"], {"code": "S02E05", "title": "Kapitel", "when": "Fr 27.11.2026, 08:00"})
        self.assertIsNone(follow_data(follow, None, timezone.utc)["next"])

    def test_number_list(self):
        self.assertEqual(number_list([1, 2, 3]), [1, 2, 3])
        for bad in (None, "1", [1, "2"], [True], [-1], list(range(3000))):
            with self.assertRaises(PanelError, msg=bad):
                number_list(bad)


class PanelSettingsTest(unittest.TestCase):
    def load(self, **extra):
        with mock.patch.dict("os.environ", {**BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_off_by_default(self):
        s = self.load()
        self.assertEqual((s.panel_port, s.panel_mc_url, s.panel_user_id), (0, "", None))

    def test_values(self):
        s = self.load(PANEL_PORT="8095", JELLYFIN_API_URL="http://jf:8096", DISCORD_ADMIN_USER_IDS="42",
                      PANEL_MC_URL="http://mc:8096/", PANEL_MC_TOKEN="t" * 64)
        self.assertEqual((s.panel_port, s.panel_user_id, s.panel_mc_url), (8095, 42, "http://mc:8096"))
        self.assertEqual(self.load(DISCORD_ADMIN_USER_IDS="1,2").panel_user_id, None)  # which one? none
        self.assertEqual(self.load(DISCORD_ADMIN_USER_IDS="1,2", PANEL_DISCORD_USER_ID="2").panel_user_id, 2)

    def test_errors(self):
        for env in ({"PANEL_PORT": "8095"},  # no Jellyfin to log in with
                    {"PANEL_PORT": "99999", "JELLYFIN_API_URL": "http://jf"},
                    {"PANEL_MC_URL": "http://mc:8096"},  # no token
                    {"PANEL_MC_URL": "http://mc:8096", "PANEL_MC_TOKEN": "short"},
                    {"PANEL_MC_URL": "mc:8096", "PANEL_MC_TOKEN": "t" * 64}):
            with self.assertRaises(ConfigError, msg=env):
                self.load(**env)


if __name__ == "__main__":
    unittest.main()
