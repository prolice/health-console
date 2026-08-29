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


class TestServerConfig(unittest.TestCase):
    def test_allow_remote_actions_string_false_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nallow_remote_actions = "false"\n'))
        self.assertIn("allow_remote_actions", str(ctx.exception))

    def test_allow_remote_actions_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nallow_remote_actions = 1\n'))
        self.assertIn("allow_remote_actions", str(ctx.exception))

    def test_allow_remote_actions_boolean_true_loads(self):
        cfg = load_config(write_toml('[server]\nallow_remote_actions = true\n'))
        self.assertTrue(cfg.allow_remote_actions)

    def test_token_as_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\ntoken = 12345\n'))
        self.assertIn("token", str(ctx.exception))

    def test_bind_as_integer_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(write_toml('[server]\nbind = 42\n'))
        self.assertIn("bind", str(ctx.exception))

    def test_valid_server_section_loads(self):
        cfg = load_config(write_toml(
            '[server]\nbind = "127.0.0.1"\nport = 9000\n'
            'allow_remote_actions = false\ntoken = "secret"\n'))
        self.assertEqual(cfg.bind, "127.0.0.1")
        self.assertEqual(cfg.port, 9000)
        self.assertFalse(cfg.allow_remote_actions)
        self.assertEqual(cfg.token, "secret")


class TestEstimate(unittest.TestCase):
    def test_default_config_estimates_about_54_MB(self):
        # BYTES_PER_METRIC_ROW=68 (measured; see config.py) puts the default
        # projection at ~54 MB, not the ~32 MB an earlier, unmeasured
        # 40-byte guess produced.
        size = estimate_db_bytes(Config(), n_metrics=25)
        self.assertGreater(size, 45_000_000)
        self.assertLess(size, 65_000_000)

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
