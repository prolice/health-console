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
