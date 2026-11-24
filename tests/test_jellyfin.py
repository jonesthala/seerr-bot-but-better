import json
import tempfile
import unittest
from unittest import mock

from bot.config import ConfigError, Settings
from bot.jellyfin import (
    JellyfinClient,
    JellyfinError,
    check_name,
    friend_policy,
    library_usage,
    new_password,
    suggest_name,
    usage_line,
    welcome_text,
)
from bot.store import FRIEND_APPROVED, FRIEND_DECLINED, Friend, Store

BASE = "http://jellyfin.test:8096"
FOLDERS = [{"Name": "Movies", "ItemId": "m1"}, {"Name": "Shows", "ItemId": "s1"}, {"Name": "Privat", "ItemId": "p1"}]
POLICY = {"AuthenticationProviderId": "auth", "PasswordResetProviderId": "reset", "IsAdministrator": False,
          "EnableAllFolders": True, "EnabledFolders": [], "EnableContentDownloading": True}


class FakeJellyfin:
    """Records calls; answers from a table of (method, path) → (status, data)."""

    def __init__(self, answers=None):
        self.answers = {
            ("GET", "/Users"): (200, [{"Name": "admin", "Id": "a0"}]),
            ("GET", "/Library/VirtualFolders"): (200, FOLDERS),
            ("POST", "/Users/New"): (200, {"Id": "u1", "Name": "max", "Policy": POLICY}),
            ("POST", "/Users/u1/Policy"): (204, None),
            ("GET", "/Users/u1"): (200, {"Id": "u1", "Policy": {**POLICY, "IsDisabled": True}}),
            ("POST", "/Users/Password"): (204, None),
            ("DELETE", "/Users/u1"): (204, None),
            **(answers or {}),
        }
        self.calls = []

    def __call__(self, method, url, headers, body):
        path, _, query = url.removeprefix(BASE).partition("?")
        self.calls.append({"method": method, "path": path, "query": query, "headers": headers,
                           "body": json.loads(body) if body else None})
        answer = self.answers.get((method, path), (404, None))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def call(self, method, path):
        return next(c for c in self.calls if c["method"] == method and c["path"] == path)


def client(fake, libraries=("Movies", "Shows")):
    return JellyfinClient(BASE, "secret-key", libraries, 4_000_000, fetch=fake)


class NamesTest(unittest.TestCase):
    def test_suggest_and_check(self):
        self.assertEqual(suggest_name("Max Muster"), "Max.Muster")
        self.assertEqual(suggest_name("Ünï 😀"), "")  # too little left
        self.assertEqual(suggest_name("x" * 30), "x" * 20)
        self.assertIsNone(check_name("max.muster"))
        self.assertIsNotNone(check_name("ma"))
        self.assertIsNotNone(check_name("max muster"))
        self.assertIsNotNone(check_name("max<script>"))

    def test_passwords(self):
        first, second = new_password(), new_password()
        self.assertEqual(len(first), 14)
        self.assertNotEqual(first, second)
        self.assertFalse(set(first) & set("0O1lI"))


