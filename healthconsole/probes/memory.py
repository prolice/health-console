"""Physical memory and swap."""

from __future__ import annotations

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, describe_exception, unavailable

NAME = "memory"
CADENCE = FAST
GIB = 1024 ** 3


def collect() -> dict:
    try:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "status": "ok",
            "total": int(virtual.total), "available": int(virtual.available),
            "available_pct": float(virtual.available) / float(virtual.total) * 100.0,
            "swap_used": int(swap.used), "swap_total": int(swap.total),
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(
            f"cannot read memory state: {describe_exception(exc)}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, kind, field_name in (
        ("mem.available", "bytes", "available"),
        ("mem.available_pct", "percent", "available_pct"),
        ("mem.swap.used", "bytes", "swap_used"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            series[key] = float(value)
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    # Check plausibility of all values before putting them in user-facing params.
    available_pct = sane("percent", sample.get("available_pct"))
    available = sane("bytes", sample.get("available"))
    swap_used = sane("bytes", sample.get("swap_used", 0))
    # We cannot honestly describe memory pressure without trustworthy figures.
    if available_pct is None or available is None:
        return []
    if swap_used is None:
        return []
    # Low "free" memory is Linux behaving normally: the cache fills whatever is
    # unused. Only active swapping signals real pressure.
    if available_pct >= rules.MEM_ATTENTION_AVAILABLE_PCT:
        return []
    if swap_used < rules.MEM_SWAP_ACTIVE_BYTES:
        return []
    return [Finding(
        id="memory.pressure",
        severity=Severity.ATTENTION,
        params={"available_bytes": available,
                "available_pct": available_pct,
                "swap_used_bytes": swap_used},
        detail=(f"available={available / GIB:.2f} GiB "
                f"({available_pct:.1f}%) · swap_used={swap_used / GIB:.2f} GiB"),
    )]
