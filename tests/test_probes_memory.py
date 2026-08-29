import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import memory

GIB = 1024 ** 3


def context():
    return EvalContext(sustained={}, cores=4)


def sample(available_pct=50.0, swap_used=0):
    return {"status": "ok", "total": 5 * GIB,
            "available": int(5 * GIB * available_pct / 100),
            "available_pct": available_pct, "swap_used": swap_used,
            "swap_total": 2 * GIB}


class TestMetrics(unittest.TestCase):
    def test_extracts_series(self):
        keys = memory.metrics(sample())
        self.assertIn("mem.available", keys)
        self.assertIn("mem.available_pct", keys)
        self.assertIn("mem.swap.used", keys)


class TestEvaluate(unittest.TestCase):
    def test_comfortable_memory_is_silent(self):
        self.assertEqual(memory.evaluate(sample(50.0), context()), [])

    def test_low_memory_alone_is_silent(self):
        # Low "free" memory without active swapping is Linux behaving
        # normally: the cache fills whatever is unused. Not a defect.
        self.assertEqual(
            memory.evaluate(sample(10.0, swap_used=0), context()), [])

    def test_low_memory_with_active_swap_warns(self):
        findings = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)
        self.assertEqual(findings[0].id, "memory.pressure")

    def test_finding_carries_figures_for_the_catalogue(self):
        finding = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())[0]
        self.assertIn("available_bytes", finding.params)
        self.assertIn("available_pct", finding.params)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(memory.evaluate(
            {"status": "unavailable", "reason": "x"}, context()), [])


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample_data = memory.collect()
        self.assertEqual(sample_data["status"], "ok")
        self.assertGreater(sample_data["total"], 0)


if __name__ == "__main__":
    unittest.main()
