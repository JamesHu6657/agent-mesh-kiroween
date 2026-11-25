"""Integration regression tests for batch7 PR #43 round-2 review findings."""

import json
import os
import sys
import tempfile
import unittest

import mesh.constants as c
from tests.helpers import MeshHarness
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_tasks import create_agent


def _wait_task_settled(test: unittest.TestCase, h: MeshHarness, task_id: str, status: str) -> None:
    test.assertTrue(
        h.wait_for(
            lambda: h.cli("task", "show", task_id, user=True)[1]["data"]["status"] == status,
            timeout=25.0,
            step=0.1,
        ),
        f"task {task_id} did not reach status {status}",
    )


class TestBatch7PR43R2CLI(unittest.TestCase):
    """CLI + daemon integration tests for Finding 2."""

    def test_f2_empty_allowed_efforts_and_default_effort_null_update(self) -> None:
        """Finding 2: agent update requires clearing default_effort before [] allowlist, and honors JSON null."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            create_agent(
                h,
                "w_eff",
                adapter="fake",
                delivery="launch",
                allowed_models=["m1"],
                default_model="m1",
                allowed_efforts=["low", "high"],
                default_effort="high",
            )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "F2-empty", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            fd, path_only = tempfile.mkstemp(prefix="ag-upd-only-", suffix=".json", dir=h.home)
            os.close(fd)
            with open(path_only, "w", encoding="utf-8") as f:
                json.dump({"allowed_efforts": []}, f)
            rc_bad, resp_bad = h.cli("agent", "update", "w_eff", "--input", path_only, user=True)
            self.assertEqual(rc_bad, c.EXIT_USAGE, resp_bad)
            self.assertEqual(resp_bad["error"]["code"], c.EFFORT_NOT_ALLOWED)

            # Updating another flag without --default-effort preserves non-null default_effort.
            rc_keep, resp_keep = h.cli("agent", "update", "w_eff", "--max-concurrency", "2", user=True)
            self.assertEqual(rc_keep, 0, resp_keep)
            self.assertEqual(resp_keep["data"]["default_effort"], "high")

            # Explicit JSON default_effort: null clears both default_effort and allowed_efforts.
            fd2, path_both = tempfile.mkstemp(prefix="ag-upd-both-", suffix=".json", dir=h.home)
            os.close(fd2)
            with open(path_both, "w", encoding="utf-8") as f:
                json.dump({"allowed_efforts": [], "default_effort": None}, f)
            rc_clr, resp_clr = h.cli("agent", "update", "w_eff", "--input", path_both, user=True)
            self.assertEqual(rc_clr, 0, resp_clr)
            self.assertIsNone(resp_clr["data"]["default_effort"])
            self.assertEqual(resp_clr["data"]["allowed_efforts"], [])

            # Task create with --effort on [] allowlist fails EFFORT_NOT_ALLOWED.
            rc_tc, resp_tc = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Effort on empty",
                "--spec", 'FAKE {"steps": [{"succeed": {}}]}',
                "--assignee", "w_eff",
                "--effort", "low",
                token=tok_lead,
            )
            self.assertEqual(rc_tc, c.EXIT_USAGE, resp_tc)
            self.assertEqual(resp_tc["error"]["code"], c.EFFORT_NOT_ALLOWED)
        finally:
            h.teardown()

    def test_f2_inbox_task_reject_and_start_recheck_allowed_efforts(self) -> None:
        """Finding 2: inbox-mode task reject and task start recheck tightened and [] allowed_efforts."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            tok_ibx = create_agent(
                h,
                "ibx_e",
                adapter="fake",
                delivery="inbox",
                allowed_models=["m1"],
                default_model="m1",
                allowed_efforts=["low", "high"],
                default_effort="low",
            )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "F2-inbox", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            # Part A: submitted inbox task rejected after allowed_efforts narrowed/emptied.
            rc_t1, resp_t1 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Inbox reject test",
                "--spec", "S",
                "--assignee", "ibx_e",
                "--effort", "high",
                token=tok_lead,
            )
            self.assertEqual(rc_t1, 0, resp_t1)
            tsk_rej = resp_t1["data"]["id"]
            _wait_task_settled(self, h, tsk_rej, "queued")

            rc_st1, resp_st1 = h.cli("task", "start", tsk_rej, token=tok_ibx)
            self.assertEqual(rc_st1, 0, resp_st1)
            rc_sub1, resp_sub1 = h.cli(
                "task", "submit", tsk_rej,
                "--run", resp_st1["data"]["run_id"],
                "--fence", str(resp_st1["data"]["fence"]),
                "--summary", "v1",
                token=tok_ibx,
            )
            self.assertEqual(rc_sub1, 0, resp_sub1)

            # Create second queued inbox task while high is still allowed.
            rc_t2, resp_t2 = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Inbox start test",
                "--spec", "S",
                "--assignee", "ibx_e",
                "--effort", "high",
                token=tok_lead,
            )
            self.assertEqual(rc_t2, 0, resp_t2)
            tsk_st = resp_t2["data"]["id"]
            _wait_task_settled(self, h, tsk_st, "queued")

            # Tighten ibx_e allowed_efforts -> ['low'].
            rc_u1, resp_u1 = h.cli(
                "agent", "update", "ibx_e", "--allowed-effort", "low", "--default-effort", "low", user=True
            )
            self.assertEqual(rc_u1, 0, resp_u1)

            # 1. inbox `task reject` fails EFFORT_NOT_ALLOWED and keeps task submitted.
            rc_rj, resp_rj = h.cli("task", "reject", tsk_rej, "--reason", "rework", token=tok_lead)
            self.assertEqual(rc_rj, c.EXIT_USAGE, resp_rj)
            self.assertEqual(resp_rj["error"]["code"], c.EFFORT_NOT_ALLOWED)
            self.assertEqual(h.cli("task", "show", tsk_rej, user=True)[1]["data"]["status"], "submitted")

            # 2. inbox `task start` fails EFFORT_NOT_ALLOWED and keeps task queued with 0 runs.
            rc_st2, resp_st2 = h.cli("task", "start", tsk_st, token=tok_ibx)
            self.assertEqual(rc_st2, c.EXIT_USAGE, resp_st2)
            self.assertEqual(resp_st2["error"]["code"], c.EFFORT_NOT_ALLOWED)
            conn = h.db()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tsk_st,)).fetchone()[0],
                    0,
                )
            finally:
                conn.close()

            # Also verify clearing allowed_efforts to [] blocks both reject and start.
            fd, clr_path = tempfile.mkstemp(prefix="ibx-clr-", suffix=".json", dir=h.home)
            os.close(fd)
            with open(clr_path, "w", encoding="utf-8") as f:
                json.dump({"allowed_efforts": [], "default_effort": None}, f)
            self.assertEqual(h.cli("agent", "update", "ibx_e", "--input", clr_path, user=True)[0], 0)

            rc_rj_empty, resp_rj_empty = h.cli("task", "reject", tsk_rej, "--reason", "rework", token=tok_lead)
            self.assertEqual(rc_rj_empty, c.EXIT_USAGE, resp_rj_empty)
            self.assertEqual(resp_rj_empty["error"]["code"], c.EFFORT_NOT_ALLOWED)

            rc_st_empty, resp_st_empty = h.cli("task", "start", tsk_st, token=tok_ibx)
            self.assertEqual(rc_st_empty, c.EXIT_USAGE, resp_st_empty)
            self.assertEqual(resp_st_empty["error"]["code"], c.EFFORT_NOT_ALLOWED)
        finally:
            h.teardown()

    def test_f2_launch_queued_task_fails_t6_when_allowed_efforts_narrowed(self) -> None:
        """Finding 2: a queued launch task fails T6 effort_not_allowed rather than spawning with removed effort."""
        h = MeshHarness(init_db=True, start_daemon=True)
        try:
            tok_lead = create_agent(h, "lead1", adapter="none", delivery="inbox", can_lead=True)
            create_agent(
                h,
                "ln_e",
                adapter="fake",
                delivery="launch",
                allowed_models=["m1"],
                default_model="m1",
                allowed_efforts=["low", "high"],
                default_effort="low",
                max_concurrency=1,
            )
            self.assertEqual(h.cli("config", "set", "lead.default_agent", '"lead1"', user=True)[0], 0)
            rc_p, resp_p = h.cli(
                "intake", "submit", "--title", "F2-t6", "--goal", "G", "--criterion", "c1", user=True
            )
            self.assertEqual(rc_p, 0, resp_p)
            prj_id = resp_p["data"]["id"]

            gate = os.path.join(h.home, "ln-busy-gate")
            rc_b, resp_b = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Busy",
                "--spec", _fake_goal({"steps": [{"wait_release": {"path": gate}}]}),
                "--assignee", "ln_e",
                "--effort", "low",
                token=tok_lead,
            )
            self.assertEqual(rc_b, 0, resp_b)
            tsk_busy = resp_b["data"]["id"]
            self.assertTrue(
                h.wait_for(
                    lambda: h.cli("task", "show", tsk_busy, user=True)[1]["data"]["status"] == "running",
                    timeout=15.0,
                    step=0.1,
                )
            )

            # Second task sits queued with --effort high while slot is busy.
            rc_q, resp_q = h.cli(
                "task", "create",
                "--project", prj_id,
                "--title", "Queued high",
                "--spec", 'FAKE {"steps": [{"succeed": {}}]}',
                "--assignee", "ln_e",
                "--effort", "high",
                token=tok_lead,
            )
            self.assertEqual(rc_q, 0, resp_q)
            tsk_q = resp_q["data"]["id"]
            _wait_task_settled(self, h, tsk_q, "queued")

            # Narrow allowed_efforts -> ['low'] -> T6 fails tsk_q before spawning any run.
            self.assertEqual(
                h.cli("agent", "update", "ln_e", "--allowed-effort", "low", "--default-effort", "low", user=True)[0],
                0,
            )
            _wait_task_settled(self, h, tsk_q, "failed")
            rc_sh, resp_sh = h.cli("task", "show", tsk_q, user=True)
            self.assertEqual(rc_sh, 0, resp_sh)
            self.assertEqual(resp_sh["data"]["failure"]["reason"], "effort_not_allowed")
            conn = h.db()
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?;", (tsk_q,)).fetchone()[0],
                    0,
                )
            finally:
                conn.close()

            with open(gate, "w", encoding="utf-8") as f:
                f.write("release\n")
            _wait_task_settled(self, h, tsk_busy, "submitted")
        finally:
            h.teardown()


