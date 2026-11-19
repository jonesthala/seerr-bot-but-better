import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bot.config import Settings
from bot.core import GB, Title, deleted_text
from bot.torrents import ArrClient, QbitClient, TorrentCleaner, TorrentError

QBIT = "http://qbit.test:8080"


class FakeArr:
    """Sonarr/Radarr: series/movies and their history."""

    def __init__(self, status=200):
        self.status = status
        self.paths = []

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.paths.append(parts.path)
        if self.status != 200:
            return self.status, None
        if parts.path.endswith("/history/series"):
            assert query["seriesId"] == "8"
            return 200, [{"eventType": "grabbed", "downloadId": "AAA"}, {"eventType": "downloadFolderImported", "downloadId": "AAA"},
                         {"eventType": "grabbed", "downloadId": "BBB"}, {"eventType": "episodeFileRenamed"}]
        if parts.path.endswith("/history/movie"):
            return 200, [{"downloadId": "CCC"}]
        if parts.path.endswith("/series"):
            return 200, [{"id": 8, "tmdbId": 31911}, {"id": 6, "tmdbId": 2190}]
        if parts.path.endswith("/movie"):
            return 200, [{"id": 3, "tmdbId": query.get("tmdbId") and int(query["tmdbId"])}]
        return 404, None


class FakeQbit:
    """qBittorrent: login sets SID, torrents/info and torrents/delete need it."""

    def __init__(self, password="richtig", torrents=None):
        self.password = password
        self.torrents = torrents if torrents is not None else [
            {"hash": "aaa", "name": "Show S01", "size": 10 * GB}, {"hash": "bbb", "name": "Show 02", "size": 2 * GB}]
        self.deleted = None
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        self.calls.append(path)
        form = {k: v[0] for k, v in parse_qs((body or b"").decode()).items()}
        if path == "/api/v2/auth/login":
            assert headers.get("Referer") == QBIT  # qBittorrent's CSRF check
            if form.get("password") != self.password:
                return 200, {}, b"Fails."
            return 200, {"set-cookie": "SID=abc123; HttpOnly; path=/"}, b"Ok."
        if headers.get("Cookie") != "SID=abc123":
            return 403, {}, b"Forbidden"
        if path == "/api/v2/torrents/info":
            wanted = parse_qs(urlsplit(url).query)["hashes"][0].split("|")
            return 200, {}, json.dumps([t for t in self.torrents if t["hash"] in wanted]).encode()
        if path == "/api/v2/torrents/delete":
            self.deleted = form
            return 200, {}, b""
        return 404, {}, b""


def cleaner(qbit, arr=None):
    arr = arr or FakeArr()
    return TorrentCleaner(QbitClient(QBIT, "admin", "richtig", fetch=qbit),
                          {"tv": ArrClient("http://sonarr", "k", "tv", fetch=arr),
                           "movie": ArrClient("http://radarr", "k", "movie", fetch=arr)})


class TorrentTest(unittest.TestCase):
    def test_finds_and_removes_with_files(self):
        qbit = FakeQbit()
        clean = cleaner(qbit)
        found = clean.prepare("tv", 31911)
        self.assertEqual([t["hash"] for t in found], ["aaa", "bbb"])  # from the history, lower case, once each
        self.assertEqual(clean.remove(found), (2, 12 * GB))
        self.assertEqual(qbit.deleted, {"hashes": "aaa|bbb", "deleteFiles": "true"})

    def test_movie(self):
        qbit = FakeQbit(torrents=[{"hash": "ccc", "size": GB}])
        self.assertEqual([t["hash"] for t in cleaner(qbit).prepare("movie", 45745)], ["ccc"])

    def test_nothing_left_in_qbittorrent(self):
        qbit = FakeQbit(torrents=[])
        clean = cleaner(qbit)
        self.assertEqual(clean.remove(clean.prepare("tv", 31911)), (0, 0))
        self.assertIsNone(qbit.deleted)

    def test_wrong_password_stops_before_deleting(self):
        with self.assertRaises(TorrentError) as caught:
            cleaner(FakeQbit(password="anders")).prepare("tv", 31911)
        self.assertIn("QBITTORRENT_PASSWORD", str(caught.exception))

    def test_arr_down(self):
        with self.assertRaises(TorrentError) as caught:
            cleaner(FakeQbit(), FakeArr(status=401)).prepare("tv", 31911)
        self.assertIn("Sonarr", str(caught.exception))

    def test_relogin_after_expired_session(self):
        qbit = FakeQbit()
        client = QbitClient(QBIT, "admin", "richtig", fetch=qbit)
        client._sid = "alt"  # expired
        self.assertEqual(len(client.torrents(["aaa"])), 1)
        self.assertIn("/api/v2/auth/login", qbit.calls)


class TextTest(unittest.TestCase):
    SHOW = Title("tv", 1, "Show", "2020", "", None, 5)

    def test_messages(self):
        self.assertIn("2 Torrents in qBittorrent", deleted_text(self.SHOW, (2, 12 * GB)))
        self.assertIn("Frei geworden: etwa 12 GB", deleted_text(self.SHOW, (2, 12 * GB)))
        self.assertIn("kein Torrent mehr", deleted_text(self.SHOW, (0, 0)))
        self.assertIn("noch in qBittorrent", deleted_text(self.SHOW, None, "qBittorrent ist nicht erreichbar."))
        self.assertIn("erst frei", deleted_text(self.SHOW))  # without qBittorrent access, as before


class SettingsTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}

    def load(self, **extra):
        with mock.patch.dict("os.environ", {**self.BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_enabled(self):
        self.assertFalse(self.load().torrent_cleanup_enabled)
        qbit = {"QBITTORRENT_URL": "http://192.168.0.103:8080", "QBITTORRENT_USERNAME": "admin", "QBITTORRENT_PASSWORD": "pw"}
        self.assertFalse(self.load(**qbit).torrent_cleanup_enabled)  # needs Sonarr or Radarr
        self.assertTrue(self.load(**qbit, SONARR_URL="http://s:8989", SONARR_API_KEY="k").torrent_cleanup_enabled)


if __name__ == "__main__":
    unittest.main()