class ClientTest(unittest.TestCase):
    def test_create_friend(self):
        fake = FakeJellyfin()
        self.assertEqual(client(fake).create_friend("max"), "u1")
        new = fake.call("POST", "/Users/New")
        self.assertEqual(new["body"]["Name"], "max")
        self.assertGreaterEqual(len(new["body"]["Password"]), 14)
        self.assertEqual(new["headers"]["Authorization"], 'MediaBrowser Token="secret-key"')
        policy = fake.call("POST", "/Users/u1/Policy")["body"]
        self.assertEqual(policy["EnabledFolders"], ["m1", "s1"])  # not "Privat"
        self.assertFalse(policy["EnableAllFolders"])
        self.assertFalse(policy["IsAdministrator"])
        self.assertFalse(policy["EnableContentDeletion"])
        self.assertTrue(policy["IsHidden"])
        self.assertEqual(policy["RemoteClientBitrateLimit"], 4_000_000)
        self.assertEqual(policy["AuthenticationProviderId"], "auth")  # Jellyfin requires it back
        self.assertTrue(policy["EnableContentDownloading"])  # untouched fields stay

    def test_name_exists(self):
        fake = FakeJellyfin({("GET", "/Users"): (200, [{"Name": "Max", "Id": "x"}])})
        with self.assertRaises(JellyfinError) as caught:
            client(fake).create_friend("max")
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse([c for c in fake.calls if c["path"] == "/Users/New"])

    def test_missing_library(self):
        with self.assertRaises(JellyfinError) as caught:
            client(FakeJellyfin(), libraries=("Movies", "Anime")).create_friend("max")
        self.assertIn("Anime", str(caught.exception))

    def test_all_libraries_if_none_named(self):
        fake = FakeJellyfin()
        client(fake, libraries=()).create_friend("max")
        self.assertEqual(fake.call("POST", "/Users/u1/Policy")["body"]["EnabledFolders"], ["m1", "s1", "p1"])

    def test_policy_fails_removes_account(self):
        fake = FakeJellyfin({("POST", "/Users/u1/Policy"): (400, None)})
        with self.assertRaises(JellyfinError):
            client(fake).create_friend("max")
        self.assertEqual(fake.calls[-1]["method"], "DELETE")

    def test_block_and_open_again(self):
        fake = FakeJellyfin()
        client(fake).set_access("u1", False)
        self.assertTrue(fake.call("POST", "/Users/u1/Policy")["body"]["IsDisabled"])
        fake = FakeJellyfin()
        client(fake).set_access("u1", True)
        policy = fake.call("POST", "/Users/u1/Policy")["body"]
        self.assertFalse(policy["IsDisabled"])
        self.assertEqual(policy["EnabledFolders"], ["m1", "s1"])

    def test_reset_password(self):
        fake = FakeJellyfin()
        password = client(fake).reset_password("u1")
        call = fake.call("POST", "/Users/Password")
        self.assertEqual(call["query"], "userId=u1")
        self.assertEqual(call["body"], {"NewPw": password, "ResetPassword": False})

    def test_errors(self):
        with self.assertRaises(JellyfinError) as caught:
            client(FakeJellyfin({("GET", "/Users"): (401, None)})).find_user("max")
        self.assertIn("JELLYFIN_API_KEY", str(caught.exception))
        with self.assertRaises(JellyfinError) as caught:
            client(FakeJellyfin({("GET", "/Users"): OSError("refused")})).find_user("max")
        self.assertIn("nicht erreichbar", str(caught.exception))

    def test_policy_keeps_other_fields(self):
        policy = friend_policy({"MaxActiveSessions": 2, "IsAdministrator": True}, ["m1"], 0)
        self.assertEqual(policy["MaxActiveSessions"], 2)
        self.assertFalse(policy["IsAdministrator"])
        self.assertEqual(policy["RemoteClientBitrateLimit"], 0)


MOVIE = {"Id": "m1", "Type": "Movie", "Name": "Sintel", "ProductionYear": 2010, "ProviderIds": {"Tmdb": "45745"},
         "MediaSources": [{"Size": 600}]}
EPISODES = [{"Id": f"e{n}", "Type": "Episode", "SeriesId": "s1", "SeriesName": "Loki", "MediaSources": [{"Size": 500}]}
            for n in range(3)]
SERIES = [{"Id": "s1", "Name": "Loki", "ProductionYear": 2021, "ProviderIds": {"Tmdb": "84958"}}]


class UsageTest(unittest.TestCase):
    def test_groups_sizes_and_watching(self):
        watched = {
            "u1": [{"Id": "e1", "UserData": {"Played": True, "LastPlayedDate": "2026-11-15T20:12:33.1234567Z"}},
                   {"Id": "m1", "UserData": {"Played": False, "PlaybackPositionTicks": 0}}],
            "u2": [{"Id": "e2", "UserData": {"Played": False, "PlaybackPositionTicks": 1200,
                                             "LastPlayedDate": "2026-11-10T08:00:00Z"}}],
        }
        loki, sintel = library_usage([MOVIE, *EPISODES], SERIES, watched)
        self.assertEqual((loki.name, loki.kind, loki.size, loki.files, loki.tmdb_id), ("Loki (2021)", "tv", 1500, 3, 84958))
        self.assertEqual(loki.viewers, 2)
        self.assertEqual(loki.last_played.day, 15)  # the latest of both
        self.assertEqual((sintel.name, sintel.kind, sintel.tmdb_id), ("Sintel (2010)", "movie", 45745))
        self.assertIsNone(sintel.last_played)  # opened but never played
        self.assertEqual(sintel.viewers, 0)

    def test_lines(self):
        loki, sintel = library_usage([MOVIE, *EPISODES], SERIES, {
            "u1": [{"Id": "e0", "UserData": {"Played": True, "LastPlayedDate": "2026-11-15T23:30:00Z"}}]})
        zone = Settings.__dataclass_fields__["zone"].default
        self.assertEqual(usage_line(1, loki, "1.5 GB", "alex", zone),
                         "1. **Loki (2021)** · 1.5 GB · 3 Dateien · 👀 zuletzt 15.11.2026, 1 Person · von alex")
        self.assertEqual(usage_line(2, sintel, "0.6 GB", None, zone),
                         "2. **Sintel (2010)** · 0.6 GB · 1 Datei · 💤 noch nie geschaut")

    def test_folder_name(self):
        items = {"Items": [
            {"Type": "Movie", "ProviderIds": {"Tmdb": "398818"},
             "Path": "/media/movies/Call Me by Your Name (2017)/Call Me By Your Name (2017) [1080p].mp4"},
            {"Type": "Series", "ProviderIds": {"Tmdb": "37854"}, "Path": "/media/shows/One Piece (1999)"},
            {"Type": "Movie", "ProviderIds": {"Tmdb": "1"}}]}
        fake = FakeJellyfin({("GET", "/Items"): (200, items)})
        self.assertEqual(client(fake).folder_name("movie", 398818), "Call Me by Your Name (2017)")
        self.assertEqual(client(fake).folder_name("tv", 37854), "One Piece (1999)")
        self.assertIsNone(client(fake).folder_name("movie", 1))  # no path
        self.assertIsNone(client(fake).folder_name("movie", 5))
        self.assertIn("Fields=ProviderIds%2CPath", fake.calls[0]["query"])

    def test_client_asks_per_user(self):
        calls = []

        def fake(method, url, headers, body):
            calls.append(url)
            if url.endswith("/Users"):
                return 200, [{"Id": "u1", "Name": "admin"}]
            if "IncludeItemTypes=Series" in url:
                return 200, {"Items": SERIES}
            if "userId=u1" in url:
                return 200, {"Items": [{"Id": "e0", "UserData": {"Played": True}}]}
            return 200, {"Items": [MOVIE, *EPISODES]}

        usages = JellyfinClient(BASE, "k", fetch=fake).usage()
        self.assertEqual([u.name for u in usages], ["Loki (2021)", "Sintel (2010)"])
        self.assertEqual(usages[0].viewers, 1)
        self.assertTrue(any("Fields=MediaSources" in c for c in calls))


