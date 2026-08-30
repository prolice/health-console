"""Run the front-end unit tests through node's built-in runner.

The console needs no JavaScript runtime -- it ships HTML, CSS and ES modules
the browser executes. node is a *test-time* convenience only, so this module
skips rather than fails when it is absent, and no package.json is created:
node --test needs neither.
"""

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
