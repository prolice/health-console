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


if __name__ == "__main__":
    unittest.main()
