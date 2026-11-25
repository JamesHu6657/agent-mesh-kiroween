"""Integration regression tests for batch7 PR #43 review findings (r1)."""

import json
import os
import sqlite3
import unittest

import mesh.constants as c
from mesh.core.proc import get_proc_environ
from tests.helpers import MeshHarness
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_tasks import create_agent


def _closed_runs(h: MeshHarness, task_id: str):
    conn = h.db()
    try:
        return conn.execute(
            "SELECT id, status, model, effort, profile, attempt FROM runs WHERE task_id = ? ORDER BY rowid ASC;",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()


def _wait_task_settled(test: unittest.TestCase, h: MeshHarness, task_id: str, status: str, run_count: int) -> None:
    test.assertTrue(
        h.wait_for(
            lambda: (
                h.cli("task", "show", task_id, user=True)[1]["data"]["status"] == status
                and len(_closed_runs(h, task_id)) == run_count
                and all(r["status"] not in ("starting", "running") for r in _closed_runs(h, task_id))
            ),
            timeout=25.0,
            step=0.1,
        ),
        f"task {task_id} did not settle to {status} with {run_count} closed runs",
    )


class TestBatch7PR43R1CLI(unittest.TestCase):
    """CLI + daemon integration tests for P1-1, P1-2, P2-3."""

    def test_p1_1_task_reject_permission_escalation_and_compound_gate(self) -> None:
        """P1-1: `task reject` checks `permission_escalation` (direct_only) and uses compound gate at attempt cap."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            tok_rel = create_agent(h, "rel1", adapter="none", delivery="inbox", capabilities=["relay"])
            create_agent(
                h,
                "w1",
                adapter="fake",
                delivery="launch",
                default_profile="edit",
                max_unapproved_profile="edit",
            )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "P1-1", "--goal", "G", "--criterion", "c1", token=tok_intake
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            spec_sub = 'FAKE {"steps": [{"succeed": {}}]}'
            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Escalated submit",
                "--spec", spec_sub,
                "--assignee", "w1",
                "--profile", "unrestricted",
                "--max-attempts", "2",
                user=True,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]
            _wait_task_settled(self, h, tsk_id, "submitted", 1)

            # 1. Attempt 1 < max_attempts (2): reject by lead MUST require permission_escalation (direct_only).
            rc_r1, resp_r1 = h.cli("task", "reject", tsk_id, "--reason", "run new cmds", token=tok_lead)
            self.assertEqual(rc_r1, c.EXIT_APPROVAL_REQUIRED, resp_r1)
            self.assertEqual(resp_r1["error"]["details"]["category"], "permission_escalation")
            apr_1 = resp_r1["error"]["details"]["approval_id"]
            hash_1 = resp_r1["error"]["details"]["op_hash"]

            # Relayed approval must be rejected for direct_only permission_escalation.
            rc_rel1, resp_rel1 = h.cli(
                "approval", "approve", apr_1, "--op-hash", hash_1,
                "--relayed", "--user-statement", "ok", token=tok_rel,
            )
            self.assertEqual(rc_rel1, c.EXIT_APPROVAL_INVALID, resp_rel1)
            self.assertEqual(resp_rel1["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            # Direct user approval + lead re-run starts attempt 2 cleanly.
            conn = h.db()
            try:
                op_doc = json.loads(
                    conn.execute("SELECT op_json FROM approvals WHERE id = ?;", (apr_1,)).fetchone()["op_json"]
                )
            finally:
                conn.close()
            self.assertEqual(op_doc["params"]["assignee"], "w1")
            self.assertEqual(op_doc["params"]["profile"], "unrestricted")

            self.assertEqual(h.cli("approval", "approve", apr_1, "--op-hash", hash_1, user=True)[0], 0)
            rc_r1_ok, resp_r1_ok = h.cli(
                "task", "reject", tsk_id, "--reason", "run new cmds", "--approval", apr_1, token=tok_lead
            )
            self.assertEqual(rc_r1_ok, 0, resp_r1_ok)
            _wait_task_settled(self, h, tsk_id, "submitted", 2)

            # 2. Attempt 2 >= max_attempts (2): compound gate (`retry_over_limit` + `permission_escalation`).
            rc_r2, resp_r2 = h.cli("task", "reject", tsk_id, "--reason", "round 3", token=tok_lead)
            self.assertEqual(rc_r2, c.EXIT_APPROVAL_REQUIRED, resp_r2)
            apr_2 = resp_r2["error"]["details"]["approval_id"]
            hash_2 = resp_r2["error"]["details"]["op_hash"]

            # Relayer MUST NOT be allowed to approve compound task.reject!
            rc_rel2, resp_rel2 = h.cli(
                "approval", "approve", apr_2, "--op-hash", hash_2,
                "--relayed", "--user-statement", "ok", token=tok_rel,
            )
            self.assertEqual(rc_rel2, c.EXIT_APPROVAL_INVALID, resp_rel2)
            self.assertEqual(resp_rel2["error"]["code"], c.CHANNEL_NOT_ALLOWED)

            # Direct user approval + lead re-run launches attempt 3.
            self.assertEqual(h.cli("approval", "approve", apr_2, "--op-hash", hash_2, user=True)[0], 0)
            rc_r2_ok, resp_r2_ok = h.cli(
                "task", "reject", tsk_id, "--reason", "round 3", "--approval", apr_2, token=tok_lead
            )
            self.assertEqual(rc_r2_ok, 0, resp_r2_ok)
            _wait_task_settled(self, h, tsk_id, "submitted", 3)
        finally:
            h.teardown()

    def test_p1_2_task_retry_op_hash_binds_assignee_and_profile(self) -> None:
        """P1-2: `task retry` approval `op_hash` binds `assignee` and `profile`."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            for name in ("w1", "w2"):
                create_agent(
                    h,
                    name,
                    adapter="fake",
                    delivery="launch",
                    default_model="m1",
                    allowed_models=["m1"],
                    default_effort="low",
                    allowed_efforts=["low"],
                    default_profile="edit",
                    max_unapproved_profile="edit",
                )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli(
                "intake", "submit", "--title", "P1-2", "--goal", "G", "--criterion", "c1", token=tok_intake
            )[1]["data"]["id"]

            rc_t, resp_t = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Bind assignee+profile",
                "--spec", 'FAKE {"fail": {"code": 1}}',
                "--assignee", "w1",
                "--profile", "dev",
                "--max-attempts", "4",
                user=True,
            )
            self.assertEqual(rc_t, 0, resp_t)
            tsk_id = resp_t["data"]["id"]
            _wait_task_settled(self, h, tsk_id, "failed", 1)

            # Lead requests retry for (w1, dev) and user approves apr_1.
            rc_ret1, resp_ret1 = h.cli("task", "retry", tsk_id, "--reason", "retry-fix", token=tok_lead)
            self.assertEqual(rc_ret1, c.EXIT_APPROVAL_REQUIRED, resp_ret1)
            apr_1 = resp_ret1["error"]["details"]["approval_id"]
            hash_1 = resp_ret1["error"]["details"]["op_hash"]
            self.assertEqual(h.cli("approval", "approve", apr_1, "--op-hash", hash_1, user=True)[0], 0)

            # User temporarily reassigns to w2 with unrestricted profile and runs attempt 2 (fails).
            rc_re, resp_re = h.cli(
                "task", "reassign", tsk_id, "--to", "w2", "--profile", "unrestricted", "--reason", "diag", user=True
            )
            self.assertEqual(rc_re, 0, resp_re)
            _wait_task_settled(self, h, tsk_id, "failed", 2)

            # Lead replays apr_1 on (w2, unrestricted): MUST fail with OP_HASH_MISMATCH!
            rc_replay, resp_replay = h.cli(
                "task", "retry", tsk_id, "--reason", "retry-fix", "--approval", apr_1, token=tok_lead
            )
            self.assertEqual(rc_replay, c.EXIT_APPROVAL_INVALID, resp_replay)
            self.assertEqual(resp_replay["error"]["code"], c.OP_HASH_MISMATCH)
            self.assertEqual(len(_closed_runs(h, tsk_id)), 2)
        finally:
            h.teardown()

    def test_p2_3_task_retry_and_reject_recheck_effort_allowlist(self) -> None:
        """P2-3: `task retry` and launch `task reject` without `--effort` recheck `allowed_efforts`, while model removal stays deferred to T6."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_intake = create_agent(h, "plain", adapter="none", delivery="inbox", capabilities=["intake"])
            create_agent(
                h,
                "w2",
                adapter="fake",
                delivery="launch",
                default_model="m1",
                allowed_models=["m1", "m2"],
                default_effort="low",
                allowed_efforts=["low", "high"],
            )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            prj_id = h.cli(
                "intake", "submit", "--title", "P2-3", "--goal", "G", "--criterion", "c1", token=tok_intake
            )[1]["data"]["id"]

            # Create failed task (at cap 1/1) and submitted task, both with (m2, high).
            rc_tf, resp_tf = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Fail m2/high",
                "--spec", 'FAKE {"fail": {"code": 1}}',
                "--assignee", "w2",
                "--model", "m2",
                "--effort", "high",
                "--max-attempts", "1",
                user=True,
            )
            self.assertEqual(rc_tf, 0, resp_tf)
            tsk_f = resp_tf["data"]["id"]

            rc_ts, resp_ts = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Sub m2/high",
                "--spec", 'FAKE {"steps": [{"succeed": {}}]}',
                "--assignee", "w2",
                "--model", "m2",
                "--effort", "high",
                "--max-attempts", "3",
                user=True,
            )
            self.assertEqual(rc_ts, 0, resp_ts)
            tsk_s = resp_ts["data"]["id"]

            _wait_task_settled(self, h, tsk_f, "failed", 1)
            _wait_task_settled(self, h, tsk_s, "submitted", 1)

            # Tighten allowed_efforts -> ['low'] while keeping m2 allowed.
            rc_u, resp_u = h.cli(
                "agent", "update", "w2", "--allowed-effort", "low", user=True
            )
            self.assertEqual(rc_u, 0, resp_u)

            # 1a. `task retry` without --effort fails with EFFORT_NOT_ALLOWED BEFORE creating approval.
            rc_rfe, resp_rfe = h.cli("task", "retry", tsk_f, "--reason", "retry", token=tok_lead)
            self.assertEqual(rc_rfe, c.EXIT_USAGE, resp_rfe)
            self.assertEqual(resp_rfe["error"]["code"], c.EFFORT_NOT_ALLOWED)
            conn = h.db()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM approvals;").fetchone()[0], 0)
            finally:
                conn.close()

            # 1b. `task reject` on launch task without --effort fails with EFFORT_NOT_ALLOWED (stays submitted, no new run).
            rc_rje, resp_rje = h.cli("task", "reject", tsk_s, "--reason", "redo", token=tok_lead)
            self.assertEqual(rc_rje, c.EXIT_USAGE, resp_rje)
            self.assertEqual(resp_rje["error"]["code"], c.EFFORT_NOT_ALLOWED)
            self.assertEqual(h.cli("task", "show", tsk_s, user=True)[1]["data"]["status"], "submitted")
            self.assertEqual(len(_closed_runs(h, tsk_s)), 1)

            # 1c. Passing allowed `--effort low` on retry proceeds cleanly to the attempt-cap approval gate.
            rc_ok, resp_ok = h.cli("task", "retry", tsk_f, "--effort", "low", "--reason", "retry", token=tok_lead)
            self.assertEqual(rc_ok, c.EXIT_APPROVAL_REQUIRED, resp_ok)

            # 2. Disallowing m2 after creation: retry without --model queues and is caught by T6 (no launch with m2).
            rc_tf2, resp_tf2 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Fail m2/low",
                "--spec", 'FAKE {"fail": {"code": 1}}',
                "--assignee", "w2",
                "--model", "m2",
                "--effort", "low",
                "--max-attempts", "3",
                user=True,
            )
            self.assertEqual(rc_tf2, 0, resp_tf2)
            tsk_m = resp_tf2["data"]["id"]
            _wait_task_settled(self, h, tsk_m, "failed", 1)

            self.assertEqual(h.cli("agent", "update", "w2", "--allowed-model", "m1", user=True)[0], 0)
            rc_rm, resp_rm = h.cli("task", "retry", tsk_m, "--reason", "retry", token=tok_lead)
            self.assertEqual(rc_rm, 0, resp_rm)
            self.assertTrue(
                h.wait_for(
                    lambda: h.cli("task", "show", tsk_m, user=True)[1]["data"]["status"] == "failed",
                    timeout=10.0,
                    step=0.1,
                )
            )
            self.assertEqual(len(_closed_runs(h, tsk_m)), 1)  # Caught by T6 without spawning a second run.
        finally:
            h.teardown()


class TestBatch7PR43R1LeadActions(LeadActionCase):
    """P2-1 integration tests: `project.complete` + `project.deliver` and `paused` ops via `run_token`."""

    needs_git = True

    def test_p2_1_complete_then_deliver_in_single_lead_turn(self) -> None:
        assert self.fixture is not None
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        repo = self.fixture.make_k0("PR43R1P21")
        project_id = self._create_project("Complete+Deliver", repo=repo)

        rc, resp = self.harness.cli(
            "task", "create", "--project", project_id, "--title", "Code task",
            "--spec", "add feature", "--assignee", "ibx", "--kind", "code", user=True,
        )
        self.assertEqual(rc, 0, resp)
        tsk_id = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(tsk_id) == "queued", timeout=15.0, step=0.1))

        rc, st = self.harness.cli("task", "start", tsk_id, token=tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, "delivered.txt"), "w", encoding="utf-8") as f:
            f.write("ready\n")
        self.fixture.git(ws, "add", "delivered.txt")
        self.fixture.git(ws, "commit", "-m", "Add delivered file", identity=True)
        self.assertEqual(
            self.harness.cli(
                "task", "submit", tsk_id, "--run", st["data"]["run_id"],
                "--fence", str(st["data"]["fence"]), "--summary", "done", token=tok_ibx,
            )[0],
            0,
        )
        self.assertEqual(self.harness.cli("task", "accept", tsk_id, user=True)[0], 0)
        self.assertEqual(self.harness.cli("task", "merge", tsk_id, user=True)[0], 0)
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(tsk_id) == "merged", timeout=30.0, step=0.1))

        crit_rows = self._rows(
            "SELECT id FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL;",
            (project_id,),
        )
        report = {
            "summary": "All done",
            "deliverables": ["delivered.txt"],
            "criteria": [{"id": r["id"], "met": True, "evidence": "merged"} for r in crit_rows],
        }
        self._set_actions(
            project_id,
            [
                {"cmd": "project.complete", "args": {"report": report}},
                {"cmd": "project.deliver", "args": {}},
            ],
            "finalize project",
        )

        self.assertTrue(
            self.harness.wait_for(
                lambda: (
                    self._rows("SELECT status FROM projects WHERE id = ?;", (project_id,))[0]["status"] == "completed"
                    and len(self._project_action_results(project_id, "project.deliver")) >= 1
                ),
                timeout=30.0,
                step=0.1,
            ),
            "lead turn did not finish project.complete + project.deliver",
        )

        for cmd in ("project.complete", "project.deliver"):
            results = self._project_action_results(project_id, cmd)
            self.assertEqual(len(results), 1, (cmd, results))
            self.assertTrue(results[0]["ok"], (cmd, results))

        self.assertIn("delivered.txt", self.fixture.git(repo, "ls-tree", "-r", "--name-only", "main").stdout)
        failed_wakes = self._rows(
            "SELECT * FROM outbox WHERE project_id = ? AND type = 'lead.action_failed';", (project_id,)
        )
        self.assertEqual(len(failed_wakes), 0, [dict(r) for r in failed_wakes])

    def test_p2_1_live_lead_run_token_allowed_on_paused_and_blocked_on_cancelled(self) -> None:
        gate_path = os.path.join(self.stubs_dir, "p21-paused-gate")
        tok_ibx = create_agent(self.harness, "ibx_p", adapter="none", delivery="inbox")
        project_id = self._create_project(_fake_goal({"steps": [{"wait_release": {"path": gate_path}}]}))
        turn = self._wait_turn_running(project_id)

        env = {}

        def _got_token() -> bool:
            nonlocal env
            got = get_proc_environ(turn["wrapper_pid"])
            if got and got.get("MESH_TOKEN"):
                env = got
                return True
            return False

        self.assertTrue(self.harness.wait_for(_got_token, timeout=10.0, step=0.05))
        run_tok = env["MESH_TOKEN"]

        self.assertEqual(self.harness.cli("project", "pause", project_id, user=True)[0], 0)

        # Paused project permits run_token mutating commands that persistent lead tokens permit.
        rc_tc, resp_tc = self.harness.cli(
            "task", "create", "--project", project_id, "--title", "While paused",
            "--spec", "s", "--assignee", "ibx_p", token=run_tok,
        )
        self.assertEqual(rc_tc, 0, resp_tc)
        rc_wp, resp_wp = self.harness.cli("workspace", "prune", "--project", project_id, token=run_tok)
        self.assertEqual(rc_wp, 0, resp_wp)

        # Release lead turn cleanly before teardown, or cancel project and verify cancel blocks run_token.
        rc_can, resp_can = self.harness.cli("project", "cancel", project_id, user=True)
        self.assertEqual(rc_can, 0, resp_can)
        rc_blk, resp_blk = self.harness.cli(
            "workspace", "prune", "--project", project_id, token=run_tok
        )
        # Either authz blocks on PROJECT_NOT_ACTIVE immediately or termination already revoked the token.
        self.assertIn(resp_blk["error"]["code"], (c.PROJECT_NOT_ACTIVE, c.TOKEN_REVOKED), resp_blk)
        self.assertIn(rc_blk, (c.EXIT_CONFLICT, c.EXIT_UNAUTHENTICATED), resp_blk)
        with open(gate_path, "w", encoding="utf-8") as f:
            f.write("release\n")
        self.assertTrue(
            self.harness.wait_for(
                lambda: self._lead_turn_runs(project_id)[0]["status"] == "killed",
                timeout=20.0,
                step=0.1,
            )
        )


if __name__ == "__main__":
    unittest.main()
