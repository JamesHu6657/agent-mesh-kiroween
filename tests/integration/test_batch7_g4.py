"""Focused integration regression tests for batch7 g4 findings."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, Tuple

import mesh.constants as c
from mesh.core.clock import now_iso_utc
from mesh.core.proc import get_proc_environ
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import MeshHarness, idle_bound
from tests.integration.test_execution_launch import create_agent as create_exec_agent
from tests.integration.test_lead_actions import LeadActionCase, _fake_goal
from tests.integration.test_projects import create_agent as create_proj_agent
from tests.stubs import create_test_stubs


class TestBatch7G4Git(unittest.TestCase):
    """Git/workspace/merge integration tests for F1, F2, F5, F6, F8."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-g4-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.tok_lead1 = create_exec_agent(self.harness, "lead1", can_lead=True)
        self.tok_grok = create_exec_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_exec_agent(self.harness, "ibx")
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)

    def _make_repo_project(self, name: str, test_command: str = "none") -> Tuple[str, str]:
        repo = self.fixture.make_k0(name)
        rc, resp = self.harness.cli(
            "intake", "submit",
            "--title", f"Code {name}",
            "--goal", "g",
            "--criterion", "c1",
            "--repo", repo,
            "--base-branch", "main",
            "--test-command", test_command,
            "--lead", "lead1",
            token=self.tok_grok,
        )
        self.assertEqual(rc, 0, resp)
        return repo, resp["data"]["id"]

    def _start_commit_submit_accept(self, prj: str, title: str, fname: str, content: str = "x\n") -> Tuple[str, str]:
        rc, resp = self.harness.cli(
            "task", "create", "--project", prj, "--title", title, "--spec", "Work",
            "--assignee", "ibx", token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, fname), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(ws, "add", fname)
        self.fixture.git(ws, "commit", "-m", title, identity=True)
        rc, sub = self.harness.cli(
            "task", "submit", tsk, "--run", st["data"]["run_id"], "--fence", str(st["data"]["fence"]),
            "--summary", "done", token=self.tok_ibx,
        )
        self.assertEqual(rc, 0, sub)
        rc, acc = self.harness.cli("task", "accept", tsk, token=self.tok_lead1)
        self.assertEqual(rc, 0, acc)
        return tsk, ws

    def _merge(self, tsk: str) -> Tuple[int, Dict[str, Any]]:
        return self.harness.cli("task", "merge", tsk, token=self.tok_lead1)

    def test_f1_workspace_delete_rejects_symlinks_to_integration_lead_or_sibling_project(self) -> None:
        _, prj_a = self._make_repo_project("G4F1A")
        _, prj_b = self._make_repo_project("G4F1B")

        rc, c1 = self.harness.cli("task", "create", "--project", prj_a, "--title", "T1", "--spec", "s", "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, c1)
        tsk_1 = c1["data"]["id"]
        rc, st1 = self.harness.cli("task", "start", tsk_1, token=self.tok_ibx)
        self.assertEqual(rc, 0, st1)
        ws_1 = st1["data"]["workspace"].rstrip(os.sep)
        rc, can = self.harness.cli("task", "cancel", tsk_1, "--reason", "release", token=self.tok_lead1)
        self.assertEqual(rc, 0, can)

        rc, c2 = self.harness.cli("task", "create", "--project", prj_b, "--title", "T2", "--spec", "s", "--assignee", "ibx", token=self.tok_lead1)
        self.assertEqual(rc, 0, c2)
        tsk_2 = c2["data"]["id"]
        rc, st2 = self.harness.cli("task", "start", tsk_2, token=self.tok_ibx)
        self.assertEqual(rc, 0, st2)
        ws_2 = st2["data"]["workspace"].rstrip(os.sep)

        integ_a = os.path.join(self.harness.home, "workspaces", prj_a, "integration")
        lead_parent_a = os.path.join(self.harness.home, "workspaces", prj_a, "lead")
        self.assertTrue(os.path.isdir(integ_a))
        self.assertTrue(os.path.isdir(lead_parent_a))
        lead_subdirs = os.listdir(lead_parent_a)
        self.assertTrue(lead_subdirs)
        lead_a = os.path.join(lead_parent_a, lead_subdirs[0])
        self.assertTrue(os.path.isdir(lead_a))

        with open(os.path.join(integ_a, ".canary_integ"), "w") as f:
            f.write("keep\n")
        with open(os.path.join(lead_a, ".canary_lead"), "w") as f:
            f.write("keep\n")
        with open(os.path.join(ws_2, ".canary_b"), "w") as f:
            f.write("keep\n")

        # Swap tsk_1 workspace dir with symlinks to sibling trees inside workspaces.root
        for target, canary in (
            (integ_a, os.path.join(integ_a, ".canary_integ")),
            (lead_a, os.path.join(lead_a, ".canary_lead")),
            (ws_2, os.path.join(ws_2, ".canary_b")),
        ):
            if os.path.islink(ws_1):
                os.unlink(ws_1)
            elif os.path.isdir(ws_1):
                shutil.rmtree(ws_1)
            os.symlink(target, ws_1)
            rc_del, resp_del = self.harness.cli("workspace", "delete", tsk_1, user=True)
            self.assertNotEqual(rc_del, 0, resp_del)
            self.assertEqual(resp_del["error"]["code"], c.INVALID_ARGUMENT)
            self.assertEqual(resp_del["error"]["details"]["reason"], "outside_workspaces_root")
            self.assertTrue(os.path.islink(ws_1))
            self.assertTrue(os.path.isfile(canary))

        # Also swap parent tasks/ dir with a symlink
        tasks_dir_a = os.path.join(self.harness.home, "workspaces", prj_a, "tasks")
        os.unlink(ws_1)
        shutil.rmtree(tasks_dir_a)
        os.symlink(os.path.join(self.harness.home, "workspaces", prj_b, "tasks"), tasks_dir_a)
        rc_par, resp_par = self.harness.cli("workspace", "delete", tsk_1, user=True)
        self.assertNotEqual(rc_par, 0, resp_par)
        self.assertEqual(resp_par["error"]["code"], c.INVALID_ARGUMENT)
        self.assertEqual(resp_par["error"]["details"]["reason"], "outside_workspaces_root")
        self.assertTrue(os.path.isfile(os.path.join(ws_2, ".canary_b")))

        # Restore empty tasks/ dir so teardown invariants are clean
        os.unlink(tasks_dir_a)
        os.makedirs(tasks_dir_a, mode=0o700)
        os.remove(os.path.join(integ_a, ".canary_integ"))
        rc_d, _ = self.harness.cli("workspace", "delete", tsk_1, user=True)
        self.assertEqual(rc_d, 0)

        conn = self.harness.db()
        try:
            del_evs = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = ?;", (tsk_1,)).fetchone()[0]
            self.assertEqual(del_evs, 1)  # only the final restored delete succeeded
        finally:
            conn.close()

    def test_f2_merge_test_command_untracked_files_cleaned_on_fail_and_pass(self) -> None:
        fail_flag = os.path.join(self.stubs_dir, "fail-tests")
        with open(fail_flag, "w") as f:
            f.write("1")
        script = os.path.join(self.stubs_dir, "emit_untracked_test.py")
        with open(script, "w") as f:
            f.write(
                "import os, sys\n"
                "with open('.coverage', 'w') as out: out.write('cov\\n')\n"
                "os.makedirs('reports', exist_ok=True)\n"
                "with open(os.path.join('reports', 'junit.xml'), 'w') as out: out.write('<xml/>\\n')\n"
                f"sys.exit(1 if os.path.exists({fail_flag!r}) else 0)\n"
            )
        _, prj = self._make_repo_project("G4F2", test_command=json.dumps([sys.executable, script]))
        tsk_1, _ = self._start_commit_submit_accept(prj, "First", "a.txt", "a\n")
        tsk_2, _ = self._start_commit_submit_accept(prj, "Second", "b.txt", "b\n")

        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")

        # 1. Failing test_command must abort cleanly AND remove untracked artifacts
        rc_fail, resp_fail = self._merge(tsk_1)
        self.assertNotEqual(rc_fail, 0, resp_fail)
        self.assertFalse(os.path.exists(os.path.join(integ, ".coverage")))
        self.assertFalse(os.path.exists(os.path.join(integ, "reports")))

        # 2. Passing test_command succeeds AND leaves integration/ clean so tsk_2 merges too
        os.remove(fail_flag)
        rc_ok1, resp_ok1 = self._merge(tsk_1)
        self.assertEqual(rc_ok1, 0, resp_ok1)
        self.assertFalse(os.path.exists(os.path.join(integ, ".coverage")))
        self.assertFalse(os.path.exists(os.path.join(integ, "reports")))

        rc_ok2, resp_ok2 = self._merge(tsk_2)
        self.assertEqual(rc_ok2, 0, resp_ok2)

    @idle_bound
    def test_f5_workspace_prune_merge_lease_and_changed_workspaces_root(self) -> None:
        started_flag = os.path.join(self.stubs_dir, "merge-started")
        release_flag = os.path.join(self.stubs_dir, "merge-release")
        slow_test = os.path.join(self.stubs_dir, "slow_test.py")
        with open(slow_test, "w") as f:
            f.write(
                "import os, time\n"
                f"open({started_flag!r}, 'w').close()\n"
                f"deadline = time.monotonic() + 10.0\n"
                f"while not os.path.exists({release_flag!r}) and time.monotonic() < deadline:\n"
                "    time.sleep(0.05)\n"
            )
        repo, prj = self._make_repo_project("G4F5", test_command=json.dumps([sys.executable, slow_test]))

        # Merge tsk_1 (fast: create & merge after allowing test to exit immediately)
        open(release_flag, "w").close()
        tsk_1, ws_1 = self._start_commit_submit_accept(prj, "M1", "f1.txt", "1\n")
        rc, r1 = self._merge(tsk_1)
        self.assertEqual(rc, 0, r1)

        # Prepare tsk_old_del under initial workspaces.root too
        tsk_old_del, ws_old_del = self._start_commit_submit_accept(prj, "M_old_del", "f_od.txt", "od\n")
        rc, rod = self._merge(tsk_old_del)
        self.assertEqual(rc, 0, rod)

        # Prepare tsk_2 and hold merge:<prj> lease during its test step
        tsk_2, _ = self._start_commit_submit_accept(prj, "M2", "f2.txt", "2\n")
        os.remove(release_flag)
        merge_res: Dict[str, Any] = {}

        def _bg_merge() -> None:
            merge_res["rc"], merge_res["resp"] = self._merge(tsk_2)

        th = threading.Thread(target=_bg_merge, daemon=True)
        th.start()
        try:
            self.assertTrue(
                self.harness.wait_for(lambda: os.path.exists(started_flag), timeout=10.0, step=0.05)
            )
            # Confirm merge:<prj> lease is held before pruning
            def _merge_lease_held() -> bool:
                conn = self.harness.db()
                try:
                    return conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"merge:{prj}",)).fetchone() is not None
                finally:
                    conn.close()
            self.assertTrue(self.harness.wait_for(_merge_lease_held, timeout=5.0, step=0.05))

            rc_pr, resp_pr = self.harness.cli("workspace", "prune", "--project", prj, user=True)
            self.assertEqual(rc_pr, 0, resp_pr)
            skipped_by_task = {s["task_id"]: s["reason"] for s in resp_pr["data"]["skipped"]}
            self.assertEqual(skipped_by_task.get(tsk_1), "merge_in_progress")
            self.assertTrue(os.path.isdir(ws_1))
        finally:
            open(release_flag, "w").close()
            th.join(timeout=15.0)
        self.assertEqual(merge_res.get("rc"), 0, merge_res)

        # Complete prj before changing workspaces.root (changing root requires no active/paused repo projects)
        crit_id = self.harness.cli("project", "show", prj, user=True)[1]["data"]["criteria"][0]["id"]
        report = json.dumps({"summary": "done", "criteria": [{"id": crit_id, "met": True, "evidence": "ok"}]})
        self.assertEqual(self.harness.cli("project", "complete", prj, "--report", "-", user=True, stdin=report)[0], 0)

        # Change workspaces.root to a new external directory and start/merge prj_new under new root
        new_root = tempfile.mkdtemp(prefix="mh-newroot-g4-")
        self.addCleanup(shutil.rmtree, new_root, True)
        self.fixture.allowed_roots.append(os.path.realpath(new_root))
        rc_cfg, resp_cfg = self.harness.cli("config", "set", "workspaces.root", json.dumps(new_root), user=True)
        self.assertEqual(rc_cfg, 0, resp_cfg)

        _, prj_new = self._make_repo_project("G4F5B")
        tsk_3, ws_3 = self._start_commit_submit_accept(prj_new, "M3", "f3.txt", "3\n")
        self.assertTrue(os.path.realpath(ws_3).startswith(os.path.realpath(new_root)))
        rc, r3 = self._merge(tsk_3)
        self.assertEqual(rc, 0, r3)

        # --dry-run on workspace delete populates transitions & events without deleting
        rc_dry, resp_dry = self.harness.cli("workspace", "delete", tsk_old_del, "--dry-run", user=True)
        self.assertEqual(rc_dry, 0, resp_dry)
        self.assertEqual(len(resp_dry["data"]["would"]["transitions"]), 1)
        self.assertEqual(resp_dry["data"]["would"]["events"], ["workspace.deleted"])
        self.assertTrue(os.path.isdir(ws_old_del))

        # workspace delete still works on tasks from the previous workspaces.root
        rc_del, resp_del = self.harness.cli("workspace", "delete", tsk_old_del, user=True)
        self.assertEqual(rc_del, 0, resp_del)
        self.assertTrue(resp_del["data"]["deleted"])
        self.assertFalse(os.path.exists(ws_old_del))

        # workspace prune still handles BOTH prior-root tasks (tsk_1, tsk_2) and new-root tasks (tsk_3) + integration wt
        rc_pr2, resp_pr2 = self.harness.cli("workspace", "prune", user=True)
        self.assertEqual(rc_pr2, 0, resp_pr2)
        pruned_ids = {r["task_id"] for r in resp_pr2["data"]["removed"]}
        self.assertIn(tsk_1, pruned_ids)
        self.assertIn(tsk_2, pruned_ids)
        self.assertIn(tsk_3, pruned_ids)
        self.assertFalse(os.path.exists(ws_1))
        self.assertFalse(os.path.exists(ws_3))

    def test_f6_config_set_and_intake_submit_reject_overlap(self) -> None:
        repo, _ = self._make_repo_project("G4F6")
        sub_in_repo = os.path.join(repo, "sub")
        os.makedirs(sub_in_repo, exist_ok=True)
        for bad in (self.harness.home, os.path.join(self.harness.home, "runs"), os.path.dirname(self.harness.home), repo, sub_in_repo):
            with self.subTest(workspaces_root=bad):
                rc, resp = self.harness.cli("config", "set", "workspaces.root", json.dumps(bad), user=True)
                self.assertNotEqual(rc, 0, resp)
                self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)

        for bad_repo in (self.harness.home, os.path.join(self.harness.home, "workspaces")):
            with self.subTest(bad_repo=bad_repo):
                rc, resp = self.harness.cli(
                    "intake", "submit", "--title", "BadRepo", "--goal", "g", "--criterion", "c1",
                    "--repo", bad_repo, "--base-branch", "main", "--test-command", "none",
                    token=self.tok_grok,
                )
                self.assertNotEqual(rc, 0, resp)
                self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT)

    def test_f8_project_wait_returns_repo_metadata(self) -> None:
        repo, prj = self._make_repo_project("G4F8")
        rc_s, resp_s = self.harness.cli("project", "show", prj, user=True)
        self.assertEqual(rc_s, 0, resp_s)
        show_data = resp_s["data"]
        crit_id = show_data["criteria"][0]["id"]

        report = json.dumps({"summary": "done", "criteria": [{"id": crit_id, "met": True, "evidence": "verified"}]})
        rc_c, resp_c = self.harness.cli("project", "complete", prj, "--report", "-", user=True, stdin=report)
        self.assertEqual(rc_c, 0, resp_c)

        rc_w, resp_w = self.harness.cli("project", "wait", prj, "--until", "completed", "--timeout", "5s", user=True)
        self.assertEqual(rc_w, 0, resp_w)
        wait_data = resp_w["data"]
        for field in ("repo", "base_branch", "integration_branch", "base_commit", "tests"):
            self.assertEqual(wait_data[field], show_data[field], field)


