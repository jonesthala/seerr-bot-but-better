import json
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bot.config import ConfigError, Settings
from bot.kavita import KavitaClient, KavitaError, ReaderSync, _query, _sample, series_link
from bot.manga import MangaService, MangaSource, Release, parse_selection
from bot.store import Store
from bot.torrents import QbitClient

from test_manga import HASH, INFO, QBIT, FakeNet, FakeQbitServer

KAVITA = "http://kavita.test:5000"


class FakeKavita:
    """Kavita's scan-folder, plugin login, file search and series-for-file, with one admin key."""

    def __init__(self):
        self.files: list[str] = []  # paths Kavita has read
        self.scanned: list[str] = []
        self.logins = 0
        self.tokens: set[str] = set()
        self.up = True

    def __call__(self, method, url, headers, body):
        if not self.up:
            raise OSError("Connection refused")
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.path == "/api/library/scan-folder":
            data = json.loads(body)
            if data["apiKey"] != "key":
                return 401, {}, b""
            assert data["abortOnNoSeriesMatch"] is False and headers["Content-Type"] == "application/json"
            self.scanned.append(data["folderPath"])
            return 200, {}, b""
        if parts.path == "/api/plugin/authenticate":
            if query.get("apiKey") != "key" or query.get("pluginName") != "seerr-bot":
                return 401, {}, b""
            self.logins += 1
            token = f"jwt{self.logins}"
            self.tokens.add(token)
            return 200, {}, json.dumps({"username": "admin", "token": token}).encode()
        if headers.get("Authorization", "").removeprefix("Bearer ") not in self.tokens:
            return 401, {}, b""
        if parts.path == "/api/search/search":
            assert query["includeChapterAndFiles"] == "true"
            hits = [{"id": i, "filePath": p} for i, p in enumerate(self.files, 1) if query["queryString"] in p]
            return 200, {}, json.dumps({"series": [], "files": hits[:15]}).encode()
        if parts.path == "/api/search/series-for-mangafile":
            return 200, {}, json.dumps({"id": 7, "libraryId": 1, "name": "Tokyo Ghoul"}).encode()
        return 404, {}, b""


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.kavita = FakeKavita()
        self.client = KavitaClient(KAVITA, "key", fetch=self.kavita)

    def test_scan_and_find(self):
        self.client.scan("/manga/Pack")
        self.assertEqual(self.kavita.scanned, ["/manga/Pack"])
        paths = ["/manga/Pack/Pack v01.cbz", "/manga/Pack/Pack v02.cbz"]
        self.assertEqual(self.client.find(paths), (False, None))
        self.kavita.files = ["/manga/Pack/Pack v01.cbz"]
        self.assertEqual(self.client.find(paths), (False, None))
        self.kavita.files = ["/manga/Other/Pack v02.cbz", *paths]  # same name elsewhere doesn't count
        self.assertEqual(self.client.find(paths), (True, (1, 7)))
        self.assertEqual(self.client.find([]), (True, None))

    def test_token_is_reused_and_renewed(self):
        self.kavita.files = ["/manga/P/P v01.cbz"]
        self.client.find(["/manga/P/P v01.cbz"])
        self.client.find(["/manga/P/P v01.cbz"])
        self.assertEqual(self.kavita.logins, 1)
        self.kavita.tokens.clear()  # Kavita restarted: the old token is no good
        self.assertEqual(self.client.find(["/manga/P/P v01.cbz"]), (True, (1, 7)))
        self.assertEqual(self.kavita.logins, 2)

    def test_wrong_key_and_down(self):
        client = KavitaClient(KAVITA, "wrong", fetch=self.kavita)
        with self.assertRaisesRegex(KavitaError, "KAVITA_API_KEY"):
            client.scan("/manga/P")
        with self.assertRaisesRegex(KavitaError, "KAVITA_API_KEY"):
            client.find(["/manga/P/P v01.cbz"])
        self.kavita.up = False
        with self.assertLogs("seerrbot.kavita", "WARNING") as logs, self.assertRaisesRegex(KavitaError, "erreichbar"):
            self.client.find(["/manga/P/P v01.cbz"])
        self.assertNotIn("key", " ".join(logs.output))  # the login's query holds the key, it is never logged

    def test_search_text_sample_and_link(self):
        self.assertEqual(_query("/manga/Re:Zero/Re:Zero - Starting Life v01 (2016).cbz"), "Zero - Starting Life v01 (2016)")
        self.assertEqual(_query("/manga/P/P v01.cbz"), "P v01")
        many = [f"/manga/P/{n}.cbz" for n in range(30)]
        self.assertEqual(_sample(many), many[:10] + many[-10:])
        self.assertEqual(series_link("http://manga.home/", (1, 7)), "http://manga.home/library/1/series/7")
        self.assertEqual(series_link("http://manga.home", None), "http://manga.home")
        self.assertEqual(series_link("", (1, 7)), "")


