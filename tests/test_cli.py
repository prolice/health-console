import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from healthconsole.cli import SECONDS_PER_DAY, _tick_once, human_bytes, main


class TestHumanBytes(unittest.TestCase):
    def test_scales_to_kilobytes(self):
        self.assertEqual(human_bytes(1536), "1.5 kB")

    def test_bytes_stay_bytes(self):
        self.assertEqual(human_bytes(512), "512 B")


class TestCommands(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_no_command_shows_usage(self):
        code, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", output.lower())

    def test_config_prints_effective_settings(self):
        code, output = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("raw_days", output)
        self.assertIn("aggregate_days", output)

    def test_config_announces_projected_size(self):
        # The cost must be announced, not suffered.
        _, output = self.run_cli("config")
        self.assertIn("Projected size", output)

    def test_unknown_command_is_refused(self):
        code, _ = self.run_cli("teleport")
        self.assertEqual(code, 2)

    def test_config_announces_the_file_it_actually_read(self):
        # The command must never claim to have read the default path when
        # --config pointed it somewhere else.
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".toml", delete=False) as handle:
            handle.write('[server]\nport = 9999\n')
            path = handle.name
        try:
            code, output = self.run_cli("--config", path, "config")
        finally:
            os.unlink(path)
        self.assertEqual(code, 0)
        self.assertIn(path, output)
        self.assertIn("9999", output)

    def test_help_exits_zero(self):
        code, output = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage", output.lower())


class TestToken(unittest.TestCase):
    """generate_token() previously had no way to reach a real config: there
    was no `token` command at all, and `bind` defaulting to 0.0.0.0 with no
    way to obtain a token meant a LAN client had no way in.
    """

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def temp_config_path(self):
        return os.path.join(tempfile.mkdtemp(), "config.toml")

    def test_no_token_configured_says_so_and_how_to_create_one(self):
        code, output = self.run_cli(
            "--config", self.temp_config_path(), "token")
        self.assertEqual(code, 0)
        self.assertIn("no token", output.lower())
        self.assertIn("--rotate", output)

    def test_configured_token_is_printed(self):
        path = self.temp_config_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[server]\ntoken = "s3cr3t"\n')
        code, output = self.run_cli("--config", path, "token")
        self.assertEqual(code, 0)
        self.assertIn("s3cr3t", output)

    # --rotate is tested only against a temporary path, never the user's
    # real ~/.config/health-console/config.toml.

    def test_rotate_creates_the_file_and_prints_the_token(self):
        path = self.temp_config_path()
        self.assertFalse(os.path.exists(path))
        code, output = self.run_cli(
            "--config", path, "token", "--rotate")
        self.assertEqual(code, 0)
        printed = output.strip()
        self.assertGreaterEqual(len(printed), 43)
        self.assertTrue(os.path.exists(path))
        # A subsequent plain `token` read must see exactly what was stored.
        _, second_output = self.run_cli("--config", path, "token")
        self.assertIn(printed, second_output)

    def test_rotate_sets_restrictive_file_permissions(self):
        path = self.temp_config_path()
        self.run_cli("--config", path, "token", "--rotate")
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_rotate_preserves_other_settings_already_in_the_file(self):
        path = self.temp_config_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '[server]\nbind = "127.0.0.1"\nport = 9001\n'
                '[retention]\nraw_days = 5\n')
        code, output = self.run_cli("--config", path, "token", "--rotate")
        self.assertEqual(code, 0)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('bind = "127.0.0.1"', text)
        self.assertIn("port = 9001", text)
        self.assertIn("raw_days = 5", text)
        self.assertIn(output.strip(), text)

    def test_rotate_replaces_an_existing_token_rather_than_duplicating_it(self):
        path = self.temp_config_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[server]\ntoken = "old-token"\n')
        self.run_cli("--config", path, "token", "--rotate")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("old-token", text)
        self.assertEqual(text.count("token ="), 1)


class _FakeScheduler:
    """A scheduler stand-in that records calls and can be told to raise.

    Used only to exercise `_tick_once`'s error handling without starting a
    real server or touching a real database.
    """

    def __init__(self, raise_on=()):
        self.raise_on = set(raise_on)
        self.calls = []

    def tick(self, now):
        self.calls.append(("tick", now))
        if "tick" in self.raise_on:
            raise RuntimeError("tick failed")

    def flush(self, now):
        self.calls.append(("flush", now))
        if "flush" in self.raise_on:
            raise RuntimeError("flush failed")

    def maintain(self, now):
        self.calls.append(("maintain", now))
        if "maintain" in self.raise_on:
            raise RuntimeError("maintain failed")


def _fake_cfg(store_seconds=30):
    return SimpleNamespace(sampling=SimpleNamespace(store_seconds=store_seconds))


class TestTickOnce(unittest.TestCase):
    """`_tick_once` is the per-iteration body of `cmd_run`'s background
    loop, extracted so the collection loop's resilience to a raising probe,
    store or scheduler call can be tested without starting a real server.
    """

    def test_a_raising_tick_does_not_propagate(self):
        scheduler = _FakeScheduler(raise_on={"tick"})
        err = io.StringIO()
        with redirect_stderr(err):
            _tick_once(scheduler, _fake_cfg(), last_flush=0,
                       last_maintain=0, now=100)
        self.assertIn("RuntimeError", err.getvalue())

    def test_a_raising_flush_does_not_propagate(self):
        # A disk-full error surfacing from write_metrics is the realistic
        # case that motivated this: it must not kill the collection loop.
        scheduler = _FakeScheduler(raise_on={"flush"})
        err = io.StringIO()
        with redirect_stderr(err):
            _tick_once(scheduler, _fake_cfg(store_seconds=30), last_flush=0,
                       last_maintain=0, now=100)
        self.assertIn("RuntimeError", err.getvalue())

    def test_the_normal_path_advances_the_bookkeeping(self):
        scheduler = _FakeScheduler()
        last_flush, _ = _tick_once(
            scheduler, _fake_cfg(store_seconds=30),
            last_flush=0, last_maintain=0, now=100)
        self.assertIn(("flush", 100), scheduler.calls)
        self.assertEqual(last_flush, 100)

        scheduler = _FakeScheduler()
        _, last_maintain = _tick_once(
            scheduler, _fake_cfg(store_seconds=30),
            last_flush=0, last_maintain=0, now=SECONDS_PER_DAY + 1)
        self.assertIn(("maintain", SECONDS_PER_DAY + 1), scheduler.calls)
        self.assertEqual(last_maintain, SECONDS_PER_DAY + 1)

    def test_a_raising_tick_leaves_bookkeeping_unchanged(self):
        # A failure must not silently skip a flush window: if tick() blew
        # up, last_flush/last_maintain should come back exactly as given.
        scheduler = _FakeScheduler(raise_on={"tick"})
        err = io.StringIO()
        with redirect_stderr(err):
            last_flush, last_maintain = _tick_once(
                scheduler, _fake_cfg(store_seconds=30),
                last_flush=0, last_maintain=0, now=100)
        self.assertEqual(last_flush, 0)
        self.assertEqual(last_maintain, 0)
        self.assertNotIn(("flush", 100), scheduler.calls)


if __name__ == "__main__":
    unittest.main()
