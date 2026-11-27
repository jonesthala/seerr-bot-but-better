import json
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from bot.config import ConfigError, Settings
from bot.signup import (SignupClient, SignupError, WebRequest, arrival, card_text, free_name, mask_email, plain,
                        wished_name)
from bot.store import FRIEND_APPROVED, FRIEND_DECLINED, FRIEND_PENDING, FRIEND_REVOKED, Friend, Store

SITE = "https://zugang.example.test"
TOKEN = "t" * 40


class FakeSite:
    """The form site's bot endpoints, like jellyfin-zugang on Vercel."""

    def __init__(self):
        self.requests = {"a1": {"id": "a1", "name": "Max Muster", "email": "Max@Example.test", "benutzer": "",
                                "zeit": 1_700_000_000_000}}
        self.approved, self.declined = [], []
        self.status = None

    def __call__(self, method, url, headers, body):
        if self.status:
            return self.status, {}, json.dumps({"fehler": "kaputt"}).encode()
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {}, b'{"fehler":"Nicht erlaubt."}'
        path = urlsplit(url).path
        data = json.loads(body) if body else {}
        if (method, path) == ("GET", "/api/bot/anfragen"):
            return 200, {}, json.dumps({"anfragen": list(self.requests.values()) + [{"id": "", "email": "x"}, "kaputt"]}).encode()
        if (method, path) == ("POST", "/api/bot/freigeben"):
            if data["id"] not in self.requests:
                return 404, {}, b'{"fehler":"Diese Anfrage gibt es nicht (mehr)."}'
            assert headers["Content-Type"] == "application/json"
            self.approved.append(data)
            del self.requests[data["id"]]
            return 200, {}, b'{"ok":true}'
        if (method, path) == ("POST", "/api/bot/ablehnen"):
            self.declined.append(data)
            self.requests.pop(data["id"], None)
            return 200, {}, b'{"ok":true}'
        return 404, {}, b""


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite()
        self.client = SignupClient(SITE + "/", TOKEN, fetch=self.site)

    def test_pending_approve_decline(self):
        request, = self.client.pending()
        self.assertEqual((request.id, request.name, request.email, request.username, request.created),
                         ("a1", "Max Muster", "max@example.test", "", 1_700_000_000.0))
        self.client.approve("a1", "max.muster", "pw", "http://100.1.2.3:8096", "https://share", True)
        self.assertEqual(self.site.approved, [{"id": "a1", "benutzer": "max.muster", "passwort": "pw",
                                               "server": "http://100.1.2.3:8096", "tailscale": "https://share",
                                               "neu": True}])
        with self.assertRaises(SignupError) as gone:
            self.client.approve("a1", "max.muster", "pw", "http://s", "https://t", False)
        self.assertEqual(gone.exception.status, 404)
        self.assertIn("gibt es nicht", str(gone.exception))
        self.client.decline("b2", True)
        self.assertEqual(self.site.declined, [{"id": "b2", "mail": True}])

    def test_wrong_token_and_down(self):
        with self.assertRaisesRegex(SignupError, "SIGNUP_TOKEN"):
            SignupClient(SITE, "falsch", fetch=self.site).pending()
        self.site.status = 500
        with self.assertRaisesRegex(SignupError, "HTTP 500: kaputt"):
            self.client.pending()

        def down(*_):
            raise OSError("refused")
        with self.assertLogs("seerrbot.signup", "WARNING"), self.assertRaisesRegex(SignupError, "nicht erreichbar"):
            SignupClient(SITE, TOKEN, fetch=down).pending()


class NamesTest(unittest.TestCase):
    def request(self, name="Max Muster", email="max@example.test", username=""):
        return WebRequest("a1", name, email, username, 0)

    def test_wished_name(self):
        self.assertEqual(wished_name(self.request(username="maxi")), "maxi")
        self.assertEqual(wished_name(self.request(username="a b")), "Max.Muster")
        self.assertEqual(wished_name(self.request(name="Zoë")), "Zoe")
        self.assertEqual(wished_name(self.request(name="Jürg Müller")), "Juerg.Mueller")
        self.assertEqual(wished_name(self.request(name="李", email="jo.ho@x.test")), "jo.ho")
        self.assertEqual(wished_name(self.request(name="李", email="x@x.test")), "freund")

    def test_free_name(self):
        taken = {"max", "max2"}
        self.assertEqual(free_name("anna", taken.__contains__), "anna")
        self.assertEqual(free_name("max", taken.__contains__), "max3")
        self.assertEqual(free_name("a" * 20, {"a" * 20}.__contains__), "a" * 19 + "2")
        with self.assertRaises(SignupError):
            free_name("x", lambda n: True)

    def test_mask_plain_and_card(self):
        self.assertEqual(mask_email("max@example.test"), "m***@example.test")
        self.assertEqual(plain("**Max**_`x`"), "Maxx")
        friend = Friend(user_id=-1, user_name="Max", jellyfin_name="max", created=0, email="max@example.test")
        self.assertEqual(card_text(friend), "🔑 **Max** 🌐 (m***@example.test, über die Webseite) möchte Zugang zu "
                                            "Jellyfin als **max**.")
        friend.jellyfin_id = "j1"
        self.assertIn("wieder geöffnet", card_text(friend))
        friend.renew = True
        self.assertIn("neue Zugangsdaten für **max**", card_text(friend))


