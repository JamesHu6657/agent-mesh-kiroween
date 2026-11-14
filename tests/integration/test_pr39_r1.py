"""PR #39 Linear review round 1: C1 daemon run --dry-run, C2 abbreviated flags, C3 onboard
replay without a token file and long keys, C4 explicit empty options, C5 JSON string values."""

import json
import os
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm


class TestPr39R1(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def _input(self, obj):
        path = os.path.join(self.harness.fixtures_dir, "in-%d.json" % len(os.listdir(self.harness.fixtures_dir)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_c1_daemon_run_dry_run(self):
        rc, resp = self.harness.cli("daemon", "run", "--dry-run", timeout=20.0)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)

    def test_c2_abbreviated_flag(self):
        rc, resp = self.harness.cli("task", "create", "--proj", self.prj, "--title", "C2", "--spec", "W",
                                    "--assignee", "ibx", "--input", self._input({"project": "prj_other"}),
                                    token=self.tok_lead1)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.DUPLICATE_PARAM), resp)

    def test_c3_replay_without_token_file_fails(self):
        out = os.path.join(self.harness.fixtures_dir, "c3r.tok")
        args = ("--idempotency-key", "c3r-key", "agent", "onboard", "c3r", "--adapter", "none", "--out", out)
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)
        os.unlink(out)
        rc, resp = self.harness.cli(*args, user=True)
        self.assertNotEqual(rc, 0, resp)
        self.assertIn("revoke", resp["error"]["message"])

    def test_c3_long_key(self):
        out = os.path.join(self.harness.fixtures_dir, "c3l.tok")
        rc, resp = self.harness.cli("--idempotency-key", "k" * 128, "agent", "onboard", "c3l", "--adapter", "none",
                                    "--out", out, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(os.path.getsize(out) > 0)

    def test_c4_empty_options_refused(self):
        rc, resp = self.harness.cli("agent", "update", "ibx", "--input", self._input({"options": {}}), user=True)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)

    def test_c5_json_string_kept(self):
        out = os.path.join(self.harness.fixtures_dir, "null.tok")
        rc, resp = self.harness.cli("agent", "onboard", "null", "--adapter", "none", "--out", out, user=True)
        if rc != 0:
            self.skipTest(f"agent name 'null' not accepted: {resp}")
        rc, resp = self.harness.cli("config", "set", "--input",
                                    self._input({"key": "lead.default_agent", "value": "null"}), user=True)
        # The string "null" names the agent; it must not be decoded again into JSON null
        # (which would clear the setting). The agent cannot lead, so the setting is refused
        # for that agent, by name.
        self.assertNotEqual(rc, 0, resp)
        self.assertEqual(resp["error"]["details"]["got"], "null", resp)
        rc2, got = self.harness.cli("config", "get", "lead.default_agent", user=True)
        self.assertEqual(got["data"]["value"], "lead1", got)


if __name__ == "__main__":
    unittest.main()
