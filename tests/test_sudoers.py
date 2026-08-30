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

`argv` is validated with an allowlist, not a denylist: a first round of
this module tried to enumerate "characters sudoers treats specially" and
missed `#` (opens a comment mid-token, proved against this machine's real
visudo with a discriminator: trailing garbage after a bare command is
`rc=1 syntax error`, the same garbage after `#` is `rc=0 parsed OK`), an
empty element, and a Unicode zero-width space that `\\s` does not match.
Both an empty argument and a `#`-truncated one degenerate the rule to a
bare command name, which sudoers(5) documents as letting the user run it
"with any arguments they wish".
"""

import io
import os
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
        self.assertIn("prolice ALL=(root) NOPASSWD:", render("prolice"))

    def test_an_uppercase_login_name_is_still_accepted(self):
        # Uppercase letters are valid in a POSIX username; the guard must
        # not hard-fail the command for an account spelled that way. Only
        # the literal 'ALL' -- not case in general -- means anything
        # special to sudoers.
        self.assertIn("Prolice ALL=(root) NOPASSWD:", render("Prolice"))


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

    def test_a_dot_dot_path_segment_is_rejected(self):
        # argv[0].startswith("/") alone is not enough: this is an absolute
        # path that does not name the file it appears to.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/../bin/sh",))

    def test_a_trailing_hash_truncating_the_rule_is_rejected(self):
        # '#' opens a comment in sudo-rs 0.2.13 -- confirmed against this
        # machine's real visudo -- so this reduces the rule to the bare
        # command "/usr/bin/apt-get", which sudoers(5) documents as
        # letting the user run it with any arguments they wish.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get#", "update"))

    def test_a_hash_inside_an_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", "update#anything"))

    def test_an_empty_argument_is_rejected(self):
        # An empty trailing element degenerates the rule the same way a
        # '#' does: the command still matches, its arguments no longer do.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", ""))

    def test_a_colon_in_an_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", "a:b"))

    def test_a_zero_width_space_is_rejected(self):
        # Invisible in a terminal or an editor, and not matched by \\s.
        with self.assertRaises(ValueError):
            self._render_with(("/usr/bin/apt-get", "​update"))

    def test_an_ordinary_argv_is_still_accepted(self):
        text = self._render_with(("/usr/bin/apt-get", "update"))
        self.assertIn("prolice ALL=(root) NOPASSWD: /usr/bin/apt-get update",
                       text)


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
        # A wrong implementation such as
        # `os.environ.get("LOGNAME") or pwd.getpwuid(...)` would pass a
        # test that only mocks getpass.getuser() and never sets a hostile
        # environment -- this one plants the payload in $LOGNAME/$USER
        # themselves and checks it never reaches the output.
        hostile = "prolice ALL=(ALL) NOPASSWD: ALL #"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch.dict(
                    os.environ, {"LOGNAME": hostile, "USER": hostile,
                                 "SUDO_USER": hostile}), \
                 mock.patch("healthconsole.cli.SUDOERS_DEST", dest):
                code, output = self.run_cli("sudoers")
        self.assertNotIn(hostile, output)
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

    @unittest.skipUnless(VISUDO, "visudo is not installed")
    def test_the_written_file_is_not_group_or_world_writable(self):
        # shutil.copyfile() copies bytes, not mode: without an explicit
        # chmod, the destination lands however the process umask says --
        # not the 0600 the temporary file was created with -- and it is
        # exactly the file the printed 'sudo install' line reads from.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            old_umask = os.umask(0o002)
            try:
                with mock.patch("healthconsole.cli.SUDOERS_DEST", dest):
                    code, _ = self.run_cli("sudoers")
            finally:
                os.umask(old_umask)
            self.assertEqual(code, 0)
            mode = dest.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

    @unittest.skipUnless(VISUDO, "visudo is not installed")
    def test_the_temporary_file_does_not_survive_a_successful_run(self):
        with tempfile.TemporaryDirectory() as scratch:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
                with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                     mock.patch("healthconsole.cli.tempfile.gettempdir",
                                return_value=scratch):
                    code, _ = self.run_cli("sudoers")
                self.assertEqual(code, 0)
            self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_the_temporary_file_does_not_survive_a_rejected_run(self):
        with tempfile.TemporaryDirectory() as scratch:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
                fake_result = subprocess.CompletedProcess(
                    args=["visudo"], returncode=1, stdout="",
                    stderr="syntax error")
                with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                     mock.patch("healthconsole.cli.tempfile.gettempdir",
                                return_value=scratch), \
                     mock.patch("healthconsole.cli.shutil.which",
                                return_value="/usr/bin/visudo"), \
                     mock.patch("healthconsole.cli.subprocess.run",
                                return_value=fake_result):
                    code, _ = self.run_cli("sudoers")
                self.assertNotEqual(code, 0)
            self.assertEqual(list(Path(scratch).iterdir()), [])

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

    def test_it_refuses_to_run_as_root_rather_than_naming_root_in_the_rule(self):
        # os.getuid() is 0 under `sudo health-console sudoers`. Rendering
        # anyway would silently produce a useless NOPASSWD grant to an
        # account that never needs one, under a header that says the
        # named account is a login account -- and recovering the real
        # invoking user from $SUDO_UID would put the environment straight
        # back in the loop this command exists to close.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.os.getuid", return_value=0):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)
            self.assertNotIn("root ALL=(root)", output)
            self.assertFalse(dest.exists())

    def test_it_refuses_cleanly_when_the_uid_has_no_passwd_entry(self):
        # A container started with --user <uid>, or an LDAP/NIS outage:
        # pwd.getpwuid() raises KeyError. Every other refusal path in this
        # command prints a clean message instead of a bare traceback.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "packaging" / "sudoers.d" / "health-console"
            with mock.patch("healthconsole.cli.SUDOERS_DEST", dest), \
                 mock.patch("healthconsole.cli.os.getuid",
                            return_value=999999), \
                 mock.patch("healthconsole.cli.pwd.getpwuid",
                            side_effect=KeyError(999999)):
                code, output = self.run_cli("sudoers")
            self.assertNotEqual(code, 0)
            self.assertNotIn("sudo install", output)
            self.assertFalse(dest.exists())

    def test_rotate_is_rejected_rather_than_silently_ignored(self):
        code, output = self.run_cli("sudoers", "--rotate")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
