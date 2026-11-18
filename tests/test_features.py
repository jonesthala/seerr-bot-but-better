"""Logic behind the newer features: approvals, 'it's there', ETA, limit, digest, trending, profiles."""
import unittest
from datetime import datetime, timedelta, timezone

from bot.core import (
    AVAILABLE,
    PARTIAL,
    PROCESSING,
    REQUEST_APPROVED,
    REQUEST_DECLINED,
    REQUEST_PENDING,
    Season,
    Title,
    approval_text,
    added_since,
    digest_key,
    eta_text,
    new_line,
    new_media,
    next_allowed,
    limit_text,
    open_requests,
    parse_digest,
    parse_time,
    pending_requests,
    pick_profiles,
    request_done_text,
    request_state,
    seasons_done,
    trending_results,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)  # a Wednesday


class EtaTest(unittest.TestCase):
    def test_texts(self):
        self.assertEqual(eta_text(None, NOW), "")
        self.assertEqual(eta_text(NOW - timedelta(minutes=5), NOW), "gleich fertig")
        self.assertEqual(eta_text(NOW + timedelta(minutes=20, seconds=20), NOW), "noch ca. 20 Min.")
        self.assertEqual(eta_text(NOW + timedelta(minutes=70), NOW), "noch ca. 1 Std.")
        self.assertEqual(eta_text(NOW + timedelta(hours=5), NOW), "noch ca. 5 Std.")
        self.assertEqual(eta_text(NOW + timedelta(hours=30), NOW), "noch ca. 1 Tag")
        self.assertEqual(eta_text(NOW + timedelta(days=3), NOW), "noch ca. 3 Tage")

    def test_parse_time(self):
        self.assertEqual(parse_time("2026-11-18T12:00:00.000Z"), NOW)
        self.assertEqual(parse_time("2026-11-18T12:00:00"), NOW)  # no zone means UTC
        self.assertIsNone(parse_time("soon"))
        self.assertIsNone(parse_time(None))

    def test_status_line_with_eta_and_discord_name(self):
        data = {"results": [{"id": 9, "status": REQUEST_APPROVED, "type": "movie",
                             "requestedBy": {"displayName": "discord"},
                             "media": {"tmdbId": 1, "status": PROCESSING, "downloadStatus": [
                                 {"size": 100, "sizeLeft": 50, "estimatedCompletionTime": "2026-11-18T12:10:00Z"},
                                 {"size": 100, "sizeLeft": 50, "estimatedCompletionTime": "2026-11-18T12:30:00Z"}]}}]}
        item = open_requests(data)[0]
        self.assertEqual(item.request_id, 9)
        self.assertEqual(item.line("Sintel", now=NOW), "**Sintel** · ⬇️ wird geladen, 50 %, noch ca. 30 Min. · von discord")
        self.assertEqual(item.line("Sintel", "alex", now=NOW), "**Sintel** · ⬇️ wird geladen, 50 %, noch ca. 30 Min. · von alex")


REQUEST = {"id": 7, "status": REQUEST_PENDING, "type": "tv", "seasons": [{"seasonNumber": 2}, {"seasonNumber": 1}],
           "requestedBy": {"displayName": "discord", "email": "x@example.com"},
           "media": {"tmdbId": 30010, "status": PARTIAL, "mediaUrl": "http://jf:8096/web/index.html#!/details?id=1"}}


class RequestStateTest(unittest.TestCase):
    def test_parse(self):
        state = request_state(REQUEST)
        self.assertEqual((state.request_id, state.kind, state.tmdb_id, state.seasons), (7, "tv", 30010, (1, 2)))
        self.assertEqual(state.media_url, "http://jf:8096/web/index.html#!/details?id=1")
        self.assertEqual(state.requested_by, "discord")
        self.assertIsNone(request_state({**REQUEST, "media": {"tmdbId": 1, "mediaUrl": "javascript:x"}}).media_url)
        for broken in (None, {}, {"id": 1}, {"id": 1, "media": {"tmdbId": "x"}, "type": "movie"}):
            self.assertIsNone(request_state(broken), broken)

    def test_only_pending(self):
        data = {"results": [REQUEST, {**REQUEST, "id": 8, "status": REQUEST_APPROVED}, "junk"]}
        self.assertEqual([s.request_id for s in pending_requests(data)], [7])
        self.assertEqual(pending_requests(None), [])

    def test_seasons_done(self):
        show = Title("tv", 1, "X", "", "", None, PARTIAL,
                     (Season(1, 6, AVAILABLE), Season(2, 6, PROCESSING), Season(3, 6, AVAILABLE)))
        self.assertTrue(seasons_done(show, (1, 3)))
        self.assertFalse(seasons_done(show, (1, 2)))
        self.assertFalse(seasons_done(show, ()))

    def test_texts(self):
        self.assertEqual(approval_text("Loki (2021)", "tv", (1, 2), "alex"),
                         "📥 Neue Anfrage von alex: **Loki (2021)**, Staffel 1–2. Wartet auf Freigabe.")
        self.assertEqual(approval_text("Sintel (2010)", "movie", (), ""),
                         "📥 Neue Anfrage: **Sintel (2010)**. Wartet auf Freigabe.")
        self.assertIn("Freigabe durch einen Admin", request_done_text("movie", REQUEST_PENDING))
        self.assertIn("Ich sage dir hier Bescheid", request_done_text("tv", REQUEST_APPROVED, notify=True))
        self.assertNotIn("Bescheid", request_done_text("tv", REQUEST_DECLINED))


