import glob
import io
import os
import tempfile
import threading
import time
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


class TestConcurrentAccess(unittest.TestCase):
    """`Store` shares one sqlite3.Connection, with check_same_thread=False,
    across every request thread of the ThreadingHTTPServer plus the
    scheduler's own writer thread. check_same_thread=False only disables
    Python's guard against cross-thread use of a connection -- it does not
    make concurrent use of one Connection/Cursor safe. Left unserialised,
    concurrent calls into the same connection corrupt cursor state: this
    was observed live as `SELECT MIN(ts) FROM metric` -- a query that by
    definition always returns exactly one row -- coming back from
    fetchone() as None, blowing up oldest_ts()'s `row[0]` with a
    TypeError. That TypeError must reach the caller (it must NOT be
    swallowed as a sqlite3.Error), so this test drives real concurrency
    through Store and asserts no exception of any kind escapes a thread."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")

    def tearDown(self):
        for path in glob.glob(self.path + "*"):
            os.remove(path)

    def test_concurrent_readers_and_a_writer_never_corrupt_a_cursor(self):
        store = Store(self.path)
        # Seed enough rows that MIN(ts) always has something to find --
        # the point being tested is that a well-formed query on a
        # non-empty table must never come back with no row at all.
        for ts in range(0, 200):
            store.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 100.0)])

        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        stop = threading.Event()

        def record(exc: BaseException) -> None:
            with errors_lock:
                errors.append(exc)

        def reader() -> None:
            while not stop.is_set():
                try:
                    store.oldest_ts("metric")
                    store.oldest_ts("metric", metric="cpu.usage")
                    store.read_series("cpu.usage", 0, 10_000)
                    store.available_depth_seconds("metric", 10_000,
                                                  metric="cpu.usage")
                    store.count_rows("metric")
                    store.distinct_metric_count()
                except BaseException as exc:                # noqa: BLE001
                    record(exc)
                    return

        def writer() -> None:
            ts = 200
            while not stop.is_set():
                try:
                    store.write_metrics(ts, [
                        ("cpu.usage", float(ts), 0.0, 100.0)])
                    ts += 1
                except BaseException as exc:                # noqa: BLE001
                    record(exc)
                    return

        # Mirrors the reported reproduction: many concurrent readers (the
        # /api/history handler, one per request thread) plus the
        # scheduler's single writer thread, all sharing one connection.
        threads = [threading.Thread(target=reader) for _ in range(10)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        # 20 rounds at 10 concurrent requests reproduced the bug 4 times;
        # run long enough to give an equally good chance of hitting it.
        time.sleep(2.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=5)
        store.close()

        if errors:
            self.fail(f"{len(errors)} exception(s) escaped a Store thread "
                      f"under concurrent access: {errors!r}")


class TestActionAudit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(os.path.join(self.dir.name, "db.sqlite3"))
        self.addCleanup(self.store.close)

    def test_a_run_is_written_and_read_back_whole(self):
        self.store.write_action_run(
            "r1", 1000, "apt.refresh", "127.0.0.1", 0, 2140, "Hit:1 …\nDone\n")
        rows = self.store.read_action_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "r1")
        self.assertEqual(rows[0]["action_id"], "apt.refresh")
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertEqual(rows[0]["duration_ms"], 2140)
        self.assertIn("Done", rows[0]["output"])

    def test_a_timed_out_run_records_a_null_exit_code(self):
        # A killed process has no exit code of its own. Storing 0 would
        # make a timeout indistinguishable from a success in the log --
        # the one place a reader looks to find out what happened.
        self.store.write_action_run(
            "r2", 1001, "apt.refresh", "127.0.0.1", None, 1_800_000, "")
        self.assertIsNone(self.store.read_action_runs()[0]["exit_code"])

    def test_runs_come_back_newest_first(self):
        for index, ts in enumerate((1000, 3000, 2000)):
            self.store.write_action_run(
                f"r{index}", ts, "apt.refresh", "127.0.0.1", 0, 1, "")
        self.assertEqual([row["ts"] for row in self.store.read_action_runs()],
                         [3000, 2000, 1000])

    def test_the_limit_is_honoured(self):
        for index in range(5):
            self.store.write_action_run(
                f"r{index}", 1000 + index, "apt.refresh", "127.0.0.1", 0, 1, "")
        self.assertEqual(len(self.store.read_action_runs(limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
