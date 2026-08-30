"""The rendered sudoers rule, and the CLI command that renders it.

This machine runs sudo-rs, not classic sudo: `visudo` and `sudo` are both
symlinked through /etc/alternatives to the Rust reimplementation, and it
accepts a stricter subset of the sudoers grammar. `render()` is validated
against whatever `visudo` is actually on PATH -- never a hard-coded
/usr/sbin/visudo and never an assumption about which implementation answers
-- and the test skips cleanly, the way tests/test_js_units.py skips when
node is absent, when no visudo is installed at all.
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from healthconsole.actions import CATALOGUE
from healthconsole.cli import main
from healthconsole.sudoers import render

VISUDO = shutil.which("visudo")


class TestSudoersRule(unittest.TestCase):
    def test_it_names_only_binaries_the_catalogue_uses(self):
        text = render("prolice")
        for action in CATALOGUE.values():
            if action.root:
                self.assertIn(" ".join(action.argv), text)

    def test_it_contains_no_wildcard_and_never_all(self):
        text = render("prolice")
        self.assertNotIn("*", text)
        self.assertNotIn("ALL=(ALL", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_it_carries_the_transitional_warning(self):
        # Whoever reads /etc/sudoers.d/health-console in two years must
        # find out from the file itself that naming a login account was a
        # stepping stone, not the design.
        self.assertIn("system user", render("prolice"))

    @unittest.skipUnless(VISUDO, "visudo is not installed")
    def test_visudo_accepts_it(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers") as handle:
            handle.write(render("prolice"))
            handle.flush()
            result = subprocess.run([VISUDO, "-c", "-f", handle.name],
                                     capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                              result.stdout + result.stderr)

    def test_the_user_is_substituted_not_left_as_a_placeholder(self):
        self.assertIn("prolice", render("prolice"))
        self.assertNotIn("<user>", render("prolice"))


class TestCmdSudoers(unittest.TestCase):
    """The CLI command: it renders, validates and prints -- it never writes
    to /etc, and there is no flag that makes it."""

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_it_never_offers_a_flag_that_installs(self):
        _, help_output = self.run_cli("--help")
        self.assertNotIn("--install", help_output)

    @unittest.skipUnless(VISUDO, "visudo is not installed")
    def test_it_creates_the_packaging_directory_if_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging_dir = Path(tmp) / "packaging"
            dest = packaging_dir / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest):
                self.assertFalse(packaging_dir.exists())
                code, output = self.run_cli("sudoers")
                self.assertEqual(code, 0)
                self.assertTrue(dest.exists())
                self.assertIn(str(dest), output)

    @unittest.skipUnless(VISUDO, "visudo is not installed")
    def test_it_prints_the_content_destination_and_install_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest):
                code, output = self.run_cli("sudoers")
            self.assertEqual(code, 0)
            self.assertIn("NOPASSWD:", output)
            self.assertIn(str(dest), output)
            self.assertIn("sudo install -m 0440 -o root -g root", output)
            self.assertIn("/etc/sudoers.d/health-console", output)

    def test_it_refuses_to_print_the_install_line_if_visudo_check_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            fake_result = subprocess.CompletedProcess(
                args=["visudo"], returncode=1, stdout="", stderr="syntax error")
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.shutil.which",
                            return_value="/usr/bin/visudo"), \
                 mock.patch("healthconsole.cli.subprocess.run",
                            return_value=fake_result):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)

    def test_it_refuses_when_visudo_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.shutil.which",
                            return_value=None):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)


if __name__ == "__main__":
    unittest.main()
