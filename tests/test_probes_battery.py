import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import battery


def context():
    return EvalContext(sustained={}, cores=4)


class TestWear(unittest.TestCase):
    def test_new_battery_has_no_wear(self):
        self.assertEqual(battery.wear_pct(full=5_000_000, design=5_000_000), 0.0)

    def test_worn_battery(self):
        self.assertAlmostEqual(
            battery.wear_pct(full=3_500_000, design=5_000_000), 30.0)

    def test_zero_design_is_unknown(self):
        self.assertIsNone(battery.wear_pct(full=1000, design=0))


class TestIncoherentDriver(unittest.TestCase):
    """Real values from the target machine: the driver lies."""

    SAMPLE = {"status": "incoherent",
              "reason": "implausible capacities reported by the driver",
              "raw": {"charge_now": 467000, "charge_full": 1000,
                      "charge_full_design": 1000}}

    def test_incoherent_sample_historises_nothing(self):
        self.assertEqual(battery.metrics(self.SAMPLE), {})

    def test_incoherent_sample_produces_an_informational_finding(self):
        # The user is told the driver is unreliable, rather than shown a
        # reassuring zero or nothing at all.
        findings = battery.evaluate(self.SAMPLE, context())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "battery.incoherent")
        self.assertIs(findings[0].severity, Severity.INFO)

    def test_incoherent_finding_keeps_raw_values_in_detail(self):
        finding = battery.evaluate(self.SAMPLE, context())[0]
        self.assertIn("467000", finding.detail)


class TestEvaluate(unittest.TestCase):
    def healthy(self, wear=0.0, charge=80.0):
        return {"status": "ok", "wear_pct": wear, "charge_pct": charge,
                "present": True, "state": "Discharging", "raw": {}}

    def test_healthy_battery_is_silent(self):
        self.assertEqual(battery.evaluate(self.healthy(), context()), [])

    def test_worn_battery_warns(self):
        findings = battery.evaluate(self.healthy(wear=35.0), context())
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].params["wear_pct"], 35.0)

    def test_very_worn_battery_is_urgent(self):
        findings = battery.evaluate(self.healthy(wear=60.0), context())
        self.assertIs(findings[0].severity, Severity.URGENT)

    def test_absent_battery_produces_no_finding(self):
        self.assertEqual(battery.evaluate(
            {"status": "unavailable", "reason": "no battery"}, context()), [])


class TestCollectSmoke(unittest.TestCase):
    def test_collect_never_raises(self):
        sample = battery.collect()
        self.assertIn(sample["status"], {"ok", "unavailable", "incoherent"})


if __name__ == "__main__":
    unittest.main()
