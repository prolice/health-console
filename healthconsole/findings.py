"""The finding: unit of interpretation.

A finding carries an identifier and parameters, never a sentence. Baking prose
into a probe would make the console untranslatable without duplicating every
probe, so the wording lives in the message catalogues and the id selects it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    OK = 0
    INFO = 1
    ATTENTION = 2
    URGENT = 3


class UnknownFindingId(ValueError):
    """A finding id with no catalogue entry would render as a blank card."""


class UnknownFindingParam(ValueError):
    """A parameter no catalogue template can interpolate."""


# Every id the code may emit, with the parameters its templates may use.
# This registry is what makes the catalogues testable: each locale must
# translate exactly these ids, using only these placeholders.
FINDING_PARAMS: dict[str, frozenset[str]] = {
    "cpu.usage_high": frozenset({"usage_pct", "sustain_minutes"}),
    "memory.pressure": frozenset({"available_bytes", "available_pct",
                                  "swap_used_bytes"}),
    "thermal.high": frozenset({"temperature_c", "sustain_minutes"}),
    "thermal.critical": frozenset({"temperature_c"}),
    "battery.wear": frozenset({"wear_pct"}),
    "battery.incoherent": frozenset(),
}

FINDING_IDS: frozenset[str] = frozenset(FINDING_PARAMS)


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: Severity
    # Numbers and identifiers the catalogue interpolates, formatted by Intl in
    # the active locale.
    params: dict[str, float | int | str] = field(default_factory=dict)
    # Raw technical string for Expert mode. Deliberately untranslated: it is
    # diagnostic material, and translating it would make reports harder to
    # compare.
    detail: str = ""
    # Action catalogue reference, or None.
    action: str | None = None

    def __post_init__(self) -> None:
        allowed = FINDING_PARAMS.get(self.id)
        if allowed is None:
            raise UnknownFindingId(
                f"{self.id!r} is not declared in FINDING_PARAMS; add it there "
                f"and to every catalogue under web/i18n/")
        unknown = set(self.params) - allowed
        if unknown:
            raise UnknownFindingParam(
                f"{self.id!r} emits {sorted(unknown)}, which no catalogue "
                f"template can interpolate; declare them in FINDING_PARAMS")
