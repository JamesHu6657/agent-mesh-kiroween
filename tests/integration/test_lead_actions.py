"""Integration tests for lead-turn actions and lead supersede (plan §12.4, §8, §11.4;
checklist L2, L4; coordinator Q8, Q10).

Every test drives the real CLI (`bin/mesh` subprocesses) against a `/tmp/mh-*` daemon
started by the harness. The lead `flead` is a launch-mode `fake` agent; the fake picks
its behaviour from the `FAKE {…}` line of the project goal, so each test puts its spec
in `--goal`. `lead.wake_debounce_s` is 2 s (the shared `LeadTurnCase` setup).
"""

import json
import os
import sqlite3
import unittest
from typing import Any, Dict, List

from tests.integration.test_lead_turns import LeadTurnCase, create_agent


def _fake_goal(spec: Dict[str, Any]) -> str:
    return "FAKE " + json.dumps(spec, separators=(",", ":"))


class LeadActionCase(LeadTurnCase):
    """`LeadTurnCase` plus small lookups used by the action tests."""

    def _agent_id(self, name: str) -> str:
        conn = self.harness.db()
        try:
            return conn.execute("SELECT id FROM principals WHERE name = ?;", (name,)).fetchone()["id"]
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        conn = self.harness.db()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _action_results(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._rows(
            "SELECT payload_json, via, actor_id, credential_id, request_id, seq FROM events "
            "WHERE type = 'lead.action_result' "
            "AND run_id = ? ORDER BY seq ASC;",
            (run_id,),
        )
        out = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            payload["_via"] = r["via"]
            payload["_actor_id"] = r["actor_id"]
            payload["_credential_id"] = r["credential_id"]
            payload["_request_id"] = r["request_id"]
            payload["_seq"] = r["seq"]
            out.append(payload)
        return out

    def _project_action_results(self, project_id: str, cmd: str) -> List[Dict[str, Any]]:
        """Every `lead.action_result` of `cmd` in the project, oldest first."""
        out: List[Dict[str, Any]] = []
        for run in self._lead_turn_runs(project_id):
            out.extend(r for r in self._action_results(run["id"]) if r["cmd"] == cmd)
        return sorted(out, key=lambda r: r["_seq"])

    def _set_actions(self, project_id: str, actions: List[Dict[str, Any]], message: str) -> None:
        """Make the next lead turns emit `actions` (the fake reads the goal) and wake the lead."""
        rc, resp = self.harness.cli("project", "update", project_id,
                                    "--goal", _fake_goal({"lead_actions": {"actions": actions}}), user=True)
        self.assertEqual(rc, 0, resp)
        self._follow_up(project_id, message)

    def _task_status(self, task_id: str) -> str:
        return self._rows("SELECT status FROM tasks WHERE id = ?;", (task_id,))[0]["status"]


class TestLeadActions(LeadActionCase):
    """ACC: L2 - `mesh-actions` of a lead turn run as the lead after exit 0."""

    def test_l2_actions_executed_as_lead_and_failures_reach_next_turn(self) -> None:
        """ACC: L2 - a turn whose final output carries `task.create` (assignee `fx`),
        `config.set`, `approval.request` and a `task.accept` of a missing task: the task
        exists, created by the lead principal through the lead run token (rc 0,
        `lead.action_result` ok, idempotency key `act:<run>:0`); the approval is
        requested by the lead agent in the turn's project; `config.set` (not in the
        allowed list) and the missing task are failures, each sent to the lead as a
        `lead.action_failed` `lead_wake` message. The failures alone start no turn
        (Q8); an intake follow-up does, and its `prompt.md` contains
        `lead.action_failed`."""
        create_agent(self.harness, "fx", adapter="fake", delivery="launch")
        actions = [
            {"cmd": "task.create",
             "args": {"title": "Lead made task", "spec": 'FAKE {"sleep":{"seconds":60}}',
                      "assignee": "fx", "kind": "general"}},
            {"cmd": "config.set", "args": {"key": "lead.wake_debounce_s", "value": "0"}},
            {"cmd": "approval.request",
             "args": {"category": "deploy", "summary": "Deploy from a lead turn",
                      "operation": {"target": "staging"}}},
            {"cmd": "task.accept", "args": {"task": "tsk_missing", "note": "n"}},
        ]
        project_id = self._create_project(_fake_goal({"lead_actions": {"actions": actions}}))
        flead_id = self._agent_id("flead")

        turn = self._wait_turn_closed(project_id, count=1)[0]
        self.assertEqual(turn["status"], "exited", dict(turn))
        self.assertEqual(turn["exit_code"], 0, dict(turn))

        results = self._action_results(turn["id"])
        self.assertEqual([r["index"] for r in results], [0, 1, 2, 3], results)
        self.assertEqual([r["cmd"] for r in results],
                         ["task.create", "config.set", "approval.request", "task.accept"])
        self.assertEqual([r["ok"] for r in results], [True, False, True, False], results)
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertIsNone(results[0]["error_code"])
        self.assertEqual(results[1]["error_code"], "cmd_not_allowed")
        self.assertEqual(results[1]["exit_code"], 4)
        self.assertEqual(results[3]["exit_code"], 5)
        self.assertEqual(results[3]["error_code"], "NOT_FOUND")
        self.assertEqual({r["_via"] for r in results}, {"lead_turn"})
        self.assertEqual({r["_actor_id"] for r in results}, {flead_id})

        # The task was created by the lead principal, through the turn's run token
        # (an assigned task's creation event is `task.assigned`).
        fx_id = self._agent_id("fx")
        tasks = self._rows("SELECT * FROM tasks WHERE project_id = ?;", (project_id,))
        self.assertEqual(len(tasks), 1, [dict(t) for t in tasks])
        self.assertEqual(tasks[0]["title"], "Lead made task")
        self.assertEqual(tasks[0]["created_by"], flead_id)
        self.assertEqual(tasks[0]["assignee_agent_id"], fx_id)
        created = self._rows(
            "SELECT * FROM events WHERE type = 'task.assigned' AND task_id = ?;", (tasks[0]["id"],))
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["actor_id"], flead_id)
        self.assertEqual(created[0]["via"], "run_token")
        # Review m2-r10 finding 5: the action's result and the handler's own event share
        # the dispatched request's id.
        self.assertIsNotNone(results[0]["request_id"])
        self.assertEqual(created[0]["request_id"], results[0]["request_id"])
        self.assertEqual(results[0]["_request_id"], results[0]["request_id"])
        self.assertIsNone(results[1]["request_id"])  # an item error dispatches nothing
        cred = self._rows(
            "SELECT id FROM credentials WHERE kind = 'run_token' "
            "AND json_extract(scope_json, '$.run_id') = ?;", (turn["id"],))
        self.assertEqual(len(cred), 1)
        self.assertEqual({r["_credential_id"] for r in results}, {cred[0]["id"]})
        idem = self._rows(
            # Pre-public review P2: run-token rows are partitioned by credential scope.
            "SELECT key, cmd, exit_code FROM idempotency WHERE principal_id LIKE ? || '#scope:%' ORDER BY key;",
            (flead_id,))
        self.assertEqual([(r["key"], r["cmd"], r["exit_code"]) for r in idem],
                         [(f"act:{turn['id']}:0", "task.create", 0),
                          (f"act:{turn['id']}:2", "approval.request", 0)])

        # approval.request with the lead run token: own project, requested by the lead.
        approvals = self._rows("SELECT * FROM approvals WHERE project_id = ?;", (project_id,))
        self.assertEqual(len(approvals), 1, [dict(a) for a in approvals])
        self.assertEqual(approvals[0]["requested_by"], flead_id)
        self.assertEqual(approvals[0]["status"], "pending")

        # Each failure is one lead.action_failed lead_wake message to the lead.
        failed = self._rows(
            "SELECT * FROM outbox WHERE type = 'lead.action_failed' AND project_id = ? "
            "ORDER BY dedup_key ASC;", (project_id,))
        self.assertEqual([r["dedup_key"] for r in failed],
                         [f"lead.action_failed:{turn['id']}:1", f"lead.action_failed:{turn['id']}:3"])
        self.assertEqual({r["recipient_id"] for r in failed}, {flead_id})
        self.assertEqual({r["channel"] for r in failed}, {"lead_wake"})
        self.assertEqual({r["status"] for r in failed}, {"pending"})
        # The turn's own messages were acked after the actions ran.
        carried = self._rows("SELECT status FROM outbox WHERE delivering_run_id = ?;", (turn["id"],))
        self.assertTrue(carried)
        self.assertEqual({r["status"] for r in carried}, {"acked"})

        # Q8: the failures alone never start a turn.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._lead_turn_runs(project_id)) > 1,
                                  timeout=3.0, step=0.1),
            "a lead.action_failed message alone started a turn",
        )

        # An intake follow-up starts the next turn, which carries the failures.
        self._follow_up(project_id, "please continue")
        second = self._wait_turn_closed(project_id, count=2)[1]
        with open(os.path.join(second["run_dir"], "prompt.md"), "r", encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("lead.action_failed", prompt)
        self.assertIn("cmd_not_allowed", prompt)
        acked = self._rows(
            "SELECT status, delivering_run_id FROM outbox WHERE type = 'lead.action_failed' "
            "AND dedup_key LIKE ?;", (f"lead.action_failed:{turn['id']}:%",))
        self.assertEqual({(r["status"], r["delivering_run_id"]) for r in acked},
                         {("acked", second["id"])})


class TestLeadSupersede(LeadActionCase):
    """ACC: L4 - `set-lead` terminates the old lead's running turn."""

    def test_l4_set_lead_terminates_turn_and_supersedes_token(self) -> None:
        """ACC: L4 - while `flead`'s turn ignores SIGTERM (`kill.grace_s=10`),
        `set-lead $PRJ lead1` [U]: within the grace window the turn's token gets rc 9
        `LEAD_SUPERSEDED` on `task create`; the turn's `delivering` messages are
        superseded and re-created for `lead1`; after the kill the same call is rc 3;
        the run is `killed` with end_reason `lead_superseded` and no
        `lead.action_result` exists for it."""
        from mesh.core.proc import get_proc_environ

        rc, resp = self.harness.cli("config", "set", "kill.grace_s", "10", user=True)
        self.assertEqual(rc, 0, resp)
        create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        lead1_id = self._agent_id("lead1")

        project_id = self._create_project(_fake_goal({"ignore_sigterm": {}}))
        turn = self._wait_turn_running(project_id)
        env = None

        def token_ready() -> bool:
            nonlocal env
            env = get_proc_environ(turn["wrapper_pid"])
            return bool(env and env.get("MESH_TOKEN"))

        self.assertTrue(self.harness.wait_for(token_ready, timeout=10.0, step=0.05))
        token = env["MESH_TOKEN"]
        delivering = self._rows(
            "SELECT id, event_seq, type FROM outbox WHERE delivering_run_id = ? "
            "AND status = 'delivering';", (turn["id"],))
        self.assertTrue(delivering)

        rc, resp = self.harness.cli("project", "set-lead", project_id, "lead1",
                                    "--reason", "supersede the running turn", user=True)
        self.assertEqual(rc, 0, resp)

        # Within the grace window: the turn is still alive, its token is superseded.
        rc, resp = self.harness.cli("task", "create", "--project", project_id,
                                    "--title", "late", "--spec", "s", token=token)
        self.assertEqual(rc, 9, resp)
        self.assertEqual(resp["error"]["code"], "LEAD_SUPERSEDED")
        run_now = self._rows("SELECT status FROM runs WHERE id = ?;", (turn["id"],))[0]
        self.assertEqual(run_now["status"], "running", "the kill came before the grace window ended")

        # The delivering messages are superseded and re-created for the new lead.
        old = self._rows(
            "SELECT status FROM outbox WHERE id IN (%s);" % ",".join("?" for _ in delivering),
            tuple(r["id"] for r in delivering))
        self.assertEqual({r["status"] for r in old}, {"superseded"})
        new = self._rows(
            "SELECT event_seq, type, channel, status FROM outbox WHERE recipient_id = ? "
            "AND project_id = ?;", (lead1_id, project_id))
        self.assertTrue({(r["event_seq"], r["type"]) for r in delivering}
                        <= {(r["event_seq"], r["type"]) for r in new}, [dict(r) for r in new])
        self.assertEqual({r["channel"] for r in new}, {"inbox"})

        runs = self._wait_turn_closed(project_id, count=1, timeout=30.0)
        closed = runs[0]
        self.assertEqual(closed["status"], "killed", dict(closed))
        self.assertEqual(closed["end_reason"], "lead_superseded", dict(closed))

        rc, resp = self.harness.cli("task", "create", "--project", project_id,
                                    "--title", "after kill", "--spec", "s", token=token)
        self.assertEqual(rc, 3, resp)
        self.assertEqual(resp["error"]["code"], "TOKEN_REVOKED")

        self.assertEqual(self._action_results(turn["id"]), [])
        self.assertEqual(self._rows("SELECT id FROM tasks WHERE project_id = ?;", (project_id,)), [])
        # The superseded rows stay superseded: the kill did not put them back to pending.
        old = self._rows(
            "SELECT status FROM outbox WHERE id IN (%s);" % ",".join("?" for _ in delivering),
            tuple(r["id"] for r in delivering))
        self.assertEqual({r["status"] for r in old}, {"superseded"})


class TestLeadLaunchGate(LeadActionCase):
    """ACC: N4 (lead path) - a lead turn launches only on a launch-enabled adapter;
    vendor adapters stay rc 13 until M3."""

    def test_vendor_launch_lead_refused(self) -> None:
        """ACC: N4 - a launch-mode lead on adapter `droid`: `intake submit --lead`,
        `project create --lead` and `set-lead` are rc 13 `ADAPTER_UNSUPPORTED` with
        `details.milestone`; a vendor lead switched to launch through the DB gets no
        lead turn, its `lead_wake` messages stay `pending`, and exactly one
        `lead.turn_refused` event is recorded."""
        create_agent(self.harness, "dlead", adapter="droid", delivery="launch", can_lead=True)
        rc, resp = self.harness.cli("intake", "submit", "--title", "Vendor", "--goal", "g",
                                    "--criterion", "C1", "--lead", "dlead", token=self.tok_grok)
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(resp["error"]["details"]["milestone"], "M3")
        rc, resp = self.harness.cli("project", "create", "--title", "Vendor", "--goal", "g",
                                    "--criterion", "C1", "--lead", "dlead", user=True)
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(resp["error"]["details"]["milestone"], "M3")
        self.assertEqual(self._rows("SELECT id FROM projects WHERE name = 'Vendor';"), [])

        # An inbox vendor lead is fine; set-lead to the launch vendor lead is rc 13.
        create_agent(self.harness, "vlead", adapter="droid", delivery="inbox", can_lead=True)
        rc, resp = self.harness.cli("intake", "submit", "--title", "Inbox vendor", "--goal", "g",
                                    "--criterion", "C1", "--lead", "vlead", token=self.tok_grok)
        self.assertEqual(rc, 0, resp)
        project_id = resp["data"]["id"]
        rc, resp = self.harness.cli("project", "set-lead", project_id, "dlead",
                                    "--reason", "vendor launch", user=True)
        self.assertEqual(rc, 13, resp)
        self.assertEqual(resp["error"]["code"], "ADAPTER_UNSUPPORTED")
        self.assertEqual(resp["error"]["details"]["milestone"], "M3")
        leads = self._rows("SELECT agent_id FROM lead_assignments WHERE project_id = ? "
                           "AND ended_at IS NULL;", (project_id,))
        self.assertEqual([r["agent_id"] for r in leads], [self._agent_id("vlead")])

        # An existing vendor lead switched to launch behind the CLI's back.
        wconn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            wconn.execute("UPDATE agents SET delivery = 'launch' WHERE id = ?;",
                          (self._agent_id("vlead"),))
            wconn.commit()
        finally:
            wconn.close()
        self._follow_up(project_id, "wake the vendor lead")
        self.assertTrue(
            self.harness.wait_for(lambda: len(self._events(project_id, "lead.turn_refused")) >= 1,
                                  timeout=15.0, step=0.1),
            "no lead.turn_refused event",
        )
        # Further ticks neither start a turn nor repeat the event.
        self.assertFalse(
            self.harness.wait_for(lambda: len(self._events(project_id, "lead.turn_refused")) > 1
                                  or bool(self._lead_turn_runs(project_id)),
                                  timeout=2.5, step=0.1),
            "a second lead.turn_refused event or a lead turn appeared",
        )
        refused = self._events(project_id, "lead.turn_refused")
        payload = json.loads(refused[0]["payload_json"])
        self.assertEqual(payload["reason"], "vendor_lead_m3_lite")
        self.assertEqual(self._lead_turn_runs(project_id), [])
        wake = self._lead_wake_rows(project_id)
        self.assertTrue(wake)
        self.assertEqual({r["status"] for r in wake}, {"pending"})


class TestLeadActionApproval(LeadActionCase):
    """ACC: L2 (approval-gated actions, Quest decision r10b finding 3) - an action item's
    optional top-level `approval` is the CLI's `--approval`."""

    def test_approval_gated_action_needs_a_matching_approval(self) -> None:
        """ACC: L2 - `task.retry` of a task at its attempt limit: without `approval` the
        action is rc 7 `APPROVAL_REQUIRED` and leaves a pending approval requested by the
        lead; after the user approves it, an item naming an approved approval of another
        project is rc 12 `OP_HASH_MISMATCH` (nothing consumed) and the item naming the
        lead's own approval succeeds (task `queued`, approval `consumed`)."""
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        flead_id = self._agent_id("flead")
        project_id = self._create_project("Lead approval action test")

        # A task at its attempt limit: one attempt, started and failed by the inbox agent.
        rc, resp = self.harness.cli("task", "create", "--project", project_id, "--title", "Capped",
                                    "--spec", "s", "--assignee", "ibx", "--max-attempts", "1", user=True)
        self.assertEqual(rc, 0, resp)
        task_id = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task_id) == "queued",
                                              timeout=15.0, step=0.1))
        rc, st = self.harness.cli("task", "start", task_id, token=tok_ibx)
        self.assertEqual(rc, 0, st)
        rc, resp = self.harness.cli("task", "fail", task_id, "--reason", "broken", "--run",
                                    st["data"]["run_id"], "--fence", str(st["data"]["fence"]), token=tok_ibx)
        self.assertEqual(rc, 0, resp)
        self.assertEqual(self._task_status(task_id), "failed")

        # An approved approval of another project.
        rc, resp = self.harness.cli("project", "create", "--title", "Other", "--goal", "g",
                                    "--criterion", "C1", "--lead", "lead1", user=True)
        self.assertEqual(rc, 0, resp)
        other_project = resp["data"]["id"]
        rc, resp = self.harness.cli("approval", "request", "--category", "retry_over_limit",
                                    "--summary", "other", "--operation", json.dumps({"task_id": task_id}),
                                    "--project", other_project, user=True)
        self.assertEqual(rc, 0, resp)
        other = self._rows("SELECT id, op_hash FROM approvals WHERE project_id = ?;", (other_project,))
        self.assertEqual(len(other), 1)
        rc, resp = self.harness.cli("approval", "approve", other[0]["id"], "--op-hash", other[0]["op_hash"],
                                    user=True)
        self.assertEqual(rc, 0, resp)

        # 1. No approval: rc 7, a pending approval requested by the lead.
        self._set_actions(project_id, [{"cmd": "task.retry", "args": {"task": task_id}}], "retry it")
        self.assertTrue(self.harness.wait_for(
            lambda: bool(self._project_action_results(project_id, "task.retry")), timeout=40.0, step=0.1),
            "no task.retry action result")
        first = self._project_action_results(project_id, "task.retry")[0]
        self.assertFalse(first["ok"], first)
        self.assertEqual(first["exit_code"], 7, first)
        self.assertEqual(first["error_code"], "APPROVAL_REQUIRED", first)
        self.assertEqual(self._task_status(task_id), "failed")
        pending = self._rows("SELECT id, op_hash, requested_by FROM approvals WHERE project_id = ? "
                             "AND op_type = 'task.retry' AND status = 'pending';", (project_id,))
        self.assertEqual(len(pending), 1, [dict(p) for p in pending])
        self.assertEqual(pending[0]["requested_by"], flead_id)
        apr_id = pending[0]["id"]
        rc, resp = self.harness.cli("approval", "approve", apr_id, "--op-hash", pending[0]["op_hash"], user=True)
        self.assertEqual(rc, 0, resp)

        # 2. The other project's approval fails; the lead's own approval succeeds.
        self._set_actions(project_id, [
            {"cmd": "task.retry", "args": {"task": task_id}, "approval": other[0]["id"]},
            {"cmd": "task.retry", "args": {"task": task_id}, "approval": apr_id},
        ], "retry with the approval")
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task_id) == "queued",
                                              timeout=40.0, step=0.1), "the approved retry did not run")
        done = [r for r in self._project_action_results(project_id, "task.retry") if r["ok"]]
        self.assertEqual(len(done), 1, done)
        self.assertEqual(done[0]["index"], 1, done)
        same_run = [r for r in self._action_results(done[0]["run_id"]) if r["cmd"] == "task.retry"]
        self.assertEqual([r["index"] for r in same_run], [0, 1])
        self.assertFalse(same_run[0]["ok"])
        self.assertEqual(same_run[0]["exit_code"], 12, same_run[0])
        self.assertEqual(same_run[0]["error_code"], "OP_HASH_MISMATCH", same_run[0])
        self.assertEqual(self._rows("SELECT status FROM approvals WHERE id = ?;", (apr_id,))[0]["status"],
                         "consumed")
        self.assertEqual(self._rows("SELECT status FROM approvals WHERE id = ?;",
                                    (other[0]["id"],))[0]["status"], "approved")


