"""CLI/fake regressions for completion and asynchronous lead wake paths."""

import json
import os
import sqlite3

from mesh.core.clock import parse_utc
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_lead_turns import create_agent


class LeadWakeCase(LeadActionCase):
    def _call(self, *args, **kwargs):
        rc, resp = self.harness.cli(*args, **kwargs)
        self.assertEqual(rc, 0, resp)
        return resp["data"]

    def _goal(self, project, actions, gate=None):
        spec = {"lead_actions": {"actions": actions}}
        if gate:
            spec = {"steps": [{"wait_release": {"path": gate}}, spec]}
        self._call("project", "update", project, "--goal", _fake_goal(spec), user=True)

    def _report(self, project):
        criteria = self._rows("SELECT id FROM acceptance_criteria WHERE project_id = ? "
                              "AND superseded_at IS NULL;", (project,))
        return {"summary": "Verified", "criteria": [
            {"id": r["id"], "met": True, "evidence": "CLI regression"} for r in criteria]}

    def _prompt(self, run):
        with open(os.path.join(run["run_dir"], "prompt.md"), encoding="utf-8") as f:
            return f.read()

    def _write_db(self, sql, params=()):
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


class TestLeadWakesM2(LeadWakeCase):
    def test_complete_ignores_only_verified_own_turn_lease(self):
        """ACC: L2 - own lease permits completion; foreign lead/merge/task leases do not."""
        # Five sequential turns, none of them about debounce timing: 1 s instead of the
        # shared 2 s (m3-a1-speed). Every follow-up is sent after the previous turn closed.
        self._call("config", "set", "lead.wake_debounce_s", "1", user=True)
        other_gate = os.path.join(self.harness.fixtures_dir, "other-lead")
        other = self._create_project(_fake_goal({"wait_release": {"path": other_gate}}))
        other_run = self._wait_turn_running(other)
        project = self._create_project(_fake_goal({"lead_actions": {"actions": []}}))
        self._wait_turn_closed(project)
        report = self._report(project)
        report_path = os.path.join(self.harness.fixtures_dir, "completion.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f)
        complete = [{"cmd": "project.complete", "args": {"report": report}}]
        # Re-key a real live run's lease as an explicit fixture, restoring its
        # original key before doctor teardown. Never loosen an existing guard.
        task = self._call("task", "create", "--project", project, "--title", "Terminal",
                          "--spec", "s", user=True)["id"]
        self._call("task", "cancel", task, "--reason", "fixture", user=True)
        for index, key in enumerate((f"merge:{project}", f"task:{task}", f"leadturn:{project}"), 2):
            gate = os.path.join(self.harness.fixtures_dir, f"complete-{index}")
            self._goal(project, complete, gate)
            self._follow_up(project, "attempt completion with a foreign lease")
            self.assertTrue(self.harness.wait_for(
                lambda: len(self._lead_turn_runs(project)) == index
                and self._lead_turn_runs(project)[-1]["status"] == "running", timeout=30))
            own = self._lead_turn_runs(project)[-1]
            if key == f"leadturn:{project}":
                self._write_db("UPDATE leases SET key = ? WHERE key = ?;",
                               (f"fixture-own:{own['id']}", key))
            self._write_db("UPDATE leases SET key = ? WHERE key = ?;", (key, f"leadturn:{other}"))
            try:
                # A user cannot claim the running turn's own lease either.
                rc, resp = self.harness.cli("project", "complete", project, "--report",
                                            report_path, user=True)
                self.assertEqual(rc, 6, resp)
                self.assertEqual(resp["error"]["code"], "COMPLETION_BLOCKED")
                with open(gate, "w"):
                    pass
                self._wait_turn_closed(project, count=index)
                result = self._action_results(own["id"])[0]
                self.assertEqual((result["ok"], result["exit_code"], result["error_code"]),
                                 (False, 6, "COMPLETION_BLOCKED"), result)
                self.assertEqual(self._rows("SELECT status FROM projects WHERE id = ?;",
                                            (project,))[0]["status"], "active")
                self.assertEqual(self._rows("SELECT run_id FROM leases WHERE key = ?;",
                                            (key,))[0]["run_id"], other_run["id"])
            finally:
                self._write_db("UPDATE leases SET key = ? WHERE key = ?;", (f"leadturn:{other}", key))
        self._goal(project, complete)
        self._follow_up(project, "complete with only the verified own lease")
        last = self._wait_turn_closed(project, count=5)[-1]
        result = self._action_results(last["id"])[0]
        self.assertEqual((result["ok"], result["exit_code"]), (True, 0), result)
        self.assertEqual(self._rows("SELECT status FROM projects WHERE id = ?;",
                                    (project,))[0]["status"], "completed")
        self.assertEqual(len(self._events(project, "project.completed")), 1)
        self.assertEqual(self._rows("SELECT key FROM leases WHERE key = ?;",
                                    (f"leadturn:{project}",)), [])
        with open(other_gate, "w"):
            pass
        self._wait_turn_closed(other)

    def test_approval_decision_alone_wakes_lead_and_retries_action(self):
        """ACC: L1, L2 - a user approval is the only new wake, with normal debounce."""
        ibx = create_agent(self.harness, "ibx")
        project = self._create_project(_fake_goal({"lead_actions": {"actions": []}}))
        self._wait_turn_closed(project)
        self.harness.hold_launch_starts()
        try:
            task = self._call("task", "create", "--project", project, "--title", "Capped",
                              "--spec", "s", "--assignee", "ibx", "--max-attempts", "1",
                              user=True)["id"]
            self.assertTrue(self.harness.wait_for(lambda: self._task_status(task) == "queued"))
            started = self._call("task", "start", task, token=ibx)
            self._call("task", "fail", task, "--reason", "broken", "--run", started["run_id"],
                       "--fence", str(started["fence"]), token=ibx)
            self._goal(project, [{"cmd": "task.retry", "args": {"task": task}}])
        finally:
            self.harness.release_launch_starts()
        first = self._wait_turn_closed(project, count=2)[1]
        result = self._action_results(first["id"])[0]
        self.assertEqual((result["ok"], result["exit_code"]), (False, 7), result)
        approval = self._rows("SELECT * FROM approvals WHERE project_id = ? AND status = 'pending';",
                              (project,))[0]
        self.assertEqual(approval["requested_by"], self._agent_id("flead"))
        self._goal(project, [{"cmd": "task.retry", "args": {"task": task},
                              "approval": approval["id"]}])
        self.harness.hold_launch_starts()
        try:
            self._call("approval", "approve", approval["id"], "--op-hash", approval["op_hash"], user=True)
            wake = [r for r in self._lead_wake_rows(project) if r["type"] == "approval.decided"]
            self.assertEqual(len(wake), 1, "approval decision did not enqueue a lead wake")
            self.assertEqual(wake[0]["status"], "pending")
            self.assertEqual(wake[0]["recipient_id"], self._agent_id("flead"))
            inbox = self._rows("SELECT id FROM outbox WHERE event_seq = ? AND channel = 'inbox' "
                               "AND recipient_id = ?;", (wake[0]["event_seq"], self._agent_id("flead")))
            self.assertEqual(len(inbox), 1)  # Preserve the requester's inbox contract.
        finally:
            self.harness.release_launch_starts()
        second = self._wait_turn_closed(project, count=3)[2]
        self.assertEqual(self._task_status(task), "queued")
        self.assertTrue(self._action_results(second["id"])[0]["ok"])
        self.assertEqual(self._rows("SELECT status FROM approvals WHERE id = ?;",
                                    (approval["id"],))[0]["status"], "consumed")
        self.assertIn("approval.decided", self._prompt(second))
        self.assertIn("lead.action_failed", self._prompt(second))
        carried = [r for r in self._lead_wake_rows(project) if r["delivering_run_id"] == second["id"]]
        self.assertEqual({r["type"] for r in carried}, {"approval.decided", "lead.action_failed"})
        self.assertEqual({r["status"] for r in carried}, {"acked"})
        # Coalesced wakes debounce from the oldest carried message, not the approval.
        oldest = min(parse_utc(r["created_at"]) for r in carried)
        self.assertGreaterEqual((parse_utc(second["started_at"]) - oldest).total_seconds(), 2)


class TestLeadMergeWakeM2(LeadWakeCase):
    needs_git = True

    def test_async_merge_success_wakes_lead_to_deliver_and_complete(self):
        """ACC: L1, L2, K10 - async merge result alone starts the delivery/completion turn."""
        ibx = create_agent(self.harness, "ibx")
        repo = self.fixture.make_k0("merge-wake")
        project = self._create_project(_fake_goal({"lead_actions": {"actions": []}}), repo=repo)
        self._wait_turn_closed(project)
        self.harness.hold_launch_starts()
        try:
            task = self._call("task", "create", "--project", project, "--title", "Feature",
                              "--spec", "edit", "--assignee", "ibx", "--kind", "code", user=True)["id"]
            self.assertTrue(self.harness.wait_for(lambda: self._task_status(task) == "queued"))
            started = self._call("task", "start", task, token=ibx)
            ws = started["workspace"]
            with open(os.path.join(ws, "feature.txt"), "w", encoding="utf-8") as f:
                f.write("merged feature\n")
            self.fixture.git(ws, "add", "feature.txt")
            self.fixture.git(ws, "commit", "-m", "Feature", identity=True)
            self._call("task", "submit", task, "--run", started["run_id"],
                       "--fence", str(started["fence"]), "--summary", "done", token=ibx)
            gate = os.path.join(self.harness.fixtures_dir, "merge-action")
            self._goal(project, [{"cmd": "task.accept", "args": {"task": task}},
                                 {"cmd": "task.merge", "args": {"task": task}}], gate)
        finally:
            self.harness.release_launch_starts()
        self.assertTrue(self.harness.wait_for(
            lambda: len(self._lead_turn_runs(project)) == 2
            and self._lead_turn_runs(project)[1]["status"] == "running", timeout=30))
        merge_turn = self._lead_turn_runs(project)[1]
        self._goal(project, [{"cmd": "project.deliver", "args": {}},
                             {"cmd": "project.complete", "args": {"report": self._report(project)}}])
        with open(gate, "w"):
            pass
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task) == "merged", timeout=60))
        queued = self._action_results(merge_turn["id"])[1]
        self.assertEqual((queued["cmd"], queued["ok"], queued["status"]), ("task.merge", True, "queued"))
        self.assertTrue(self.harness.wait_for(
            lambda: any(r["type"] == "task.merged" for r in self._lead_wake_rows(project)), timeout=10),
            "merge success did not enqueue a lead wake")
        last = self._wait_turn_closed(project, count=3)[2]
        self.assertEqual([(r["cmd"], r["ok"], r["exit_code"]) for r in self._action_results(last["id"])],
                         [("project.deliver", True, 0), ("project.complete", True, 0)])
        self.assertEqual(self._rows("SELECT status FROM projects WHERE id = ?;",
                                    (project,))[0]["status"], "completed")
        self.assertEqual(self.fixture.head(repo, "main"),
                         self.fixture.head(repo, f"mesh/{project}/integration"))
        self.assertIn("task.merged", self._prompt(last))
        wake = [r for r in self._lead_wake_rows(project) if r["delivering_run_id"] == last["id"]]
        self.assertEqual([(r["type"], r["status"]) for r in wake], [("task.merged", "acked")])
        self.assertGreaterEqual((parse_utc(last["started_at"]) -
                                 parse_utc(wake[0]["created_at"])).total_seconds(), 2)
