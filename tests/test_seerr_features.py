"""Seerr calls behind the newer features, against the fake Seerr from test_seerr."""
import unittest

from bot.core import AVAILABLE, PARTIAL, PROCESSING, REQUEST_PENDING, SeerrError, request_state, title_from
from test_seerr import SEARCH, TV, FakeSeerr, client

REQUEST = {"id": 7, "status": REQUEST_PENDING, "type": "tv", "seasons": [{"seasonNumber": 2}],
           "media": {"tmdbId": 30010, "status": PARTIAL}}
THERE = {"id": 45745, "mediaType": "movie", "title": "Sintel", "mediaInfo": {"id": 12, "status": AVAILABLE}}
SHOW_THERE = {**TV, "mediaType": "tv", "mediaInfo": {"id": 13, "status": PARTIAL}}


class RequestStateTest(unittest.TestCase):
    def test_found_and_gone(self):
        fake = FakeSeerr({("GET", "/request/7"): (200, REQUEST)})
        self.assertEqual(client(fake).request_state(7).seasons, (2,))
        with self.assertLogs("seerrbot.seerr", "WARNING"):
            self.assertIsNone(client(FakeSeerr()).request_state(8))  # 404

    def test_other_errors_are_raised(self):
        fake = FakeSeerr({("GET", "/request/7"): (500, None)})
        with self.assertRaises(SeerrError) as caught, self.assertLogs("seerrbot.seerr", "WARNING"):
            client(fake).request_state(7)
        self.assertEqual(caught.exception.status, 500)

    def test_done_for_movies_and_seasons(self):
        seasons = [{"seasonNumber": 1, "status": AVAILABLE}, {"seasonNumber": 2, "status": PROCESSING}]
        fake = FakeSeerr({("GET", "/tv/30010"): (200, {**TV, "mediaInfo": {"status": PARTIAL, "seasons": seasons}})})
        seerr = client(fake)
        self.assertTrue(seerr.is_done(request_state({**REQUEST, "media": {"tmdbId": 1, "status": AVAILABLE}})))
        self.assertFalse(seerr.is_done(request_state({**REQUEST, "media": {"tmdbId": 1, "status": PROCESSING}})))
        self.assertFalse(seerr.is_done(request_state(REQUEST)))  # season 2 is still loading
        seasons[1]["status"] = AVAILABLE
        self.assertTrue(seerr.is_done(request_state(REQUEST)))

    def test_pending_list(self):
        fake = FakeSeerr({("GET", "/request"): (200, {"results": [REQUEST, {**REQUEST, "id": 8, "status": 2}]})})
        self.assertEqual([s.request_id for s in client(fake).pending_requests()], [7])
        self.assertIn("filter=pending", fake.calls[0]["query"])


class ApprovalTest(unittest.TestCase):
    def test_approve_and_decline(self):
        fake = FakeSeerr({("POST", "/request/7/approve"): (200, {}), ("POST", "/request/7/decline"): (200, {})})
        client(fake).set_request_status(7, "approve")
        client(fake).set_request_status(7, "decline")
        self.assertEqual([c["path"] for c in fake.posted()], ["/request/7/approve", "/request/7/decline"])
        with self.assertRaises(ValueError):
            client(fake).set_request_status(7, "delete")

    def test_request_returns_id_and_sends_profile(self):
        fake = FakeSeerr({("POST", "/request"): (201, {"id": 9, "status": REQUEST_PENDING})})
        result = client(fake, user_id=2).request(title_from(SEARCH["results"][0]), profile_id=5)
        self.assertEqual((result.status, result.request_id), (REQUEST_PENDING, 9))
        self.assertEqual(fake.posted()[0]["body"], {"mediaType": "movie", "mediaId": 45745, "profileId": 5})
        self.assertEqual(fake.posted()[0]["headers"]["X-API-User"], "2")


class IssueTest(unittest.TestCase):
    def test_movie(self):
        fake = FakeSeerr({("POST", "/issue"): (201, {"id": 3})})
        self.assertEqual(client(fake).report_issue(title_from(THERE), 2, "  kein Ton  "), 3)
        self.assertEqual(fake.posted()[0]["body"], {"issueType": 2, "message": "kein Ton", "mediaId": 12})

    def test_show_with_season_and_episode(self):
        fake = FakeSeerr({("POST", "/issue"): (201, {"id": 4})})
        seerr = client(fake)
        seerr.report_issue(title_from(SHOW_THERE), 3, "falsche Sprache", 2, 5)
        seerr.report_issue(title_from(SHOW_THERE), 1, "ruckelt", None, 5)  # an episode needs its season
        bodies = [c["body"] for c in fake.posted()]
        self.assertEqual((bodies[0]["problemSeason"], bodies[0]["problemEpisode"]), (2, 5))
        self.assertEqual((bodies[1]["problemSeason"], bodies[1]["problemEpisode"]), (0, 0))

    def test_only_for_the_library(self):
        fake = FakeSeerr()
        with self.assertRaises(SeerrError):
            client(fake).report_issue(title_from(SEARCH["results"][0]), 1, "x")
        with self.assertRaises(ValueError):
            client(fake).report_issue(title_from(THERE), 9, "x")
        self.assertEqual(fake.calls, [])


class DiscoverTest(unittest.TestCase):
    def test_trending_names_are_remembered(self):
        fake = FakeSeerr({("GET", "/discover/trending"): (200, {"results": SEARCH["results"]})})
        seerr = client(fake)
        self.assertEqual([t.kind for t in seerr.trending()], ["movie", "tv"])
        self.assertEqual(seerr.name_of("movie", 45745), "Sintel (2010)")
        self.assertEqual(len(fake.calls), 1)  # no extra lookup

    def test_recently_added(self):
        data = {"results": [{"mediaType": "movie", "tmdbId": 45745, "mediaAddedAt": "2026-11-17T13:28:19Z"}]}
        fake = FakeSeerr({("GET", "/media"): (200, data)})
        self.assertEqual([i.tmdb_id for i in client(fake).recently_added(5)], [45745])
        self.assertIn("sort=mediaAdded", fake.calls[0]["query"])
        self.assertIn("take=5", fake.calls[0]["query"])

    def test_profiles(self):
        fake = FakeSeerr({
            ("GET", "/service/radarr"): (200, [{"id": 0, "isDefault": True}]),
            ("GET", "/service/radarr/0"): (200, {"profiles": [{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}]}),
        })
        seerr = client(fake)
        self.assertEqual(seerr.profiles("movie", []), [])
        self.assertEqual(fake.calls, [])
        with self.assertLogs("seerrbot.seerr", "WARNING") as logs:
            self.assertEqual(seerr.profiles("movie", ["Ultra-HD", "Deutsch"]), [(5, "Ultra-HD")])
        self.assertIn("deutsch", logs.output[0])
        self.assertEqual(seerr.profiles("tv", ["HD-1080p"]), [])  # no Sonarr server


if __name__ == "__main__":
    unittest.main()