class ArrivalTest(unittest.TestCase):
    def friend(self, state, decided=0.0, jellyfin_id=""):
        return Friend(user_id=-1, user_name="M", jellyfin_name="m", created=0, state=state, decided=decided,
                      jellyfin_id=jellyfin_id, email="m@x.test")

    def test_cases(self):
        now = 10 * 86400
        self.assertEqual(arrival(None, now), "new")
        self.assertEqual(arrival(self.friend(FRIEND_PENDING), now), "replace")
        self.assertEqual(arrival(self.friend(FRIEND_APPROVED, jellyfin_id="j"), now), "renew")
        self.assertEqual(arrival(self.friend(FRIEND_DECLINED, decided=now - 86400), now), "drop")
        self.assertEqual(arrival(self.friend(FRIEND_DECLINED, decided=now - 8 * 86400), now), "new")
        self.assertEqual(arrival(self.friend(FRIEND_REVOKED, jellyfin_id="j"), now), "reopen")


class StoreTest(unittest.TestCase):
    def test_web_friends(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.save_friend(Friend(user_id=123, user_name="Disco", jellyfin_name="disco", created=1))
            self.assertEqual(store.next_web_id(), -1)
            web = Friend(user_id=store.next_web_id(), user_name="Max", jellyfin_name="Max.M", created=2,
                         email="max@example.test", web_id="a1", web_reply="welcome", state=FRIEND_APPROVED)
            store.save_friend(web)
            self.assertEqual(store.next_web_id(), -2)
            again = Store(folder)
            loaded = again.friend(-1)
            self.assertTrue(loaded.from_web)
            self.assertFalse(again.friend(123).from_web)
            self.assertEqual((loaded.email, loaded.web_id, loaded.web_reply), ("max@example.test", "a1", "welcome"))
            self.assertIs(again.friend_by_email(" MAX@example.test"), loaded)
            self.assertIs(again.friend_by_name("max.m"), loaded)
            self.assertIs(again.friend_by_web_id("a1"), loaded)
            self.assertIsNone(again.friend_by_web_id(""))
            self.assertEqual(again.web_outbox(), [loaded])
            self.assertTrue(again.name_taken("MAX.M", 123))
            loaded.web_reply = ""
            self.assertEqual(again.web_outbox(), [])


class SettingsTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k",
            "DISCORD_ADMIN_USER_IDS": "5", "JELLYFIN_API_URL": "http://j:8096", "JELLYFIN_API_KEY": "k",
            "JELLYFIN_FRIEND_URL": "http://100.1.2.3:8096", "TAILSCALE_SHARE_URL": "https://login.tailscale.com/x"}

    def load(self, **extra):
        with mock.patch.dict("os.environ", {**self.BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_settings(self):
        self.assertFalse(self.load().signup_enabled)
        s = self.load(SIGNUP_URL="https://zugang.example.test/", SIGNUP_TOKEN=TOKEN)
        self.assertEqual((s.signup_enabled, s.signup_url), (True, "https://zugang.example.test"))
        with self.assertRaises(ConfigError):
            self.load(SIGNUP_URL="https://zugang.example.test", SIGNUP_TOKEN="kurz")
        with self.assertRaises(ConfigError):
            self.load(SIGNUP_URL="http://zugang.example.test", SIGNUP_TOKEN=TOKEN)
        with mock.patch.dict("os.environ", {**self.BASE, "SIGNUP_URL": "https://z.test", "SIGNUP_TOKEN": TOKEN},
                             clear=True):
            plain_env = {k: v for k, v in self.BASE.items() if k != "TAILSCALE_SHARE_URL"}
        with mock.patch.dict("os.environ", {**plain_env, "SIGNUP_URL": "https://z.test", "SIGNUP_TOKEN": TOKEN},
                             clear=True):
            self.assertFalse(Settings.from_env().signup_enabled)  # needs the Jellyfin friend setup


if __name__ == "__main__":
    unittest.main()
