"""SQLite persistence.

Metric keys are normalised to integers: storing the string
"net.enp0s25.rx_bps" on every row would cost more than the measurement itself.
"""

from __future__ import annotations

import sqlite3
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
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._key_cache: dict[str, int] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def key_id(self, key: str) -> int:
        cached = self._key_cache.get(key)
        if cached is not None:
            return cached
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
        payload = [(ts, self.key_id(key), avg, low, high)
                   for key, avg, low, high in rows]
        self.conn.executemany(
            "INSERT INTO metric(ts, key_id, avg, min, max) VALUES (?,?,?,?,?)",
            payload)
        self.conn.commit()

    def read_series(self, key: str, since: int, until: int,
                    table: str = "metric") -> list[tuple[int, float]]:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        cursor = self.conn.execute(
            f"SELECT ts, avg FROM {table} "
            "WHERE key_id = (SELECT id FROM metric_key WHERE key = ?) "
            "AND ts BETWEEN ? AND ? ORDER BY ts",
            (key, since, until))
        return [(int(ts), float(value)) for ts, value in cursor.fetchall()]

    def count_rows(self, table: str) -> int:
        if table not in ALL_TABLES:
            raise ValueError(f"unknown table: {table}")
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def distinct_metric_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(DISTINCT key_id) FROM metric").fetchone()[0])

    def oldest_ts(self, table: str) -> int | None:
        if table not in METRIC_TABLES:
            raise ValueError(f"unknown table: {table}")
        row = self.conn.execute(f"SELECT MIN(ts) FROM {table}").fetchone()
        return None if row[0] is None else int(row[0])

    def db_bytes(self) -> int:
        if self.path == ":memory:":
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
        rows = self.conn.execute(
            "SELECT (ts / ?) * ? AS bucket, key_id, AVG(avg), MIN(min), MAX(max) "
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
        for table, cutoff in cutoffs.items():
            cursor = self.conn.execute(
                f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = max(0, cursor.rowcount)
        cursor = self.conn.execute(
            "DELETE FROM event WHERE closed_ts IS NOT NULL AND closed_ts < ?",
            (now - cfg.retention.event_days * day,))
        deleted["event"] = max(0, cursor.rowcount)
        self.conn.commit()
        return deleted

    def available_depth_seconds(self, table: str, now: int) -> int:
        oldest = self.oldest_ts(table)
        return 0 if oldest is None else max(0, now - oldest)

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
        self.conn.commit()
