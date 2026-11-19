import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from bot.arr import ArrClient, Download, Progress, bar, download_line, downloads_from_queue, progress_lines, speed_text
from bot.store import Store

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
PAST, FUTURE = "2026-04-05T00:00:00Z", "2026-12-24T00:00:00Z"


def episode(eid, season, number, has_file, aired=True, monitored=True):
    return {"id": eid, "seasonNumber": season, "episodeNumber": number, "hasFile": has_file, "monitored": monitored,
            "airDateUtc": PAST if aired else FUTURE}


EPISODES = [episode(1, 23, 1, True), episode(2, 23, 2, True), episode(3, 23, 3, False), episode(4, 23, 4, False),
            episode(5, 23, 5, False), episode(6, 23, 6, False, aired=False), episode(7, 0, 1, False),
            episode(8, 22, 1, False, monitored=False)]
QUEUE = [{"seriesId": 9, "episodeId": 3, "downloadId": "ABC", "size": 1000, "sizeleft": 520, "status": "downloading",
          "trackedDownloadState": "downloading", "estimatedCompletionTime": (NOW + timedelta(minutes=9)).isoformat(),
          "episode": {"seasonNumber": 23, "episodeNumber": 3}},
         {"seriesId": 9, "episodeId": 4, "downloadId": "DEF", "size": 1000, "sizeleft": 1000, "status": "warning",
          "trackedDownloadState": "downloading", "trackedDownloadStatus": "warning",
          "statusMessages": [{"title": "x", "messages": ["The download is stalled with no connections"]}],
          "episode": {"seasonNumber": 23, "episodeNumber": 4}},
         {"seriesId": 1, "episodeId": 99, "downloadId": "OTHER", "size": 5, "sizeleft": 5, "status": "downloading"}]


class FakeSonarr:
    def __init__(self, commands=None):
        self.commands = commands or []

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if path.endswith("/queue"):
            return 200, {"records": QUEUE}
        if path.endswith("/command"):
            return 200, self.commands
        if path.endswith("/series"):
            return 200, [{"id": 9, "tmdbId": 37854}]
        if path.endswith("/episode"):
            assert query["seriesId"] == "9"
            return 200, EPISODES
        return 404, None


class ProgressTest(unittest.TestCase):
    def test_show(self):
        progress = ArrClient("http://s", "k", "tv", fetch=FakeSonarr()).progress([37854, 1], NOW)
        self.assertEqual(list(progress), [37854])  # 1 is not in Sonarr: left out
        p = progress[37854]
        self.assertEqual((p.have, p.total, p.upcoming), (2, 5, 1))  # specials and unmonitored don't count
        self.assertEqual(p.missing, {23: [5]})  # 3 and 4 are downloading
        self.assertEqual([(d.what, d.state, d.percent, d.hash) for d in p.downloads],
                         [("S23E03", "downloading", 48, "abc"), ("S23E04", "stalled", 0, "def")])
        self.assertFalse(p.searching)

    def test_search_running(self):
        commands = [{"name": "SeasonSearch", "status": "started", "body": {"seriesId": 9, "seasonNumber": 23}},
                    {"name": "RssSync", "status": "started", "body": {}}]
        self.assertTrue(ArrClient("http://s", "k", "tv", fetch=FakeSonarr(commands)).progress([37854], NOW)[37854].searching)
        done = [{"name": "SeasonSearch", "status": "completed", "body": {"seriesId": 9}}]
        self.assertFalse(ArrClient("http://s", "k", "tv", fetch=FakeSonarr(done)).progress([37854], NOW)[37854].searching)

    def test_season_pack_is_one_download(self):
        records = [{"downloadId": "P", "size": 100, "sizeleft": 50, "status": "downloading",
                    "episode": {"seasonNumber": 1, "episodeNumber": n}} for n in (1, 2, 3)]
        (pack,) = downloads_from_queue(records)
        self.assertEqual((pack.what, pack.episodes), ("S01E01–E03", 3))

    def test_import_states(self):
        blocked = {"downloadId": "x", "status": "completed", "trackedDownloadState": "importPending",
                   "trackedDownloadStatus": "warning", "statusMessages": [{"messages": ["Episode 0x01 was not found"]}]}
        importing = {"downloadId": "y", "status": "completed", "trackedDownloadState": "importPending"}
        states = [(d.state, d.message) for d in downloads_from_queue([blocked, importing])]
        self.assertEqual(states, [("blocked", "Episode 0x01 was not found"), ("importing", "")])


