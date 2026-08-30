import unittest

from healthconsole.verdict import HysteresisTracker


class TestFlapping(unittest.TestCase):
    def setUp(self):
        self.tracker = HysteresisTracker()

    def test_brief_spike_does_not_open(self):
        # A compile loading the CPU for three seconds must not turn the
        # console red; it would never be trusted again.
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=3, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 10, 90, 70, now=4, sustain_seconds=300))

    def test_sustained_breach_opens(self):
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=299, sustain_seconds=300))
        self.assertTrue(self.tracker.update(
            "cpu", 95, 90, 70, now=300, sustain_seconds=300))

    def test_without_sustain_opens_immediately(self):
        self.assertTrue(self.tracker.update("disk", 85, 80, 75, now=0))

    def test_stays_open_between_low_and_high(self):
        self.tracker.update("disk", 85, 80, 75, now=0)
        self.assertTrue(self.tracker.update("disk", 78, 80, 75, now=10))

    def test_closes_only_below_low(self):
        self.tracker.update("disk", 85, 80, 75, now=0)
        self.assertFalse(self.tracker.update("disk", 74, 80, 75, now=10))

    def test_stays_closed_between_low_and_high(self):
        self.assertFalse(self.tracker.update("disk", 78, 80, 75, now=0))

    def test_sustain_timer_resets_when_value_drops(self):
        self.tracker.update("cpu", 95, 90, 70, now=0, sustain_seconds=300)
        self.tracker.update("cpu", 50, 90, 70, now=100, sustain_seconds=300)
        self.tracker.update("cpu", 95, 90, 70, now=200, sustain_seconds=300)
        self.assertFalse(self.tracker.update(
            "cpu", 95, 90, 70, now=450, sustain_seconds=300))
        self.assertTrue(self.tracker.update(
            "cpu", 95, 90, 70, now=500, sustain_seconds=300))


class TestUnavailableValues(unittest.TestCase):
    def test_none_never_opens(self):
        tracker = HysteresisTracker()
        self.assertFalse(tracker.update("bat", None, 30, 25, now=0))

    def test_none_does_not_close_an_open_state(self):
        # Losing the measurement is not proof the problem went away.
        tracker = HysteresisTracker()
        tracker.update("bat", 40, 30, 25, now=0)
        self.assertTrue(tracker.update("bat", None, 30, 25, now=10))


class TestIntrospection(unittest.TestCase):
    def test_opened_at_records_the_moment(self):
        tracker = HysteresisTracker()
        tracker.update("disk", 85, 80, 75, now=1234)
        self.assertEqual(tracker.opened_at("disk"), 1234)

    def test_opened_at_is_none_when_closed(self):
        self.assertIsNone(HysteresisTracker().opened_at("disk"))

    def test_snapshot_lists_open_keys_with_their_start(self):
        tracker = HysteresisTracker()
        tracker.update("disk", 85, 80, 75, now=100)
        tracker.update("cpu", 10, 90, 70, now=100)
        self.assertEqual(tracker.snapshot(), {"disk": 100})


if __name__ == "__main__":
    unittest.main()
