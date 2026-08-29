"""Probe registry and shared contract.

A probe that fails returns an `unavailable` sample carrying its reason; it never
brings the others down. A diagnostic tool that crashes when something is wrong
is worse than useless.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType

FAST = "fast"
SLOW = "slow"

PROBE_MODULES: tuple[str, ...] = (
    "cpu", "memory", "thermal", "network", "battery",
)


@dataclass(frozen=True)
class EvalContext:
    """Everything `evaluate()` needs beyond the sample.

    Breaches confirmed over time are computed by the scheduler and passed in
    here, which keeps `evaluate()` purely functional: with no clock of its own,
    it stays testable against frozen samples.
    """

    sustained: dict[str, bool] = field(default_factory=dict)
    cores: int = 1


def load_probes(cadence: str | None = None) -> list[ModuleType]:
    modules = []
    for name in PROBE_MODULES:
        try:
            module = importlib.import_module(f"healthconsole.probes.{name}")
            if cadence is None or module.CADENCE == cadence:
                modules.append(module)
        except ModuleNotFoundError:
            pass
    return modules


def unavailable(reason: str) -> dict:
    """`reason` is diagnostic material in English, not user-facing prose."""
    return {"status": "unavailable", "reason": reason}
