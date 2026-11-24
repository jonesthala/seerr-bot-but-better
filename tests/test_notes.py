import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot.config import ConfigError, Settings
from bot.notes import NotesError, note_text, obsidian_link, parse_note, read_notes

BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}

TODO = """---
tags: [betrieb, todo]
---
# Offene Punkte

Ergebnis vom Check.

## 🔴 Wichtig
- [x] **Backups**: erledigt → [[Backups]]
- [ ] **Zweite Disk**: alles liegt auf einer NVMe
  - [x] Platte bestellt
  - [ ] Platte einbauen
  - Notiz dazu ohne Haken
- [ ] **qBittorrent-Passwort** ([[media|Medien]])

> [!note] Callout, kein Punkt
> - [ ] gehört zum Callout

## 🟢 Nice-to-have
* [ ] 2FA für Proxmox, siehe [Doku](https://pve.proxmox.com)
```
- [ ] im Codeblock, zählt nicht
```
"""

IDEAS = """# Projektideen
## ✅ Umgesetzt
- **Minecraft on demand**
## 💡 Neu
- ✅ ⭐ **Notbremse** (seit 19.11.)
- ⭐ **Apps statt Browser**
  - Jellyfin-App
- **Exit-Node**
"""


class ParseTest(unittest.TestCase):
    def test_tasks(self):
        note = parse_note(TODO)
        self.assertEqual(note["title"], "Offene Punkte")
        self.assertEqual([s["title"] for s in note["sections"]], ["🔴 Wichtig", "🟢 Nice-to-have"])
        important = note["sections"][0]
        self.assertTrue(important["important"])
        self.assertEqual(len(important["items"]), 3)  # the callout line is not one
        first, second, third = important["items"]
        self.assertEqual((first["task"], first["done"]), (True, True))
        self.assertEqual(first["text"], "**Backups**: erledigt → Backups")
        self.assertEqual([(c["task"], c["done"]) for c in second["children"]], [(True, True), (True, False), (False, False)])
        self.assertEqual(third["text"], "**qBittorrent-Passwort** (Medien)")
        self.assertEqual(note["sections"][1]["items"][0]["text"], "2FA für Proxmox, siehe Doku")
        # open: zweite Disk, Platte einbauen, Passwort, 2FA; done: Backups, Platte bestellt
        self.assertEqual((note["open"], note["done"], note["important"], note["ideas"]), (4, 2, 3, 0))

    def test_ideas(self):
        note = parse_note(IDEAS)
        done_section, new = note["sections"]
        self.assertTrue(done_section["done"])
        brake, apps, exit_node = new["items"]
        self.assertEqual((brake["done"], brake["star"], brake["text"]), (True, True, "**Notbremse** (seit 19.11.)"))
        self.assertEqual((apps["done"], apps["star"], apps["task"]), (False, True, False))
        self.assertEqual(apps["children"][0]["text"], "Jellyfin-App")
        self.assertFalse(exit_node["star"])
        self.assertEqual((note["open"], note["ideas"]), (0, 2))  # Apps and Exit-Node; done ones don't count

    def test_text(self):
        self.assertEqual(note_text("[[Ordner/Notiz#Teil]] und [[a|b]] und [x](http://y)"), "Notiz und b und x")
        self.assertEqual(parse_note("nur Text\n")["sections"], [])
        self.assertEqual(parse_note("- [ ] ohne Überschrift")["sections"][0]["title"], "")

    def test_obsidian_link(self):
        self.assertEqual(obsidian_link("homelab-brain", "Betrieb/Offene Punkte.md"),
                         "obsidian://open?vault=homelab-brain&file=Betrieb%2FOffene%20Punkte")
        self.assertIsNone(obsidian_link("", "a.md"))


class ReadTest(unittest.TestCase):
    def test_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Betrieb").mkdir()
            (root / "Betrieb" / "Offene Punkte.md").write_text(TODO, encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "FETCH_HEAD").write_text("x", encoding="utf-8")
            os.utime(root / ".git" / "FETCH_HEAD", (1_800_000_000, 1_800_000_000))
            data = read_notes(tmp, ("Betrieb/Offene Punkte.md", "Fehlt.md", "../draussen.md"), "vault")
            self.assertEqual([n["title"] for n in data["notes"]], ["Offene Punkte", "Fehlt"])  # never outside the folder
            self.assertEqual(data["notes"][0]["open"], 4)
            self.assertTrue(data["notes"][1]["missing"])
            self.assertEqual(data["fetched"], 1_800_000_000)
            self.assertTrue(data["notes"][0]["obsidian"].startswith("obsidian://open?vault=vault"))
        with self.assertRaises(NotesError):
            read_notes(os.path.join(tmp, "weg"), ("a.md",))


class SettingsTest(unittest.TestCase):
    def load(self, **extra):
        with mock.patch.dict("os.environ", {**BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_todo_settings(self):
        s = self.load(PANEL_TODO_DIR="/brain", PANEL_TODO_FILES="Betrieb/Offene Punkte.md, Ideen/Projektideen.md",
                      PANEL_OBSIDIAN_VAULT="homelab-brain")
        self.assertEqual(s.panel_todo_files, ("Betrieb/Offene Punkte.md", "Ideen/Projektideen.md"))
        self.assertEqual((s.panel_todo_dir, s.panel_obsidian_vault), ("/brain", "homelab-brain"))
        self.assertEqual(self.load().panel_todo_dir, "")
        for env in ({"PANEL_TODO_DIR": "/brain"}, {"PANEL_TODO_DIR": "/b", "PANEL_TODO_FILES": "../etc/passwd"},
                    {"PANEL_TODO_DIR": "/b", "PANEL_TODO_FILES": "/etc/passwd"}):
            with self.assertRaises(ConfigError, msg=env):
                self.load(**env)


if __name__ == "__main__":
    unittest.main()
