import io
import json
import unittest
import urllib.error
from unittest import mock

from bot.core import AVAILABLE, GB, PARTIAL, REQUEST_APPROVED, Requested, SeerrError, title_from
from bot.seerr import UNREACHABLE, SeerrClient, http_fetch

BASE = "http://seerr.test:5055"


class FakeSeerr:
    """Records calls; answers from a table of (method, path) → (status, data)."""

    def __init__(self, answers=None):
        # No Radarr/Sonarr servers unless a test sets some, so free space is "unknown".
        self.answers = {("GET", "/service/radarr"): (200, []), ("GET", "/service/sonarr"): (200, []), **(answers or {})}
        self.calls = []

    def posted(self):
        return [call for call in self.calls if call["method"] == "POST"]

    def __call__(self, method, url, headers, body):
        path, _, query = url.removeprefix(BASE + "/api/v1").partition("?")
        self.calls.append({"method": method, "path": path, "query": query, "headers": headers,
                           "body": json.loads(body) if body else None})
        answer = self.answers.get((method, path), (404, {"message": "not found"}))
        if isinstance(answer, Exception):
            raise answer
        return answer


def client(fake, **kwargs):
    return SeerrClient(BASE, "secret-key", fetch=fake, **kwargs)


SEARCH = {"results": [
    {"id": 45745, "mediaType": "movie", "title": "Sintel", "releaseDate": "2010-09-27"},
    {"id": 30010, "mediaType": "tv", "name": "Pioneer One", "firstAirDate": "2010-06-15"},
]}
TV = {"id": 30010, "name": "Pioneer One", "firstAirDate": "2010-06-15",
      "seasons": [{"seasonNumber": 1, "episodeCount": 6}, {"seasonNumber": 2, "episodeCount": 6}],
      "mediaInfo": {"status": PARTIAL, "seasons": [{"seasonNumber": 1, "status": AVAILABLE}]}}


class SearchTest(unittest.TestCase):
    def test_query_is_encoded_and_authenticated(self):
        fake = FakeSeerr({("GET", "/search"): (200, SEARCH)})
        titles = client(fake).search("  Amélie!  (2001) ", "movie")
        self.assertEqual([t.label for t in titles], ["Sintel (2010)"])
        call = fake.calls[0]
        self.assertEqual(call["query"], "query=Am%C3%A9lie%21%20%282001%29&page=1&language=de")
        self.assertEqual(call["headers"]["X-Api-Key"], "secret-key")

    def test_empty_query(self):
        with self.assertRaises(SeerrError):
            client(FakeSeerr()).search("   ", "movie")

    def test_details(self):
        fake = FakeSeerr({("GET", "/tv/30010"): (200, TV)})
        title = client(fake).details("tv", 30010)
        self.assertEqual(title.requestable_seasons(), [2])


class RequestTest(unittest.TestCase):
    def test_movie(self):
        fake = FakeSeerr({("POST", "/request"): (201, {"id": 7, "status": REQUEST_APPROVED})})
        title = title_from(SEARCH["results"][0])
        self.assertEqual(client(fake).request(title), Requested(REQUEST_APPROVED, 7))
        self.assertEqual(fake.posted()[0]["body"], {"mediaType": "movie", "mediaId": 45745})
        self.assertEqual(fake.posted()[0]["headers"]["Content-Type"], "application/json")
        self.assertEqual([c["path"] for c in fake.calls], ["/service/radarr", "/request"])  # space first

    def test_tv_missing_seasons_by_default_and_as_user(self):
        fake = FakeSeerr({("POST", "/request"): (201, {"status": 1})})
        title = title_from(TV, "tv")
        self.assertEqual(client(fake, user_id=5).request(title), Requested(1, None))
        self.assertEqual(fake.posted()[0]["body"], {"mediaType": "tv", "mediaId": 30010, "seasons": [2], "userId": 5})

    def test_tv_chosen_seasons(self):
        fake = FakeSeerr({("POST", "/request"): (201, {"status": 2})})
        client(fake).request(title_from(TV, "tv"), [2, 2])
        self.assertEqual(fake.posted()[0]["body"]["seasons"], [2])

    def test_nothing_to_request(self):
        there = title_from({**SEARCH["results"][0], "mediaInfo": {"status": AVAILABLE}})
        fake = FakeSeerr()
        with self.assertRaises(SeerrError) as caught:
            client(fake).request(there)
        self.assertIn("Schon da", str(caught.exception))
        self.assertEqual(fake.calls, [])


RADARR = {("GET", "/service/radarr"): (200, [{"id": 0, "is4k": False, "isDefault": True,
                                               "activeDirectory": "/mnt/shared/movies"}]),
          ("GET", "/service/radarr/0"): (200, {"rootFolders": [{"path": "/mnt/shared/movies", "freeSpace": 40 * GB}]})}
SONARR = {("GET", "/service/sonarr"): (200, [{"id": 0, "isDefault": True, "activeDirectory": "/mnt/shared/shows/"}]),
          ("GET", "/service/sonarr/0"): (200, {"rootFolders": [{"path": "/mnt/shared/shows", "freeSpace": 12 * GB}]})}