class TestBatch7G4Lifecycle(unittest.TestCase):
    """Non-git daemon integration tests for F3, F4, F9, F10, F11."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-g4l-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.harness = MeshHarness(init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.tok_lead1 = create_proj_agent(self.harness, "lead1", can_lead=True)
        self.tok_lead2 = create_proj_agent(self.harness, "lead2", can_lead=True)
        self.tok_grok = create_proj_agent(self.harness, "grok", capabilities=["intake", "relay"])
        self.tok_ibx = create_proj_agent(self.harness, "ibx")
        rc, _ = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0)
        rc, resp = self.harness.cli(
            "intake", "submit", "--title", "Life", "--goal", "G", "--criterion", "C1", "--lead", "lead1",
            token=self.tok_grok,
        )
        self.assertEqual(rc, 0, resp)
        self.prj = resp["data"]["id"]

    def test_f3_set_lead_cancels_approved_approvals_and_approval_cancel(self) -> None:
        rc, p0 = self.harness.cli("project", "show", self.prj, user=True)
        self.assertEqual(rc, 0, p0)
        crit_id = p0["data"]["criteria"][0]["id"]
        orig_assignment = p0["data"]["lead_assignment_id"] if "lead_assignment_id" in p0["data"] else None

        # Self set-lead is a no-op (unchanged=True, preserves lead_assignments row)
        rc_self, resp_self = self.harness.cli(
            "project", "set-lead", self.prj, "lead1", "--reason", "noop check", user=True
        )
        self.assertEqual(rc_self, 0, resp_self)
        self.assertTrue(resp_self["data"].get("unchanged"), resp_self)

        # Lead1 requests project.pause -> requires approval; user approves (status='approved')
        rc1, req1 = self.harness.cli("project", "pause", self.prj, token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_APPROVAL_REQUIRED, req1)
        apr1 = req1["error"]["details"]["approval_id"]
        hash1 = req1["error"]["details"]["op_hash"]
        rc_a1, _ = self.harness.cli("approval", "approve", apr1, "--op-hash", hash1, user=True)
        self.assertEqual(rc_a1, 0)

        # User can also directly cancel an unconsumed approved row
        rc_can, resp_can = self.harness.cli("approval", "cancel", apr1, user=True)
        self.assertEqual(rc_can, 0, resp_can)
        self.assertEqual(resp_can["data"]["status"], "cancelled")

        # Lead1 gets another approved project.pause left unconsumed, then swapped to lead2
        rc2, req2 = self.harness.cli("project", "pause", self.prj, token=self.tok_lead1)
        self.assertEqual(rc2, c.EXIT_APPROVAL_REQUIRED, req2)
        apr2 = req2["error"]["details"]["approval_id"]
        hash2 = req2["error"]["details"]["op_hash"]
        self.assertEqual(self.harness.cli("approval", "approve", apr2, "--op-hash", hash2, user=True)[0], 0)

        rc_swap, resp_swap = self.harness.cli(
            "project", "set-lead", self.prj, "lead2", "--reason", "swap", user=True
        )
        self.assertEqual(rc_swap, 0, resp_swap)
        conn = self.harness.db()
        try:
            st = conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr2,)).fetchone()["status"]
            self.assertEqual(st, "cancelled")
        finally:
            conn.close()

        # Lead2 is not blocked from completing the project by lead1's cancelled approval
        report = json.dumps({"summary": "done", "criteria": [{"id": crit_id, "met": True, "evidence": "ok"}]})
        rc_comp, resp_comp = self.harness.cli(
            "project", "complete", self.prj, "--report", "-", token=self.tok_lead2, stdin=report
        )
        self.assertEqual(rc_comp, 0, resp_comp)
        self.assertEqual(resp_comp["data"]["status"], "completed")

    def test_f4_project_complete_rejects_non_string_evidence(self) -> None:
        rc, p0 = self.harness.cli("project", "show", self.prj, user=True)
        self.assertEqual(rc, 0, p0)
        crit_id = p0["data"]["criteria"][0]["id"]

        for bad_ev in (None, False, 0, "   "):
            with self.subTest(evidence=bad_ev):
                report = json.dumps({"summary": "ok", "criteria": [{"id": crit_id, "met": True, "evidence": bad_ev}]})
                rc_c, resp_c = self.harness.cli("project", "complete", self.prj, "--report", "-", user=True, stdin=report)
                self.assertEqual(rc_c, c.EXIT_USAGE, resp_c)
                self.assertEqual(resp_c["error"]["code"], c.CRITERIA_REPORT_INCOMPLETE)

        good_report = json.dumps({"summary": "ok", "criteria": [{"id": crit_id, "met": True, "evidence": "tests pass"}]})
        rc_g, resp_g = self.harness.cli("project", "complete", self.prj, "--report", "-", user=True, stdin=good_report)
        self.assertEqual(rc_g, 0, resp_g)

    def test_f9_expired_idempotency_key_allows_reuse(self) -> None:
        rc1, r1 = self.harness.cli(
            "task", "create", "--project", self.prj, "--title", "First", "--spec", "S",
            "--idempotency-key", "g4-idem-1", user=True,
        )
        self.assertEqual(rc1, 0, r1)
        # Live reuse with different title fails with IDEMPOTENCY_KEY_REUSED
        rc_conf, resp_conf = self.harness.cli(
            "task", "create", "--project", self.prj, "--title", "Different", "--spec", "S",
            "--idempotency-key", "g4-idem-1", user=True,
        )
        self.assertEqual(rc_conf, c.EXIT_CONFLICT, resp_conf)
        self.assertEqual(resp_conf["error"]["code"], c.IDEMPOTENCY_KEY_REUSED)

        # Backdate idempotency row by 8 days (beyond 7-day TTL)
        old_iso = now_iso_utc(time.time() - (8 * 86400))
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            with conn:
                conn.execute("UPDATE idempotency SET created_at = ? WHERE key = 'g4-idem-1';", (old_iso,))
        finally:
            conn.close()

        # Expired key with different params succeeds and replaces the expired entry
        rc2, r2 = self.harness.cli(
            "task", "create", "--project", self.prj, "--title", "Second", "--spec", "S",
            "--idempotency-key", "g4-idem-1", user=True,
        )
        self.assertEqual(rc2, 0, r2)
        self.assertNotEqual(r2["data"]["id"], r1["data"]["id"])

    def test_f10_dep_noop_events_and_intake_whitespace(self) -> None:
        t1 = self.harness.cli("task", "create", "--project", self.prj, "--title", "A", "--spec", "S", user=True)[1]["data"]["id"]
        t2 = self.harness.cli("task", "create", "--project", self.prj, "--title", "B", "--spec", "S", user=True)[1]["data"]["id"]

        rc, _ = self.harness.cli("dep", "add", t2, "--on", t1, user=True)
        self.assertEqual(rc, 0)
        conn = self.harness.db()
        try:
            added_1 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.added';").fetchone()[0]
        finally:
            conn.close()

        # Duplicate dep add is a no-op emitting 0 events
        rc_dup, _ = self.harness.cli("dep", "add", t2, "--on", t1, user=True)
        self.assertEqual(rc_dup, 0)
        conn = self.harness.db()
        try:
            added_2 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.added';").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(added_1, added_2)

        # dep remove on nonexistent task -> NOT_FOUND
        rc_nf, resp_nf = self.harness.cli("dep", "remove", t2, "--on", "tsk_doesnotexist", user=True)
        self.assertEqual(rc_nf, c.EXIT_NOT_FOUND, resp_nf)
        self.assertEqual(resp_nf["error"]["code"], c.NOT_FOUND)

        # dep remove twice: second call emits 0 events
        self.assertEqual(self.harness.cli("dep", "remove", t2, "--on", t1, user=True)[0], 0)
        conn = self.harness.db()
        try:
            rem_1 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.removed';").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(self.harness.cli("dep", "remove", t2, "--on", t1, user=True)[0], 0)
        conn = self.harness.db()
        try:
            rem_2 = conn.execute("SELECT COUNT(*) FROM events WHERE type = 'dep.removed';").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rem_1, rem_2)

        # Whitespace validation
        for cmd in (
            ("intake", "submit", "--title", "   ", "--goal", "G", "--criterion", "C"),
            ("intake", "submit", "--project", self.prj, "--message", "   \n "),
            ("project", "update", self.prj, "--goal", "  \t "),
            ("project", "update", self.prj, "--criterion", "   "),
        ):
            rc_w, resp_w = self.harness.cli(*cmd, user=True)
            self.assertEqual(rc_w, c.EXIT_USAGE, (cmd, resp_w))
            self.assertEqual(resp_w.get("error", {}).get("code"), c.INVALID_ARGUMENT, (cmd, resp_w))

    def test_f11_approval_check_expired_and_text_output(self) -> None:
        # Expired approval check raises APPROVAL_EXPIRED even before expiry sweep runs
        rc_ar, resp_ar = self.harness.cli(
            "approval", "request", "--category", "deploy", "--summary", "Deploy prod environment",
            "--operation", '{"env":"prod"}', user=True,
        )
        self.assertEqual(rc_ar, 0, resp_ar)
        apr_id = resp_ar["data"]["approval_id"]
        op_hash = resp_ar["data"]["op_hash"]

        # --format text for approval pending renders full command & details; token list shows LOOKUP/EXPIRES_AT/REVOKED_AT
        rc_pend, out_pend = self.harness.cli("--format", "text", "approval", "pending", user=True)
        self.assertEqual(rc_pend, 0, out_pend)
        self.assertIn(apr_id, out_pend["raw_stdout"])
        self.assertIn("Deploy prod environment", out_pend["raw_stdout"])
        rc_ti, resp_ti = self.harness.cli("agent", "token", "issue", "lead1", "--expires-in", "1h", user=True)
        self.assertEqual(rc_ti, 0, resp_ti)
        rc_rev, resp_rev = self.harness.cli("agent", "token", "revoke", resp_ti["data"]["credential_id"], user=True)
        self.assertEqual(rc_rev, 0, resp_rev)
        rc_tok, out_tok = self.harness.cli("--format", "text", "agent", "token", "list", "lead1", user=True)
        self.assertEqual(rc_tok, 0, out_tok)
        self.assertIn("LOOKUP", out_tok["raw_stdout"])
        self.assertIn("EXPIRES_AT", out_tok["raw_stdout"])
        self.assertIn("REVOKED_AT", out_tok["raw_stdout"])

        self.assertEqual(self.harness.cli("approval", "approve", apr_id, "--op-hash", op_hash, user=True)[0], 0)

        past_iso = now_iso_utc(time.time() - 3600)
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            with conn:
                conn.execute("UPDATE approvals SET expires_at = ? WHERE id = ?;", (past_iso, apr_id))
        finally:
            conn.close()

        rc_chk, resp_chk = self.harness.cli("approval", "check", apr_id, "--op-hash", op_hash, user=True)
        self.assertEqual(rc_chk, c.EXIT_APPROVAL_INVALID, resp_chk)
        self.assertEqual(resp_chk["error"]["code"], c.APPROVAL_EXPIRED)


class TestBatch7G4SelfSetLeadLiveTurn(LeadActionCase):
    """F3 + F11: self set-lead keeps active turn alive; scoped lead run token checked on approval.list."""

    def test_f3_self_set_lead_keeps_active_lead_turn_and_f11_approval_list_scope(self) -> None:
        gate = os.path.join(self.stubs_dir, "g4-f3-gate")
        prj = self._create_project(_fake_goal({"steps": [{"wait_release": {"path": gate}}]}))
        turn = self._wait_turn_running(prj)

        # F11: lead run token scoped to prj cannot list approvals of another project
        create_exec_agent(self.harness, "lead_other", can_lead=True)
        rc_b, resp_b = self.harness.cli(
            "project", "create", "--title", "Other", "--goal", "g", "--criterion", "C1", "--lead", "lead_other", user=True
        )
        self.assertEqual(rc_b, 0, resp_b)
        prj_b = resp_b["data"]["id"]

        env: Dict[str, str] = {}

        def _tok_ready() -> bool:
            nonlocal env
            got = get_proc_environ(turn["wrapper_pid"])
            if got and got.get("MESH_TOKEN"):
                env = got
                return True
            return False

        self.assertTrue(self.harness.wait_for(_tok_ready, timeout=10.0, step=0.05))
        rc_ls, resp_ls = self.harness.cli("approval", "list", "--project", prj_b, token=env["MESH_TOKEN"])
        self.assertEqual(rc_ls, c.EXIT_FORBIDDEN, resp_ls)
        self.assertEqual(resp_ls["error"]["code"], c.SCOPE_MISMATCH)

        # F3: self set-lead does NOT kill active lead turn
        rc, resp = self.harness.cli("project", "set-lead", prj, "flead", "--reason", "noop check", user=True)
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp["data"].get("unchanged"), resp)

        time.sleep(0.3)
        row_mid = self._rows("SELECT status, end_reason FROM runs WHERE id = ?;", (turn["id"],))[0]
        self.assertIn(row_mid["status"], ("starting", "running"))

        open(gate, "w").close()
        self.assertTrue(
            self.harness.wait_for(
                lambda: self._rows("SELECT status FROM runs WHERE id = ?;", (turn["id"],))[0]["status"] == "exited",
                timeout=15.0,
                step=0.1,
            )
        )


if __name__ == "__main__":
    unittest.main()
