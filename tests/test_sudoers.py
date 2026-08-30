"""The rendered sudoers rule, and the CLI command that renders it.

This machine runs sudo-rs, not classic sudo: `visudo` and `sudo` are both
symlinked through /etc/alternatives to the Rust reimplementation, and it
accepts a stricter subset of the sudoers grammar. `render()` is validated
against whatever `visudo` is actually on PATH -- never a hard-coded
/usr/sbin/visudo and never an assumption about which implementation answers
-- and the test skips cleanly, the way tests/test_js_units.py skips when
node is absent, when no visudo is installed at all.

`render()`'s two interpolated inputs -- the username and each root action's
`argv` -- are both attacker-reachable in a way that is easy to miss because
neither looks like network input: a username can come straight from
$LOGNAME/$USER (set by anything that spawns a shell -- a poisoned profile
script, a systemd unit, `su` without `-`, a CI runner), and `argv` is
whatever a future edit to actions.py puts there. TestRenderRejects* proves
each crafted case is refused with `ValueError` rather than silently
rendered into a rule that grants more than the comment above it claims.
"""

import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from healthconsole.actions import CATALOGUE, Action, Risk
from healthconsole.cli import main
from healthconsole.sudoers import render

VISUDO = shutil.which("visudo")


def _root_action(argv):
    return Action(id="test.action", argv=argv, root=True, risk=Risk.SAFE)


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


class TestRenderRejectsUnsafeUsers(unittest.TestCase):
    """`render()` must not trust its `user` argument just because it is
    usually a plain login name. `getpass.getuser()` (not used by
    cmd_sudoers -- see TestCmdSudoers -- but exactly the kind of source a
    future caller might reach for) returns $LOGNAME / $USER verbatim, and
    each of these values `parsed OK` under this machine's real sudo-rs
    visudo when interpolated without a guard."""

    def test_a_trailing_comment_that_swallows_the_real_rule_is_rejected(self):
        # sudo-rs parsed this cleanly and reported exit 0: the comment
        # character eats the rest of the line, so the intended rule
        # vanishes and the injected "ALL=(ALL) NOPASSWD: ALL" is the
        # entire file.
        with self.assertRaises(ValueError):
            render("prolice ALL=(ALL) NOPASSWD: ALL #")

    def test_the_literal_all_is_rejected(self):
        # No '#', no 'NOPASSWD: ALL', nothing today's shape-only checks
        # would catch: "ALL ALL=(root) NOPASSWD: ..." grants every
        # account on the machine and reads, at a glance, like a normal
        # username.
        with self.assertRaises(ValueError):
            render("ALL")

    def test_a_group_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            render("%sudo")

    def test_a_user_list_is_rejected(self):
        with self.assertRaises(ValueError):
            render("prolice,root")

    def test_an_embedded_newline_is_rejected(self):
        with self.assertRaises(ValueError):
            render("prolice\nroot ALL=(ALL) NOPASSWD: ALL")

    def test_a_plain_login_name_is_still_accepted(self):
        # The guard must reject unsafe input without also rejecting the
        # ordinary case it exists to let through.
        render("prolice")


class TestRenderRejectsUnsafeArgv(unittest.TestCase):
    """`CATALOGUE` is trusted, auditable data today -- one frozen entry --
    but `render()` renders whatever it is handed, and the sudoers grammar
    treats several ordinary-looking characters as syntax. Each of these
    `argv` values `parsed OK` under sudo-rs when joined with spaces and
    emitted unescaped."""

    def _render_with(self, argv):
        with mock.patch.dict(CATALOGUE, {"test.action": _root_action(argv)},
                              clear=True):
            return render("prolice")

    def test_a_comma_that_grants_a_second_command_is_rejected(self):
        # The comma is the sudoers command-list separator: this silently
        # grants /bin/sh in addition to apt-get.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get,/bin/sh",))

    def test_a_wildcard_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", "install", "*"))

    def test_an_embedded_newline_smuggling_a_second_rule_is_rejected(self):
        with self.assertRaises(ValueError):
            self._render_with((
                "/usr/bin/apt-get",
                "update\nprolice ALL=(ALL) NOPASSWD: ALL",
            ))

    def test_an_argument_containing_whitespace_is_rejected(self):
        # sudoers splits commands on whitespace: emitted verbatim, this
        # produces a rule that silently stops matching what the runner
        # actually runs.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", "install", "my package"))

    def test_a_relative_command_path_is_rejected(self):
        with self.assertRaises(ValueError):
            self._render_with(("apt-get", "update"))

    def test_an_ordinary_argv_is_still_accepted(self):
        self._render_with(("/usr/bin/apt-get", "update"))


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

    def test_it_does_not_resolve_the_user_from_the_environment(self):
        # getpass.getuser() reads $LOGNAME / $USER before ever consulting
        # the password database -- exactly the string an attacker with
        # non-root code execution as this user controls. The account name
        # must come from the password database instead.
        import getpass as getpass_module
        with mock.patch.object(
                getpass_module, "getuser",
                return_value="prolice ALL=(ALL) NOPASSWD: ALL #"):
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
                with mock.patch("healthconsole.cli.SUDOERS_DEST", dest):
                    code, output = self.run_cli("sudoers")
        self.assertNotIn("ALL=(ALL) NOPASSWD: ALL #", output)

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
            # A rejected render must never be left at the path a previous
            # successful run may have printed into an operator's runbook.
            self.assertFalse(dest.exists())

    def test_it_refuses_when_visudo_is_not_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.shutil.which",
                            return_value=None):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)
            self.assertFalse(dest.exists())

    def test_it_refuses_an_unsafe_username_without_writing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            fake_pw = mock.Mock(pw_name="ALL")
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.pwd.getpwuid",
                            return_value=fake_pw):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)
            self.assertFalse(dest.exists())

    def test_rotate_is_rejected_rather_than_silently_ignored(self):
        code, output = self.run_cli("sudoers", "--rotate")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
