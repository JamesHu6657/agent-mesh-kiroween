"""Integration regression tests for batch7 g2 findings (FIFO lead merges and project cancel)."""

import json
import os
import unittest
from typing import Any, Dict

import mesh.constants as c
from mesh.core.proc import get_proc_environ, get_proc_stat, is_tgid_alive
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_lead_turns import create_agent


def _pid_dead(pid: int, st: int) -> bool:
    info = get_proc_stat(pid)
    if info is None or int(info["starttime"]) != int(st):
        return True
    return info["state"] in ("Z", "X") and not is_tgid_alive(pid, info)


class TestBatch7G2LeadMergeOrder(LeadActionCase):
    """Finding 5: multiple `task.merge` actions in one lead turn serialize in FIFO order."""

    needs_git = True

    def test_f5_multiple_lead_merge_actions_serialize_in_fifo_order(self) -> None:
        assert self.fixture is not None
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        repo = self.fixture.make_k0("B7G2M")
        project_id = self._create_project("FIFO merge test", repo=repo)

        task_ids = []
        for idx in (1, 2):
            rc, resp = self.harness.cli(
                "task", "create", "--project", project_id, "--title", f"Code task {idx}",
                "--spec", f"edit {idx}", "--assignee", "ibx", "--kind", "code", user=True,
            )
            self.assertEqual(rc, 0, resp)
            tid = resp["data"]["id"]
            task_ids.append(tid)
            self.assertTrue(self.harness.wait_for(lambda t=tid: self._task_status(t) == "queued", timeout=15.0, step=0.1))
            rc, st = self.harness.cli("task", "start", tid, token=tok_ibx)
            self.assertEqual(rc, 0, st)
            ws = st["data"]["workspace"]
            fname = f"feature_{idx}.txt"
            with open(os.path.join(ws, fname), "w", encoding="utf-8") as f:
                f.write(f"feature {idx}\n")
            self.fixture.git(ws, "add", fname)
            self.fixture.git(ws, "commit", "-m", f"Task {idx}", identity=True)
            rc, sub = self.harness.cli(
                "task", "submit", tid, "--run", st["data"]["run_id"],
                "--fence", str(st["data"]["fence"]), "--summary", f"done {idx}", token=tok_ibx,
            )
            self.assertEqual(rc, 0, sub)

        tsk_1, tsk_2 = task_ids
        self._set_actions(
            project_id,
            [
                {"cmd": "task.accept", "args": {"task": tsk_1}},
                {"cmd": "task.accept", "args": {"task": tsk_2}},
                {"cmd": "task.merge", "args": {"task": tsk_1}},
                {"cmd": "task.merge", "args": {"task": tsk_2}},
            ],
            "accept and merge both in order",
        )

        self.assertTrue(
            self.harness.wait_for(
                lambda: self._task_status(tsk_1) == "merged" and self._task_status(tsk_2) == "merged",
                timeout=60.0,
                step=0.1,
            ),
            f"statuses={(self._task_status(tsk_1), self._task_status(tsk_2))}",
        )

        # Verify strict FIFO order: tsk_1 finishes merging BEFORE tsk_2 enters merging, and zero failures.
        ev_t1_merged = self._rows(
            "SELECT seq FROM events WHERE type = 'task.merged' AND task_id = ?;", (tsk_1,)
        )
        ev_t2_merging = self._rows(
            "SELECT seq FROM events WHERE type = 'task.merging' AND task_id = ?;", (tsk_2,)
        )
        self.assertEqual(len(ev_t1_merged), 1)
        self.assertEqual(len(ev_t2_merging), 1)
        self.assertLess(ev_t1_merged[0]["seq"], ev_t2_merging[0]["seq"])

        merge_results = self._project_action_results(project_id, "task.merge")
        self.assertTrue(all(r["ok"] for r in merge_results), merge_results)


