"""M3 gate re-review P2 (L11 kiro hold), end to end: a testing-mode meshd without
``MESH_TEST_LAUNCH_UNHOLD`` never starts a kiro launch task that reached ``queued`` past AG
step 4 (as one assigned before the hold landed would). The scheduler fails it with reason
``launch_held`` and records no run.

ACC: N19

Offline: the kiro agent's binary is a local stub that is never executed (the hold is decided
before any install check), and no launch evidence is staged.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from typing import List

from mesh.adapters import evidence
from mesh.core.clock import now_iso_utc
from tests.helpers import MeshHarness

MODEL = "claude-haiku-5.5"
STUB = """#!{python}
import sys
if sys.argv[1:] == ["--version"]:
    print("kiro-cli 3.1.0")
    sys.exit(0)
sys.exit(99)
"""


class QueuedHeldKiroTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertNotIn(evidence.UNHOLD_ENV, os.environ)
        self.assertNotIn(evidence.OVERRIDE_ENV, os.environ)
        self.tmp = tempfile.mkdtemp(prefix="mh-lhq-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.harness = MeshHarness(init_db=True, start_daemon=False)
        self.addCleanup(self.harness.teardown)
        # testing mode (MESH_TESTING=1); M4: the production table is empty, so hold kiro via the
        # testing-only MESH_TEST_LAUNCH_HOLD to exercise the scheduler re-check.
        self.harness.start_daemon(extra_env={evidence.HOLD_ENV: "kiro:M4"})
        self.tok_lead = self._agent("lead1", ["--adapter", "none", "--delivery", "inbox", "--can-lead"])[1]
        self.tok_plain = self._agent("plain", ["--adapter", "none", "--delivery", "inbox",
                                               "--capability", "intake"])[1]
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ lhq", "--goal", "g", "--criterion", "C1",
                                    "--lead", "lead1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]
        binary = os.path.join(self.tmp, "bin", "kiro-cli")
        os.makedirs(os.path.dirname(binary))
        with open(binary, "w", encoding="utf-8") as f:
            f.write(STUB.format(python=sys.executable))
        os.chmod(binary, 0o755)
        self.kiro_id = self._agent("kx", ["--adapter", "kiro", "--delivery", "launch", "--binary", binary,
                                          "--allowed-model", MODEL, "--default-model", MODEL])[0]

    def _agent(self, name: str, args: List[str]):
        rc, resp = self.harness.setup_cli("agent", "create", "--name", name, "--max-concurrency", "1", *args, user=True)
        self.assertEqual(rc, 0, resp)
        agent_id = resp["data"]["id"]
        rc, resp = self.harness.setup_cli("agent", "token", "issue", name, user=True)
        self.assertEqual(rc, 0, resp)
        return agent_id, resp["data"]["token"]

    def _status(self, task_id: str) -> str:
        conn = self.harness.db()
        try:
            return conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()["status"]
        finally:
            conn.close()

    def test_queued_held_kiro_task_fails_launch_held_and_starts_no_run(self):
        """ACC: N19 - a kiro launch task put straight into `queued` (bypassing AG step 4, like a
        task assigned before the L11 hold) is failed by the scheduler with reason launch_held,
        details {adapter: kiro, reason: launch_held, milestone: M4}; 0 runs, no lease, one
        task.failed event; still 0 runs after further ticks."""
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "t",
                                    "--spec", "say MESH_SMOKE_OK", "--profile", "readonly", token=self.tok_lead)
        self.assertEqual(rc, 0, resp)
        tid = resp["data"]["id"]
        self.assertEqual(resp["data"]["status"], "created")

        now = now_iso_utc()
        wconn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            cur = wconn.execute(
                "UPDATE tasks SET assignee_agent_id = ?, status = 'queued', queued_at = ?, updated_at = ?, "
                "version = version + 1 WHERE id = ? AND status = 'created';",
                (self.kiro_id, now, now, tid),
            )
            wconn.commit()
            self.assertEqual(cur.rowcount, 1)
        finally:
            wconn.close()

        self.assertTrue(self.harness.wait_for(lambda: self._status(tid) != "queued", timeout=15.0),
                        "queued held kiro task was never handled by the scheduler")
        time.sleep(1.5)  # more scheduler ticks: the task must still have no run

        conn = self.harness.db()
        try:
            task = conn.execute("SELECT * FROM tasks WHERE id = ?;", (tid,)).fetchone()
            n_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tid,)).fetchone()[0]
            lease = conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{tid}",)).fetchone()
            failed = [dict(r) for r in conn.execute(
                "SELECT reason, payload_json FROM events WHERE task_id = ? AND type = 'task.failed';", (tid,))]
        finally:
            conn.close()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(json.loads(task["failure_json"]),
                         {"reason": "launch_held",
                          "details": {"adapter": "kiro", "reason": "launch_held", "milestone": "M4"}})
        self.assertEqual(n_runs, 0)
        self.assertIsNone(lease)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "launch_held")
        self.assertEqual(json.loads(failed[0]["payload_json"])["details"]["milestone"], "M4")


if __name__ == "__main__":
    unittest.main()
