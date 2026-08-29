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
