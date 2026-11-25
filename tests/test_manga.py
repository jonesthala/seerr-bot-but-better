import hashlib
import json
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from bot.config import ConfigError, Settings
from bot.manga import (MangaError, MangaService, MangaSource, Release, classify, item_data, parse_selection, part_rows,
                       parts_from_files, qbit_parts, read_torrent, releases_from, selection_label, short_name)
from bot.store import Store
from bot.torrents import QbitClient

MB = 1024 * 1024
QBIT = "http://qbit.test:8080"


def bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(value)


INFO = {"name": "Tokyo Ghoul (2015-2017) (Digital)", "piece length": 262144, "pieces": b"\0" * 20, "files": [
    {"path": [f"Tokyo Ghoul v{n:02d} (2016) (Digital).cbz"], "length": (400 + n) * MB} for n in range(1, 15)
] + [{"path": [".pad", "1234"], "length": 100, "attr": "p"}, {"path": ["Readme.txt"], "length": 300}]}
TORRENT = bencode({"announce": "http://tracker.test/announce", "info": INFO})
HASH = hashlib.sha1(bencode(INFO)).hexdigest()


class TorrentFileTest(unittest.TestCase):
    def test_reads_name_files_and_hash(self):
        t = read_torrent(TORRENT)
        self.assertEqual(t.info_hash, HASH)
        self.assertEqual(t.name, "Tokyo Ghoul (2015-2017) (Digital)")
        self.assertEqual(len(t.files), 15)  # without the padding file
        self.assertEqual(t.files[0], ("Tokyo Ghoul v01 (2016) (Digital).cbz", 401 * MB))

    def test_single_file_and_broken(self):
        single = bencode({"info": {"name": "One Shot.cbz", "length": 5, "piece length": 1, "pieces": b""}})
        self.assertEqual(read_torrent(single).files, (("One Shot.cbz", 5),))
        for bad in (b"", b"<html>", TORRENT[:-5], bencode({"announce": "x"})):
            with self.assertRaises(MangaError):
                read_torrent(bad)


class PartsTest(unittest.TestCase):
    def test_classify(self):
        cases = {
            "Tokyo Ghoul v10 (2016) (Digital) (DigitalMangaFan).cbz": ("band", 10, None),
            "Chainsaw Man 187 (2025) (Digital) (1r0n).cbz": ("kapitel", 187, None),
            "One Piece - Chapter 1100.cbz": ("kapitel", 1100, None),
            "Berserk Vol. 3.cbr": ("band", 3, None),
            "Kaiju No. 8 v01-02 (2021).cbz": ("band", 1, 2),
            "Dandadan c150.5.cbz": ("kapitel", 150.5, None),
            "Akira (1984).cbz": ("datei", None, None),
            "Folder/Chainsaw Man v05.cbz": ("band", 5, None),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(classify(name), expected)

    def test_parts_sorted_volumes_then_chapters_without_extras(self):
        files = [("Chainsaw Man 188 (2025).cbz", 8), ("Chainsaw Man v02 (2021).cbz", 200), ("cover.jpg", 1),
                 ("Chainsaw Man v01 (2021).cbz", 190), ("Chainsaw Man 187 (2025).cbz", 7), ("info.nfo", 1)]
        parts = parts_from_files(files)
        self.assertEqual([p.label for p in parts], ["Band 1", "Band 2", "Kapitel 187", "Kapitel 188"])
        self.assertEqual(selection_label(parts), "Band 1–2 und Kapitel 187–188")

    def test_image_folders_are_one_part_each(self):
        files = [(f"Manga/Chapter {c}/{i:03d}.jpg", 100) for c in (1, 2) for i in range(5)] + [("Manga/cover.jpg", 50)]
        parts = parts_from_files(files)
        self.assertEqual([(p.label, len(p.paths), p.size) for p in parts], [("Kapitel 1", 5, 500), ("Kapitel 2", 5, 500)])

    def test_labels(self):
        parts = parts_from_files([(f"X v{n:02d}.cbz", 1) for n in (1, 2, 3, 5)] + [("X 7.5.cbz", 1), ("Bonus.pdf", 1)])
        self.assertEqual(selection_label(parts), "Band 1–3, 5, Kapitel 7.5 und 1 weitere Datei")
        self.assertEqual(selection_label(parts[:1]), "Band 1")
        self.assertEqual(selection_label([]), "nichts")
        self.assertEqual(selection_label(parts_from_files([("Bonus.pdf", 1)])), "Bonus.pdf")


class SelectionTest(unittest.TestCase):
    parts = parts_from_files([(f"X v{n:02d}.cbz", 1) for n in range(1, 21)] + [(f"X {c} (2025).cbz", 1) for c in range(187, 231)])

    def labels(self, text):
        return [self.parts[i].label for i in parse_selection(text, self.parts)]

    def test_ranges_and_prefixes(self):
        self.assertEqual(self.labels("10-14"), ["Band 10", "Band 11", "Band 12", "Band 13", "Band 14"])
        self.assertEqual(self.labels("Band 3, 5"), ["Band 3", "Band 5"])
        self.assertEqual(self.labels("v2 k190-191"), ["Band 2", "Kapitel 190", "Kapitel 191"])
        self.assertEqual(self.labels("Kapitel 229 – 230"), ["Kapitel 229", "Kapitel 230"])
        self.assertEqual(self.labels("bände 1-2"), ["Band 1", "Band 2"])
        self.assertEqual(len(parse_selection("alle", self.parts)), 64)

    def test_chapters_by_default_without_volumes(self):
        only = parts_from_files([(f"X {c} (2025).cbz", 1) for c in range(1, 6)])
        self.assertEqual([only[i].label for i in parse_selection("2-3", only)], ["Kapitel 2", "Kapitel 3"])

    def test_problems(self):
        for text, hint in (("", "Bitte"), ("irgendwas", "verstehe"), ("Band 99", "gibt es"), ("k5", "gibt es")):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, hint):
                parse_selection(text, self.parts)

    def test_short_name(self):
        self.assertEqual(short_name("Tokyo Ghoul (2015-2017) (Digital) (DigitalMangaFan)"), "Tokyo Ghoul")
        self.assertEqual(short_name("Chainsaw Man v01-20 + 187-230 (2020-2025) (Digital) (1r0n)"), "Chainsaw Man")
        self.assertEqual(short_name("Kaiju No. 8 v01"), "Kaiju No. 8")
        self.assertEqual(short_name("Chainsaw Man 232 (2026)"), "Chainsaw Man")


