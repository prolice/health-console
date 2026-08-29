import unittest

from healthconsole.ring import Ring


class TestCapacity(unittest.TestCase):
    def test_capacity_matches_window_over_resolution(self):
        self.assertEqual(Ring(window_seconds=3600, live_seconds=2).capacity, 1800)

    def test_oldest_points_are_dropped(self):
        ring = Ring(window_seconds=10, live_seconds=2)  # 5 points
        for i in range(8):
            ring.push("cpu", float(i), ts=float(i))
        self.assertEqual([v for _, v in ring.series("cpu")], [3, 4, 5, 6, 7])

    def test_rejects_zero_resolution(self):
        with self.assertRaises(ValueError):
            Ring(window_seconds=3600, live_seconds=0)


class TestSeries(unittest.TestCase):
    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(Ring().series("nonexistent"), [])

    def test_keys_lists_what_was_pushed(self):
        ring = Ring()
        ring.push("cpu", 1.0, ts=0)
        ring.push("mem", 2.0, ts=0)
        self.assertEqual(sorted(ring.keys()), ["cpu", "mem"])


class TestAggregate(unittest.TestCase):
    def test_returns_avg_min_max(self):
        ring = Ring()
        for i, value in enumerate([10.0, 20.0, 30.0]):
            ring.push("cpu", value, ts=float(i))
        self.assertEqual(ring.aggregate("cpu", since=0.0), (20.0, 10.0, 30.0))

    def test_only_considers_points_since(self):
        ring = Ring()
        for i, value in enumerate([10.0, 20.0, 30.0]):
            ring.push("cpu", value, ts=float(i))
        self.assertEqual(ring.aggregate("cpu", since=1.0), (25.0, 20.0, 30.0))

    def test_empty_window_returns_none(self):
        # None, not zero: a zero would read as a measurement.
        ring = Ring()
        ring.push("cpu", 10.0, ts=0.0)
        self.assertIsNone(ring.aggregate("cpu", since=100.0))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(Ring().aggregate("nonexistent", since=0.0))


class TestMemoryFootprint(unittest.TestCase):
    def test_stays_bounded_under_sustained_push(self):
        ring = Ring(window_seconds=3600, live_seconds=2)
        for i in range(50_000):
            ring.push("cpu", float(i), ts=float(i))
        self.assertEqual(len(ring.series("cpu")), 1800)


if __name__ == "__main__":
    unittest.main()
