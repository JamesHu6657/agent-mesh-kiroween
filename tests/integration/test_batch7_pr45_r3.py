"""Integration regression tests for PR #45 round-3 findings (P1-1, P1-2, P2-1, P2-2)."""

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.integration.test_execution_launch import create_agent as create_exec_agent
from tests.stubs import create_test_stubs


class TestBatch7PR45R3Integration(unittest.TestCase):
    """End-to-end daemon/subprocess tests for staged-index checks, gitlinks, merge-abort crash recovery, and prune crashes."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-pr45r3-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.tok_lead1 = create_exec_agent(self.harness, "lead1", can_lead=True)
        self.tok_ibx = create_exec_agent(self.harness, "ibx")
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)

    def _create_repo_project(self, name: str, test_command: str = "none", *, with_gitlink: bool = False):
        repo = self.fixture.make_k0(name)
        if with_gitlink:
            base_sha = self.fixture.head(repo, "main")
            self.fixture.git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base_sha},vendor_sub")
            os.makedirs(os.path.join(repo, "vendor_sub"), exist_ok=True)
            self.fixture.git(repo, "commit", "-q", "-m", "add gitlink vendor_sub")
        rc, resp = self.harness.cli(
            "project", "create",
            "--title", f"P-{name}",
            "--goal", "G",
            "--criterion", "C1",
            "--repo", repo,
            "--base-branch", "main",
            "--test-command", test_command,
            "--lead", "lead1",
            user=True,
        )
        self.assertEqual(rc, 0, resp)
        return repo, resp["data"]["id"]

    def _start_commit_submit_accept(self, prj: str, title: str, fname: str, content: str = None):
        rc, resp = self.harness.cli(
            "task", "create", "--project", prj, "--title", title, "--spec", "S",
            "--assignee", "ibx", token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        tsk = resp["data"]["id"]
        rc, st = self.harness.cli("task", "start", tsk, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        ws = st["data"]["workspace"]
        with open(os.path.join(ws, fname), "w", encoding="utf-8") as f:
            f.write(content if content is not None else (fname + "\n"))
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

    def _doctor_and_audit_ok(self) -> None:
        rc_d, resp_d = self.harness.cli("doctor", "--check-invariants", user=True, timeout=60.0)
        self.assertEqual(rc_d, 0, resp_d)
        rc_a, resp_a = self.harness.cli("audit", "verify", user=True, timeout=60.0)
        self.assertEqual(rc_a, 0, resp_a)

    def test_p1_1_e2e_staged_index_changes_aborted_as_integration_dirty(self) -> None:
        mode_file = os.path.join(self.stubs_dir, "test_mode.txt")
        script = os.path.join(self.stubs_dir, "idx_test.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, subprocess\n"
                f"mode = open({mode_file!r}).read().strip()\n"
                "if mode == 'add':\n"
                "    open('staged_by_test.txt', 'w').write('leaked\\n')\n"
                "    subprocess.run(['git', 'add', 'staged_by_test.txt'], check=True)\n"
                "elif mode == 'rm_cached':\n"
                "    subprocess.run(['git', 'rm', '--cached', '-q', 'calc.py'], check=True)\n"
            )
        repo, prj = self._create_repo_project("R3P11", test_command=json.dumps([sys.executable, script]))
        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")
        pre_head = self.fixture.head(integ)

        # 1. git add staged_by_test.txt with exit 0 -> INTEGRATION_DIRTY!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("add\n")
        tsk1, _ = self._start_commit_submit_accept(prj, "AddTask", "f1.txt")
        rc1, resp1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_MERGE_FAILED, resp1)
        self.assertEqual(resp1["error"]["code"], c.INTEGRATION_DIRTY, resp1)
        self.assertIs(resp1["error"]["details"].get("restored"), True, resp1)
        self.assertIn("staged_by_test.txt", resp1["error"]["details"].get("dirty_files", []), resp1)
        self.assertEqual(self.fixture.head(integ), pre_head)
        self.assertTrue(self.fixture.is_clean(integ))

        # 2. git rm --cached calc.py with exit 0 -> INTEGRATION_DIRTY and calc.py intact!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("rm_cached\n")
        tsk2, _ = self._start_commit_submit_accept(prj, "RmCachedTask", "f2.txt")
        rc2, resp2 = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc2, c.EXIT_MERGE_FAILED, resp2)
        self.assertEqual(resp2["error"]["code"], c.INTEGRATION_DIRTY, resp2)
        self.assertIs(resp2["error"]["details"].get("restored"), True, resp2)
        self.assertTrue(os.path.isfile(os.path.join(integ, "calc.py")))
        self.assertEqual(self.fixture.head(integ), pre_head)
        self.assertTrue(self.fixture.is_clean(integ))

        # 3. Clean test -> merges cleanly without leaked artifacts.
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("pass\n")
        tsk3, _ = self._start_commit_submit_accept(prj, "CleanTask", "f3.txt")
        rc3, resp3 = self.harness.cli("task", "merge", tsk3, token=self.tok_lead1)
        self.assertEqual(rc3, 0, resp3)
        ls = self.fixture.git(integ, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertIn("f3.txt", ls)
        self.assertNotIn("staged_by_test.txt", ls)
        self._doctor_and_audit_ok()

    def test_p1_2_e2e_gitlink_deleted_and_test_command_none_crash_recovery(self) -> None:
        fail_flag = os.path.join(self.stubs_dir, "fail_flag.txt")
        with open(fail_flag, "w", encoding="utf-8") as f:
            f.write("1\n")
        script = os.path.join(self.stubs_dir, "cond_fail.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"import sys; sys.exit(int(open({fail_flag!r}).read().strip()))\n")

        # Part A: Task branch removes 160000 vendor_sub, tests fail -> must restore vendor_sub and stay unblocked.
        _, prj_a = self._create_repo_project("R3P12A", test_command=json.dumps([sys.executable, script]), with_gitlink=True)
        integ_a = os.path.join(self.harness.home, "workspaces", prj_a, "integration")
        self.assertTrue(os.path.isdir(os.path.join(integ_a, "vendor_sub")))

        rc, resp = self.harness.cli(
            "task", "create", "--project", prj_a, "--title", "DropSub", "--spec", "S",
            "--assignee", "ibx", token=self.tok_lead1,
        )
        self.assertEqual(rc, 0, resp)
        tsk_drop = resp["data"]["id"]
        rc, st = self.harness.cli("task", "start", tsk_drop, token=self.tok_ibx)
        self.assertEqual(rc, 0, st)
        ws_drop = st["data"]["workspace"]
        self.fixture.git(ws_drop, "rm", "-q", "vendor_sub")
        with open(os.path.join(ws_drop, "dropped.txt"), "w", encoding="utf-8") as f:
            f.write("dropped\n")
        self.fixture.git(ws_drop, "add", "dropped.txt")
        self.fixture.git(ws_drop, "commit", "-m", "drop sub", identity=True)
        self.harness.cli("task", "submit", tsk_drop, "--run", st["data"]["run_id"], "--fence", str(st["data"]["fence"]), "--summary", "done", token=self.tok_ibx)
        self.harness.cli("task", "accept", tsk_drop, token=self.tok_lead1)

        rc_m, resp_m = self.harness.cli("task", "merge", tsk_drop, token=self.tok_lead1)
        self.assertEqual(rc_m, c.EXIT_MERGE_FAILED, resp_m)
        self.assertEqual(resp_m["error"]["details"]["reason"], "tests_failed", resp_m)
        self.assertIs(resp_m["error"]["details"].get("restored"), True, resp_m)
        self.assertTrue(os.path.isdir(os.path.join(integ_a, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(integ_a))

        # Next merge succeeds rather than wedging on INTEGRATION_DIRTY.
        with open(fail_flag, "w", encoding="utf-8") as f:
            f.write("0\n")
        tsk_ok, _ = self._start_commit_submit_accept(prj_a, "NextOk", "next_ok.txt")
        rc_ok, resp_ok = self.harness.cli("task", "merge", tsk_ok, token=self.tok_lead1)
        self.assertEqual(rc_ok, 0, resp_ok)
        self.assertTrue(os.path.isdir(os.path.join(integ_a, "vendor_sub")))

        # Part B: test_command="none" + after_merge_commit crash leaves vendor_sub intact across recovery.
        _, prj_b = self._create_repo_project("R3P12B", test_command="none", with_gitlink=True)
        integ_b = os.path.join(self.harness.home, "workspaces", prj_b, "integration")
        tsk_b, _ = self._start_commit_submit_accept(prj_b, "NoneTask", "b.txt")
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_commit"})
        rc_cr, _ = self.harness.cli("task", "merge", tsk_b, token=self.tok_lead1)
        self.assertNotEqual(rc_cr, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)
        self.harness.restart_daemon()
        self.assertTrue(os.path.isdir(os.path.join(integ_b, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(integ_b))
        self._doctor_and_audit_ok()

    def test_p2_1_e2e_crash_after_merge_abort_restores_dirty_worktree_and_000_dirs(self) -> None:
        script = os.path.join(self.stubs_dir, "dirty_uninv_fail.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys\n"
                "with open('calc.py', 'w') as fp:\n"
                "    fp.write('# dirty uninvolved calc\\n')\n"
                "sys.exit(1)\n"
            )
        _, prj = self._create_repo_project("R3P21", test_command=json.dumps([sys.executable, script]), with_gitlink=True)
        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()
        tsk1, _ = self._start_commit_submit_accept(prj, "CrashAbortLive", "uninvolved_branch.txt")

        # Part A: Live pipeline crash at MESH_TEST_CRASH_AT=after_merge_abort leaves calc.py dirty without MERGE_HEAD.
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_abort"})
        rc_cr, _ = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertNotEqual(rc_cr, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)

        self.assertFalse(self.fixture.has_merge_head(integ))
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# dirty uninvolved calc\n")

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            trow = conn.execute("SELECT status, merge_json FROM tasks WHERE id = ?;", (tsk1,)).fetchone()
            self.assertEqual(trow["status"], "merge_failed")
            mj = json.loads(trow["merge_json"])
            self.assertEqual(mj["reason"], "interrupted", mj)
            self.assertIs(mj["details"].get("restored"), True, mj)
            rec = json.loads(
                conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' ORDER BY seq DESC LIMIT 1;").fetchone()[0]
            )
            self.assertEqual(rec["merges_interrupted"], 1, rec)

        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertTrue(os.path.isdir(os.path.join(integ, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(integ))

        # Part B: Crash mid-test while tracked + untracked + 0o000 dirs exist, then recovery crashes at after_merge_abort
        # leaving MERGE_HEAD gone while calc.py, artifact.tmp, and top000 are ALL still dirty on disk!
        ready_flag = os.path.join(self.stubs_dir, "mid_test_ready.flag")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, time\n"
                "with open('calc.py', 'w') as fp:\n"
                "    fp.write('# mid-test calc dirty\\n')\n"
                "with open('artifact.tmp', 'w') as fp:\n"
                "    fp.write('leftover\\n')\n"
                "os.makedirs('top000', mode=0o700, exist_ok=True)\n"
                "with open('top000/f', 'w') as fp:\n"
                "    fp.write('1')\n"
                "os.chmod('top000', 0o000)\n"
                f"open({ready_flag!r}, 'w').write('ready')\n"
                "time.sleep(30)\n"
            )
        import threading
        import time

        top000_path = os.path.join(integ, "top000")
        self.addCleanup(lambda: os.path.isdir(top000_path) and os.chmod(top000_path, 0o700))

        merge_res = []
        t = threading.Thread(
            target=lambda: merge_res.append(self.harness.cli("task", "merge", tsk1, token=self.tok_lead1, timeout=20.0))
        )
        t.start()
        try:
            deadline = time.monotonic() + 15.0
            while not os.path.exists(ready_flag) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(ready_flag))
            self.harness.kill9_daemon()
            t.join(timeout=10.0)
        finally:
            t.join(timeout=2.0)

        # Boot 1 recovery runs decide_recovery -> _abort_and_verify and crashes right after git merge --abort.
        self.harness.start_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_abort"}, wait_ready=False)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=20.0), -signal.SIGKILL)
        self.assertFalse(self.fixture.has_merge_head(integ))
        self.assertTrue(os.path.exists(os.path.join(integ, "artifact.tmp")))
        self.assertTrue(os.path.exists(top000_path))

        # Boot 2 recovery completes no-MERGE_HEAD restoration & cleanup cleanly!
        self.harness.restart_daemon()
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertFalse(os.path.exists(os.path.join(integ, "artifact.tmp")))
        self.assertFalse(os.path.exists(top000_path))
        self.assertTrue(os.path.isdir(os.path.join(integ, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(integ))

        # Idempotent second restart + subsequent merge passes!
        self.harness.restart_daemon()
        with open(script, "w", encoding="utf-8") as f:
            f.write("pass\n")
        tsk2, _ = self._start_commit_submit_accept(prj, "AfterAbortRec", "clean_after.txt")
        rc2, resp2 = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc2, 0, resp2)
        self._doctor_and_audit_ok()

    def test_p2_2_e2e_cancelled_after_delete_and_prune_crash_windows(self) -> None:
        repo, prj = self._create_repo_project("R3P22")

        # 1. Zero-commit cancelled task after `workspace delete` must be pruned by `workspace prune`,
        # while a committed cancelled task after `workspace delete` must stay skipped (`unmerged_commits`).
        rc, r_c0 = self.harness.cli(
            "task", "create", "--project", prj, "--title", "Cancel0", "--spec", "S",
            "--assignee", "ibx", token=self.tok_lead1,
        )
        tsk_c0 = r_c0["data"]["id"]
        rc, st_c0 = self.harness.cli("task", "start", tsk_c0, token=self.tok_ibx)
        ws_c0 = st_c0["data"]["workspace"]
        self.harness.cli("task", "cancel", tsk_c0, "--reason", "unused", token=self.tok_lead1)
        rc, d0 = self.harness.cli("workspace", "delete", tsk_c0, user=True)
        self.assertEqual(rc, 0, d0)
        self.assertFalse(os.path.exists(ws_c0))

        rc, r_c1 = self.harness.cli(
            "task", "create", "--project", prj, "--title", "Cancel1", "--spec", "S",
            "--assignee", "ibx", token=self.tok_lead1,
        )
        tsk_c1 = r_c1["data"]["id"]
        rc, st_c1 = self.harness.cli("task", "start", tsk_c1, token=self.tok_ibx)
        ws_c1 = st_c1["data"]["workspace"]
        with open(os.path.join(ws_c1, "wip.txt"), "w", encoding="utf-8") as f:
            f.write("wip\n")
        self.fixture.git(ws_c1, "add", "wip.txt")
        self.fixture.git(ws_c1, "commit", "-m", "wip", identity=True)
        self.harness.cli("task", "cancel", tsk_c1, "--reason", "abandoned", token=self.tok_lead1)
        rc, d1 = self.harness.cli("workspace", "delete", tsk_c1, user=True)
        self.assertEqual(rc, 0, d1)

        rc_p1, resp_p1 = self.harness.cli("workspace", "prune", "--project", prj, token=self.tok_lead1)
        self.assertEqual(rc_p1, 0, resp_p1)
        self.assertEqual([r["task_id"] for r in resp_p1["data"]["removed"]], [tsk_c0], resp_p1)
        self.assertEqual(
            [(s["task_id"], s["reason"]) for s in resp_p1["data"]["skipped"]],
            [(tsk_c1, "unmerged_commits")],
            resp_p1,
        )
        self.assertNotIn(f"mesh/{prj}/{tsk_c0}", self.fixture.branches(repo, "mesh/*"))
        self.assertIn(f"mesh/{prj}/{tsk_c1}", self.fixture.branches(repo, "mesh/*"))

        # 2. Crash before_ws_prune_delete -> intact worktree, rollback event, 0 pruned, retry succeeds.
        tsk_m1, ws_m1 = self._start_commit_submit_accept(prj, "M1", "m1.txt")
        self.assertEqual(self.harness.cli("task", "merge", tsk_m1, token=self.tok_lead1)[0], 0)
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "before_ws_prune_delete"})
        rc_cb, _ = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-before-del", token=self.tok_lead1)
        self.assertNotEqual(rc_cb, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)
        self.assertTrue(os.path.isdir(ws_m1))
        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk_m1,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.prune_rolled_back' AND project_id = ?;", (prj,)).fetchone()[0], 1)

        # 3. Crash after_ws_prune_worktree_remove -> dir gone, branch kept -> rollback, retry cleans branch!
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune_worktree_remove"})
        rc_cw, _ = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-wt-rm", token=self.tok_lead1)
        self.assertNotEqual(rc_cw, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)
        self.assertFalse(os.path.exists(ws_m1))
        self.assertIn(f"mesh/{prj}/{tsk_m1}", self.fixture.branches(repo, "mesh/*"))
        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk_m1,)).fetchone()[0], 0)
        rc_rwt, resp_rwt = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-wt-rm", token=self.tok_lead1)
        self.assertEqual(rc_rwt, 0, resp_rwt)
        self.assertEqual([r["task_id"] for r in resp_rwt["data"]["removed"]], [tsk_m1], resp_rwt)
        self.assertNotIn(f"mesh/{prj}/{tsk_m1}", self.fixture.branches(repo, "mesh/*"))

        # 4. Multi-item [tsk_m2, tsk_m3] crash at after_ws_prune_delete (on first item tsk_m2 before ckpt write):
        # recovery emits 1 workspace.pruned for tsk_m2, rolls back, retry with same key prunes tsk_m3 cleanly!
        tsk_m2, ws_m2 = self._start_commit_submit_accept(prj, "M2", "m2.txt")
        self.assertEqual(self.harness.cli("task", "merge", tsk_m2, token=self.tok_lead1)[0], 0)
        tsk_m3, ws_m3 = self._start_commit_submit_accept(prj, "M3", "m3.txt")
        self.assertEqual(self.harness.cli("task", "merge", tsk_m3, token=self.tok_lead1)[0], 0)

        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune_delete"})
        rc_cd, _ = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-mid-del", token=self.tok_lead1)
        self.assertNotEqual(rc_cd, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)
        self.assertFalse(os.path.exists(ws_m2))
        self.assertTrue(os.path.isdir(ws_m3))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk_m2,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk_m3,)).fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-mid-del';").fetchone())

        rc_rm3, resp_rm3 = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-mid-del", token=self.tok_lead1)
        self.assertEqual(rc_rm3, 0, resp_rm3)
        self.assertEqual([r["task_id"] for r in resp_rm3["data"]["removed"]], [tsk_m3], resp_rm3)

        # 5. Last-item crash after_ws_prune_last_delete -> recovery promotes in_flight AND derives completed=True;
        # idempotent retry returns idempotent_replay=True with real removed=[tsk_m4]!
        tsk_m4, ws_m4 = self._start_commit_submit_accept(prj, "M4", "m4.txt")
        self.assertEqual(self.harness.cli("task", "merge", tsk_m4, token=self.tok_lead1)[0], 0)

        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune_last_delete"})
        rc_cl, _ = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-last-del", token=self.tok_lead1)
        self.assertNotEqual(rc_cl, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), -signal.SIGKILL)
        self.assertFalse(os.path.exists(ws_m4))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            for tid in (tsk_c0, tsk_m1, tsk_m2, tsk_m3, tsk_m4):
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tid,)).fetchone()[0],
                    1,
                    f"expected exactly 1 workspace.pruned event for {tid}",
                )

        rc_rep, resp_rep = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-last-del", token=self.tok_lead1)
        self.assertEqual(rc_rep, 0, resp_rep)
        self.assertIs(resp_rep.get("idempotent_replay"), True, resp_rep)
        self.assertEqual([r["task_id"] for r in resp_rep["data"]["removed"]], [tsk_m4], resp_rep)
        self._doctor_and_audit_ok()


if __name__ == "__main__":
    unittest.main()
