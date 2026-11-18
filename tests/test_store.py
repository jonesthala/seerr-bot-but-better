import json
import tempfile
import unittest
from pathlib import Path

from bot.store import FILE_NAME, KEEP_DAYS, Store

DAY = 86400


class Clock:
    def __init__(self, now=1000 * DAY):
        self.now = now

    def __call__(self):
        return self.now


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()

    def store(self):
        return Store(self.dir.name, now=self.clock)

    def test_survives_a_restart(self):
        store = self.store()
        store.add_request(7, "movie", 45745, "Sintel (2010)", 11, "alex")
        store.add_approval(7, 100, 200, "📥 Neue Anfrage")
        store.set_digest("2026-W47")
        again = self.store()
        self.assertEqual(again.request(7).label, "Sintel (2010)")
        self.assertEqual(again.request(7).user_id, 11)
        self.assertEqual(again.approval(7).message_id, 200)
        self.assertEqual(again.digest, "2026-W47")
        self.assertIsNone(again.request(None))

    def test_waiting_and_notified(self):
        store = self.store()
        store.add_request(1, "movie", 1, "A", 11, "alex")
        self.clock.now += 10
        store.add_request(2, "tv", 2, "B", 12, "kim")
        self.assertEqual([t.request_id for t in store.waiting()], [1, 2])
        store.mark_notified(1)
        self.assertEqual([t.request_id for t in self.store().waiting()], [2])

    def test_request_times_per_person(self):
        store = self.store()
        store.add_request(1, "movie", 1, "A", 11, "alex")
        store.add_request(2, "movie", 2, "B", 12, "kim")
        store.add_request(3, "movie", 3, "C", 11, "alex")
        self.assertEqual(len(store.request_times(11)), 2)

    def test_approvals(self):
        store = self.store()
        store.add_approval(5, 1, 2, "x")
        store.add_approval(6, 1, 3, "y")
        store.close_approval(5)
        self.assertEqual([a.request_id for a in self.store().open_approvals()], [6])

    def test_old_finished_requests_are_forgotten(self):
        store = self.store()
        store.add_request(1, "movie", 1, "A", 11, "alex")
        store.add_request(2, "movie", 2, "B", 11, "alex")
        store.mark_notified(1)
        self.clock.now += (KEEP_DAYS + 1) * DAY
        store.save()
        self.assertEqual(sorted(self.store().requests), [2])  # not yet told: kept

    def test_broken_or_missing_file(self):
        Path(self.dir.name, FILE_NAME).write_text("{not json", "utf-8")
        with self.assertLogs("seerrbot.store", "ERROR"):
            store = self.store()
        self.assertEqual(store.requests, {})
        Path(self.dir.name, FILE_NAME).write_text(json.dumps({"requests": [{"request_id": 1}, "junk"]}), "utf-8")
        with self.assertLogs("seerrbot.store", "WARNING"):
            self.assertEqual(self.store().requests, {})

    def test_without_folder_only_in_memory(self):
        with self.assertLogs("seerrbot.store", "WARNING"):
            store = Store(None)
        store.add_request(1, "movie", 1, "A", 11, "alex")
        self.assertEqual(store.request(1).user_name, "alex")

    def test_unwritable_folder_is_logged(self):
        blocker = Path(self.dir.name, "file")
        blocker.write_text("x", "utf-8")
        store = Store(blocker / "sub", now=self.clock)  # a folder below a file can't be created
        with self.assertLogs("seerrbot.store", "ERROR"):
            store.add_request(1, "movie", 1, "A", 11, "alex")
        self.assertEqual(store.request(1).label, "A")


if __name__ == "__main__":
    unittest.main()
