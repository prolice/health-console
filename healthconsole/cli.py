"""Command line interface.

Output here is operator-facing diagnostic text and stays in English: it is
compared, pasted into issues and grepped, so it is not part of the translated
interface.
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

from healthconsole.config import (
    DEFAULT_CONFIG_PATH, ConfigError, estimate_db_bytes, load_config,
)
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import WEB_DIR, generate_token, is_loopback, make_server
from healthconsole.store import Store
from healthconsole.sudoers import render as render_sudoers

DATA_DIR = Path.home() / ".local" / "share" / "health-console"
WARN_DB_BYTES = 500 * 1024 ** 2
ASSUMED_METRIC_COUNT = 25
SECONDS_PER_DAY = 86_400

# packaging/sudoers.d/health-console, relative to the repository root --
# never /etc. See cmd_sudoers: there is no code path anywhere in this
# module that writes outside this directory.
SUDOERS_DEST = (
    Path(__file__).resolve().parent.parent / "packaging" / "sudoers.d"
    / "health-console")


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


def cmd_config(cfg, config_path: Path) -> int:
    retention, sampling = cfg.retention, cfg.sampling
    print(f"Config file      : {config_path}")
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
    # No Ring/Scheduler needed here: status only reads the store, so open
    # it directly rather than paying for a Scheduler (which loads probes).
    store = Store(default_db_path())
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


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_TOKEN_LINE_RE = re.compile(r"^\s*token\s*=")


def _rotate_token(config_path: Path) -> str:
    """Generate a new token and store it in `config_path`'s [server] section.

    The project has no TOML writer, so this reads and rewrites the file as
    text: it locates the [server] section (creating one at the end of the
    file if absent) and replaces its `token = ...` line, or adds one, rather
    than touching anything else a user may already have configured there.
    """
    token = generate_token()
    lines = (config_path.read_text(encoding="utf-8").splitlines()
             if config_path.exists() else [])

    server_start = None
    server_end = len(lines)
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if server_start is not None:
            server_end = index
            break
        if match.group(1).strip() == "server":
            server_start = index

    token_line = f'token = "{token}"'
    if server_start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[server]")
        lines.append(token_line)
    else:
        token_index = next(
            (i for i in range(server_start + 1, server_end)
             if _TOKEN_LINE_RE.match(lines[i])), None)
        if token_index is None:
            lines.insert(server_start + 1, token_line)
        else:
            lines[token_index] = token_line

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config_path.chmod(0o600)
    return token


def cmd_token(cfg, config_path: Path, rotate: bool) -> int:
    if not rotate:
        if cfg.token:
            print(cfg.token)
        else:
            print("No token is set.")
            print("Run 'health-console token --rotate' to generate one.")
        return 0
    token = _rotate_token(config_path)
    print(token)
    return 0


def cmd_sudoers(cfg, config_path, rotate) -> int:
    """Render the sudoers rule, validate it, and print it -- never install
    it.

    This is the console's only interaction with /etc/sudoers.d, and it
    stops short of touching /etc at all: the file is validated under a
    temporary name, only ever copied into packaging/ once `visudo` has
    accepted it, and the exact install command is printed for the operator
    to run themselves. There is no flag here that writes to /etc, and
    there must never be one: an install mode that exists but goes unused
    is still an install mode someone will eventually use.

    The account name comes from `pwd.getpwuid(os.getuid())`, never from
    `getpass.getuser()` (which reads $LOGNAME / $USER verbatim, before it
    ever consults the password database) -- the whole point of this
    command is to defend the machine against exactly the kind of
    environment-controlled string that would otherwise flow straight into
    a sudoers rule.

    `--rotate` means nothing here (there is no token to rotate) and is
    refused rather than silently ignored.
    """
    if rotate:
        print("'sudoers' has no '--rotate': there is no token to rotate "
              "here.", file=sys.stderr)
        return 2

    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        text = render_sudoers(user)
    except ValueError as exc:
        print(f"Refusing to render a sudoers rule: {exc}", file=sys.stderr)
        return 1

    # Resolved at runtime, never hard-coded: this machine's visudo is
    # sudo-rs's reimplementation, reached through /etc/alternatives, and a
    # future one may differ again. Whichever `visudo` answers on PATH is
    # the one whose opinion of this file matters.
    visudo = shutil.which("visudo")
    if visudo is None:
        print("No 'visudo' is on PATH: cannot validate the rendered rule.",
              file=sys.stderr)
        print("Install sudo (or sudo-rs) and rerun before trusting this "
              "file.", file=sys.stderr)
        return 1

    # Validate a throwaway copy before SUDOERS_DEST is touched at all. A
    # previous successful run may have left a *valid* file there, already
    # copied into an operator's runbook as the install source; a rejected
    # render must never overwrite it with text `visudo -c` refused.
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".sudoers", delete=False)
    try:
        handle.write(text)
        handle.close()
        result = subprocess.run([visudo, "-c", "-f", handle.name],
                                 capture_output=True, text=True)
        if result.returncode != 0:
            print("visudo -c rejected the rendered rule:", file=sys.stderr)
            print((result.stdout + result.stderr).strip(), file=sys.stderr)
            return 1
        SUDOERS_DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(handle.name, SUDOERS_DEST)
    finally:
        Path(handle.name).unlink(missing_ok=True)

    print(text)
    print(f"Written to  : {SUDOERS_DEST}")
    print(f"Validated by: {visudo}")
    print()
    print("Never installed automatically. To install it, run:")
    print("  sudo install -m 0440 -o root -g root \\")
    print(f"    {SUDOERS_DEST} /etc/sudoers.d/health-console")
    return 0


def _lan_address() -> str | None:
    """The machine's own first up, non-loopback IPv4 address, if any --
    "http://0.0.0.0:8787" is not a URL anyone's phone can open; this is."""
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        interface = stats.get(name)
        if interface is None or not interface.isup:
            continue
        for address in addresses:
            if (address.family.name == "AF_INET"
                    and not address.address.startswith("127.")):
                return address.address
    return None


