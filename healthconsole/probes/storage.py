"""Filesystem capacity and reclaimable local package cache."""

from __future__ import annotations

import shutil
from pathlib import Path

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, describe_exception, unavailable

NAME = "storage"
CADENCE = FAST
ROOT = Path("/")
APT_CACHE = Path("/var/cache/apt/archives")


def _tree_size(path: Path) -> int:
    total = 0
    try:
        entries = list(path.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def collect() -> dict:
    try:
        usage = shutil.disk_usage(ROOT)
        used = usage.total - usage.free
        used_pct = used / usage.total * 100.0 if usage.total else 0.0
        return {
            "status": "ok",
            "root": str(ROOT),
            "total": int(usage.total),
            "used": int(used),
            "free": int(usage.free),
            "used_pct": used_pct,
            "apt_cache_bytes": _tree_size(APT_CACHE),
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(
            f"cannot read filesystem capacity: {describe_exception(exc)}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, kind, field_name in (
        ("disk.root.used_pct", "percent", "used_pct"),
        ("disk.root.free", "bytes", "free"),
        ("disk.apt_cache", "bytes", "apt_cache_bytes"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            series[key] = float(value)
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    used_pct = sane("percent", sample.get("used_pct"))
    free = sane("bytes", sample.get("free"))
    if used_pct is None or free is None:
        return []
    if used_pct < rules.DISK_ATTENTION_PCT and free >= rules.DISK_URGENT_FREE_BYTES:
        return []
    urgent = used_pct >= rules.DISK_URGENT_PCT or free < rules.DISK_URGENT_FREE_BYTES
    return [Finding(
        id="storage.root_full",
        severity=Severity.URGENT if urgent else Severity.ATTENTION,
        params={"used_pct": used_pct, "free_bytes": free},
        detail=f"root={sample.get('root')} used={used_pct:.1f}% free={free}",
        action="clean.aptcache",
    )]