class TestBatch7G2ProjectCancelLeadTurn(LeadActionCase):
    """Finding 6: `project cancel` terminates an active `lead_turn` and blocks mutating
    `lead_run_token` commands immediately."""

    def test_f6_project_cancel_terminates_active_lead_turn_and_blocks_token(self) -> None:
        rc, resp = self.harness.cli("config", "set", "kill.grace_s", "8", user=True)
        self.assertEqual(rc, 0, resp)
        rc, resp = self.harness.cli("agent", "update", "flead", "--capability", "intake", user=True)
        self.assertEqual(rc, 0, resp)

        ready_path = os.path.join(self.stubs_dir, "f6-sigign-ready")
        project_id = self._create_project(
            _fake_goal({"steps": [{"spawn_envless_member": {"path": ready_path}}, {"ignore_sigterm": {}}]})
        )
        turn = self._wait_turn_running(project_id)
        self.assertTrue(
            self.harness.wait_for(lambda: os.path.exists(ready_path), timeout=15.0, step=0.05),
            "lead turn did not finish SIGTERM-ignore setup in time",
        )
        with open(ready_path, encoding="utf-8") as f:
            member_pid = int(f.readline().strip())
        member_info = get_proc_stat(member_pid)
        self.assertIsNotNone(member_info)
        assert member_info is not None
        member_st = int(member_info["starttime"])

        env: Dict[str, str] = {}

        def token_ready() -> bool:
            nonlocal env
            got = get_proc_environ(turn["wrapper_pid"])
            if got and got.get("MESH_TOKEN"):
                env = got
                return True
            return False

        self.assertTrue(self.harness.wait_for(token_ready, timeout=10.0, step=0.05))
        token = env["MESH_TOKEN"]

        rc, resp = self.harness.cli("project", "cancel", project_id, user=True)
        self.assertEqual(rc, 0, resp)

        # Within the kill grace window: mutating lead_run_token commands get rc 6 PROJECT_NOT_ACTIVE.
        for cmd_args in (
            ("task", "create", "--project", project_id, "--title", "late", "--spec", "s"),
            ("workspace", "prune", "--project", project_id),
            ("intake", "submit", "--project", project_id, "--message", "late follow-up"),
        ):
            rc_m, resp_m = self.harness.cli(*cmd_args, token=token)
            self.assertEqual(rc_m, c.EXIT_CONFLICT, (cmd_args, resp_m))
            self.assertEqual(resp_m["error"]["code"], c.PROJECT_NOT_ACTIVE, (cmd_args, resp_m))

        # Termination finishes after grace_s -> killed with end_reason='project_cancelled', lease freed, token revoked.
        self.assertTrue(
            self.harness.wait_for(
                lambda: self._rows("SELECT status FROM runs WHERE id = ?;", (turn["id"],))[0]["status"] == "killed",
                timeout=30.0,
                step=0.1,
            )
        )
        self.assertTrue(
            self.harness.wait_for(lambda: _pid_dead(member_pid, member_st), timeout=10.0, step=0.05),
            f"expected lead-turn member {member_pid} to be dead: {get_proc_stat(member_pid)}",
        )
        closed = self._rows("SELECT status, end_reason FROM runs WHERE id = ?;", (turn["id"],))[0]
        self.assertEqual((closed["status"], closed["end_reason"]), ("killed", "project_cancelled"))
        self.assertEqual(
            len(self._rows("SELECT 1 FROM leases WHERE key = ?;", (f"leadturn:{project_id}",))),
            0,
        )
        rc_after, resp_after = self.harness.cli("project", "show", project_id, token=token)
        self.assertEqual(rc_after, c.EXIT_UNAUTHENTICATED, resp_after)

        wakes = self._rows("SELECT status FROM outbox WHERE project_id = ? AND channel = 'lead_wake';", (project_id,))
        self.assertTrue(all(r["status"] == "superseded" for r in wakes), [dict(r) for r in wakes])
        unreachable = self._rows(
            "SELECT 1 FROM events WHERE type = 'lead.unreachable' AND project_id = ?;", (project_id,)
        )
        self.assertEqual(len(unreachable), 0)


if __name__ == "__main__":
    unittest.main()
