import unittest
from unittest import mock

from healthconsole.probes import EvalContext
from healthconsole.probes import storage


class Usage:
    total = 100
    used = 85
    free = 15


class TestStorageProbe(unittest.TestCase):
    def test_collect_reports_root_usage(self):
        with mock.patch("shutil.disk_usage",
                        return_value=Usage), \
             mock.patch("healthconsole.probes.storage._tree_size",
                        return_value=1234):
            sample = storage.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertEqual(sample["used_pct"], 85.0)
        self.assertEqual(sample["apt_cache_bytes"], 1234)

    def test_metrics_are_plausibility_checked(self):
        sample = {"status": "ok", "used_pct": 85.0, "free": 15,
                  "apt_cache_bytes": 1234}
        self.assertEqual(storage.metrics(sample)["disk.root.used_pct"], 85.0)

    def test_full_root_emits_a_cleaning_action(self):
        sample = {"status": "ok", "root": "/", "used_pct": 85.0,
                  "free": 15, "apt_cache_bytes": 1234}
        findings = storage.evaluate(sample, EvalContext())
        self.assertEqual(findings[0].id, "storage.root_full")
        self.assertEqual(findings[0].action, "clean.aptcache")


if __name__ == "__main__":
    unittest.main()
