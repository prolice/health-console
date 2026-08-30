"""Smoke test: runs the real probes on this machine.

Unlike every other test, this one depends on the hardware.
"""

import math
import unittest

from healthconsole.probes import EvalContext, load_probes


class TestRealProbes(unittest.TestCase):
    def test_every_probe_collects_without_raising(self):
        for module in load_probes():
            with self.subTest(probe=module.NAME):
                sample = module.collect()
                self.assertIn(sample.get("status"),
                              {"ok", "unavailable", "incoherent"})

    def test_every_probe_evaluates_its_own_sample(self):
        ctx = EvalContext(sustained={}, cores=1)
        for module in load_probes():
            with self.subTest(probe=module.NAME):
                self.assertIsInstance(
                    module.evaluate(module.collect(), ctx), list)

    def test_metrics_are_all_finite_numbers(self):
        for module in load_probes():
            for key, value in module.metrics(module.collect()).items():
                with self.subTest(metric=key):
                    self.assertTrue(math.isfinite(value))


if __name__ == "__main__":
    unittest.main()
