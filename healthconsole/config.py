"""Configuration loading, validation and cost estimation.

An invalid configuration stops the service and names the offending field.
Starting up while silently ignoring a broken setting is a trap.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "health-console" / "config.toml"

# Average cost of one metric row including its index, key normalised to an int.
BYTES_PER_METRIC_ROW = 40
# Weight of the non-metric tables (snapshots, events, audit) in steady state.
OVERHEAD_BYTES = 3_000_000
AGGREGATE_PERIOD_SECONDS = 300
SECONDS_PER_DAY = 86_400


class ConfigError(ValueError):
    """Invalid configuration. The message always names the offending field."""


@dataclass(frozen=True)
class Retention:
    raw_days: int = 2
    aggregate_days: int = 90
    snapshot_days: int = 7
    event_days: int = 365
    audit_days: int = 365


@dataclass(frozen=True)
class Sampling:
    live_seconds: int = 2
    store_seconds: int = 30


@dataclass(frozen=True)
class Config:
    retention: Retention = field(default_factory=Retention)
    sampling: Sampling = field(default_factory=Sampling)
    bind: str = "0.0.0.0"
    port: int = 8787
    allow_remote_actions: bool = False
    token: str = ""


def _int_field(table: dict, name: str, default: int, section: str) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"[{section}] {name} must be an integer number of days, "
            f"got: {value!r}")
    return value


def _bool_field(table: dict, name: str, default: bool, section: str) -> bool:
    value = table.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(
            f"[{section}] {name} must be a boolean, "
            f"got: {value!r}")
    return value


def _str_field(table: dict, name: str, default: str, section: str) -> str:
    value = table.get(name, default)
    if not isinstance(value, str):
        raise ConfigError(
            f"[{section}] {name} must be a string, "
            f"got: {value!r}")
    return value


def load_config(path: Path | None = None) -> Config:
    path = DEFAULT_CONFIG_PATH if path is None else path
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: unreadable TOML file — {exc}") from exc

    retention_table = data.get("retention", {})
    retention = Retention(
        raw_days=_int_field(retention_table, "raw_days", Retention.raw_days, "retention"),
        aggregate_days=_int_field(
            retention_table, "aggregate_days", Retention.aggregate_days, "retention"),
        snapshot_days=_int_field(
            retention_table, "snapshot_days", Retention.snapshot_days, "retention"),
        event_days=_int_field(retention_table, "event_days", Retention.event_days, "retention"),
        audit_days=_int_field(retention_table, "audit_days", Retention.audit_days, "retention"),
    )
    sampling_table = data.get("sampling", {})
    sampling = Sampling(
        live_seconds=_int_field(
            sampling_table, "live_seconds", Sampling.live_seconds, "sampling"),
        store_seconds=_int_field(
            sampling_table, "store_seconds", Sampling.store_seconds, "sampling"),
    )
    server_table = data.get("server", {})
    cfg = Config(
        retention=retention,
        sampling=sampling,
        bind=_str_field(server_table, "bind", Config.bind, "server"),
        port=_int_field(server_table, "port", Config.port, "server"),
        allow_remote_actions=_bool_field(
            server_table, "allow_remote_actions", Config.allow_remote_actions, "server"),
        token=_str_field(server_table, "token", Config.token, "server"),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    retention, sampling = cfg.retention, cfg.sampling
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        if getattr(retention, name) < 1:
            raise ConfigError(
                f"[retention] {name} must be at least 1 day, "
                f"got: {getattr(retention, name)}")
    if retention.raw_days > retention.aggregate_days:
        raise ConfigError(
            f"[retention] raw_days ({retention.raw_days}) cannot exceed "
            f"aggregate_days ({retention.aggregate_days}): keeping fine-grained "
            f"samples longer than the averages makes no sense.")
    if sampling.live_seconds < 1:
        raise ConfigError(
            f"[sampling] live_seconds must be at least 1, "
            f"got: {sampling.live_seconds}")
    if (sampling.store_seconds < sampling.live_seconds
            or sampling.store_seconds % sampling.live_seconds):
        raise ConfigError(
            f"[sampling] store_seconds ({sampling.store_seconds}) must be a "
            f"multiple of live_seconds ({sampling.live_seconds})")
    if sampling.store_seconds > AGGREGATE_PERIOD_SECONDS:
        raise ConfigError(
            f"[sampling] store_seconds ({sampling.store_seconds}) cannot exceed "
            f"{AGGREGATE_PERIOD_SECONDS} seconds")
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"[server] port out of range: {cfg.port}")


def estimate_db_bytes(cfg: Config, n_metrics: int) -> int:
    """Projected database size in bytes for this many metrics."""
    raw_rows_per_day = SECONDS_PER_DAY / cfg.sampling.store_seconds * n_metrics
    agg_rows_per_day = SECONDS_PER_DAY / AGGREGATE_PERIOD_SECONDS * n_metrics
    return int(
        cfg.retention.raw_days * raw_rows_per_day * BYTES_PER_METRIC_ROW
        + cfg.retention.aggregate_days * agg_rows_per_day * BYTES_PER_METRIC_ROW
        + OVERHEAD_BYTES)
