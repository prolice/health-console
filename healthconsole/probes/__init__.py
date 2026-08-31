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
    "cpu", "memory", "thermal", "network", "battery", "storage", "updates",
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
        module = importlib.import_module(f"healthconsole.probes.{name}")
        if cadence is None or module.CADENCE == cadence:
            modules.append(module)
    return modules


def unavailable(reason: str) -> dict:
    """`reason` is diagnostic material in English, not user-facing prose."""
    return {"status": "unavailable", "reason": reason}


def describe_exception(exc: Exception) -> str:
    """"TypeName: message", or just "TypeName" when str(exc) is empty.

    Some exceptions (a handful of OSError subclasses; a bare
    `raise SomeError()` with no message) have an empty str(). Always
    appending ": {exc}" to a reason would then leave it ending in a bare
    colon with nothing after it -- exactly the kind of half-sentence this
    console's reasons must never show.
    """
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