class ReleasesTest(unittest.TestCase):
    def test_nyaa_packs_once_each_most_seeders_first(self):
        found = releases_from([
            {"title": "A v01-10", "size": 4 * MB, "seeders": 10, "infoUrl": "https://nyaa.iss.ink/view/11", "infoHash": "A" * 40},
            {"title": "A v01-10", "size": 4 * MB, "seeders": 12, "infoUrl": "https://nyaa.si/view/11", "infoHash": "a" * 40,
             "publishDate": "2026-03-05T13:20:00Z"},
            {"title": "B", "size": 1, "seeders": 90, "guid": "magnet:?xt=urn:btih:abc"},  # no torrent file to read
            {"title": "C 232", "size": 2, "seeders": 50, "infoUrl": "https://nyaa.si/view/22", "infoHash": "zz"},
            "nonsense",
        ])
        self.assertEqual([(r.nyaa_id, r.seeders, r.info_hash) for r in found], [(22, 50, ""), (11, 12, "a" * 40)])
        self.assertEqual(found[1].published, "2026-03-05")
        self.assertEqual(releases_from({"error": 1}), [])


class FakeNet:
    """Prowlarr's search and Nyaa's torrent download."""

    def __init__(self):
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path == "/api/v1/search":
            if headers.get("X-Api-Key") != "key":
                return 401, {}, b""
            query = parse_qs(parts.query)
            assert query["categories"] == ["7000"] and query["query"] == ["tokyo ghoul"]
            return 200, {}, json.dumps([{"title": "Tokyo Ghoul", "size": 5, "seeders": 58,
                                         "infoUrl": "https://nyaa.si/view/1608677", "infoHash": HASH}]).encode()
        if parts.path == "/download/1608677.torrent":
            return 200, {}, TORRENT
        return 404, {}, b""


class SourceTest(unittest.TestCase):
    def test_search_and_torrent(self):
        net = FakeNet()
        source = MangaSource("http://prowlarr.test:9696/", "key", fetch=net)
        found = source.search("  tokyo   ghoul ")
        self.assertEqual([(r.nyaa_id, r.seeders) for r in found], [(1608677, 58)])
        self.assertEqual(source.torrent(1608677).info_hash, HASH)
        self.assertEqual(net.urls[-1], "https://nyaa.si/download/1608677.torrent")
        with self.assertRaisesRegex(MangaError, "nicht mehr"):
            source.torrent(5)
        with self.assertRaisesRegex(MangaError, "PROWLARR_API_KEY"):
            MangaSource("http://prowlarr.test:9696", "falsch", fetch=net).search("tokyo ghoul")
        with self.assertRaisesRegex(MangaError, "zwei"):
            source.search("x")


