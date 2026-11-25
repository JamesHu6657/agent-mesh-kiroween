"""M1c run 5: failure policy on real launch failures, and the idle-system check.

ACC: F10, H9, H10, H14, J6, F2
"""

import json
import os
import threading
import time
from typing import Any, Dict, List

import mesh.constants as c
from tests.helpers import idle_bound
from tests.integration.test_execution_acc_run2 import Run2AccBase


class FailurePolicyBase(Run2AccBase):
    def marker_spec(self, name: str, code: int = 3) -> str:
        """First run succeeds (and creates the marker); every later run exits `code`."""
        marker = os.path.join(self.harness.fixtures_dir, f"marker-{name}")
        return "FAKE " + json.dumps({"steps": [{"fail_if_marker": {"path": marker, "code": code}}, {"succeed": {}}]})

    def failing_spec(self, code: int = 3) -> str:
        return "FAKE " + json.dumps({"fail": {"code": code, "stderr": "always failing"}})

    def task_row(self, task_id: str) -> Any:
        return self.q1("SELECT * FROM tasks WHERE id = ?;", (task_id,))

    def failure_reason(self, task_id: str) -> str:
        return json.loads(self.task_row(task_id)["failure_json"])["reason"]

    def agent_id(self, name: str) -> str:
        return self.q1("SELECT id FROM principals WHERE name = ?;", (name,))["id"]

    def last_event(self, task_id: str, etype: str) -> Any:
        return self.q1("SELECT * FROM events WHERE task_id = ? AND type = ? ORDER BY seq DESC LIMIT 1;",
                       (task_id, etype))

    def wait_failed_exit_nonzero(self, task_id: str, attempt: int) -> None:
        self.wait_status(task_id, "failed", timeout=40.0)
        self.assertEqual(self.failure_reason(task_id), "exit_nonzero")
        self.assertEqual(self.task_row(task_id)["attempt"], attempt)
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)) is None, timeout=15.0))

    def assert_over_limit(self, rc: int, resp: Dict[str, Any]) -> None:
        self.assertEqual(rc, c.EXIT_APPROVAL_REQUIRED, resp)
        self.assertEqual(resp["error"]["code"], c.APPROVAL_REQUIRED, resp)
        self.assertEqual(resp["error"]["details"]["category"], "retry_over_limit", resp)


class TestFailurePolicy(FailurePolicyBase):
    def test_f10_rework_then_reassign_from_exit_nonzero(self) -> None:
        """ACC: F10 - rework, then the next attempt fails exit_nonzero; reassign --to ibx keeps workspace_path, changes the assignee, event has the reason."""
        task_id = self.create_task(self.marker_spec("f10"))
        self.wait_status(task_id, "submitted", timeout=40.0)
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)) is None, timeout=15.0))
        workspace = self.task_row(task_id)["workspace_path"]
        self.assertTrue(workspace)
        rc, resp = self.harness.cli("task", "reject", task_id, "--reason", "needs more", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.wait_failed_exit_nonzero(task_id, attempt=2)
        self.assertEqual(len(self.runs_of(task_id)), 2)
        self.assertEqual(self.task_row(task_id)["workspace_path"], workspace)
        rc, resp = self.harness.cli("task", "reassign", task_id, "--to", "ibx", "--reason", "r-f10",
                                    token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        row = self.task_row(task_id)
        self.assertEqual(row["assignee_agent_id"], self.agent_id("ibx"))
        self.assertEqual(row["workspace_path"], workspace)
        evt = self.last_event(task_id, "task.reassigned")
        self.assertIsNotNone(evt)
        self.assertEqual(evt["reason"], "r-f10")
        self.assertEqual(json.loads(evt["payload_json"]).get("reason"), "r-f10")
        self.doctor_ok()

    def test_h9_retry_cap_on_real_failures(self) -> None:
        """ACC: H9 - --max-attempts 2, always exit_nonzero: retry once rc 0; after the 2nd failure retry rc 7 retry_over_limit, task stays failed; --max-attempts 11 rc 2 INVALID_ARGUMENT param max_attempts."""
        task_id = self.create_task(self.failing_spec(), "--max-attempts", "2")
        self.wait_failed_exit_nonzero(task_id, attempt=1)
        rc, resp = self.harness.cli("task", "retry", task_id, token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.wait_failed_exit_nonzero(task_id, attempt=2)
        rc, resp = self.harness.cli("task", "retry", task_id, token=self.tok_lead1)
        self.assert_over_limit(rc, resp)
        time.sleep(1.2)  # a scheduler tick: nothing restarts it
        self.assertEqual(self.status(task_id), "failed")
        self.assertEqual(len(self.runs_of(task_id)), 2)

        rc, resp = self.harness.cli("task", "create", "--project", self.project_id, "--title", "cap",
                                    "--spec", self.failing_spec(), "--assignee", "fx", "--max-attempts", "11",
                                    token=self.tok_lead1)
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT, resp)
        self.assertEqual(resp["error"]["details"]["param"], "max_attempts", resp)
        self.doctor_ok()

    def test_h10_reassign_and_escalate_after_exit_nonzero(self) -> None:
        """ACC: H10 - after a real exit_nonzero failure: reassign --to ibx rc 0; escalate on another task rc 0, escalated, user and grok (origin) inboxes get task.escalated."""
        t1 = self.create_task(self.failing_spec(), title="H10a")
        t2 = self.create_task(self.failing_spec(), title="H10b")
        self.wait_failed_exit_nonzero(t1, attempt=1)
        self.wait_failed_exit_nonzero(t2, attempt=1)
        rc, resp = self.harness.cli("task", "reassign", t1, "--to", "ibx", "--reason", "r", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.task_row(t1)["assignee_agent_id"], self.agent_id("ibx"))
        rc, resp = self.harness.cli("task", "escalate", t2, "--reason", "r", token=self.tok_lead1)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self.status(t2), "escalated")
        user = self.q1("SELECT id FROM principals WHERE kind = 'user';")["id"]
        evt = self.last_event(t2, "task.escalated")
        recipients = {r["recipient_id"] for r in self.q(
            "SELECT recipient_id FROM outbox WHERE type = 'task.escalated' AND event_seq = ?;", (evt["seq"],))}
        self.assertEqual(recipients, {user, self.agent_id("grok")})
        self.doctor_ok()

    def test_h14_reject_at_attempt_cap(self) -> None:
        """ACC: H14 - --max-attempts 1, succeed: reject rc 7 retry_over_limit; task stays submitted; no new run."""
        task_id = self.create_task('FAKE {"succeed": {}}', "--max-attempts", "1")
        self.wait_status(task_id, "submitted", timeout=40.0)
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)) is None, timeout=15.0))
        rc, resp = self.harness.cli("task", "reject", task_id, "--reason", "r", token=self.tok_lead1)
        self.assert_over_limit(rc, resp)
        time.sleep(1.2)
        self.assertEqual(self.status(task_id), "submitted")
        self.assertEqual(len(self.runs_of(task_id)), 1)
        self.doctor_ok()