class TestBatch7PR43R2LeadMerges(LeadActionCase):
    """Lead-turn queued FIFO merge integration tests for Finding 1 and Finding 3."""

    needs_git = True

    def _create_test_cmd_project(self, goal: str, test_argv: list[str], repo: str) -> str:
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", "FIFO Merge Project",
            "--goal", goal,
            "--criterion", "C1",
            "--lead", "flead",
            "--repo", repo,
            "--base-branch", "main",
            "--test-command", json.dumps(test_argv),
            token=self.tok_grok,
        )
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def _submit_and_accept_code_task(
        self, project_id: str, tok_ibx: str, title: str, filename: str
    ) -> str:
        assert self.fixture is not None
        rc, resp = self.harness.cli(
            "task", "create",
            "--project", project_id,
            "--title", title,
            "--spec", f"create {filename}",
            "--assignee", "ibx",
            "--kind", "code",
            user=True,
        )
        self.assertEqual(rc, 0, resp)
        tid = resp["data"]["id"]
        self.assertTrue(self.harness.wait_for(lambda: self._task_status(tid) == "queued", timeout=15.0, step=0.1))
        rc_s, st = self.harness.cli("task", "start", tid, token=tok_ibx)
        self.assertEqual(rc_s, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, filename), "w", encoding="utf-8") as f:
            f.write(filename + "\n")
        self.fixture.git(ws, "add", filename)
        self.fixture.git(ws, "commit", "-m", f"Add {filename}", identity=True)
        self.assertEqual(
            self.harness.cli(
                "task", "submit", tid,
                "--run", st["data"]["run_id"],
                "--fence", str(st["data"]["fence"]),
                "--summary", f"added {filename}",
                token=tok_ibx,
            )[0],
            0,
        )
        self.assertEqual(self.harness.cli("task", "accept", tid, user=True)[0], 0)
        return tid

    def test_f1_disabled_lead_queued_merge_does_not_run_tests_or_merge(self) -> None:
        """Finding 1: disabling lead while FIFO merge #1 holds prevents queued merge #2 from testing or merging."""
        assert self.fixture is not None
        tok_ibx = create_agent(self.harness, "ibx", adapter="none", delivery="inbox")
        repo = self.fixture.make_k0("PR43R2F1")

        f1_started = os.path.join(self.stubs_dir, "f1_started")
        f1_release = os.path.join(self.stubs_dir, "f1_release")
        f2_ran = os.path.join(self.stubs_dir, "f2_ran")
        script = (
            "import os, time\n"
            f"f1_s = {f1_started!r}\n"
            f"f1_r = {f1_release!r}\n"
            f"f2_r = {f2_ran!r}\n"
            "if os.path.exists('f2.txt'):\n"
            "    open(f2_r, 'w').write('ran\\n')\n"
            "elif os.path.exists('f1.txt'):\n"
            "    open(f1_s, 'w').write('started\\n')\n"
            "    deadline = time.monotonic() + 25.0\n"
            "    while not os.path.exists(f1_r) and time.monotonic() < deadline:\n"
            "        time.sleep(0.05)\n"
        )
        project_id = self._create_test_cmd_project("initial goal", [sys.executable, "-c", script], repo)

        t1 = self._submit_and_accept_code_task(project_id, tok_ibx, "Task 1", "f1.txt")
        t2 = self._submit_and_accept_code_task(project_id, tok_ibx, "Task 2", "f2.txt")

        self._set_actions(
            project_id,
            [
                {"cmd": "task.merge", "args": {"task": t1}},
                {"cmd": "task.merge", "args": {"task": t2}},
            ],
            "merge t1 then t2",
        )

        # Wait until t1 is actively inside test_command (so t2 is queued behind it).
        self.assertTrue(self.harness.wait_for(lambda: os.path.exists(f1_started), timeout=25.0, step=0.05))

        # Disable the lead while t1 is mid-test and t2 is queued in _project_merge_queues.
        rc_dis, resp_dis = self.harness.cli("agent", "disable", "flead", user=True)
        self.assertEqual(rc_dis, 0, resp_dis)

        # Release t1 so t1 merges cleanly and the drain reaches t2.
        with open(f1_release, "w", encoding="utf-8") as f:
            f.write("go\n")

        self.assertTrue(
            self.harness.wait_for(
                lambda: (
                    self._task_status(t1) == "merged"
                    and any(
                        r.get("ok") is False and r.get("phase") == "merge_start"
                        for r in self._project_action_results(project_id, "task.merge")
                    )
                ),
                timeout=25.0,
                step=0.1,
            ),
            "expected t1 to merge and t2 queued merge to record failure",
        )

        # t2 must NOT run tests and must remain accepted (unmerged).
        self.assertFalse(os.path.exists(f2_ran), "disabled lead queued merge ran test_command for t2")
        self.assertEqual(self._task_status(t2), "accepted")
        failures = [
            r for r in self._project_action_results(project_id, "task.merge")
            if r.get("ok") is False and r.get("phase") == "merge_start"
        ]
        self.assertEqual(len(failures), 1, failures)
        self.assertEqual(failures[0]["error_code"], c.TOKEN_INVALID)


if __name__ == "__main__":
    unittest.main()
