import unittest

from healthconsole.config import Config
from healthconsole.findings import Finding, Severity
from healthconsole.probes import FAST
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.store import Store


class FakeProbe:
    """A controlled probe, to drive the scheduler without hardware."""

    NAME = "cpu"
    CADENCE = FAST

    def __init__(self):
        self.value = 10.0
        self.status = "ok"

    def collect(self):
        return {"status": self.status, "usage_pct": self.value, "cores": 4}

    def metrics(self, sample):
        if sample.get("status") != "ok":
            return {}
        return {"cpu.usage": sample["usage_pct"]}

    def evaluate(self, sample, ctx):
        if ctx.sustained.get("cpu.usage_high"):
            return [Finding(id="cpu.usage_high", severity=Severity.ATTENTION,
                            params={"usage_pct": sample["usage_pct"]},
                            detail="raw")]
        return []


class FakeNetworkProbe:
    NAME = "network"
    CADENCE = FAST

    def __init__(self):
        self.rx = 1000
        self.tx = 500

    def collect(self):
        return {
            "status": "ok",
            "counters": {"enp0s25": {"rx": self.rx, "tx": self.tx}},
            "addresses": {"enp0s25": ["192.168.0.3"]},
            "up": {"enp0s25": True},
        }

    def metrics(self, sample):
        return {}

    def evaluate(self, sample, ctx):
        return []


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.ring = Ring(window_seconds=3600, live_seconds=2)
        self.probe = FakeProbe()
        self.scheduler = Scheduler(Config(), self.store, self.ring,
                                   probes=[self.probe])

    def tearDown(self):
        self.store.close()


class TestTick(SchedulerCase):
    def test_tick_fills_the_ring_not_the_database(self):
        self.scheduler.tick(now=1000.0)
        self.assertEqual(len(self.ring.series("cpu.usage")), 1)
        self.assertEqual(self.store.count_rows("metric"), 0)

    def test_tick_returns_the_state(self):
        state = self.scheduler.tick(now=1000.0)
        self.assertEqual(state["score"], 100)
        self.assertEqual(state["findings"], [])
        self.assertIn("cpu", state["probes"])

    def test_state_is_locale_neutral(self):
        self.probe.value = 99.0
        self.scheduler.tick(now=1000.0)
        state = self.scheduler.tick(now=1301.0)
        finding = state["findings"][0]
        self.assertEqual(set(finding),
                         {"id", "severity", "params", "detail", "action"})
        self.assertEqual(finding["severity"], "ATTENTION")

    def test_a_failing_probe_does_not_stop_the_others(self):
        class Exploding(FakeProbe):
            NAME = "memory"

            def collect(self):
                raise RuntimeError("broken sensor")

        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[Exploding(), self.probe])
        state = scheduler.tick(now=1000.0)
        self.assertEqual(state["probes"]["memory"]["status"], "unavailable")
        self.assertEqual(state["probes"]["cpu"]["status"], "ok")

    def test_a_raising_evaluate_does_not_stop_the_others_findings(self):
        class Raising(FakeProbe):
            NAME = "memory"

            def evaluate(self, sample, ctx):
                raise RuntimeError("bad rule")

        raising = Raising()
        self.probe.value = 99.0
        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[raising, self.probe])
        scheduler.tick(now=1000.0)
        state = scheduler.tick(now=1301.0)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["probes"]["memory"]["status"], "ok")
        self.assertIn("RuntimeError", state["probes"]["memory"]["eval_error"])

    def test_a_raising_metrics_marks_the_probe_unavailable(self):
        class BadMetrics(FakeProbe):
            NAME = "memory"

            def metrics(self, sample):
                raise RuntimeError("bad metrics")

        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[BadMetrics()])
        state = scheduler.tick(now=1000.0)
        self.assertEqual(state["probes"]["memory"]["status"], "unavailable")
        self.assertIn("RuntimeError", state["probes"]["memory"]["reason"])

    def test_network_rates_are_in_the_ring_and_current_state(self):
        network = FakeNetworkProbe()
        scheduler = Scheduler(Config(), self.store, self.ring,
                              probes=[network])
        scheduler.tick(now=1000.0)
        network.rx = 3000
        network.tx = 1500
        state = scheduler.tick(now=1002.0)
        self.assertEqual(
            state["probes"]["network"]["rates"]["net.enp0s25.rx_bps"],
            1000.0)
        self.assertEqual(len(self.ring.series("net.enp0s25.tx_bps")), 1)