class LinesTest(unittest.TestCase):
    def test_bar_and_speed(self):
        self.assertEqual(bar(48), "▓▓▓▓▓░░░░░")
        self.assertEqual(bar(0), "░" * 10)
        self.assertEqual(speed_text(3_100_000), "3.1 MB/s")
        self.assertEqual(speed_text(850_000), "850 KB/s")

    def test_download_lines(self):
        loading = Download("S23E03", 1000, 520, NOW + timedelta(minutes=9), "downloading", hash="abc")
        self.assertEqual(download_line(loading, {"dlspeed": 3_100_000, "num_seeds": 12}, NOW),
                         "⬇️ S23E03 `▓▓▓▓▓░░░░░` 48 % · 3.1 MB/s · noch ca. 9 Min. · 12 Quellen")
        self.assertEqual(download_line(loading, None, NOW), "⬇️ S23E03 `▓▓▓▓▓░░░░░` 48 % · noch ca. 9 Min.")
        stalled = Download("S23E04", 1000, 1000, None, "stalled", "no connections")
        self.assertTrue(download_line(stalled, None, NOW).startswith("⏸️ S23E04 hängt: keine Quellen"))
        blocked = Download("", 1, 1, None, "blocked", "Not a Custom Format upgrade")
        self.assertEqual(download_line(blocked, None, NOW), "⚠️ Übernahme blockiert: Not a Custom Format upgrade")

    def test_show_lines(self):
        p = Progress(12, 25, {23: [16, 17, 18, 20]}, 2,
                     (Download("S23E13", 100, 52, None, "downloading", hash="a", episodes=1),), True)
        lines = progress_lines("tv", p, {}, NOW)
        self.assertEqual(lines[0], "📦 12 von 25 Folgen da · ⬇️ 1 laden · 🔎 4 fehlen · 📅 2 kommen noch")
        self.assertEqual(lines[-1], "🔎 Fehlen noch: Staffel 23: Folge 16–18, 20 · Sonarr sucht gerade")

    def test_moving_first_paused_counted_apart(self):
        downloads = tuple([Download(f"S01E{n:02d}", 10, 10, None, "paused", hash=f"p{n}", episodes=1) for n in range(1, 5)]
                          + [Download("S01E09", 10, 2, None, "downloading", hash="a", episodes=1),
                             Download("S01E08", 10, 8, None, "downloading", hash="b", episodes=1)])
        lines = progress_lines("tv", Progress(0, 6, {}, 0, downloads, False), {}, NOW)
        self.assertEqual(lines[0], "📦 0 von 6 Folgen da · ⬇️ 2 laden · ⏸️ 4 pausiert")
        self.assertTrue(lines[1].startswith("⬇️ S01E09"))  # most progress first
        self.assertTrue(lines[2].startswith("⬇️ S01E08"))
        self.assertTrue(lines[3].startswith("⏸️"))

    def test_movie_lines(self):
        self.assertEqual(progress_lines("movie", Progress(0, 1, {}, 0, (), False), {}, NOW),
                         ["🔎 Radarr wartet auf ein passendes Release"])
        self.assertEqual(progress_lines("movie", Progress(1, 1, {}, 0, (), False), {}, NOW),
                         ["✅ Ist da, taucht gleich in Jellyfin auf"])

    def test_many_downloads_are_cut(self):
        many = tuple(Download(f"S01E{n:02d}", 10, 5, None, "downloading", hash=str(n), episodes=1) for n in range(7))
        lines = progress_lines("tv", Progress(0, 7, {}, 0, many, False), {}, NOW)
        self.assertEqual(lines[-1], "… und 3 weitere Downloads")


class AutoAboTest(unittest.TestCase):
    def test_pending_until_the_show_is_there(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.add_pending_abo(37854, 11)
            store.add_pending_abo(37854, 11)  # once
            store.add_pending_abo(37854, 12)
            again = Store(folder)  # survives a restart
            self.assertEqual(again.pending_abos[37854][0], [11, 12])
            self.assertEqual(again.take_pending_abos(37854), [11, 12])
            self.assertEqual(again.pending_abos, {})

    def test_subscribe_never_unsubscribes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.subscribe(11, "s1", "One Piece", ["S23E01"])
            store.subscribe(11, "s1", "One Piece", ["S23E01"])
            self.assertEqual(store.user_abos(11), ["s1"])

    def test_old_pending_are_forgotten(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = [1000.0 * 86400]
            store = Store(folder, now=lambda: clock[0])
            store.add_pending_abo(1, 11)
            clock[0] += 31 * 86400
            store.drop_old_pending_abos()
            self.assertEqual(store.pending_abos, {})


if __name__ == "__main__":
    unittest.main()
