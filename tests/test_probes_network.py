import unittest
from unittest import mock

from healthconsole.probes import network


class TestRates(unittest.TestCase):
    def test_computes_bytes_per_second(self):
        previous = {"enp0s25": {"rx": 1000, "tx": 500}}
        current = {"enp0s25": {"rx": 3000, "tx": 1500}}
        rates = network.rates(previous, current, dt=2.0)
        self.assertEqual(rates["net.enp0s25.rx_bps"], 1000.0)
        self.assertEqual(rates["net.enp0s25.tx_bps"], 500.0)

    def test_counter_reset_yields_no_negative_rate(self):
        # A restarting interface resets its counters. That is not a negative
        # throughput: the sample is skipped.
        previous = {"wlo1": {"rx": 10_000, "tx": 10_000}}
        current = {"wlo1": {"rx": 5, "tx": 5}}
        self.assertEqual(network.rates(previous, current, dt=2.0), {})

    def test_new_interface_is_ignored_until_second_sample(self):
        self.assertEqual(
            network.rates({}, {"eth0": {"rx": 1, "tx": 1}}, dt=2.0), {})

    def test_zero_dt_is_refused(self):
        previous = {"eth0": {"rx": 1, "tx": 1}}
        current = {"eth0": {"rx": 2, "tx": 2}}
        self.assertEqual(network.rates(previous, current, dt=0.0), {})


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_interfaces(self):
        sample = network.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertIn("lo", sample["counters"])


class TestCollectFailureReason(unittest.TestCase):
    def test_reason_includes_the_exception_type_even_when_str_is_empty(self):
        class Blank(Exception):
            def __str__(self):
                return ""

        with mock.patch("psutil.net_io_counters", side_effect=Blank("")):
            sample = network.collect()
        self.assertEqual(sample["status"], "unavailable")
        self.assertIn("Blank", sample["reason"])
        self.assertFalse(sample["reason"].rstrip().endswith(":"))


if __name__ == "__main__":
    unittest.main()
