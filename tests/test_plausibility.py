import unittest

from healthconsole.plausibility import (
    battery_capacity_is_coherent, is_plausible, sane,
)


class TestRanges(unittest.TestCase):
    def test_percent_domain(self):
        self.assertTrue(is_plausible("percent", 0))
        self.assertTrue(is_plausible("percent", 100))
        self.assertFalse(is_plausible("percent", -1))
        self.assertFalse(is_plausible("percent", 46700))

    def test_temperature_domain(self):
        self.assertTrue(is_plausible("celsius", 77.0))
        self.assertFalse(is_plausible("celsius", -273.0))
        self.assertFalse(is_plausible("celsius", 200.0))

    def test_frequency_domain_in_hertz(self):
        self.assertTrue(is_plausible("hertz", 3_400_000_000))
        self.assertFalse(is_plausible("hertz", 50_000_000))

    def test_none_is_never_plausible(self):
        self.assertFalse(is_plausible("percent", None))

    def test_unknown_kind_is_refused_rather_than_assumed_valid(self):
        self.assertFalse(is_plausible("unknown", 42))


class TestSane(unittest.TestCase):
    def test_returns_value_when_plausible(self):
        self.assertEqual(sane("percent", 42.0), 42.0)

    def test_returns_none_when_implausible(self):
        self.assertIsNone(sane("percent", 46700))


class TestBatteryCoherence(unittest.TestCase):
    def test_healthy_battery_is_coherent(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=3_000_000, full=4_000_000, design=5_000_000))

    def test_slightly_over_full_is_tolerated(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=4_100_000, full=4_000_000, design=5_000_000))

    def test_this_machine_reports_nonsense(self):
        # Real values from the target HP laptop: charge_now is 467 times
        # charge_full. The driver lies; we must detect it.
        self.assertFalse(battery_capacity_is_coherent(
            now=467_000, full=1_000, design=1_000))

    def test_full_above_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=6_000_000, design=5_000_000))

    def test_zero_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=1_000, design=0))


if __name__ == "__main__":
    unittest.main()