class LimitTest(unittest.TestCase):
    DAY = 86400

    def test_next_allowed(self):
        now = 100 * self.DAY
        self.assertIsNone(next_allowed([now] * 10, 0, 7, now))  # off
        self.assertIsNone(next_allowed([now - self.DAY, now - 2 * self.DAY], 3, 7, now))
        self.assertIsNone(next_allowed([now - 8 * self.DAY] * 5, 3, 7, now))  # old ones don't count
        times = [now - 6 * self.DAY, now - self.DAY, now - 3 * self.DAY]
        self.assertEqual(next_allowed(times, 3, 7, now), now + self.DAY)  # when the oldest drops out
        self.assertEqual(next_allowed(times, 2, 7, now), now + 4 * self.DAY)

    def test_text(self):
        text = limit_text(3, 7, datetime(2026, 11, 20, 9, 5))
        self.assertIn("3 Anfragen in 7 Tagen", text)
        self.assertIn("20.11. um 09:05", text)
        self.assertIn("eine Anfrage", limit_text(1, 1, NOW))


class DigestTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_digest("sun 18:00"), (6, 18, 0))
        self.assertEqual(parse_digest(" So 8:30 "), (6, 8, 30))
        self.assertEqual(parse_digest("mo 00:00"), (0, 0, 0))
        self.assertIsNone(parse_digest(""))
        for bad in ("sunday 18:00", "sun 25:00", "sun 18", "xx 10:00", "18:00 sun"):
            with self.assertRaises(ValueError, msg=bad):
                parse_digest(bad)

    def test_due_once_the_time_passed_this_week(self):
        sunday_six = (6, 18, 0)
        self.assertIsNone(digest_key(NOW, sunday_six))  # Wednesday
        sunday = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
        self.assertIsNone(digest_key(sunday - timedelta(minutes=1), sunday_six))
        self.assertEqual(digest_key(sunday, sunday_six), "2026-W47")
        self.assertEqual(digest_key(NOW, (0, 9, 0)), "2026-W47")  # Monday already passed

    def test_new_media(self):
        data = {"results": [
            {"mediaType": "movie", "tmdbId": 1, "mediaAddedAt": "2026-11-17T13:28:19.137Z", "mediaUrl": "http://jf/1"},
            {"mediaType": "tv", "tmdbId": 2, "mediaAddedAt": "2026-11-01T10:00:00Z"},
            {"mediaType": "person", "tmdbId": 3}, "junk",
        ]}
        items = new_media(data)
        self.assertEqual([(i.kind, i.tmdb_id) for i in items], [("movie", 1), ("tv", 2)])
        self.assertEqual([i.tmdb_id for i in added_since(items, NOW - timedelta(days=7))], [1])
        self.assertEqual(new_line(items[0], "Sintel [2010]", timezone.utc), "🎬 [Sintel (2010)](http://jf/1) · 17.11.")
        self.assertEqual(new_line(items[1], "Loki", timezone.utc), "📺 **Loki** · 01.11.")


class TrendingAndProfilesTest(unittest.TestCase):
    def test_trending(self):
        data = {"results": [
            {"id": 1, "mediaType": "movie", "title": "A", "releaseDate": "2026-01-01"},
            {"id": 2, "mediaType": "person", "name": "Somebody"},
            {"id": 3, "mediaType": "tv", "name": "B", "firstAirDate": "2025-01-01", "mediaInfo": {"status": AVAILABLE}},
        ]}
        self.assertEqual([t.tmdb_id for t in trending_results(data, None)], [1, 3])
        self.assertEqual([t.tmdb_id for t in trending_results(data, "tv")], [3])
        self.assertEqual(len(trending_results(data, None, limit=1)), 1)
        self.assertEqual(trending_results("junk", None), [])

    def test_profiles(self):
        details = {"profiles": [{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}, {"id": "x", "name": "bad"}]}
        self.assertEqual(pick_profiles(details, ["ultra-hd", "HD-1080p", "missing"]), [(5, "Ultra-HD"), (4, "HD-1080p")])
        self.assertEqual(pick_profiles(None, ["HD-1080p"]), [])


if __name__ == "__main__":
    unittest.main()