class TestSustained(SchedulerCase):
    def test_brief_spike_produces_no_finding(self):
        self.probe.value = 99.0
        self.assertEqual(self.scheduler.tick(now=1000.0)["findings"], [])

    def test_sustained_load_produces_a_finding(self):
        self.probe.value = 99.0
        self.scheduler.tick(now=1000.0)
        state = self.scheduler.tick(now=1301.0)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["score"], 92)
        self.assertEqual(state["breakdown"], [["cpu.usage_high", 8]])


class TestFlush(SchedulerCase):
    def test_flush_writes_the_aggregate_to_the_database(self):
        for i in range(15):
            self.scheduler.tick(now=1000.0 + i * 2)
        self.assertEqual(self.scheduler.flush(now=1030.0), 1)
        self.assertEqual(len(self.store.read_series("cpu.usage", 0, 2000)), 1)

    def test_flush_with_no_new_point_writes_nothing(self):
        self.assertEqual(self.scheduler.flush(now=1000.0), 0)

    def test_flush_averages_the_window(self):
        for i, value in enumerate([10.0, 20.0, 30.0]):
            self.probe.value = value
            self.scheduler.tick(now=1000.0 + i * 2)
        self.scheduler.flush(now=1030.0)
        self.assertAlmostEqual(
            self.store.read_series("cpu.usage", 0, 2000)[0][1], 20.0)


class TestMaintain(SchedulerCase):
    def test_maintain_aggregates_and_prunes(self):
        report = self.scheduler.maintain(now=100 * 86400)
        self.assertIn("aggregated", report)
        self.assertIn("deleted", report)


class TestDepth(SchedulerCase):
    def test_depth_reports_what_exists_not_what_is_configured(self):
        self.assertEqual(self.scheduler.state()["depth_days"], 0.0)


class TestDepthCaching(unittest.TestCase):
    """available_depth_seconds() is a full-table MIN(ts) scan (~29ms
    measured on two days of data) that tick()'s 2s cadence cannot absorb.
    It must be computed at construction time and refreshed only by
    flush() -- never recomputed by tick() itself."""

    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_a_fresh_scheduler_computes_depth_from_disk_immediately(self):
        # state()'s depth_days is only refreshed by the next tick(), so the
        # cache itself -- not state() -- is what proves this was computed
        # eagerly rather than left at 0 until the first flush (up to
        # store_seconds, 30s by default, after startup).
        self.store.write_metrics(1000, [("cpu.usage", 1.0, 1.0, 1.0)])
        scheduler = Scheduler(Config(), self.store, Ring(), probes=[],
                              clock=lambda: 1000.0 + 86_400)
        self.assertGreater(scheduler._depth_days_cached, 0.0)

    def test_tick_alone_does_not_refresh_the_cache_but_flush_does(self):
        scheduler = Scheduler(Config(), self.store, Ring(), probes=[])
        scheduler.tick(now=1000.0)
        self.assertEqual(scheduler.state()["depth_days"], 0.0)
        # Written directly, bypassing the scheduler -- simulating history
        # that was already on disk shifting further into the past.
        self.store.write_metrics(1000, [("cpu.usage", 1.0, 1.0, 1.0)])
        scheduler.tick(now=1000.0 + 86_400)
        self.assertEqual(scheduler.state()["depth_days"], 0.0)
        scheduler.flush(now=1000.0 + 86_400)
        scheduler.tick(now=1000.0 + 86_400)
        self.assertGreater(scheduler.state()["depth_days"], 0.0)


class TestEmptyState(SchedulerCase):
    def test_state_before_any_tick_is_not_a_measurement(self):
        # A reader must be able to tell "no measurement has happened yet"
        # apart from a real 100/OK reading -- a zero or a fixed "OK" here
        # would be exactly the reassuring lie this console refuses to tell.
        state = self.scheduler.state()
        self.assertIsNone(state["ts"])
        self.assertIsNone(state["score"])
        self.assertIsNone(state["severity"])
        self.assertEqual(state["findings"], [])


if __name__ == "__main__":
    unittest.main()
