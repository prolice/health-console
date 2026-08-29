import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from healthconsole.cli import human_bytes, main


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


if __name__ == "__main__":
    unittest.main()
