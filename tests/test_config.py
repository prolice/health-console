import tempfile
import unittest
from pathlib import Path

from healthconsole.config import (
    Config, ConfigError, estimate_db_bytes, load_config,
)


def write_toml(text):
    path = Path(tempfile.mkdtemp()) / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults(unittest.TestCase):
    def test_absent_file_yields_defaults(self):
        cfg = load_config(Path("/nonexistent/config.toml"))
        self.assertEqual(cfg.retention.raw_days, 2)
        self.assertEqual(cfg.retention.aggregate_days, 90)
        self.assertEqual(cfg.sampling.live_seconds, 2)
        self.assertEqual(cfg.sampling.store_seconds, 30)
        self.assertEqual(cfg.port, 8787)
        self.assertFalse(cfg.allow_remote_actions)

    def test_partial_file_keeps_other_defaults(self):
        cfg = load_config(write_toml("[retention]\nraw_days = 5\n"))
        self.assertEqual(cfg.retention.raw_days, 5)
        self.assertEqual(cfg.retention.aggregate_days, 90)


class TestValidation(unittest.TestCase):
    def test_zero_days_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml("[retention]\nraw_days = 0\n"))
        self.assertIn("raw_days", str(ctx.exception))

    def test_raw_longer_than_aggregate_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml(
                "[retention]\nraw_days = 100\naggregate_days = 30\n"))
        self.assertIn("raw_days", str(ctx.exception))
        self.assertIn("aggregate_days", str(ctx.exception))

    def test_store_seconds_must_be_multiple_of_live(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml(
                "[sampling]\nlive_seconds = 2\nstore_seconds = 25\n"))
        self.assertIn("store_seconds", str(ctx.exception))

    def test_store_seconds_capped_at_300(self):
        with self.assertRaises(ConfigError):
            load_config(write_toml(
                "[sampling]\nlive_seconds = 2\nstore_seconds = 600\n"))

    def test_non_integer_days_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[retention]\nraw_days = "two"\n'))
        self.assertIn("raw_days", str(ctx.exception))

    def test_malformed_toml_names_the_file(self):
        path = write_toml("[retention\nraw_days = 2\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn(str(path), str(ctx.exception))


class TestEstimate(unittest.TestCase):
    def test_default_config_estimates_about_32_MB(self):
        size = estimate_db_bytes(Config(), n_metrics=25)
        self.assertGreater(size, 25_000_000)
        self.assertLess(size, 40_000_000)

    def test_estimate_grows_with_retention(self):
        from healthconsole.config import Retention
        small = estimate_db_bytes(Config(), n_metrics=25)
        big = estimate_db_bytes(
            Config(retention=Retention(raw_days=30, aggregate_days=365)),
            n_metrics=25)
        self.assertGreater(big, small * 5)

    def test_slower_store_rate_shrinks_the_database(self):
        from healthconsole.config import Sampling
        fast = estimate_db_bytes(Config(), n_metrics=25)
        slow = estimate_db_bytes(
            Config(sampling=Sampling(live_seconds=2, store_seconds=300)),
            n_metrics=25)
        self.assertLess(slow, fast)


if __name__ == "__main__":
    unittest.main()
