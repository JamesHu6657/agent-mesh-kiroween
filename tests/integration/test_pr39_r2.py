"""PR #39 Linear review round 2: C3 an onboard replay whose token file holds another
credential's token is not reported as success."""

import os
import unittest

from tests.integration import test_merge as tm


class TestOnboardReplayStaleFile(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def test_c3_stale_token_file(self):
        a = os.path.join(self.harness.fixtures_dir, "a.tok")
        b = os.path.join(self.harness.fixtures_dir, "b.tok")
        rc, resp = self.harness.cli("agent", "onboard", "wka", "--adapter", "none", "--out", a, user=True)
        self.assertEqual(rc, 0, resp)
        args = ("--idempotency-key", "kb", "agent", "onboard", "wkb", "--adapter", "none", "--out", b, "--force")
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)
        with open(a, "r", encoding="utf-8") as f:
            stale = f.read()
        with open(b, "w", encoding="utf-8") as f:
            f.write(stale)  # as if B's replace had failed and left A's token
        rc, resp = self.harness.cli(*args, user=True)
        self.assertNotEqual(rc, 0, resp)
        self.assertIn("revoke", resp["error"]["message"])
        # the genuine file still replays as success
        os.unlink(b)
        rc, resp = self.harness.cli("--idempotency-key", "kc", "agent", "onboard", "wkc", "--adapter", "none",
                                    "--out", b, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("--idempotency-key", "kc", "agent", "onboard", "wkc", "--adapter", "none",
                                    "--out", b, "--force", user=True)
        self.assertEqual(rc, 0, resp)


if __name__ == "__main__":
    unittest.main()
