import unittest

from healthconsole.findings import (
    FINDING_IDS, FINDING_PARAMS, Finding, Severity, UnknownFindingId,
    UnknownFindingParam,
)
from healthconsole.verdict import score, score_breakdown, worst


def make(finding_id, severity):
    return Finding(id=finding_id, severity=severity, params={}, detail="raw")


class TestFindingShape(unittest.TestCase):
    def test_known_id_is_accepted(self):
        self.assertIn("cpu.usage_high", FINDING_IDS)
        make("cpu.usage_high", Severity.INFO)

    def test_unknown_id_is_refused(self):
        # A finding whose id has no catalogue entry would render as a blank
        # card. Refusing it here turns that into a loud failure.
        with self.assertRaises(UnknownFindingId):
            make("cpu.invented", Severity.INFO)

    def test_finding_is_immutable(self):
        finding = make("cpu.usage_high", Severity.INFO)
        with self.assertRaises(Exception):
            finding.severity = Severity.URGENT

    def test_no_prose_fields_exist(self):
        # Wording lives in the catalogues, never on the finding.
        finding = make("cpu.usage_high", Severity.INFO)
        for forbidden in ("title", "why", "titre", "pourquoi", "message"):
            self.assertFalse(hasattr(finding, forbidden),
                             f"{forbidden} must not exist on Finding")

    def test_params_carry_numbers_for_the_catalogue(self):
        finding = Finding(id="battery.wear", severity=Severity.ATTENTION,
                          params={"wear_pct": 35.2}, detail="raw")
        self.assertEqual(finding.params["wear_pct"], 35.2)

    def test_undeclared_param_is_refused(self):
        # A catalogue template can only interpolate what the probe emits. If a
        # probe invents a parameter, the template that would use it was never
        # written, so the value would be silently dropped.
        with self.assertRaises(UnknownFindingParam):
            Finding(id="battery.wear", severity=Severity.ATTENTION,
                    params={"wear": 35.2}, detail="raw")

    def test_finding_ids_are_exactly_the_registry_keys(self):
        self.assertEqual(FINDING_IDS, frozenset(FINDING_PARAMS))

    def test_every_declared_id_has_a_param_set(self):
        for finding_id, params in FINDING_PARAMS.items():
            self.assertIsInstance(params, frozenset, finding_id)


class TestScore(unittest.TestCase):
    def test_no_finding_is_exactly_100(self):
        # Not 94 "to look serious": an unexplained score is a lie.
        self.assertEqual(score([]), 100)

    def test_ok_findings_cost_nothing(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.OK)]), 100)

    def test_info_costs_two(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.INFO)]), 98)

    def test_attention_costs_eight(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.ATTENTION)]), 92)

    def test_urgent_costs_twentyfive(self):
        self.assertEqual(score([make("cpu.usage_high", Severity.URGENT)]), 75)

    def test_penalties_accumulate(self):
        findings = [make("cpu.usage_high", Severity.URGENT),
                    make("memory.pressure", Severity.ATTENTION),
                    make("battery.wear", Severity.INFO)]
        self.assertEqual(score(findings), 100 - 25 - 8 - 2)

    def test_score_never_goes_below_zero(self):
        findings = [make(i, Severity.URGENT) for i in sorted(FINDING_IDS)]
        findings = findings * 10
        self.assertEqual(score(findings), 0)


class TestBreakdown(unittest.TestCase):
    def test_every_lost_point_is_traceable(self):
        findings = [make("thermal.high", Severity.ATTENTION),
                    make("battery.wear", Severity.INFO)]
        detail = score_breakdown(findings)
        self.assertEqual(detail, [("thermal.high", 8), ("battery.wear", 2)])
        self.assertEqual(100 - sum(points for _, points in detail),
                         score(findings))

    def test_breakdown_is_empty_when_perfect(self):
        self.assertEqual(score_breakdown([]), [])

    def test_breakdown_is_sorted_by_cost(self):
        findings = [make("battery.wear", Severity.INFO),
                    make("thermal.critical", Severity.URGENT)]
        self.assertEqual([fid for fid, _ in score_breakdown(findings)],
                         ["thermal.critical", "battery.wear"])


class TestWorst(unittest.TestCase):
    def test_worst_of_nothing_is_ok(self):
        self.assertIs(worst([]), Severity.OK)

    def test_worst_wins_over_milder(self):
        findings = [make("battery.wear", Severity.INFO),
                    make("thermal.critical", Severity.URGENT)]
        self.assertIs(worst(findings), Severity.URGENT)


if __name__ == "__main__":
    unittest.main()
