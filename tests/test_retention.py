import unittest

from healthconsole.config import Config, Retention
from healthconsole.store import Store

DAY = 86_400


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def fill_days(self, days, now, key="cpu.usage", step=3600):
        """One point per hour over `days` days, ending at `now`."""
        ts = now - days * DAY
        while ts <= now:
            self.store.write_metrics(ts, [(key, 50.0, 40.0, 60.0)])
            ts += step


class TestAggregation(RetentionCase):
    def test_raw_points_become_five_minute_buckets(self):
        now = 10 * DAY
        for i in range(10):
            self.store.write_metrics(
                now + i * 30, [("cpu.usage", float(i), 0.0, 9.0)])
        written = self.store.aggregate_5m(now=now + 600)
        self.assertGreater(written, 0)
        rows = self.store.read_series(
            "cpu.usage", 0, now + 600, table="metric_5m")
        self.assertEqual(len(rows), 1)

    def test_bucket_average_is_correct(self):
        now = 10 * DAY
        for i, value in enumerate([10.0, 20.0, 30.0]):
            self.store.write_metrics(
                now + i * 30, [("cpu.usage", value, value, value)])
        self.store.aggregate_5m(now=now + 600)
        rows = self.store.read_series(
            "cpu.usage", 0, now + 600, table="metric_5m")
        self.assertAlmostEqual(rows[0][1], 20.0)

    def test_aggregation_is_idempotent(self):
        now = 10 * DAY
        for i in range(6):
            self.store.write_metrics(now + i * 30, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.store.aggregate_5m(now=now + 600)
        before = self.store.count_rows("metric_5m")
        self.store.aggregate_5m(now=now + 600)
        self.assertEqual(self.store.count_rows("metric_5m"), before)


class TestPrune(RetentionCase):
    def test_raw_older_than_raw_days_is_removed(self):
        now = 100 * DAY
        self.fill_days(10, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertGreaterEqual(self.store.oldest_ts("metric"), now - 2 * DAY)

    def test_prune_reports_what_it_deleted(self):
        now = 100 * DAY
        self.fill_days(10, now)
        deleted = self.store.prune(
            Config(retention=Retention(raw_days=2)), now=now)
        self.assertIn("metric", deleted)
        self.assertGreater(deleted["metric"], 0)

    def test_reducing_retention_purges_more(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=20)), now=now)
        after_twenty = self.store.count_rows("metric")
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLess(self.store.count_rows("metric"), after_twenty)

    def test_increasing_retention_resurrects_nothing(self):
        # History restarts from the date of the change. The console must show
        # the depth actually available, never the one requested.
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        remaining = self.store.count_rows("metric")
        self.store.prune(Config(retention=Retention(raw_days=90)), now=now)
        self.assertEqual(self.store.count_rows("metric"), remaining)

    def test_nothing_is_deleted_when_everything_is_recent(self):
        now = 100 * DAY
        self.fill_days(1, now)
        deleted = self.store.prune(
            Config(retention=Retention(raw_days=30)), now=now)
        self.assertEqual(deleted["metric"], 0)


class TestAvailableDepth(RetentionCase):
    def test_depth_is_zero_when_empty(self):
        self.assertEqual(
            self.store.available_depth_seconds("metric", now=DAY), 0)

    def test_depth_reflects_what_is_actually_stored(self):
        now = 100 * DAY
        self.fill_days(3, now)
        depth = self.store.available_depth_seconds("metric", now=now)
        self.assertAlmostEqual(depth / DAY, 3.0, places=1)

    def test_depth_shrinks_after_prune(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.store.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLessEqual(
            self.store.available_depth_seconds("metric", now=now),
            2 * DAY + 3600)


if __name__ == "__main__":
    unittest.main()
