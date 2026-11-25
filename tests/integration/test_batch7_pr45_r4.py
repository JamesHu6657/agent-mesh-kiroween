"""Integration regression tests for PR #45 round-4 findings (P1-1, P1-2, P2-3, P2-4, P2-5)."""

import contextlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import EXPECTED_CRASH_RETURNCODE, restore_accessible_for_test, set_inaccessible_for_test
from tests.integration.test_execution_launch import create_agent as create_exec_agent
from tests.stubs import create_test_stubs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MESH_PROC_LOCK = "shared"


class TestBatch7PR45R4Integration(unittest.TestCase):
    """End-to-end daemon/subprocess tests for rogue commits, hide-flags, gitlinks, prune, and delete recovery."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-pr45r4-")
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
        full = os.path.join(ws, fname)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
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

    def test_e2e_p1_1_rogue_commit_live_and_mid_test_kill9_recovery(self) -> None:
        mode_file = os.path.join(self.stubs_dir, "r4_p11_mode.txt")
        ready_flag = os.path.join(self.stubs_dir, "r4_p11_ready.flag")
        script = os.path.join(self.stubs_dir, "r4_p11_test.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, subprocess, time\n"
                f"mode = open({mode_file!r}).read().strip()\n"
                "if mode == 'rogue_live':\n"
                "    open('evil.txt', 'w').write('unreviewed\\n')\n"
                "    subprocess.run(['git', 'add', 'evil.txt'], check=True)\n"
                "    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@e.invalid', "
                "'commit', '-m', 'rogue commit'], check=True)\n"
                "elif mode == 'rogue_crash':\n"
                "    open('evil_crash.txt', 'w').write('unreviewed\\n')\n"
                "    subprocess.run(['git', 'add', 'evil_crash.txt'], check=True)\n"
                "    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@e.invalid', "
                "'commit', '-m', os.environ['ROGUE_MSG']], check=True)\n"
                f"    open({ready_flag!r}, 'w').write('ready')\n"
                "    time.sleep(30)\n"
            )
        _, prj = self._create_repo_project("R4E2E11", test_command=json.dumps([sys.executable, script]))
        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")
        pre_head = self.fixture.head(integ)

        # 1. Live rogue commit exits 0 -> INTEGRATION_DIRTY, restored=True, HEAD rolled back, tree clean!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("rogue_live\n")
        tsk1, _ = self._start_commit_submit_accept(prj, "RogueLive", "clean1.txt")
        rc1, resp1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_MERGE_FAILED, resp1)
        self.assertEqual(resp1["error"]["code"], c.INTEGRATION_DIRTY, resp1)
        self.assertIs(resp1["error"]["details"].get("restored"), True, resp1)
        self.assertEqual(self.fixture.head(integ), pre_head)
        self.assertFalse(os.path.exists(os.path.join(integ, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(integ, "clean1.txt")))
        self.assertTrue(self.fixture.is_clean(integ))

        # 2. Rogue commit naming task_id during test + kill -9 daemon -> recovery rejects fake commit and restores!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("rogue_crash\n")
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"ROGUE_MSG": f"Merge mesh task {tsk1}: fake title"})
        merge_res = []
        th = threading.Thread(
            target=lambda: merge_res.append(self.harness.cli("task", "merge", tsk1, token=self.tok_lead1, timeout=20.0))
        )
        th.start()
        try:
            deadline = time.monotonic() + 15.0
            while not os.path.exists(ready_flag) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(ready_flag))
            self.harness.kill9_daemon()
            th.join(timeout=10.0)
        finally:
            th.join(timeout=2.0)

        self.assertNotEqual(self.fixture.head(integ), pre_head)
        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            trow = conn.execute("SELECT status, merge_json FROM tasks WHERE id = ?;", (tsk1,)).fetchone()
            self.assertEqual(trow["status"], "merge_failed")
            mj = json.loads(trow["merge_json"])
            self.assertEqual(mj["reason"], "interrupted", mj)
            self.assertIs(mj["details"].get("restored"), True, mj)
        self.assertEqual(self.fixture.head(integ), pre_head)
        self.assertFalse(os.path.exists(os.path.join(integ, "evil_crash.txt")))
        self.assertTrue(self.fixture.is_clean(integ))

        # 3. Clean merge now succeeds!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("pass\n")
        rc_ok, resp_ok = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc_ok, 0, resp_ok)
        self._doctor_and_audit_ok()

    def test_e2e_p1_2_skip_worktree_and_abort_crash_recovery(self) -> None:
        mode_file = os.path.join(self.stubs_dir, "r4_p12_mode.txt")
        script = os.path.join(self.stubs_dir, "r4_p12_test.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import subprocess, sys\n"
                f"mode = open({mode_file!r}).read().strip()\n"
                "if mode == 'skip_pass':\n"
                "    open('calc.py', 'w').write('# CORRUPTED\\n')\n"
                "    subprocess.run(['git', 'update-index', '--skip-worktree', 'calc.py'], check=True)\n"
                "elif mode == 'assume_fail':\n"
                "    open('calc.py', 'w').write('# ASSUME CORRUPT\\n')\n"
                "    subprocess.run(['git', 'update-index', '--assume-unchanged', 'calc.py'], check=True)\n"
                "    sys.exit(1)\n"
            )
        _, prj = self._create_repo_project("R4E2E12", test_command=json.dumps([sys.executable, script]))
        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()

        # 1. --skip-worktree + pass -> INTEGRATION_DIRTY, restored, follow-up modifying calc.py succeeds!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("skip_pass\n")
        tsk_a, _ = self._start_commit_submit_accept(prj, "T2a", "f2a.txt")
        tsk_b, _ = self._start_commit_submit_accept(prj, "T2b", "calc.py", "def add(a, b):\n    return a + b + 2\n")
        rc_a, resp_a = self.harness.cli("task", "merge", tsk_a, token=self.tok_lead1)
        self.assertEqual(rc_a, c.EXIT_MERGE_FAILED, resp_a)
        self.assertEqual(resp_a["error"]["code"], c.INTEGRATION_DIRTY, resp_a)
        self.assertIs(resp_a["error"]["details"].get("restored"), True, resp_a)
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)

        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("pass\n")
        rc_b, resp_b = self.harness.cli("task", "merge", tsk_b, token=self.tok_lead1)
        self.assertEqual(rc_b, 0, resp_b)

        # 2. --assume-unchanged + fail + MESH_TEST_CRASH_AT=after_merge_abort -> recovery clears flag & restores calc.py!
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            calc_v2 = f.read()
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("assume_fail\n")
        tsk_c, _ = self._start_commit_submit_accept(prj, "T2c", "f2c.txt")
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_merge_abort"})
        rc_c, _ = self.harness.cli("task", "merge", tsk_c, token=self.tok_lead1)
        self.assertNotEqual(rc_c, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)

        self.harness.restart_daemon()
        with open(os.path.join(integ, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), calc_v2)
        self.assertTrue(self.fixture.is_clean(integ))
        self._doctor_and_audit_ok()

    def test_e2e_p2_3_p2_4_p2_5_gitlinks_and_ws_crash_windows(self) -> None:
        mode_file = os.path.join(self.stubs_dir, "r4_p23_mode.txt")
        script = os.path.join(self.stubs_dir, "r4_p23_test.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys\n"
                f"sys.path.insert(0, {_REPO_ROOT!r})\n"
                "from tests.helpers import set_inaccessible_for_test\n"
                f"mode = open({mode_file!r}).read().strip()\n"
                "if mode == 'sym_000':\n"
                "    open('tests/test_calc.py', 'w').write('# dirt\\n')\n"
                "    set_inaccessible_for_test('tests')\n"
                "    os.rmdir('vendor_sub')\n"
                "    os.symlink('.', 'vendor_sub', target_is_directory=True)\n"
                "elif mode == 'leak_gl':\n"
                "    os.makedirs('vendor_sub/subcache', exist_ok=True)\n"
                "    open('vendor_sub/leaked.txt', 'w').write('leftover\\n')\n"
            )
        repo, prj = self._create_repo_project(
            "R4E2E23", test_command=json.dumps([sys.executable, script]), with_gitlink=True
        )
        integ = os.path.join(self.harness.home, "workspaces", prj, "integration")
        tests_dir = os.path.join(integ, "tests")
        self.addCleanup(lambda: os.path.isdir(tests_dir) and restore_accessible_for_test(tests_dir))

        # 1. Symlinked vendor_sub -> . + 0o000 tests -> INTEGRATION_DIRTY + restored!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("sym_000\n")
        tsk1, _ = self._start_commit_submit_accept(prj, "Sym000", "sym1.txt")
        rc1, resp1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc1, c.EXIT_MERGE_FAILED, resp1)
        self.assertEqual(resp1["error"]["code"], c.INTEGRATION_DIRTY, resp1)
        self.assertIs(resp1["error"]["details"].get("restored"), True, resp1)
        self.assertFalse(os.path.islink(os.path.join(integ, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(integ))

        # 2. Uninitialized vendor_sub leaked files cleaned cleanly on passing merge!
        with open(mode_file, "w", encoding="utf-8") as f:
            f.write("leak_gl\n")
        rc2, resp2 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc2, 0, resp2)
        self.assertEqual(os.listdir(os.path.join(integ, "vendor_sub")), [])

        # 3. Normal prune + crash at after_ws_prune_reserve with already-pruned historical task: zero duplicate events!
        rc_p, resp_p = self.harness.cli("workspace", "prune", "--project", prj, token=self.tok_lead1)
        self.assertEqual(rc_p, 0, resp_p)
        self.assertEqual([r["task_id"] for r in resp_p["data"]["removed"]], [tsk1])

        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune_reserve"})
        rc_pr, _ = self.harness.cli("workspace", "prune", "--project", prj, "--idempotency-key", "k-r4-hist", token=self.tok_lead1)
        self.assertNotEqual(rc_pr, 0)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)
        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk1,)).fetchone()[0],
                1,
            )

        # 4. Dry-run prune with historical task returns empty transitions cleanly.
        rc_pd, resp_pd = self.harness.cli("workspace", "prune", "--project", prj, "--dry-run", token=self.tok_lead1)
        self.assertEqual(rc_pd, 0, resp_pd)
        self.assertEqual(resp_pd["data"]["would"]["transitions"], [])
        self._doctor_and_audit_ok()


if __name__ == "__main__":
    unittest.main()