class TestIdleSystem(FailurePolicyBase):
    # Parallel phase (A1; same reasoning as H8, review m1c-run2 speedup 4): J6 counts only
    # this home's runs while this home has no work, so other tests' load on the box cannot
    # add runs here; load can only lengthen the idle window.
    @idle_bound
    def test_j6_idle_60s_no_runs(self) -> None:
        """ACC: J6 - 60 s idle with agents blocked in `inbox wait`: `run list` count unchanged (no polling, no model sessions)."""
        # Some history first, so the count is not trivially zero.
        done = self.create_task('FAKE {"succeed": {}}', title="before")
        self.wait_status(done, "submitted", timeout=40.0)
        self.assertTrue(self.harness.wait_for(
            lambda: self.q1("SELECT 1 FROM leases WHERE key = ?;", (f"task:{done}",)) is None, timeout=15.0))

        def run_list_count() -> int:
            rc, resp = self.harness.cli("run", "list", "--user", user=True)
            self.assertEqual(rc, 0, resp)
            data = resp["data"]
            items = data.get("items", data) if isinstance(data, dict) else data
            return len(items)

        before = run_list_count()
        before_db = self.q1("SELECT COUNT(*) AS n FROM runs;")["n"]
        self.assertEqual(before, before_db)
        results: List[Any] = []

        def waiter(agent: str) -> None:
            results.append((agent,) + self.harness.cli("inbox", "wait", "--timeout", "65",
                                                       token=self.tokens[agent], timeout=90.0))

        for agent in ("ibx", "grok", "lead2"):  # drain: wait must block, not return old items
            rc, resp = self.harness.cli("inbox", "list", token=self.tokens[agent])
            self.assertEqual(rc, 0, resp)
        threads = [threading.Thread(target=waiter, args=(a,), daemon=True) for a in ("ibx", "grok", "lead2")]
        for t in threads:
            t.start()
        self.assertTrue(self.harness.wait_for_active_waits(3, timeout=15.0), "agents never blocked in inbox wait")
        time.sleep(60)
        self.assertGreaterEqual(self.harness.active_waits(), 3)
        self.assertEqual(run_list_count(), before)
        self.assertEqual(self.q1("SELECT COUNT(*) AS n FROM runs;")["n"], before_db)
        self.assertEqual(self.q1("SELECT COUNT(*) AS n FROM runs WHERE status IN ('starting','running');")["n"], 0)
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(results), 3, results)
        for agent, rc, resp in results:
            self.assertEqual(rc, c.EXIT_WAIT_TIMEOUT, (agent, resp))
        self.doctor_ok()