class ReaderSyncTest(unittest.TestCase):
    def setUp(self):
        self.qbit = FakeQbitServer()
        self.service = MangaService(MangaSource("http://prowlarr.test", "key", fetch=FakeNet()),
                                    QbitClient(QBIT, "u", "p", fetch=self.qbit), sleep=lambda s: None)
        key, prepared = self.service.prepare(Release(1608677, "Tokyo Ghoul", 5, 58, 0, HASH, ""))
        self.service.add(key, parse_selection("10-11", prepared.parts))
        self.kavita = FakeKavita()
        self.sync = ReaderSync(self.service, KavitaClient(KAVITA, "key", fetch=self.kavita), "/manga/")
        folder = f"/manga/{INFO['name']}"
        self.paths = [f"{folder}/Tokyo Ghoul v{n} (2016) (Digital).cbz" for n in (10, 11)]

    def test_scans_when_done_then_finds(self):
        self.assertEqual(self.sync.start([HASH]), [])  # still loading
        self.qbit.torrents[HASH]["state"] = "pausedUP"
        item, = self.sync.start([HASH])
        self.assertEqual((item.name, item.scanned, item.paths), (INFO["name"], True, self.paths))
        self.assertEqual(self.kavita.scanned, [f"/manga/{INFO['name']}"])
        seen, = self.sync.check({HASH: item.paths})
        self.assertFalse(seen.found)
        self.kavita.files = list(self.paths)
        seen, = self.sync.check({HASH: item.paths})
        self.assertEqual((seen.found, seen.series), (True, (1, 7)))

    def test_without_kavita_or_when_it_is_down(self):
        self.qbit.torrents[HASH]["state"] = "pausedUP"
        plain = ReaderSync(self.service, None)
        item, = plain.start([HASH])
        self.assertFalse(item.scanned)
        self.assertEqual(plain.check({HASH: self.paths}), [])
        self.kavita.up = False
        with self.assertLogs("seerrbot.kavita", "WARNING"):
            item, = self.sync.start([HASH])
            self.assertFalse(item.scanned)  # then the message goes out right away, like before
            self.assertEqual(self.sync.check({HASH: self.paths}), [])
        gone, = self.sync.start(["f" * 40])
        self.assertEqual((gone.name, gone.scanned), ("", False))

    def test_single_file_lies_in_the_library_folder(self):
        self.qbit.torrents["a" * 40] = {"hash": "a" * 40, "name": "One.cbz", "category": "manga", "state": "pausedUP"}
        self.qbit.files["a" * 40] = [{"index": 0, "name": "One.cbz", "size": 1, "progress": 1, "priority": 1}]
        self.assertEqual(self.sync.reader_paths("a" * 40), ("/manga", ["/manga/One.cbz"]))


class ConfigAndStoreTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k",
            "QBITTORRENT_URL": "http://q:8080", "QBITTORRENT_USERNAME": "u", "QBITTORRENT_PASSWORD": "p",
            "PROWLARR_URL": "http://p:9696", "PROWLARR_API_KEY": "k"}

    def load(self, **extra):
        with mock.patch.dict("os.environ", {**self.BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_settings(self):
        self.assertFalse(self.load().kavita_enabled)
        s = self.load(KAVITA_URL="http://192.168.0.103:5000/", KAVITA_API_KEY="key")
        self.assertEqual((s.kavita_enabled, s.kavita_url, s.kavita_library_dir),
                         (True, "http://192.168.0.103:5000", "/manga"))
        self.assertEqual(self.load(KAVITA_URL="http://k", KAVITA_API_KEY="key", KAVITA_LIBRARY_DIR="/books/").kavita_library_dir,
                         "/books")
        for bad in ({"KAVITA_URL": "http://k"}, {"KAVITA_URL": "http://k", "KAVITA_API_KEY": "key",
                                                 "KAVITA_LIBRARY_DIR": "manga"},
                    {"KAVITA_URL": "http://k", "KAVITA_API_KEY": "key", "KAVITA_LIBRARY_DIR": "/manga/../etc"}):
            with self.assertRaises(ConfigError):
                self.load(**bad)

    def test_store_keeps_the_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, now=lambda: 100.0)
            store.manga_wait("ABC", "Band 10–14", 42)
            store.manga_scanned("abc", "Pack", ["/manga/Pack/Pack v10.cbz"])
            store.manga_scanned("def", "Nobody waits", [])  # ignored
            again = Store(folder)
            self.assertEqual(again.manga_pending, {"abc": {"label": "Band 10–14", "user_id": 42, "since": 100.0,
                                                           "scanned": 100.0, "name": "Pack",
                                                           "paths": ["/manga/Pack/Pack v10.cbz"]}})
            store.manga_wait("abc", "Band 1", 42)  # more volumes wanted: loading again, no scan yet
            self.assertNotIn("scanned", Store(folder).manga_pending["abc"])


if __name__ == "__main__":
    unittest.main()
