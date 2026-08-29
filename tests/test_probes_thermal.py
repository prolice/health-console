import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import thermal


def context(sustained=None):
    return EvalContext(sustained=sustained or {}, cores=4)


def sample(package=60.0, zones=None):
    return {"status": "ok", "package_c": package,
            "zones": zones or {"coretemp": 60.0, "acpitz": 45.0}}


class TestMetrics(unittest.TestCase):
    def test_each_zone_becomes_a_series(self):
        series = thermal.metrics(sample())
        self.assertEqual(series["thermal.coretemp"], 60.0)
        self.assertEqual(series["cpu.temp.pkg"], 60.0)

    def test_impossible_temperature_is_dropped(self):
        series = thermal.metrics(
            sample(zones={"broken": 5000.0, "coretemp": 60.0}))
        self.assertNotIn("thermal.broken", series)
        self.assertIn("thermal.coretemp", series)


class TestEvaluate(unittest.TestCase):
    def test_normal_temperature_is_silent(self):
        # 77 °C is this machine's normal loaded temperature.
        self.assertEqual(thermal.evaluate(sample(77.0), context()), [])

    def test_hot_but_brief_is_silent(self):
        self.assertEqual(thermal.evaluate(sample(88.0), context()), [])

    def test_sustained_heat_warns(self):
        findings = thermal.evaluate(
            sample(88.0), context({"cpu.temp_high": True}))
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].params["temperature_c"], 88.0)

    def test_critical_temperature_is_urgent_without_waiting(self):
        findings = thermal.evaluate(sample(97.0), context())
        self.assertIs(findings[0].severity, Severity.URGENT)
        self.assertEqual(findings[0].id, "thermal.critical")

    def test_missing_package_temperature_is_silent(self):
        self.assertEqual(thermal.evaluate(
            {"status": "ok", "package_c": None, "zones": {}}, context()), [])


if __name__ == "__main__":
    unittest.main()
