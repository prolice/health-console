import unittest
from unittest import mock

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

    def test_implausible_available_pct_produces_no_finding_even_with_active_swap(self):
        # Implausible available_pct must not result in a finding, even if swap
        # is genuinely active. We cannot honestly claim memory pressure with
        # unchecked sensor data.
        implausible = {"status": "ok", "total": 5 * GIB,
                       "available": int(5 * GIB * 10 / 100),
                       "available_pct": 4700.0, "swap_used": 512 * 1024 ** 2,
                       "swap_total": 2 * GIB}
        self.assertEqual(memory.evaluate(implausible, context()), [])

    def test_low_memory_with_active_swap_produces_finding_with_checked_params(self):
        # Verify the normal low-memory-with-swap case still produces a finding
        # with plausibility-checked params.
        findings = memory.evaluate(
            sample(10.0, swap_used=512 * 1024 ** 2), context())
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.id, "memory.pressure")
        self.assertIn("available_bytes", finding.params)
        self.assertIn("available_pct", finding.params)
        self.assertIn("swap_used_bytes", finding.params)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample_data = memory.collect()
        self.assertEqual(sample_data["status"], "ok")
        self.assertGreater(sample_data["total"], 0)


class TestCollectFailureReason(unittest.TestCase):
    def test_reason_includes_the_exception_type_even_when_str_is_empty(self):
        class Blank(Exception):
            def __str__(self):
                return ""

        with mock.patch("psutil.virtual_memory", side_effect=Blank("")):
            sample_data = memory.collect()
        self.assertEqual(sample_data["status"], "unavailable")
        self.assertIn("Blank", sample_data["reason"])
        self.assertFalse(sample_data["reason"].rstrip().endswith(":"))


if __name__ == "__main__":
    unittest.main()
