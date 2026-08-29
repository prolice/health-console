import unittest

from healthconsole.findings import FINDING_IDS, Severity
from healthconsole.probes import FAST, EvalContext, load_probes
from healthconsole.probes import cpu


def context(sustained=None, cores=4):
    return EvalContext(sustained=sustained or {}, cores=cores)


class TestRegistry(unittest.TestCase):
    def test_cpu_is_registered_as_fast(self):
        self.assertIn("cpu", [module.NAME for module in load_probes(FAST)])

    def test_every_probe_honours_the_contract(self):
        for module in load_probes():
            for attribute in ("NAME", "CADENCE", "collect", "metrics", "evaluate"):
                self.assertTrue(hasattr(module, attribute),
                                f"{module.__name__} lacks {attribute}")


class TestMetrics(unittest.TestCase):
    def test_extracts_numeric_series(self):
        sample = {"status": "ok", "usage_pct": 23.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertEqual(cpu.metrics(sample),
                         {"cpu.usage": 23.0, "cpu.freq": 3.4e9, "load.1": 0.62})

    def test_implausible_values_are_not_historised(self):
        sample = {"status": "ok", "usage_pct": 4700.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertNotIn("cpu.usage", cpu.metrics(sample))

    def test_unavailable_sample_yields_no_metric(self):
        self.assertEqual(
            cpu.metrics({"status": "unavailable", "reason": "x"}), {})


class TestEvaluate(unittest.TestCase):
    IDLE = {"status": "ok", "usage_pct": 12.0, "freq_hz": 1.6e9,
            "load1": 0.3, "cores": 4}
    BUSY = {"status": "ok", "usage_pct": 99.0, "freq_hz": 3.4e9,
            "load1": 4.0, "cores": 4}

    def test_idle_machine_produces_no_finding(self):
        self.assertEqual(cpu.evaluate(self.IDLE, context()), [])

    def test_spike_alone_produces_no_finding(self):
        # Without the scheduler confirming the breach held, a spike must not
        # trigger anything.
        self.assertEqual(cpu.evaluate(self.BUSY, context()), [])

    def test_sustained_load_produces_attention(self):
        findings = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].id, "cpu.usage_high")

    def test_finding_carries_parameters_not_prose(self):
        finding = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))[0]
        self.assertIn(finding.id, FINDING_IDS)
        self.assertIn("usage_pct", finding.params)
        self.assertEqual(finding.params["usage_pct"], 99.0)
        self.assertTrue(finding.detail)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(
            cpu.evaluate({"status": "unavailable", "reason": "x"}, context()), [])

    def test_implausible_usage_produces_no_finding_even_while_sustained(self):
        # A breach sustained by earlier valid ticks does not justify emitting a
        # finding with an implausible sensor value.
        sample = {"status": "ok", "usage_pct": 4700.0, "freq_hz": 3.4e9,
                  "load1": 0.3, "cores": 4}
        self.assertEqual(
            cpu.evaluate(sample, context({"cpu.usage_high": True})), [])

    def test_sustained_load_produces_finding_with_checked_params(self):
        # Verify the normal sustained-load case still produces a finding with
        # plausibility-checked params.
        findings = cpu.evaluate(self.BUSY, context({"cpu.usage_high": True}))
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.id, "cpu.usage_high")
        self.assertEqual(finding.params["usage_pct"], 99.0)
        self.assertIn("sustain_minutes", finding.params)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample = cpu.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertGreaterEqual(sample["cores"], 1)
        self.assertIsInstance(sample["usage_pct"], float)


if __name__ == "__main__":
    unittest.main()
