"""Hardware lies. Not maliciously: sloppy drivers, careless ACPI firmware,
units that differ by vendor.

Displaying a wrong number with confidence is worse than admitting ignorance —
it is what destroys trust in a diagnostic tool. Every value passes through here
before being displayed or stored.
"""

from __future__ import annotations

# Validity domains. An unknown kind is refused rather than assumed valid:
# forgetting to declare a domain must not open a silent door.
RANGES: dict[str, tuple[float, float]] = {
    "percent": (0.0, 100.0),
    "celsius": (-20.0, 125.0),
    "hertz": (100_000_000.0, 10_000_000_000.0),
    "bytes": (0.0, 1e15),
    "bytes_per_second": (0.0, 1e12),
    "seconds": (0.0, 1e10),
    "count": (0.0, 1e9),
    "load": (0.0, 1024.0),
}

# A driver may report slightly more than full capacity right after a complete
# charge. Five percent of tolerance, no more.
CAPACITY_TOLERANCE = 1.05


def is_plausible(kind: str, value: float | None) -> bool:
    if value is None:
        return False
    bounds = RANGES.get(kind)
    if bounds is None:
        return False
    low, high = bounds
    return low <= value <= high


def sane(kind: str, value: float | None) -> float | None:
    """The value if credible, otherwise None. Never a fallback to zero:
    a zero reads as a measurement, whereas None reads as an absence."""
    return value if is_plausible(kind, value) else None


def battery_capacity_is_coherent(now: float, full: float, design: float) -> bool:
    """Cross-check the three battery capacities.

    On the target machine the driver reports charge_now = 467000 against
    charge_full = 1000. Without this check the console would display
    "0 % wear, charged to 46,700 %".
    """
    if design <= 0 or full <= 0 or now < 0:
        return False
    if full > design * CAPACITY_TOLERANCE:
        return False
    if now > full * CAPACITY_TOLERANCE:
        return False
    return True