class FakeQbitServer:
    """qBittorrent with one category, adding .torrent files, file lists and priorities."""

    def __init__(self):
        self.torrents: dict[str, dict] = {}
        self.files: dict[str, list[dict]] = {}
        self.priorities: list[tuple[str, str, str]] = []
        self.resumed: list[str] = []
        self.deleted: list[str] = []
        self.added_fields: dict[str, str] = {}
        self.categories = {"manga": {"name": "manga", "savePath": "/mnt/shared/manga"}}

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        form = {k: v[0] for k, v in parse_qs((body or b"").decode("latin-1")).items()} \
            if headers.get("Content-Type", "").startswith("application/x-www") else {}
        if path == "/api/v2/auth/login":
            return 200, {"set-cookie": "SID=s; path=/"}, b"Ok."
        if path == "/api/v2/torrents/add":
            assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
            for field in ("category", "paused", "ratioLimit"):
                marker = f'name="{field}"\r\n\r\n'.encode()
                self.added_fields[field] = body.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
            data = body.split(b"Content-Type: application/x-bittorrent\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            t = read_torrent(data)
            self.torrents[t.info_hash] = {"hash": t.info_hash, "name": t.name, "category": "manga", "state": "pausedDL",
                                          "progress": 0, "added_on": 1}
            self.files[t.info_hash] = [{"index": i, "name": f"{t.name}/{p}", "size": s, "progress": 0, "priority": 1}
                                       for i, (p, s) in enumerate(t.files)]
            return 200, {}, b"Ok."
        if path == "/api/v2/torrents/info":
            found = list(self.torrents.values())
            if "hashes" in query:
                found = [t for t in found if t["hash"] in query["hashes"].split("|")]
            if "category" in query:
                found = [t for t in found if t["category"] == query["category"]]
            return 200, {}, json.dumps(found).encode()
        if path == "/api/v2/torrents/files":
            if query["hash"] not in self.files:
                return 404, {}, b"Not Found"
            return 200, {}, json.dumps(self.files[query["hash"]]).encode()
        if path == "/api/v2/torrents/filePrio":
            self.priorities.append((form["hash"], form["id"], form["priority"]))
            for i in form["id"].split("|"):
                self.files[form["hash"]][int(i)]["priority"] = int(form["priority"])
            return 200, {}, b""
        if path == "/api/v2/torrents/resume":
            self.resumed.append(form["hashes"])
            return 200, {}, b""
        if path == "/api/v2/torrents/delete":
            self.deleted.append(form["hashes"])
            self.torrents.pop(form["hashes"], None)
            return 200, {}, b""
        if path == "/api/v2/torrents/categories":
            return 200, {}, json.dumps(self.categories).encode()
        if path == "/api/v2/torrents/createCategory":
            self.categories[form["category"]] = {"savePath": form["savePath"]}
            return 200, {}, b""
        return 404, {}, b""


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeQbitServer()
        self.net = FakeNet()
        self.service = MangaService(MangaSource("http://prowlarr.test", "key", fetch=self.net),
                                    QbitClient(QBIT, "u", "p", fetch=self.server), sleep=lambda s: None)

    def release(self):
        return Release(1608677, "Tokyo Ghoul", 5, 58, 0, HASH, "")

    def test_loads_only_the_chosen_volumes(self):
        key, prepared = self.service.prepare(self.release())
        chosen = parse_selection("10-14", prepared.parts)
        info_hash, what, size = self.service.add(key, chosen)
        self.assertEqual((info_hash, what, size), (HASH, "Band 10–14", sum((400 + n) * MB for n in range(10, 15))))
        self.assertEqual(self.server.added_fields, {"category": "manga", "paused": "true", "ratioLimit": "0"})
        (_, ids, priority), = self.server.priorities
        self.assertEqual((sorted(int(i) for i in ids.split("|")), priority), (list(range(0, 9)) + [14], "0"))
        self.assertEqual(self.server.resumed, [HASH])

        item, = self.service.items()
        self.assertEqual((item["chosen"], item["all"], item["count"], item["total"], item["done"]),
                         ("Band 10–14", "Band 1–14", 5, 14, False))
        # the same pack again: only what is missing gets added
        key, _ = self.service.prepare(self.release())
        info_hash, what, _ = self.service.add(key, parse_selection("1-2, 10", prepared.parts))
        self.assertEqual(what, "Band 1–2")
        self.assertEqual(self.server.priorities[-1], (HASH, "0|1", "1"))
        with self.assertRaisesRegex(MangaError, "schon alles"):
            self.service.extend(HASH, [9])  # Band 10, loading already

    def test_finished_and_delete(self):
        key, prepared = self.service.prepare(self.release())
        self.service.add(key, [0])
        self.assertEqual(self.service.finished([HASH]), [])
        self.server.torrents[HASH]["state"] = "pausedUP"
        self.assertEqual(self.service.finished([HASH, "f" * 40]), [(HASH, prepared.torrent.name), ("f" * 40, "")])
        name, size = self.service.delete(HASH)
        self.assertEqual((name, size, self.server.deleted), (prepared.torrent.name, 401 * MB, [HASH]))
        with self.assertRaisesRegex(MangaError, "nicht \\(mehr\\)"):
            self.service.delete(HASH)

    def test_category_and_expiry(self):
        self.server.categories = {}
        key, _ = self.service.prepare(self.release())
        with self.assertRaisesRegex(MangaError, "Kategorie"):
            self.service.add(key, [0])
        self.service.folder = "/mnt/shared/manga"
        self.service.add(key, [0])
        self.assertEqual(self.server.categories["manga"], {"savePath": "/mnt/shared/manga"})
        with self.assertRaisesRegex(MangaError, "abgelaufen"):
            self.service.add("0" * 32, [0])
        with self.assertRaisesRegex(MangaError, "nichts"):
            self.service.add(key, [999])


class ItemTest(unittest.TestCase):
    def test_parts_from_qbittorrent_and_rows(self):
        torrent = {"hash": "h", "name": "Pack", "state": "downloading", "dlspeed": 5, "eta": 60}
        files = [{"index": 0, "name": "Pack/Pack v01.cbz", "size": 100, "progress": 1, "priority": 1},
                 {"index": 1, "name": "Pack/Pack v02.cbz", "size": 100, "progress": 0.5, "priority": 1},
                 {"index": 2, "name": "Pack/Pack v03.cbz", "size": 100, "progress": 0, "priority": 0}]
        parts, by_path = qbit_parts(torrent, files)
        self.assertEqual(list(by_path), ["Pack v01.cbz", "Pack v02.cbz", "Pack v03.cbz"])
        rows = part_rows(parts, by_path)
        self.assertEqual([(r["label"], r["have"], r["percent"]) for r in rows],
                         [("Band 1", True, 100), ("Band 2", True, 50), ("Band 3", False, 0)])
        item = item_data(torrent, files)
        self.assertEqual((item["chosen"], item["size"], item["percent"], item["done"], item["eta"]),
                         ("Band 1–2", 200, 75.0, False, 60))
        single = qbit_parts({"name": "One.cbz"}, [{"name": "One.cbz", "size": 1, "priority": 1}])[1]
        self.assertEqual(list(single), ["One.cbz"])


class QbitFilesTest(unittest.TestCase):
    def test_missing_torrent_has_no_files(self):
        client = QbitClient(QBIT, "u", "p", fetch=FakeQbitServer())
        self.assertEqual(client.files("0" * 40), [])


class ConfigAndStoreTest(unittest.TestCase):
    BASE = {"DISCORD_TOKEN": "x", "DISCORD_GUILD_ID": "1", "DISCORD_CHANNEL_ID": "2", "SEERR_API_KEY": "k"}

    def load(self, **extra):
        with mock.patch.dict("os.environ", {**self.BASE, **extra}, clear=True):
            return Settings.from_env()

    def test_settings(self):
        self.assertFalse(self.load().manga_enabled)
        qbit = {"QBITTORRENT_URL": "http://q:8080", "QBITTORRENT_USERNAME": "u", "QBITTORRENT_PASSWORD": "p"}
        s = self.load(PROWLARR_URL="http://p:9696/", PROWLARR_API_KEY="k", MANGA_READER_URL="http://manga.home/", **qbit)
        self.assertTrue(s.manga_enabled)
        self.assertEqual((s.prowlarr_url, s.manga_category, s.manga_reader_url, s.nyaa_url),
                         ("http://p:9696", "manga", "http://manga.home", "https://nyaa.si"))
        self.assertFalse(self.load(PROWLARR_URL="http://p:9696", PROWLARR_API_KEY="k").manga_enabled)
        with self.assertRaises(ConfigError):
            self.load(PROWLARR_URL="http://p:9696")
        with self.assertRaises(ConfigError):
            self.load(MANGA_CATEGORY="../x")

    def test_store_remembers_who_waits(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, now=lambda: 100.0)
            store.manga_wait("ABC", "Band 10–14", 42)
            again = Store(folder)
            self.assertEqual(again.manga_pending, {"abc": {"label": "Band 10–14", "user_id": 42, "since": 100.0}})
            self.assertEqual(again.manga_done("abc")["user_id"], 42)
            self.assertIsNone(Store(folder).manga_done("abc"))


if __name__ == "__main__":
    unittest.main()
