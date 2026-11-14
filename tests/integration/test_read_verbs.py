"""Integration tests for the run/task-log/workspace read verbs (plan §13.3, §7.3, §10.1, §15.2).

ACC: F9 (workspace list part; the full F9 scenario is in test_execution_acc_run2.py)
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import Any, Dict, Tuple

import mesh.constants as c
from mesh.core.proc import get_proc_environ
from tests.helpers import MeshHarness
from tests.integration.test_execution_launch import create_agent
from tests.stubs import create_test_stubs


def run_cli_as(env: Dict[str, str], *args: str) -> Tuple[int, Dict[str, Any]]:
    res = subprocess.run(["mesh", *args], capture_output=True, text=True, env=dict(env), timeout=60)
    try:
        return res.returncode, json.loads(res.stdout)
    except ValueError:
        return res.returncode, {"raw": res.stdout}


class TestReadVerbs(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", can_lead=True)
        self.tok_plain = create_agent(self.harness, "plain", capabilities=["intake"])
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        create_agent(self.harness, "fx", adapter="fake", delivery="launch",
                     allowed_models=["m-a", "m-b"], default_model="m-a", max_concurrency=2)
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)
        rc, resp = self.harness.cli("intake", "submit", "--title", "PRJ", "--goal", "g", "--criterion", "c1",
                                    "--lead", "lead1", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.project_id = resp["data"]["id"]

    def tearDown(self) -> None:
        self.harness.teardown()
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _create(self, spec: str) -> str:
        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "T",
                                    "--spec", spec, "--assignee", "fx", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _wait_status(self, task_id: str, status: str, timeout: float = 20.0) -> None:
        def ok() -> bool:
            conn = self.harness.db()
            try:
                return conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()["status"] == status
            finally:
                conn.close()

        self.assertTrue(self.harness.wait_for(ok, timeout=timeout), f"{task_id} never reached {status}")

    def test_run_and_task_log_read_verbs_redacted(self) -> None:
        """run list/show/log and task log: filters, --stream/--tail, redacted output (§15.2)."""
        task_id = self._create('FAKE {"steps": [{"print_secrets": {}}, {"fail": {"code": 3, "stderr": "boom"}}]}')
        self._wait_status(task_id, "failed")

        rc, resp = self.harness.cli("run", "list", "--task", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        runs = resp["data"]["runs"]
        self.assertEqual(len(runs), 1)
        run_id = runs[0]["id"]
        self.assertEqual(runs[0]["status"], "exited")
        self.assertEqual(runs[0]["exit_code"], 3)

        rc, resp = self.harness.cli("run", "list", "--status", "running", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["runs"], [])
        rc, resp = self.harness.cli("run", "list", "--status", "succeeded", user=True)
        self.assertEqual(rc, 2, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

        rc, resp = self.harness.cli("run", "show", run_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["task_id"], task_id)
        self.assertEqual(resp["data"]["model"], "m-a")
        self.assertEqual(resp["data"]["agent"], "fx")
        self.assertEqual(resp["data"]["end_reason"], "exited")

        rc, resp = self.harness.cli("run", "log", run_id, "--stream", "stderr", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(list(resp["data"]["streams"].keys()), ["stderr"])
        self.assertIn("boom", resp["data"]["streams"]["stderr"]["lines"])

        rc, resp = self.harness.cli("task", "log", task_id, "--stream", "stdout", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["run_id"], run_id)
        stdout = resp["data"]["streams"]["stdout"]
        self.assertTrue(stdout["available"])
        text = "\n".join(stdout["lines"])
        self.assertIn("[REDACTED", text)
        self.assertNotIn("sk-acc0123456789abcdef", text)

        rc, resp = self.harness.cli("task", "log", task_id, "--tail", "1", "--stream", "stdout", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(len(resp["data"]["streams"]["stdout"]["lines"]), 1)
        self.assertEqual(resp["data"]["streams"]["stdout"]["truncated"], stdout["total_lines"] > 1)

        rc, resp = self.harness.cli("task", "log", task_id, "--run", "run_nope", token=self.tok_lead1)
        self.assertEqual(rc, 5, resp)
        self.assertEqual(resp["error"]["code"], c.NOT_FOUND)

        # Read authz (§7.3): lead2 has no relation to PRJ; grok (relay) reads everything.
        for argv in (("run", "show", run_id), ("run", "log", run_id), ("task", "log", task_id),
                     ("run", "list", "--task", task_id), ("workspace", "show", task_id)):
            rc, resp = self.harness.cli(*argv, token=self.tok_lead2)
            self.assertEqual((rc, resp["error"]["code"]), (4, c.SCOPE_MISMATCH), argv)
            rc, resp = self.harness.cli(*argv, token=self.tok_grok)
            self.assertEqual(rc, 0, (argv, resp))
        rc, resp = self.harness.cli("run", "list", token=self.tok_lead2)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["runs"], [])
        rc, resp = self.harness.cli("workspace", "list", "--project", self.project_id, token=self.tok_lead2)
        self.assertEqual((rc, resp["error"]["code"]), (4, c.SCOPE_MISMATCH))

    def test_run_token_reads_only_own_task(self) -> None:
        """§7.3: an executor run token reads its own task's logs/run; any other task -> 4 SCOPE_MISMATCH; run list -> 4."""
        other = self._create('FAKE {"succeed": {}}')
        self._wait_status(other, "submitted")
        task_id = self._create('FAKE {"sleep": {"seconds": 120}}')

        found: Dict[str, Any] = {}

        def running() -> bool:
            conn = self.harness.db()
            try:
                row = conn.execute("SELECT * FROM runs WHERE task_id = ? AND status = 'running';", (task_id,)).fetchone()
            finally:
                conn.close()
            if row:
                found["run"] = row
            return row is not None

        self.assertTrue(self.harness.wait_for(running, timeout=15.0))
        run = found["run"]
        env = get_proc_environ(run["wrapper_pid"])
        self.assertIsNotNone(env)

        self.assertEqual(run_cli_as(env, "task", "log", task_id)[0], 0)
        self.assertEqual(run_cli_as(env, "run", "show", run["id"])[0], 0)
        self.assertEqual(run_cli_as(env, "run", "log", run["id"])[0], 0)
        self.assertEqual(run_cli_as(env, "workspace", "show", task_id)[0], 0)
        rc, resp = run_cli_as(env, "task", "log", other)
        self.assertEqual((rc, resp["error"]["code"]), (4, c.SCOPE_MISMATCH))
        rc, resp = run_cli_as(env, "run", "list")
        self.assertEqual((rc, resp["error"]["code"]), (4, c.CAPABILITY_MISSING))
        rc, resp = run_cli_as(env, "workspace", "list")
        self.assertEqual((rc, resp["error"]["code"]), (4, c.CAPABILITY_MISSING))

    def test_intake_dry_run_creates_no_lead_dir(self) -> None:
        """§5 effects after COMMIT (review m1c-run2 finding 2): intake submit --dry-run and set-lead --dry-run create no dir."""
        root = os.path.join(self.harness.home, "workspaces")
        before = sorted(os.listdir(root)) if os.path.isdir(root) else []
        rc, resp = self.harness.cli("intake", "submit", "--title", "DRY", "--goal", "g", "--criterion", "c1",
                                    "--lead", "lead1", "--dry-run", token=self.tok_plain)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(sorted(os.listdir(root)) if os.path.isdir(root) else [], before)
        rc, resp = self.harness.cli("project", "set-lead", self.project_id, "lead2", "--reason", "dry", "--dry-run",
                                    user=True)
        self.assertEqual(rc, 0, resp)
        self.assertFalse(os.path.isdir(os.path.join(root, self.project_id, "lead",
                                                    self._principal_id("lead2"))))

    def _principal_id(self, name: str) -> str:
        conn = self.harness.db()
        try:
            return conn.execute("SELECT id FROM principals WHERE name = ?;", (name,)).fetchone()["id"]
        finally:
            conn.close()

    def test_lead_dir_at_p1_and_set_lead_and_workspace_list(self) -> None:
        """§10.1/Q6: lead dir created at P1 and at set-lead; workspace list shows lead and task dirs; show lists files."""
        conn = self.harness.db()
        try:
            lead1_id = conn.execute("SELECT id FROM principals WHERE name = 'lead1';").fetchone()["id"]
            lead2_id = conn.execute("SELECT id FROM principals WHERE name = 'lead2';").fetchone()["id"]
        finally:
            conn.close()
        lead1_dir = os.path.join(self.harness.home, "workspaces", self.project_id, "lead", lead1_id)
        self.assertTrue(os.path.isdir(lead1_dir))
        self.assertEqual(os.stat(lead1_dir).st_mode & 0o777, 0o700)

        task_id = self._create('FAKE {"write_file": {"path": "out.txt", "content": "hello"}}')
        self._wait_status(task_id, "submitted")

        rc, resp = self.harness.cli("project", "set-lead", self.project_id, "lead2", "--reason", "swap", user=True)
        self.assertEqual(rc, 0, resp)
        lead2_dir = os.path.join(self.harness.home, "workspaces", self.project_id, "lead", lead2_id)
        self.assertTrue(os.path.isdir(lead2_dir))

        rc, resp = self.harness.cli("workspace", "list", "--project", self.project_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        entries = resp["data"]["workspaces"]
        leads = {e["agent"]: e for e in entries if e["kind"] == "lead"}
        tasks = [e for e in entries if e["kind"] == "task"]
        self.assertEqual(set(leads), {"lead1", "lead2"})
        self.assertTrue(leads["lead2"]["current"])
        self.assertFalse(leads["lead1"]["current"])
        self.assertEqual([t["task_id"] for t in tasks], [task_id])
        task_path = tasks[0]["path"]
        self.assertTrue(task_path.startswith(os.path.join(self.harness.home, "workspaces", self.project_id, "tasks", task_id)))
        self.assertTrue(tasks[0]["exists"])
        for lead in leads.values():
            self.assertNotEqual(lead["path"].rstrip("/"), task_path.rstrip("/"))

        rc, resp = self.harness.cli("workspace", "show", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(resp["data"]["files"], ["out.txt"])
        rc, resp = self.harness.cli("workspace", "show", "tsk_nope", user=True)
        self.assertEqual(rc, 5, resp)


if __name__ == "__main__":
    unittest.main()
