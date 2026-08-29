"""Explainable score.

The score is a consequence of the findings, never an oracle. Every lost point
traces back to a named finding: this is the explicit refusal of the antivirus
magic number that nobody can explain.
"""

from __future__ import annotations

from healthconsole import rules
from healthconsole.findings import Finding, Severity

PENALTIES: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: rules.PENALTY_INFO,
    Severity.ATTENTION: rules.PENALTY_ATTENTION,
    Severity.URGENT: rules.PENALTY_URGENT,
}


def score_breakdown(findings: list[Finding]) -> list[tuple[str, int]]:
    """(finding id, points removed), most expensive first."""
    detail = [(f.id, PENALTIES[f.severity]) for f in findings
              if PENALTIES[f.severity] > 0]
    detail.sort(key=lambda pair: pair[1], reverse=True)
    return detail


def score(findings: list[Finding]) -> int:
    lost = sum(points for _, points in score_breakdown(findings))
    return max(0, 100 - lost)


def worst(findings: list[Finding]) -> Severity:
    return max((f.severity for f in findings), default=Severity.OK)


class HysteresisTracker:
    """Prevents findings from flapping.

    A threshold must be crossed *and held* to open a finding, and the value
    must fall below a distinct lower threshold to close it. Without this, a
    three-second load spike would turn the console red and nobody would
    believe it again.
    """

    def __init__(self) -> None:
        self._open: dict[str, float] = {}           # key -> opening instant
        self._breach_since: dict[str, float] = {}   # key -> breach start

    def update(self, key: str, value: float | None, high: float, low: float,
               now: float, sustain_seconds: float = 0.0) -> bool:
        if value is None:
            # Losing the measurement is not proof the problem went away: keep
            # the state, and never open one on an absence.
            return key in self._open

        if key in self._open:
            if value < low:
                del self._open[key]
                self._breach_since.pop(key, None)
            return key in self._open

        if value >= high:
            since = self._breach_since.setdefault(key, now)
            if now - since >= sustain_seconds:
                self._open[key] = now
                self._breach_since.pop(key, None)
        else:
            self._breach_since.pop(key, None)
        return key in self._open

    def is_open(self, key: str) -> bool:
        return key in self._open

    def opened_at(self, key: str) -> float | None:
        return self._open.get(key)

    def snapshot(self) -> dict[str, float]:
        return dict(self._open)
