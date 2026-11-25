"""Linear full-codebase review of main 1f78a4f, part B (mesh/daemon), end to end.

B1 overlapping identical keyed `task start` requests all replay the one start.
B3 a cancel kills a setsid descendant whose environment was replaced (no MESH_RUN_ID).
B6 a revoked token's pending `inbox wait` ends with TOKEN_REVOKED, delivering nothing."""

import json
import os
import threading
import time
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm
from tests.integration.test_execution_termination import TerminationBase, _alive


class TestConcurrentKeyedStart(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def test_parallel_identical_starts_replay(self) -> None:
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "B1", "--spec", "W",
                                    "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        results = []
        lock = threading.Lock()

        def go() -> None:
            r = self.harness.cli("--idempotency-key", "b1-key", "task", "start", tsk, token=self.tok_ibx,
                                 extra_env={"MESH_SESSION": "ses_b1"}, timeout=120.0)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([rc for rc, _ in results], [0] * 8, results)
        self.assertEqual(len({r["data"]["run_id"] for _, r in results}), 1)


class TestRevokedWaitDeliversNothing(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def test_revoke_during_wait(self) -> None:
        rc, resp = self.harness.cli("agent", "token", "issue", "ibx", user=True)
        self.assertEqual(rc, 0, resp)
        tok = resp["data"]["token"]
        cred = resp["data"].get("credential_id") or resp["data"].get("id")
        self.harness.cli("inbox", "ack", "--all", token=tok)
        out = {}

        def wait() -> None:
            out["r"] = self.harness.cli("inbox", "wait", "--timeout", "20s", token=tok, timeout=40.0)

        th = threading.Thread(target=wait)
        th.start()
        time.sleep(1.5)
        rc, resp = self.harness.cli("agent", "token", "revoke", cred, user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("task", "create", "--project", self.prj, "--title", "Secret title",
                                    "--spec", "secret spec", "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        th.join(45)
        rc, resp = out["r"]
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
        self.assertEqual(resp["error"]["code"], c.TOKEN_REVOKED)
        self.assertNotIn("secret spec", json.dumps(resp))


class TestCancelKillsSanitizedDescendant(TerminationBase):
    def test_setsid_child_with_replaced_env_is_killed(self) -> None:
        pid_path = os.path.join(self.harness.fixtures_dir, "b3.pid")
        spec = {"steps": [{"spawn_setsid_grandchild": {"env": {"PATH": "/usr/bin:/bin"}, "path": pid_path}},
                          {"sleep": {"seconds": 60}}]}
        task_id = self.create_task("FAKE " + json.dumps(spec))
        self.wait_running_run(task_id)
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(pid_path), timeout=15.0))
        with open(pid_path, "r", encoding="utf-8") as f:
            gpid = int(f.read().strip())
        self.assertTrue(_alive(gpid))
        self.cancel(task_id)
        self.wait_status(task_id, "cancelled", timeout=30.0)
        self.assertTrue(self.harness.wait_for(lambda: not _alive(gpid), timeout=10.0),
                        "the sanitized descendant survived the cancel")


if __name__ == "__main__":
    unittest.main()
