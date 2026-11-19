import tempfile
import unittest

from bot.core import help_text
from bot.jellyfin import JellyfinClient, episode_key, new_episodes_text
from bot.store import Store

COMMANDS = [("film", "Einen Film suchen"), ("status", "Offene Anfragen"), ("löschen", "Löschen (nur Admins)"),
            ("speicher", "Platz (nur Admins)"), ("abo", "Serie abonnieren"), ("hilfe", "Hilfe")]


class HelpTest(unittest.TestCase):
    def test_everyone_without_role(self):
        text = help_text(COMMANDS, admin=False, may_request=False)
        self.assertIn("`/status`", text)
        self.assertIn("`/hilfe`", text)
        self.assertNotIn("/film", text)  # needs the request role
        self.assertNotIn("Admins", text)

    def test_friend_with_role(self):
        text = help_text(COMMANDS, admin=False, may_request=True)
        self.assertIn("`/film Sintel`: Einen Film suchen", text)
        self.assertIn("`/abo South Park`", text)
        self.assertNotIn("/löschen", text)
        self.assertNotIn("/speicher", text)

    def test_admin_sees_everything(self):
        text = help_text(COMMANDS, admin=True, may_request=False)
        mine, admins = text.split("**Nur für Admins**")
        self.assertIn("/film", mine)
        self.assertIn("`/löschen Sintel`", admins)
        self.assertIn("`/speicher`", admins)

    def test_unknown_command_is_shown_plainly(self):
        self.assertIn("`/neuer-befehl`: Etwas Neues", help_text([("neuer-befehl", "Etwas Neues")], False, False))


class EpisodeTextTest(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(episode_key({"ParentIndexNumber": 1, "IndexNumber": 5}), "S01E05")
        self.assertEqual(episode_key({"ParentIndexNumber": 27, "IndexNumber": 1000}), "S27E1000")
        self.assertIsNone(episode_key({"IndexNumber": 5}))

    def test_one_and_many(self):
        self.assertEqual(new_episodes_text("South Park", {"S27E05": "Titel"}),
                         "📺 Neue Folge von **South Park**: S27E05 «Titel» ist jetzt in Jellyfin.")
        text = new_episodes_text("FMA", {"S01E10": "", "S01E11": "", "S01E12": "", "S01E15": "", "S02E01": ""})
        self.assertEqual(text, "📺 **5 neue Folgen** von **FMA** sind jetzt in Jellyfin "
                               "(Staffel 1: Folge 10–12, 15; Staffel 2: Folge 1).")

    def test_client_episodes(self):
        def fake(method, url, headers, body):
            self.assertIn("ParentId=s1", url)
            return 200, {"Items": [{"Name": "Pilot", "ParentIndexNumber": 1, "IndexNumber": 1},
                                   {"Name": "Extra"},  # no numbers: skipped
                                   {"Name": "Zwei", "ParentIndexNumber": 1, "IndexNumber": 2}]}
        self.assertEqual(JellyfinClient("http://j", "k", fetch=fake).episodes("s1"), {"S01E01": "Pilot", "S01E02": "Zwei"})


class AboStoreTest(unittest.TestCase):
    def test_subscribe_announce_unsubscribe(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            self.assertTrue(store.toggle_abo(11, "s1", "South Park (1997)", ["S01E01", "S01E02"]))
            self.assertEqual(store.new_episodes("s1", ["S01E01", "S01E02"]), [])  # old episodes are not news
            self.assertTrue(store.toggle_abo(12, "s1", "South Park (1997)", ["S01E01"]))  # shares what is known
            again = Store(folder)  # survives a restart
            self.assertEqual(again.subscribers("s1"), [11, 12])
            self.assertEqual(again.new_episodes("s1", ["S01E01", "S01E02", "S01E03"]), ["S01E03"])
            self.assertEqual(again.new_episodes("s1", ["S01E01", "S01E02", "S01E03"]), [])  # only once
            self.assertFalse(again.toggle_abo(11, "s1", "South Park (1997)", []))
            self.assertEqual(again.subscribers("s1"), [12])
            self.assertFalse(again.toggle_abo(12, "s1", "South Park (1997)", []))
            self.assertEqual(again.subscribed_series(), [])
            self.assertNotIn("s1", again.abo_known)  # forgotten when nobody follows it


if __name__ == "__main__":
    unittest.main()
