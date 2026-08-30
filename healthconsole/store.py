"""SQLite persistence.

Metric keys are normalised to integers: storing the string
"net.enp0s25.rx_bps" on every row would cost more than the measurement itself.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_key (
    id  INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS metric (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_5m (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    ts INTEGER NOT NULL, probe TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL, severity TEXT NOT NULL,
    opened_ts INTEGER NOT NULL, closed_ts INTEGER
);
CREATE TABLE IF NOT EXISTS action_run (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, action_id TEXT NOT NULL,
    source TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER, output TEXT
);
CREATE INDEX IF NOT EXISTS metric_key_ts ON metric(key_id, ts);
CREATE INDEX IF NOT EXISTS metric_5m_key_ts ON metric_5m(key_id, ts);
CREATE INDEX IF NOT EXISTS snapshot_ts ON snapshot(ts);
"""

METRIC_TABLES = ("metric", "metric_5m")
ALL_TABLES = METRIC_TABLES + ("metric_key", "snapshot", "event", "action_run")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._key_cache: dict[str, int] = {}
        # A single sqlite3.Connection is opened once and shared by every
        # thread that touches this Store: the ThreadingHTTPServer's request
        # threads (reads) and the Scheduler's own thread (writes). SQLite's
        # check_same_thread=False only disables Python's guard against
        # cross-thread use -- it does not make concurrent use of one
        # Connection/Cursor safe. Two threads calling execute()/fetchone()
        # at the same time can interleave inside the connection's C-level
        # state (execute() releases the GIL while SQLite runs), corrupting
        # cursor results -- observed as SELECT MIN(ts) returning no row at
        # all in oldest_ts(). This lock serialises every access to
        # self.conn so only one thread is ever inside SQLite through this
        # connection at a time. It is an RLock because some methods call
        # others that also take it (e.g. write_metrics -> key_id) and a
        # future caller doing the same must not deadlock.
        self._lock = threading.RLock()
        try:
            self._connect()
        except sqlite3.DatabaseError as exc:
            # Spec §13: "Database corrupt -> recreated, incident logged --
            # history is valuable, not vital." A laptop losing power
            # mid-write is the ordinary case here, not an edge case: this
            # must not take the whole console down with it. ":memory:" has
            # no file to quarantine and cannot suffer this in practice, so
            # it is left to raise normally.
            if self.path == ":memory:":
                raise
            self._quarantine_corrupt_file(exc)
            self._connect()

    def _connect(self) -> None:
        with self._lock:
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            # A corrupt file is not always detected by connect() itself --
            # SQLite opens it lazily, so the first real access (this
            # PRAGMA) is what actually surfaces the error.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()

    def _quarantine_corrupt_file(self, exc: Exception) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:                      # noqa: BLE001
                pass
        # Only the filesystem is touched below -- no reason to hold the
        # lock across it, and this runs once during __init__ before the
        # Store is shared with any other thread regardless.
        quarantined = f"{self.path}.corrupt-{int(time.time())}"
        print(f"health-console: database at {self.path} is corrupt "
              f"({type(exc).__name__}: {exc}); moving it aside to "
              f"{quarantined} and starting a fresh database. Past history "
              f"is lost; collection continues.", file=sys.stderr)
        for suffix in ("", "-wal", "-shm"):
            source = Path(self.path + suffix)
            if source.exists():
                source.rename(quarantined + suffix)

    def _create_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        # An unbounded acquire here can hang forever: cli.py's cmd_run gives
        # a wedged scheduler thread (e.g. stuck inside a probe's sysfs read)
        # only thread.join(timeout=5) before calling close(), so the thread
        # -- and the lock -- may still be alive when this runs. That is
        # exactly the terminal noise the shutdown fix exists to remove, so
        # this must give up on the lock rather than wait on it forever.
        # 2 seconds is comfortably above the longest locked section measured
        # anywhere in this class (0.33s for aggregate_5m's largest batch),
        # so an ordinarily busy Store is never the reason this gives up --
        # only a genuinely wedged one is. The connection is closed either
        # way: sqlite3 allows close() from a thread other than the one
        # using the connection (check_same_thread=False), and a process
        # that is exiting has no further use for a lock a wedged thread
        # will never release.
        acquired = self._lock.acquire(timeout=2)
        try:
            self.conn.close()
        finally:
            if acquired:
                self._lock.release()

    def key_id(self, key: str) -> int:
        cached = self._key_cache.get(key)
        if cached is not None:
            return cached
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM metric_key WHERE key = ?", (key,)).fetchone()
            if row is None:
                cursor = self.conn.execute(
                    "INSERT INTO metric_key(key) VALUES (?)", (key,))
                self.conn.commit()
                key_id = int(cursor.lastrowid)
            else:
                key_id = int(row[0])
            self._key_cache[key] = key_id
            return key_id

    def write_metrics(self, ts: int,
                      rows: list[tuple[str, float, float, float]]) -> None:
        if not rows:
            return
        with self._lock:
            payload = [(ts, self.key_id(key), avg, low, high)
                       for key, avg, low, high in rows]
            self.conn.executemany(
                "INSERT INTO metric(ts, key_id, avg, min, max) "
                "VALUES (?,?,?,?,?)", payload)
            self.conn.commit()

    def read_series(self, key: str, since: int, until: int,
                    table: str = "metric") -> list[tuple[int, float]]:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        with self._lock:
            cursor = self.conn.execute(
                f"SELECT ts, avg FROM {table} "
                "WHERE key_id = (SELECT id FROM metric_key WHERE key = ?) "
                "AND ts BETWEEN ? AND ? ORDER BY ts",
                (key, since, until))
            return [(int(ts), float(value)) for ts, value in cursor.fetchall()]

    def count_rows(self, table: str) -> int:
        if table not in ALL_TABLES:
            raise ValueError(f"unknown table: {table}")
        with self._lock:
            return int(self.conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def distinct_metric_count(self) -> int:
        with self._lock:
            return int(self.conn.execute(
                "SELECT COUNT(DISTINCT key_id) FROM metric").fetchone()[0])

    def oldest_ts(self, table: str, metric: str | None = None) -> int | None:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        with self._lock:
            if metric is None:
                row = self.conn.execute(
                    f"SELECT MIN(ts) FROM {table}").fetchone()
            else:
                row = self.conn.execute(
                    f"SELECT MIN(ts) FROM {table} "
                    "WHERE key_id = (SELECT id FROM metric_key WHERE key = ?)",
                    (metric,)).fetchone()
            return None if row[0] is None else int(row[0])

    def write_action_run(self, run_id: str, ts: int, action_id: str,
                         source: str, exit_code: int | None,
                         duration_ms: int, output: str) -> None:
        # exit_code is None for a run that was killed: a timeout has no
        # exit status of its own, and recording 0 would make it read as a
        # success in the one place someone looks to find out what happened.
        with self._lock:
            self.conn.execute(
                "INSERT INTO action_run"
                "(id, ts, action_id, source, exit_code, duration_ms, output) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, ts, action_id, source, exit_code, duration_ms, output))
            self.conn.commit()

    def read_action_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cursor = self.conn.execute(
                "SELECT id, ts, action_id, source, exit_code, duration_ms, "
                "output FROM action_run ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,))
            rows = cursor.fetchall()
        return [{"id": row[0], "ts": int(row[1]), "action_id": row[2],
                 "source": row[3],
                 "exit_code": None if row[4] is None else int(row[4]),
                 "duration_ms": int(row[5]), "output": row[6]}
                for row in rows]

    def db_bytes(self) -> int:
        if self.path == ":memory:":
            with self._lock:
                pages = self.conn.execute("PRAGMA page_count").fetchone()[0]
                page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
            return int(pages) * int(page_size)
        return sum(
            Path(self.path + suffix).stat().st_size
            for suffix in ("", "-wal", "-shm")
            if Path(self.path + suffix).exists())

    # --- Aggregation and retention ------------------------------------

    def aggregate_5m(self, now: int, period: int = 300) -> int:
        """Fold raw points into buckets of `period` seconds.

        Idempotent: a bucket already written is not written again. Only fully
        elapsed buckets are processed, so no partial average is frozen in.
        """
        boundary = (now // period) * period
        with self._lock:
            rows = self.conn.execute(
                "SELECT (ts / ?) * ? AS bucket, key_id, "
                "AVG(avg), MIN(min), MAX(max) "
                "FROM metric WHERE ts < ? GROUP BY bucket, key_id",
                (period, period, boundary)).fetchall()
            written = 0
            for bucket, key_id, avg, low, high in rows:
                exists = self.conn.execute(
                    "SELECT 1 FROM metric_5m WHERE ts = ? AND key_id = ?",
                    (bucket, key_id)).fetchone()
                if exists:
                    continue
                self.conn.execute(
                    "INSERT INTO metric_5m(ts, key_id, avg, min, max) "
                    "VALUES (?,?,?,?,?)", (bucket, key_id, avg, low, high))
                written += 1
            self.conn.commit()
            return written

    def prune(self, cfg, now: int) -> dict[str, int]:
        """Apply the retention periods. Returns rows deleted per table.

        Raising a period resurrects nothing: deletion is permanent, and the
        console will report the depth actually available rather than the one
        requested.

        Must run AFTER aggregate_5m: pruning first destroys raw metric rows
        before they are folded into the 5-minute aggregates, permanently losing
        that history.
        """
        day = 86_400
        cutoffs = {
            "metric": now - cfg.retention.raw_days * day,
            "metric_5m": now - cfg.retention.aggregate_days * day,
            "snapshot": now - cfg.retention.snapshot_days * day,
            "action_run": now - cfg.retention.audit_days * day,
        }
        deleted: dict[str, int] = {}
        with self._lock:
            for table, cutoff in cutoffs.items():
                cursor = self.conn.execute(
                    f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                deleted[table] = max(0, cursor.rowcount)
            cursor = self.conn.execute(
                "DELETE FROM event WHERE closed_ts IS NOT NULL "
                "AND closed_ts < ?",
                (now - cfg.retention.event_days * day,))
            deleted["event"] = max(0, cursor.rowcount)
            self.conn.commit()
            return deleted

    def available_depth_seconds(self, table: str, now: int,
                                metric: str | None = None) -> int:
        # Without `metric`, this is per-table, not per-metric: a request for
        # a metric this table has never stored a point for would otherwise
        # report the depth of whatever OTHER metric happens to be oldest --
        # contradicting the rule that history states the depth actually
        # stored for what was asked. `metric=None` keeps the original,
        # whole-table behaviour the scheduler's own housekeeping call uses.
        oldest = self.oldest_ts(table, metric)
        return 0 if oldest is None else max(0, now - oldest)

    def vacuum(self) -> None:
        # VACUUM can take a while on a large database, but this is only ever
        # invoked from the standalone `health-console prune` CLI command
        # (see cli.py), which opens its own Store in its own process -- it
        # is never called from the running console's server or scheduler,
        # so holding the lock here never competes with a concurrent reader.
        with self._lock:
            self.conn.execute("VACUUM")
            self.conn.commit()
