"""Command line interface.

Output here is operator-facing diagnostic text and stays in English: it is
compared, pasted into issues and grepped, so it is not part of the translated
interface.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from healthconsole.config import (
    DEFAULT_CONFIG_PATH, ConfigError, estimate_db_bytes, load_config,
)
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import WEB_DIR, make_server
from healthconsole.store import Store

DATA_DIR = Path.home() / ".local" / "share" / "health-console"
WARN_DB_BYTES = 500 * 1024 ** 2
ASSUMED_METRIC_COUNT = 25
SECONDS_PER_DAY = 86_400


def default_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "db.sqlite3"


def human_bytes(n: float) -> str:
    units = ["B", "kB", "MB", "GB", "TB"]
    index = 0
    while n >= 1024 and index < len(units) - 1:
        n /= 1024
        index += 1
    text = f"{n:.1f}".rstrip("0").rstrip(".")
    return f"{text} {units[index]}"


def _open(cfg):
    store = Store(default_db_path())
    ring = Ring(window_seconds=3600, live_seconds=cfg.sampling.live_seconds)
    return store, Scheduler(cfg, store, ring)


def cmd_config(cfg) -> int:
    retention, sampling = cfg.retention, cfg.sampling
    print(f"Config file      : {DEFAULT_CONFIG_PATH}")
    print(f"Listening on     : {cfg.bind}:{cfg.port}")
    print(f"Token            : {'set' if cfg.token else 'absent'}")
    print("Retention (days)")
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        print(f"  {name:<15} {getattr(retention, name)}")
    print(f"Cadences         : display {sampling.live_seconds}s · "
          f"write {sampling.store_seconds}s")
    projected = estimate_db_bytes(cfg, n_metrics=ASSUMED_METRIC_COUNT)
    print(f"Projected size   : {human_bytes(projected)} "
          f"(for {ASSUMED_METRIC_COUNT} metrics)")
    if projected > WARN_DB_BYTES:
        print("  Warning: these settings will produce a large database. "
              "Lower aggregate_days or raise store_seconds if that is not "
              "intended.")
    return 0


def cmd_status(cfg) -> int:
    store, _ = _open(cfg)
    try:
        now = int(time.time())
        print(f"Database         : {store.path}")
        print(f"Actual size      : {human_bytes(store.db_bytes())}")
        print(f"Metrics tracked  : {store.distinct_metric_count()}")
        for table in ("metric", "metric_5m"):
            depth = store.available_depth_seconds(table, now) / SECONDS_PER_DAY
            print(f"Depth {table:<12}: {depth:.2f} days "
                  f"({store.count_rows(table)} rows)")
    finally:
        store.close()
    return 0


def cmd_prune(cfg) -> int:
    store, scheduler = _open(cfg)
    try:
        report = scheduler.maintain()
        print(f"Aggregated       : {report['aggregated']} buckets")
        for table, count in report["deleted"].items():
            print(f"Pruned {table:<11}: {count} rows")
        store.vacuum()
        print(f"Size after       : {human_bytes(store.db_bytes())}")
    finally:
        store.close()
    return 0


def cmd_run(cfg) -> int:
    store, scheduler = _open(cfg)
    server = make_server(cfg, scheduler, WEB_DIR)

    def loop():
        last_flush = time.time()
        last_maintain = time.time()
        while True:
            now = time.time()
            scheduler.tick(now)
            if now - last_flush >= cfg.sampling.store_seconds:
                scheduler.flush(now)
                last_flush = now
            if now - last_maintain >= SECONDS_PER_DAY:
                scheduler.maintain(now)
                last_maintain = now
            time.sleep(cfg.sampling.live_seconds)

    threading.Thread(target=loop, daemon=True).start()
    print(f"Health Console on http://{cfg.bind}:{cfg.port}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
        store.close()
    return 0


COMMANDS = {"run": cmd_run, "config": cmd_config,
            "status": cmd_status, "prune": cmd_prune}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="health-console", description="System health console.")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="run, config, status or prune")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to a configuration file")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        parser.print_usage()
        return 2
    if args.command is None:
        parser.print_usage()
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    return COMMANDS[args.command](cfg)