def _log_loop_error(exc: Exception) -> None:
    print(f"scheduler loop error: {type(exc).__name__}: {exc}",
          file=sys.stderr)


def _tick_once(scheduler, cfg, last_flush, last_maintain, now=None):
    """One iteration of the background collection loop.

    Kept as a separate, importable function — rather than inlined in the
    closure below — so the loop's resilience to a raising probe, store or
    scheduler call can be exercised by a test without starting a real
    server or thread. This console's whole premise is that it does not
    quietly stop telling the truth, so that resilience is worth covering
    directly rather than only by reading the diff.
    """
    now = time.time() if now is None else now
    try:
        scheduler.tick(now)
        if now - last_flush >= cfg.sampling.store_seconds:
            scheduler.flush(now)
            last_flush = now
        if now - last_maintain >= SECONDS_PER_DAY:
            scheduler.maintain(now)
            last_maintain = now
    except Exception as exc:                  # noqa: BLE001
        # A transient disk or database error must not permanently stop
        # collection: log it and keep looping so the console recovers
        # once the condition clears, instead of leaving the server
        # answering with a state frozen at the last successful tick
        # forever.
        _log_loop_error(exc)
    return last_flush, last_maintain


def cmd_run(cfg) -> int:
    store, scheduler = _open(cfg)
    server = make_server(cfg, scheduler, WEB_DIR)
    stop = threading.Event()

    def loop():
        # Enforce retention once at startup, before entering the periodic
        # loop below. A machine that restarts daily (suspends, reboots, or
        # has its service restarted nightly) may never keep this thread
        # alive for the full 86400 seconds the periodic check waits for, so
        # without this call `store.prune()` could simply never run and the
        # database would grow unbounded.
        try:
            scheduler.maintain()
        except Exception as exc:                  # noqa: BLE001
            _log_loop_error(exc)
        last_flush = time.time()
        last_maintain = time.time()
        while not stop.is_set():
            last_flush, last_maintain = _tick_once(
                scheduler, cfg, last_flush, last_maintain)
            stop.wait(cfg.sampling.live_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    # cfg.bind is frequently a wildcard address ("0.0.0.0", "::") that no
    # browser can actually open: always show the loopback URL, which
    # always works, and only add the machine's real LAN address (not
    # cfg.bind itself) as a second line when it is listening beyond
    # loopback.
    print(f"Health Console on http://127.0.0.1:{cfg.port}")
    if not is_loopback(cfg.bind):
        lan_address = _lan_address()
        if lan_address:
            print(f"Also reachable on the LAN at http://{lan_address}:{cfg.port}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop.set()
        server.server_close()
        # Told before the store is: server_close() only stops the listening
        # socket, it does not wait for request threads already in flight,
        # and daemon_threads=True means nothing below joins them either (the
        # /api/stream handler loops forever by design). Setting this first
        # closes the window for any *new* request on a still-open keep-alive
        # connection; a request already inside the store when it closes is
        # covered separately, by the try/except in server.py's _history.
        server.shutdown_event.set()
        thread.join(timeout=5)
        store.close()
    return 0


COMMANDS = {"run": cmd_run, "config": cmd_config,
            "status": cmd_status, "prune": cmd_prune, "token": cmd_token,
            "sudoers": cmd_sudoers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="health-console", description="System health console.")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="config, prune, run, status, sudoers or token")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to a configuration file")
    parser.add_argument("--rotate", action="store_true",
                        help="with the 'token' command, generate a new "
                             "token and store it in the configuration file")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # -h/--help exits 0 after printing the full help to stdout; an
        # invalid argument exits 2 after argparse has already printed usage
        # and the error to stderr. Either way argparse already wrote
        # everything needed, so do not print a second, duplicate usage line.
        return exc.code if exc.code else 0
    if args.command is None:
        parser.print_usage()
        return 2
    config_path = DEFAULT_CONFIG_PATH if args.config is None else args.config
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 1
    if args.command == "config":
        return cmd_config(cfg, config_path)
    if args.command == "token":
        return cmd_token(cfg, config_path, args.rotate)
    if args.command == "sudoers":
        return cmd_sudoers(cfg, config_path, args.rotate)
    return COMMANDS[args.command](cfg)
