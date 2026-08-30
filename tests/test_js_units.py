"""Run the front-end unit tests through node's built-in runner.

The console needs no JavaScript runtime -- it ships HTML, CSS and ES modules
the browser executes. node is a *test-time* convenience only, so this module
skips rather than fails when it is absent, and no package.json is created:
node --test needs neither.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS = ROOT / "web" / "js" / "tests"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed; front-end unit tests skipped")
class TestJavaScriptUnits(unittest.TestCase):
    def test_node_test_suite_passes(self):
        result = subprocess.run(
            [NODE, "--test", str(JS_TESTS / "*.test.js")],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(
            result.returncode, 0,
            f"node --test failed:\n{result.stdout}\n{result.stderr}")
        # returncode 0 alone proves nothing ran: a glob matching no files
        # (e.g. after web/js/tests/ got renamed) also exits 0, and the
        # whole front-end unit suite would vanish from CI silently. Node's
        # own summary line ("ℹ tests N" with the default reporter, "# tests
        # N" under --test-reporter=tap) is parsed and required to be
        # positive.
        match = re.search(r"tests (\d+)", result.stdout)
        self.assertIsNotNone(
            match, f"could not find a test count in node's output:\n{result.stdout}")
        self.assertGreater(
            int(match.group(1)), 0,
            "node --test reported zero tests -- the suite did not run")