class SpaceTest(unittest.TestCase):
    def test_free_bytes_per_kind_and_both(self):
        fake = FakeSeerr({**RADARR, **SONARR})
        self.assertEqual(client(fake).free_bytes("movie"), 40 * GB)
        self.assertEqual(client(fake).free_bytes("tv"), 12 * GB)
        self.assertEqual(client(fake).free_bytes(), 12 * GB)

    def test_unknown_space_never_blocks(self):
        fake = FakeSeerr({("GET", "/service/radarr"): (500, None), ("POST", "/request"): (201, {"status": 2})})
        with self.assertLogs("seerrbot.seerr", "WARNING"):
            self.assertIsNone(client(fake).free_bytes("movie"))
            self.assertEqual(client(fake).request(title_from(SEARCH["results"][0])).status, 2)

    def test_request_refused_when_full(self):
        fake = FakeSeerr({**SONARR, ("POST", "/request"): (201, {"status": 2})})
        with self.assertRaises(SeerrError) as caught:
            client(fake, min_free=15 * GB).request(title_from(TV, "tv"))
        self.assertIn("Kein Platz", str(caught.exception))
        self.assertIn("12 GB", str(caught.exception))
        self.assertEqual(fake.posted(), [])
        self.assertEqual(client(fake, min_free=10 * GB).request(title_from(TV, "tv")).status, 2)


class DeleteTest(unittest.TestCase):
    THERE = {"id": 45745, "mediaType": "movie", "title": "Sintel", "mediaInfo": {"id": 12, "status": AVAILABLE}}

    def test_deletes_files_then_entry(self):
        fake = FakeSeerr({("DELETE", "/media/12/file"): (204, None), ("DELETE", "/media/12"): (204, None)})
        client(fake).delete(title_from(self.THERE))
        self.assertEqual([(c["method"], c["path"]) for c in fake.calls],
                         [("DELETE", "/media/12/file"), ("DELETE", "/media/12")])

    def test_entry_failure_is_only_logged(self):
        fake = FakeSeerr({("DELETE", "/media/12/file"): (204, None), ("DELETE", "/media/12"): (500, None)})
        with self.assertLogs("seerrbot.seerr", "WARNING"):
            client(fake).delete(title_from(self.THERE))

    def test_file_failure_stops(self):
        fake = FakeSeerr({("DELETE", "/media/12/file"): (403, None)})
        with self.assertRaises(SeerrError), self.assertLogs("seerrbot.seerr", "WARNING"):
            client(fake).delete(title_from(self.THERE))
        self.assertEqual(len(fake.calls), 1)

    def test_not_in_library(self):
        fake = FakeSeerr()
        with self.assertRaises(SeerrError):
            client(fake).delete(title_from(SEARCH["results"][0]))
        self.assertEqual(fake.calls, [])

    def test_library_search(self):
        data = {"results": [SEARCH["results"][0], self.THERE,
                            {**TV, "mediaType": "tv", "mediaInfo": {"id": 13, "status": PARTIAL}}]}
        fake = FakeSeerr({("GET", "/search"): (200, data)})
        titles = client(fake).search("Sintel", None, library_only=True)
        self.assertEqual([(t.kind, t.media_id) for t in titles], [("movie", 12), ("tv", 13)])


class ErrorTest(unittest.TestCase):
    def test_status_codes(self):
        title = title_from(SEARCH["results"][0])
        for status, expected in ((401, "API-Key"), (403, "abgelehnt"), (409, "schon angefragt"), (500, "HTTP 500")):
            fake = FakeSeerr({("POST", "/request"): (status, {"message": "x"})})
            with self.assertRaises(SeerrError, msg=status) as caught:
                with self.assertLogs("seerrbot.seerr", "WARNING"):
                    client(fake).request(title)
            self.assertIn(expected, str(caught.exception))

    def test_unreachable(self):
        fake = FakeSeerr({("GET", "/search"): OSError("connection refused")})
        with self.assertRaises(SeerrError) as caught:
            with self.assertLogs("seerrbot.seerr", "WARNING"):
                client(fake).search("Sintel", "movie")
        self.assertEqual(str(caught.exception), UNREACHABLE)


class OpenRequestsTest(unittest.TestCase):
    def test_names_are_looked_up_once(self):
        pending = {"results": [{"status": 1, "type": "movie", "media": {"tmdbId": 45745, "status": 2}}]}
        processing = {"results": [
            {"status": 2, "type": "movie", "media": {"tmdbId": 45745, "status": 3}},
            {"status": 2, "type": "tv", "media": {"tmdbId": 30010, "status": 3}},
        ]}
        answers = {("GET", "/movie/45745"): (200, {"id": 45745, "title": "Sintel", "releaseDate": "2010-01-01"}),
                   ("GET", "/tv/30010"): (500, None)}

        class Fake(FakeSeerr):
            def __call__(self, method, url, headers, body):
                if "/request?" in url:
                    which = "pending" if "filter=pending" in url else "processing"
                    self.calls.append({"path": "/request", "filter": which})
                    return 200, pending if which == "pending" else processing
                return super().__call__(method, url, headers, body)

        fake = Fake(answers)
        with self.assertLogs("seerrbot.seerr", "WARNING"):
            items = client(fake).open_requests()
        self.assertEqual([name for _, name in items], ["Sintel (2010)", "Serie #30010"])
        self.assertEqual(sum(1 for c in fake.calls if c["path"] == "/movie/45745"), 1)


class HttpFetchTest(unittest.TestCase):
    def test_error_status_with_body(self):
        error = urllib.error.HTTPError(BASE, 409, "Conflict", {}, io.BytesIO(b'{"message": "exists"}'))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(http_fetch("POST", BASE, {}, b"{}"), (409, {"message": "exists"}))

    def test_ok(self):
        response = mock.MagicMock(status=200)
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response) as opened:
            self.assertEqual(http_fetch("GET", BASE, {"X-Api-Key": "k"}, None), (200, {"ok": True}))
        self.assertEqual(opened.call_args.args[0].get_method(), "GET")


if __name__ == "__main__":
    unittest.main()