class TestLeadMergeAction(LeadActionCase):
    """ACC: L2 (`task.merge` action, Quest decision r10b finding 2 option b) - checked in
    the close TX under the lead run principal, started after the close commits."""

    needs_git = True

    def test_merge_action_queued_then_merged_and_other_project_refused(self) -> None:
        """ACC: L2 - a turn emits `task.accept` + `task.merge` for a submitted code task of
        its project and `task.merge` for a task of another project: the own merge is a
        `lead.action_result` ok with status `queued`, the merge starts after the turn's
        close (its `task.merging` event follows the result, same request id, actor the
        lead via `run_token`) and the task ends `merged`; the other project's task is
        rc 4 `SCOPE_MISMATCH` and unchanged. A duplicate `task.merge` of the same task is
        also queued. The two asynchronous preflights can finish in either order, but
        exactly one starts and the other's TX1 is refused (the task is already merging
        or merged): one more `lead.action_result` (ok false, `phase:merge_start`) and a
        `lead.action_failed` message to the lead, correlated by request id."""
        assert self.fixture is not None
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        create_agent(self.harness, "lead1", adapter="none", delivery="inbox", can_lead=True)
        flead_id = self._agent_id("flead")
        repo = self.fixture.make_k0("RLM")
        project_id = self._create_project("Lead merge action test", repo=repo)

        rc, resp = self.harness.cli("project", "create", "--title", "Other", "--goal", "g",
                                    "--criterion", "C1", "--lead", "lead1", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("task", "create", "--project", resp["data"]["id"], "--title", "Foreign",
                                    "--spec", "s", user=True)
        self.assertEqual(rc, 0, resp)
        other_task = resp["data"]["id"]
        other_status = self._task_status(other_task)

        # A submitted code task of the lead's project.
        rc, resp = self.harness.cli("task", "create", "--project", project_id, "--title", "Code task",
                                    "--spec", "edit", "--assignee", "ibx", "--kind", "code", user=True)
        self.assertEqual(rc, 0, resp)
        task_id = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task_id) == "queued",
                                              timeout=15.0, step=0.1))
        rc, st = self.harness.cli("task", "start", task_id, token=tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, "feature.txt"), "w", encoding="utf-8") as f:
            f.write("feature\n")
        self.fixture.git(ws, "add", "feature.txt")
        self.fixture.git(ws, "commit", "-m", "Code task", identity=True)
        rc, resp = self.harness.cli("task", "submit", task_id, "--run", st["data"]["run_id"],
                                    "--fence", str(st["data"]["fence"]), "--summary", "done", token=tok_ibx)
        self.assertEqual(rc, 0, resp)

        self._set_actions(project_id, [
            {"cmd": "task.accept", "args": {"task": task_id}},
            {"cmd": "task.merge", "args": {"task": task_id}},
            {"cmd": "task.merge", "args": {"task": task_id}},
            {"cmd": "task.merge", "args": {"task": other_task}},
        ], "merge it")
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(task_id) == "merged",
                                              timeout=60.0, step=0.1),
                        f"task not merged: {self._task_status(task_id)}")

        queued = [r for r in self._project_action_results(project_id, "task.merge") if r["ok"]]
        self.assertEqual([(r["index"], r["exit_code"], r["status"]) for r in queued],
                         [(1, 0, "queued"), (2, 0, "queued")], queued)
        run_id = queued[0]["run_id"]
        self.assertEqual({r["run_id"] for r in queued}, {run_id})
        self.assertEqual(len({r["request_id"] for r in queued}), 2, queued)
        self.assertTrue(all(r["request_id"] for r in queued), queued)
        self.assertTrue(self.harness.wait_for(lambda: len(self._action_results(run_id)) == 5,
                                              timeout=15.0, step=0.1), self._action_results(run_id))
        results = self._action_results(run_id)
        close = results[:4]
        self.assertEqual([(r["index"], r["cmd"], r["ok"]) for r in close],
                         [(0, "task.accept", True), (1, "task.merge", True), (2, "task.merge", True),
                          (3, "task.merge", False)])
        self.assertEqual((close[3]["exit_code"], close[3]["error_code"]), (4, "SCOPE_MISMATCH"))
        merging = self._rows("SELECT * FROM events WHERE type = 'task.merging' AND task_id = ?;", (task_id,))
        self.assertEqual(len(merging), 1)
        # Queue order is asserted above. Worker-thread preflight completion order is
        # not queue order: use the committed TX1 event to identify the winning request.
        started = [r for r in queued if r["request_id"] == merging[0]["request_id"]]
        self.assertEqual(len(started), 1, queued)
        own = started[0]
        duplicates = [r for r in queued if r["request_id"] != own["request_id"]]
        self.assertEqual(len(duplicates), 1, queued)
        duplicate = duplicates[0]
        refused = results[4]
        self.assertEqual((refused["index"], refused["cmd"], refused["ok"], refused["phase"]),
                         (duplicate["index"], "task.merge", False, "merge_start"), refused)
        self.assertIn(refused["exit_code"], (6, 11), refused)
        self.assertEqual(refused["request_id"], duplicate["request_id"])
        failed = self._rows("SELECT recipient_id FROM outbox WHERE type = 'lead.action_failed' "
                            "AND dedup_key = ?;", (f"lead.action_failed:{run_id}:{duplicate['index']}",))
        self.assertEqual([r["recipient_id"] for r in failed], [flead_id])

        self.assertEqual(merging[0]["request_id"], own["request_id"])
        self.assertEqual((merging[0]["actor_id"], merging[0]["via"]), (flead_id, "run_token"))
        self.assertGreater(merging[0]["seq"], close[-1]["_seq"])
        turn = self._rows("SELECT status, ended_at FROM runs WHERE id = ?;", (run_id,))[0]
        self.assertEqual(turn["status"], "exited")
        self.assertLessEqual(turn["ended_at"], merging[0]["ts"])
        self.assertEqual(self._task_status(other_task), other_status)

if __name__ == "__main__":
    unittest.main()
