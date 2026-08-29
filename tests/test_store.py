import glob
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr

from healthconsole.store import Store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()


class TestSchema(StoreCase):
    def test_tables_exist(self):
        names = {row[0] for row in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"metric_key", "metric", "metric_5m", "snapshot", "event",
             "action_run"}, names)

    def test_creating_the_schema_again_is_idempotent(self):
        self.store._create_schema()
        self.assertEqual(self.store.count_rows("metric"), 0)


class TestKeyNormalisation(StoreCase):
    def test_same_key_yields_same_id(self):
        self.assertEqual(self.store.key_id("cpu.usage"),
                         self.store.key_id("cpu.usage"))

    def test_different_keys_yield_different_ids(self):
        self.assertNotEqual(self.store.key_id("cpu.usage"),
                            self.store.key_id("mem.used"))

    def test_ids_are_cached_without_extra_rows(self):
        for _ in range(10):
            self.store.key_id("cpu.usage")
        self.assertEqual(self.store.count_rows("metric_key"), 1)


class TestWriteRead(StoreCase):
    def test_written_points_come_back(self):
        self.store.write_metrics(1000, [("cpu.usage", 20.0, 10.0, 30.0)])
        self.assertEqual(self.store.read_series("cpu.usage", 0, 2000),
                         [(1000, 20.0)])

    def test_range_is_inclusive_and_bounded(self):
        for ts in (100, 200, 300):
            self.store.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual(
            [ts for ts, _ in self.store.read_series("cpu.usage", 200, 300)],
            [200, 300])

    def test_results_are_ordered_by_time(self):
        for ts in (300, 100, 200):
            self.store.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual(
            [ts for ts, _ in self.store.read_series("cpu.usage", 0, 999)],
            [100, 200, 300])

    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(self.store.read_series("nonexistent", 0, 999), [])

    def test_unknown_table_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.read_series("cpu.usage", 0, 999, table="sqlite_master")

    def test_batch_write_is_one_transaction(self):
        self.store.write_metrics(1000, [
            ("cpu.usage", 1.0, 1.0, 1.0),
            ("mem.available", 2.0, 2.0, 2.0)])
        self.assertEqual(self.store.count_rows("metric"), 2)

    def test_empty_batch_writes_nothing(self):
        self.store.write_metrics(1000, [])
        self.assertEqual(self.store.count_rows("metric"), 0)


class TestIntrospection(StoreCase):
    def test_distinct_metric_count(self):
        self.store.write_metrics(1, [("a", 1.0, 1.0, 1.0), ("b", 1.0, 1.0, 1.0)])
        self.store.write_metrics(2, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.distinct_metric_count(), 2)

    def test_oldest_ts_is_none_when_empty(self):
        self.assertIsNone(self.store.oldest_ts("metric"))

    def test_oldest_ts_reports_the_first_point(self):
        self.store.write_metrics(500, [("a", 1.0, 1.0, 1.0)])
        self.store.write_metrics(100, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.oldest_ts("metric"), 100)


class TestPerMetricDepth(StoreCase):
    """available_depth_seconds() was per-table, not per-metric: a request
    for a metric with no rows of its own reported whatever OTHER metric
    happened to be oldest, contradicting the rule that history states the
    depth actually stored for what was asked."""

    def test_depth_is_scoped_to_the_requested_metric(self):
        self.store.write_metrics(100, [("old.metric", 1.0, 1.0, 1.0)])
        self.store.write_metrics(900, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.available_depth_seconds(
            "metric", 1000, metric="cpu.usage"), 100)
        self.assertEqual(self.store.available_depth_seconds(
            "metric", 1000, metric="old.metric"), 900)

    def test_omitting_metric_keeps_the_whole_table_behaviour(self):
        self.store.write_metrics(100, [("old.metric", 1.0, 1.0, 1.0)])
        self.store.write_metrics(900, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.assertEqual(
            self.store.available_depth_seconds("metric", 1000), 900)

    def test_unknown_metric_reports_zero_not_another_metrics_depth(self):
        self.store.write_metrics(100, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.assertEqual(self.store.available_depth_seconds(
            "metric", 1000, metric="nonexistent"), 0)


class TestCorruptDatabase(unittest.TestCase):
    """Spec §13: 'Database corrupt -> recreated, incident logged'. A laptop
    losing power mid-write is the ordinary case this guards, not an edge
    case, so a corrupt file must not take the whole console down."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        with open(self.path, "wb") as handle:
            handle.write(os.urandom(4096))

    def tearDown(self):
        for path in glob.glob(self.path + "*"):
            os.remove(path)

    def test_corrupt_file_is_quarantined_and_a_fresh_database_opens(self):
        err = io.StringIO()
        with redirect_stderr(err):
            store = Store(self.path)
        try:
            self.assertEqual(store.count_rows("metric"), 0)
            self.assertIn("corrupt", err.getvalue())
        finally:
            store.close()

    def test_the_corrupt_file_is_preserved_alongside_the_new_one(self):
        with redirect_stderr(io.StringIO()):
            store = Store(self.path)
        try:
            quarantined = [p for p in glob.glob(self.path + ".corrupt-*")]
            self.assertEqual(len(quarantined), 1)
            with open(quarantined[0], "rb") as handle:
                self.assertNotEqual(handle.read(16), b"SQLite format 3\x00")
        finally:
            store.close()

    def test_writes_succeed_on_the_recreated_database(self):
        with redirect_stderr(io.StringIO()):
            store = Store(self.path)
        try:
            store.write_metrics(1000, [("cpu.usage", 1.0, 1.0, 1.0)])
            self.assertEqual(store.count_rows("metric"), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
