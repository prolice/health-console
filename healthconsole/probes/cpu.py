"""Processor usage, frequency and system load."""

from __future__ import annotations

import os

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, describe_exception, unavailable

NAME = "cpu"
CADENCE = FAST


def collect() -> dict:
    try:
        freq = psutil.cpu_freq()
        load1, load5, load15 = os.getloadavg()
        return {
            "status": "ok",
            "usage_pct": float(psutil.cpu_percent(interval=None)),
            "freq_hz": float(freq.current * 1e6) if freq else None,
            "load1": float(load1), "load5": float(load5), "load15": float(load15),
            "cores": psutil.cpu_count(logical=True) or 1,
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(
            f"cannot read processor state: {describe_exception(exc)}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    series: dict[str, float] = {}
    for key, kind, field_name in (
        ("cpu.usage", "percent", "usage_pct"),
        ("cpu.freq", "hertz", "freq_hz"),
        ("load.1", "load", "load1"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            series[key] = value
    return series


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    if not ctx.sustained.get("cpu.usage_high"):
        return []
    # Do not emit a finding with an unchecked sensor value. A breach that was
    # sustained by earlier valid ticks does not justify displaying a wrong number.
    usage = sane("percent", sample.get("usage_pct"))
    if usage is None:
        return []
    load1 = sane("load", sample.get("load1"))
    load1_str = f"{load1}" if load1 is not None else "unknown"
    return [Finding(
        id="cpu.usage_high",
        severity=Severity.ATTENTION,
        params={"usage_pct": usage,
                "sustain_minutes": rules.CPU_USAGE_SUSTAIN_SECONDS // 60},
        detail=(f"usage={usage:.0f}% sustained >= "
                f"{rules.CPU_USAGE_SUSTAIN_SECONDS}s · "
                f"load1={load1_str} over {sample.get('cores')} cores"),
    )]
