import unittest
from datetime import timezone
from unittest import mock

from bot.config import ConfigError, Settings

BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}


class SettingsTest(unittest.TestCase):
    def load(self, **extra):
        with mock.patch.dict("os.environ", {**BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_defaults(self):
        s = self.load()
        self.assertEqual((s.guild_id, s.channel_id, s.request_role_id, s.seerr_user_id), (1, 2, None, None))
        self.assertEqual(s.seerr_url, "http://localhost:5055")
        self.assertEqual(s.seerr_language, "de")
        self.assertIsNone(s.page_url("movie", 1))

    def test_values(self):
        s = self.load(SEERR_URL="http://192.168.1.20:5055/", DISCORD_REQUEST_ROLE_ID="7", SEERR_USER_ID="3",
                      SEERR_PUBLIC_URL="http://seerr.home/", SEERR_LANGUAGE="en")
        self.assertEqual(s.seerr_url, "http://192.168.1.20:5055")
        self.assertEqual((s.request_role_id, s.seerr_user_id, s.seerr_language), (7, 3, "en"))
        self.assertEqual(s.page_url("tv", 42), "http://seerr.home/tv/42")

    def test_admins_and_space(self):
        s = self.load()
        self.assertFalse(s.deleting_enabled)
        self.assertEqual(s.min_free_gb, 15)
        s = self.load(DISCORD_ADMIN_USER_IDS="3, 4", MIN_FREE_GB="20")
        self.assertEqual(s.admin_user_ids, frozenset({3, 4}))
        self.assertTrue(s.deleting_enabled)
        self.assertEqual(s.min_free_gb, 20)
        self.assertTrue(self.load(DISCORD_ADMIN_ROLE_ID="9").deleting_enabled)
        with self.assertRaises(ConfigError):
            self.load(DISCORD_ADMIN_USER_IDS="3;4")

    def test_quotes_are_removed(self):
        s = self.load(DISCORD_TOKEN='"a.b.c"', SEERR_URL="'http://seerr:5055'", DISCORD_CHANNEL_ID='"5"')
        self.assertEqual((s.discord_token, s.seerr_url, s.channel_id), ("a.b.c", "http://seerr:5055", 5))

    def test_other_app_values_are_not_a_token(self):
        for value, hint in (("ab12" * 16, "Public Key"), ("1234567890123456789", "ID"),
                            ("Ab-_" * 8, "Client Secret")):
            with self.assertRaisesRegex(ConfigError, hint, msg=value):
                self.load(DISCORD_TOKEN=value)
        self.assertEqual(self.load(DISCORD_TOKEN="MTIz.Gx1y2z.abc_DEF-123").discord_token, "MTIz.Gx1y2z.abc_DEF-123")

    def test_newer_settings_defaults(self):
        s = self.load()
        self.assertEqual((s.request_limit, s.request_limit_days, s.weekly_digest), (0, 7, None))
        self.assertEqual((s.data_dir, s.movie_profiles, s.tv_profiles), ("data", (), ()))
        self.assertEqual(s.approvals_channel, 2)
        self.assertFalse(s.approvals_enabled)
        self.assertEqual(s.zone, timezone.utc)
        self.assertEqual(s.watch_url("http://192.168.1.7:8096/web/#!/details?id=1"), "http://192.168.1.7:8096/web/#!/details?id=1")
        self.assertIsNone(s.watch_url(None))
        self.assertIsNone(s.issue_url(3))

    def test_newer_settings(self):
        s = self.load(DISCORD_APPROVAL_CHANNEL_ID="9", DISCORD_ADMIN_USER_IDS="3", REQUEST_LIMIT="3",
                      REQUEST_LIMIT_DAYS="14", WEEKLY_DIGEST="so 18:30", DATA_DIR="/data",
                      MOVIE_PROFILES="HD-1080p, Ultra-HD", TV_PROFILES="HD-1080p", JELLYFIN_URL="http://jellyfin.home/",
                      SEERR_PUBLIC_URL="http://seerr.home")
        self.assertEqual((s.approvals_channel, s.approvals_enabled), (9, True))
        self.assertEqual((s.request_limit, s.request_limit_days, s.weekly_digest), (3, 14, (6, 18, 30)))
        self.assertEqual(s.profile_names("movie"), ("HD-1080p", "Ultra-HD"))
        self.assertEqual(s.profile_names("tv"), ("HD-1080p",))
        self.assertEqual(s.watch_url("http://192.168.1.7:8096/web/#!/details?id=1"), "http://jellyfin.home/web/#!/details?id=1")
        self.assertEqual(s.issue_url(3), "http://seerr.home/issues/3")

    def test_errors(self):
        for env in ({"SEERR_API_KEY": ""}, {"DISCORD_GUILD_ID": "abc"}, {"SEERR_URL": "seerr:5055"},
                    {"SEERR_PUBLIC_URL": "seerr.home"}, {"DISCORD_TOKEN": ""}, {"WEEKLY_DIGEST": "sunday"},
                    {"TIMEZONE": "Mars/Olympus"}, {"REQUEST_LIMIT": "-1"}, {"REQUEST_LIMIT_DAYS": "0"},
                    {"JELLYFIN_URL": "jellyfin.home"}):
            with self.assertRaises(ConfigError, msg=env):
                self.load(**env)


if __name__ == "__main__":
    unittest.main()
