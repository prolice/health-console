import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import MAX_STREAMS, RANGES, make_server
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.scheduler = Scheduler(Config(), cls.store, Ring())
        cls.scheduler.tick(now=1000.0)
        cls.store.write_metrics(1000, [("cpu.usage", 20.0, 10.0, 30.0)])
        cls.store.write_metrics(2000, [("cpu.usage", 40.0, 30.0, 50.0)])
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.scheduler, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


class TestHistory(HttpCase):
    def test_known_range_returns_points(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"),
            timeout=5).read())
        self.assertEqual(body["metric"], "cpu.usage")
        self.assertGreaterEqual(len(body["points"]), 1)

    def test_unknown_range_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                self.url("/api/history?metric=cpu.usage&range=42x"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_missing_metric_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/history?range=24h"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_metric_returns_empty_not_an_error(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=nonexistent&range=24h"),
            timeout=5).read())
        self.assertEqual(body["points"], [])

    def test_response_states_the_available_depth(self):
        # Always the depth actually available: a half-empty 90-day chart
        # would suggest a collection failure rather than a young history.
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"),
            timeout=5).read())
        self.assertIn("depth_days", body)


class TestStream(HttpCase):
    def test_stream_announces_the_right_content_type(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        self.assertEqual(response.headers["Content-Type"],
                         "text/event-stream; charset=utf-8")
        response.close()

    def test_first_event_carries_the_state(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        lines = [response.readline().decode("utf-8") for _ in range(3)]
        response.close()
        joined = "".join(lines)
        self.assertIn("event: state", joined)
        self.assertIn('"score"', joined)

    def test_stream_cap_is_declared(self):
        self.assertEqual(MAX_STREAMS, 8)

    def test_known_ranges(self):
        self.assertEqual(set(RANGES), {"1h", "24h", "7d", "90d"})


if __name__ == "__main__":
    unittest.main()
