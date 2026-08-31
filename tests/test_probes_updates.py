import unittest
from unittest import mock

from healthconsole.probes import EvalContext
from healthconsole.probes import updates


class TestUpdatesProbe(unittest.TestCase):
    def test_collect_uses_apt_check_when_present(self):
        with mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="3;1", stderr="", returncode=0)
            sample = updates.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertEqual(sample["pending_count"], 3)
        self.assertEqual(sample["security_count"], 1)

    def test_pending_security_update_gets_attention(self):
        sample = {"status": "ok", "pending_count": 3, "security_count": 1}
        findings = updates.evaluate(sample, EvalContext())
        self.assertEqual(findings[0].id, "updates.security_pending")
        self.assertEqual(findings[0].action, "apt.security")

    def test_regular_updates_get_info(self):
        sample = {"status": "ok", "pending_count": 3, "security_count": 0}
        findings = updates.evaluate(sample, EvalContext())
        self.assertEqual(findings[0].id, "updates.pending")
        self.assertEqual(findings[0].action, "apt.upgrade")


if __name__ == "__main__":
    unittest.main()
