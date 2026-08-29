import unittest

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


if __name__ == "__main__":
    unittest.main()
