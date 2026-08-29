"""Network interfaces and throughput.

`collect()` returns absolute counters; rate computation is a separate pure
function, so it is testable without a machine and without hidden state.
"""

from __future__ import annotations

import psutil

from healthconsole.findings import Finding
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, describe_exception, unavailable

NAME = "network"
CADENCE = FAST


def collect() -> dict:
    try:
        counters = {
            name: {"rx": int(counter.bytes_recv), "tx": int(counter.bytes_sent)}
            for name, counter in psutil.net_io_counters(pernic=True).items()
        }
        addresses = {
            name: [entry.address for entry in entries
                   if entry.family.name == "AF_INET"]
            for name, entries in psutil.net_if_addrs().items()
        }
        up = {name: stat.isup for name, stat in psutil.net_if_stats().items()}
        return {"status": "ok", "counters": counters,
                "addresses": addresses, "up": up}
    except Exception as exc:                      # noqa: BLE001
        return unavailable(
            f"cannot read network state: {describe_exception(exc)}")


def rates(previous: dict, current: dict, dt: float) -> dict[str, float]:
    """Throughput in bytes per second between two counter samples.

    A restarting interface resets its counters: that is not a negative
    throughput, so the sample is simply skipped.
    """
    if dt <= 0:
        return {}
    series: dict[str, float] = {}
    for name, counters in current.items():
        earlier = previous.get(name)
        if earlier is None:
            continue
        for direction in ("rx", "tx"):
            delta = counters[direction] - earlier[direction]
            if delta < 0:
                continue
            value = sane("bytes_per_second", delta / dt)
            if value is not None:
                series[f"net.{name}.{direction}_bps"] = value
    return series


def metrics(sample: dict) -> dict[str, float]:
    # Rates need two samples: the scheduler computes them with rates() and
    # pushes them into the ring itself.
    return {}


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    return []