class WelcomeTest(unittest.TestCase):
    def test_contains_everything(self):
        text = welcome_text("max", "pw123", "http://100.1.2.3:8096", "https://login.tailscale.com/admin/invite/x")
        for part in ("max", "pw123", "http://100.1.2.3:8096", "https://login.tailscale.com/admin/invite/x",
                     "Quick Connect", "SyncPlay"):
            self.assertIn(part, text)
        self.assertIn("im Browser <http://100.1.2.3:8096>", text)  # PC: clickable, without a link preview
        self.assertLess(len(text), 2000)  # one Discord message


class FriendStoreTest(unittest.TestCase):
    def test_survives_a_restart_without_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.save_friend(Friend(11, "Max", "max", 1000.0, state=FRIEND_APPROVED, jellyfin_id="u1"))
            store.save_friend(Friend(12, "Kim", "kim", 1001.0, state=FRIEND_DECLINED))
            again = Store(folder)
            self.assertEqual(again.friend(11).jellyfin_id, "u1")
            self.assertEqual([f.user_id for f in again.all_friends()], [11, 12])
            self.assertTrue(again.name_taken("MAX", 99))
            self.assertFalse(again.name_taken("max", 11))  # their own
            self.assertFalse(again.name_taken("kim", 99))  # declined names are free again
            with open(again.path, encoding="utf-8") as file:
                self.assertNotIn("password", file.read().lower())


class FriendSettingsTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}
    FULL = {"DISCORD_ADMIN_USER_IDS": "3", "JELLYFIN_API_URL": "http://192.168.0.207:8096/",
            "JELLYFIN_API_KEY": "k", "JELLYFIN_FRIEND_URL": "http://100.1.2.3:8096",
            "TAILSCALE_SHARE_URL": "https://login.tailscale.com/admin/invite/x"}

    def load(self, **extra):
        with mock.patch.dict("os.environ", {**self.BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_enabled_only_when_complete(self):
        self.assertFalse(self.load().friends_enabled)
        s = self.load(**self.FULL, JELLYFIN_FRIEND_LIBRARIES="Movies, Shows")
        self.assertTrue(s.friends_enabled)
        self.assertEqual(s.jellyfin_api_url, "http://192.168.0.207:8096")
        self.assertEqual(s.friend_libraries, ("Movies", "Shows"))
        self.assertEqual(s.friend_bitrate_mbit, 4)
        without_admins = {k: v for k, v in self.FULL.items() if k != "DISCORD_ADMIN_USER_IDS"}
        self.assertFalse(self.load(**without_admins).friends_enabled)

    def test_bad_values(self):
        with self.assertRaises(ConfigError):
            self.load(**{**self.FULL, "TAILSCALE_SHARE_URL": "login.tailscale.com/x"})
        with self.assertRaises(ConfigError):
            self.load(**{**self.FULL, "JELLYFIN_API_URL": "192.168.0.207:8096"})
        with self.assertRaises(ConfigError):
            self.load(**{**self.FULL, "JELLYFIN_FRIEND_BITRATE_MBIT": "-1"})


if __name__ == "__main__":
    unittest.main()
