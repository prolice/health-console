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
