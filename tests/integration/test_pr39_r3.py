"""PR #39 Linear review round 3: C3 replay with a corrupted secret; B5 intake.submit runs
under the request-wide git budget."""

import os
import unittest
from unittest import mock

from mesh.core import repos
from mesh.daemon.handlers import project
from tests.integration import test_merge as tm


class TestPr39R3(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def test_c3_corrupted_secret(self):
        b = os.path.join(self.harness.fixtures_dir, "r3.tok")
        args = ("--idempotency-key", "r3", "agent", "onboard", "wr3", "--adapter", "none", "--out", b, "--force")
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)  # genuine file: replay succeeds
        with open(b, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        with open(b, "w", encoding="utf-8") as f:
            f.write(tok[:-5] + "AAAAA\n")
        rc, resp = self.harness.cli(*args, user=True)
        self.assertNotEqual(rc, 0, resp)
        self.assertIn("revoke", resp["error"]["message"])


class TestIntakeBudget(unittest.TestCase):
    def test_b5_intake_runs_under_budget(self):
        seen = {}

        def inner(ctx):
            seen["d"] = getattr(repos._budget, "deadline", None)
            return "ok"

        with mock.patch.object(project, "_handle_intake_submit", inner):
            self.assertEqual(project.handle_intake_submit(object()), "ok")
        self.assertIsNotNone(seen["d"])


if __name__ == "__main__":
    unittest.main()
