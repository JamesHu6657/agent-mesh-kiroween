"""Linear full-codebase review of main 1f78a4f, part C (mesh/cli), end to end.

C1 --dry-run never writes: init, doctor --fix-perms, daemon start refuse it.
C2 JSON --input cannot silently replace a positional target given on the command line.
C3 a retried onboard with the same idempotency key issues no extra token.
C4 agent update refuses fields it cannot change instead of ignoring them.
C5 config set --input with a native JSON value works."""

import json
import os
import subprocess
import tempfile
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm


class TestLinearFullCli(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def _input(self, obj) -> str:
        path = os.path.join(self.harness.fixtures_dir, "in-%d.json" % len(os.listdir(self.harness.fixtures_dir)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_c1_dry_run_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "h")
            cp = subprocess.run([self.harness.bin_mesh, "--home", home, "init", "--dry-run"],
                                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                env={**os.environ, "MESH_TESTING": "1"})
            self.assertEqual(cp.returncode, c.EXIT_USAGE, cp.stdout + cp.stderr)
            self.assertFalse(os.path.exists(os.path.join(home, "mesh.db")))
        rc, resp = self.harness.cli("doctor", "--fix-perms", "--dry-run", user=True)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)
        rc, resp = self.harness.cli("daemon", "start", "--dry-run")
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)

    def test_c2_json_cannot_override_positional(self) -> None:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "C2", "--spec", "W",
                                    "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("task", "show", resp["data"]["id"], "--input",
                                    self._input({"task_id": "tsk_other"}), token=self.tok_lead1)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.DUPLICATE_PARAM), resp)

    def test_c3_onboard_retry_issues_no_extra_token(self) -> None:
        out = os.path.join(self.harness.fixtures_dir, "c3.tok")
        args = ("--idempotency-key", "c3-key", "agent", "onboard", "c3w", "--adapter", "none", "--out", out)
        rc, resp = self.harness.cli(*args, user=True)
        self.assertEqual(rc, 0, resp)
        with open(out, "r", encoding="utf-8") as f:
            first = f.read()
        rc, resp = self.harness.cli(*args, "--force", user=True)
        self.assertEqual(rc, 0, resp)
        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), first)
        rc, resp = self.harness.cli("agent", "token", "list", "c3w", user=True)
        self.assertEqual(rc, 0, resp)
        items = resp["data"].get("items") or resp["data"].get("tokens") or resp["data"].get("credentials")
        active = [t for t in items if not t.get("revoked_at")]
        self.assertEqual(len(active), 1, items)

    def test_c4_update_refuses_fixed_fields(self) -> None:
        rc, resp = self.harness.cli("agent", "update", "ibx", "--binary", "/bin/true", user=True)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)
        rc, resp = self.harness.cli("agent", "update", "ibx", "--delivery", "launch", user=True)
        self.assertEqual((rc, resp["error"]["code"]), (c.EXIT_USAGE, c.INVALID_ARGUMENT), resp)

    def test_c5_config_set_native_json(self) -> None:
        rc, resp = self.harness.cli("config", "set", "--input",
                                    self._input({"key": "scheduler.max_total_runs", "value": 7}), user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("config", "get", "scheduler.max_total_runs", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertIn(7, (resp["data"].get("value"), resp["data"].get("effective")), resp)


if __name__ == "__main__":
    unittest.main()
