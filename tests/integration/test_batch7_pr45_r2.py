"""Integration regression tests for PR #45 round-2 findings, including crash recovery."""

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
from tests.helpers import EXPECTED_CRASH_RETURNCODE, restore_accessible_for_test, set_inaccessible_for_test
from tests.integration.test_execution_launch import create_agent as create_exec_agent
from tests.stubs import create_test_stubs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBatch7PR45R2Integration(unittest.TestCase):
    """End-to-end daemon/subprocess crash recovery, merge tracked-file cleanup, and symlink checks."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-pr45r2-")
        self.addCleanup(shutil.rmtree, self.stubs_dir, True)
        create_test_stubs(self.stubs_dir)
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True, extra_path=self.stubs_dir)
        self.addCleanup(self.harness.teardown)
        self.tok_lead1 = create_exec_agent(self.harness, "lead1", can_lead=True)
        self.tok_ibx = create_exec_agent(self.harness, "ibx")
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)

    def _create_repo_project(self, name: str, test_command: str = "none"):
        repo = self.fixture.make_k0(name)
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

    def _request_and_approve_delete(self, tsk: str) -> str:
        rc_req, resp_req = self.harness.cli("workspace", "delete", tsk, token=self.tok_lead1)
        self.assertEqual(rc_req, c.EXIT_APPROVAL_REQUIRED, resp_req)
        apr_id = resp_req["error"]["details"]["approval_id"]
        op_hash = resp_req["error"]["details"]["op_hash"]
        rc_app, resp_app = self.harness.cli(
            "approval", "approve", apr_id, "--op-hash", op_hash, user=True
        )
        self.assertEqual(rc_app, 0, resp_app)
        return apr_id

    def test_p1_1_workspace_delete_crash_after_reserve_and_after_delete(self) -> None:
        _, prj = self._create_repo_project("P11DelCrash")
        tsk1, ws1 = self._start_commit_submit_accept(prj, "CrashRes", "f1.txt")
        with open(os.path.join(ws1, "uncommitted.txt"), "w", encoding="utf-8") as f:
            f.write("still here\n")
        apr1 = self._request_and_approve_delete(tsk1)

        # 1. Crash right after TX1 reserve -> directory untouched, approval unconsumed; recovery rolls back.
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_reserve"})
        rc_c1, resp_c1 = self.harness.cli(
            "workspace", "delete", tsk1, "--approval", apr1, "--idempotency-key", "k-del-res",
            token=self.tok_lead1,
        )
        self.assertNotEqual(rc_c1, 0, resp_c1)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)
        self.assertTrue(os.path.isdir(ws1))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr1,)).fetchone()["status"], "approved")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = ?;", (tsk1,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.delete_rolled_back' AND task_id = ?;", (tsk1,)).fetchone()[0],
                1,
            )
            rec = json.loads(
                conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' ORDER BY seq DESC LIMIT 1;").fetchone()[0]
            )
            self.assertEqual(rec["workspace_deletes_rolled_back"], 1, rec)
        self._doctor_and_audit_ok()

        # Retry with the same approval & idempotency key now succeeds!
        rc_r1, resp_r1 = self.harness.cli(
            "workspace", "delete", tsk1, "--approval", apr1, "--idempotency-key", "k-del-res",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_r1, 0, resp_r1)
        self.assertTrue(resp_r1["data"]["deleted"])
        self.assertFalse(os.path.exists(ws1))

        # 2. Crash right after Phase-2 deletion (before TX2 commits) -> recovery finalizes cleanly!
        tsk2, ws2 = self._start_commit_submit_accept(prj, "CrashDel", "f2.txt")
        apr2 = self._request_and_approve_delete(tsk2)
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_delete"})
        rc_c2, resp_c2 = self.harness.cli(
            "workspace", "delete", tsk2, "--approval", apr2, "--idempotency-key", "k-del-fin",
            token=self.tok_lead1,
        )
        self.assertNotEqual(rc_c2, 0, resp_c2)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)
        self.assertFalse(os.path.exists(ws2))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr2,)).fetchone()["status"], "consumed")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = ?;", (tsk2,)).fetchone()[0],
                1,
            )
            rec2 = json.loads(
                conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' ORDER BY seq DESC LIMIT 1;").fetchone()[0]
            )
            self.assertEqual(rec2["workspace_deletes_finalized"], 1, rec2)
        self._doctor_and_audit_ok()

        # Replaying with k-del-fin returns idempotent_replay=True cleanly!
        rc_rep, resp_rep = self.harness.cli(
            "workspace", "delete", tsk2, "--approval", apr2, "--idempotency-key", "k-del-fin",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_rep, 0, resp_rep)
        self.assertIs(resp_rep.get("idempotent_replay"), True, resp_rep)
        self.assertTrue(resp_rep["data"]["deleted"], resp_rep)

    def test_p1_1_workspace_prune_crash_after_reserve_and_after_prune(self) -> None:
        _, prj = self._create_repo_project("P11PrnCrash")
        tsk1, ws1 = self._start_commit_submit_accept(prj, "PruneCrash1", "p1.txt")
        rc_m1, resp_m1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc_m1, 0, resp_m1)

        # 1. Crash after TX1 reserve -> worktree intact, recovery rolls back reservation; retry succeeds.
        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune_reserve"})
        rc_cr1, resp_cr1 = self.harness.cli(
            "workspace", "prune", "--project", prj, "--idempotency-key", "k-prn-res",
            token=self.tok_lead1,
        )
        self.assertNotEqual(rc_cr1, 0, resp_cr1)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)
        self.assertTrue(os.path.isdir(ws1))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.prune_rolled_back' AND project_id = ?;", (prj,)).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk1,)).fetchone()[0],
                0,
            )
        self._doctor_and_audit_ok()

        rc_ok1, resp_ok1 = self.harness.cli(
            "workspace", "prune", "--project", prj, "--idempotency-key", "k-prn-res",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_ok1, 0, resp_ok1)
        self.assertEqual([r["task_id"] for r in resp_ok1["data"]["removed"]], [tsk1])
        self.assertFalse(os.path.exists(ws1))

        # 2. Crash after Phase-2 prune (before TX2) -> recovery emits workspace.pruned & stores idempotency!
        tsk2, ws2 = self._start_commit_submit_accept(prj, "PruneCrash2", "p2.txt")
        rc_m2, resp_m2 = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc_m2, 0, resp_m2)

        self.harness.kill9_daemon()
        self.harness.restart_daemon(extra_env={"MESH_TEST_CRASH_AT": "after_ws_prune"})
        rc_cr2, resp_cr2 = self.harness.cli(
            "workspace", "prune", "--project", prj, "--idempotency-key", "k-prn-fin",
            token=self.tok_lead1,
        )
        self.assertNotEqual(rc_cr2, 0, resp_cr2)
        self.assertEqual(self.harness.wait_daemon_exit(timeout=30.0), EXPECTED_CRASH_RETURNCODE)
        self.assertFalse(os.path.exists(ws2))

        self.harness.restart_daemon()
        with contextlib.closing(self.harness.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = ?;", (tsk2,)).fetchone()[0],
                1,
            )
            rec = json.loads(
                conn.execute("SELECT payload_json FROM events WHERE type = 'daemon.recovered' ORDER BY seq DESC LIMIT 1;").fetchone()[0]
            )
            self.assertEqual(rec["workspace_prunes_finalized"], 1, rec)
        self._doctor_and_audit_ok()

        rc_rep, resp_rep = self.harness.cli(
            "workspace", "prune", "--project", prj, "--idempotency-key", "k-prn-fin",
            token=self.tok_lead1,
        )
        self.assertEqual(rc_rep, 0, resp_rep)
        self.assertIs(resp_rep.get("idempotent_replay"), True, resp_rep)
        self.assertEqual([r["task_id"] for r in resp_rep["data"]["removed"]], [tsk2])

    def test_p1_2_online_merge_tracked_dirty_and_000_dir(self) -> None:
        # 1. test_command modifies involved tracked file calc.py + creates 0o000 & empty dir -> aborts & restores!
        dirty_script = os.path.join(self.stubs_dir, "dirty_tracked.py")
        with open(dirty_script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys\n"
                f"sys.path.insert(0, {_REPO_ROOT!r})\n"
                "from tests.helpers import set_inaccessible_for_test\n"
                "with open('calc.py', 'w') as fp:\n"
                "    fp.write('corrupted = True\\n')\n"
                "os.makedirs('top000', mode=0o700, exist_ok=True)\n"
                "open('top000/f', 'w').write('x')\n"
                "set_inaccessible_for_test('top000')\n"
                "os.makedirs('empty_leftover', mode=0o700, exist_ok=True)\n"
            )
        _, prj1 = self._create_repo_project("P12Tracked", test_command=json.dumps([sys.executable, dirty_script]))
        tsk1, _ = self._start_commit_submit_accept(
            prj1, "ModifyCalc", "calc.py", content="def add(a, b):\n    return a + b + 10\n"
        )
        rc_m1, resp_m1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc_m1, c.EXIT_MERGE_FAILED, resp_m1)
        self.assertEqual(resp_m1["error"]["details"]["reason"], "integration_dirty", resp_m1)
        self.assertIs(resp_m1["error"]["details"].get("restored"), True, resp_m1)

        integ1 = os.path.join(self.harness.home, "workspaces", prj1, "integration")
        self.assertFalse(os.path.exists(os.path.join(integ1, "top000")))
        self.assertFalse(os.path.exists(os.path.join(integ1, "empty_leftover")))
        self.assertFalse(self.fixture.has_merge_head(integ1))
        self.assertTrue(self.fixture.is_clean(integ1))

        # 2. test_command creates only 0o000 + empty dir and exits 0 -> merge succeeds & artifacts are removed!
        ro_script = os.path.join(self.stubs_dir, "mk_000_empty.py")
        with open(ro_script, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys\n"
                f"sys.path.insert(0, {_REPO_ROOT!r})\n"
                "from tests.helpers import set_inaccessible_for_test\n"
                "os.makedirs('top000', mode=0o700, exist_ok=True)\n"
                "open('top000/f', 'w').write('x')\n"
                "set_inaccessible_for_test('top000')\n"
                "os.makedirs('empty_leftover', mode=0o700, exist_ok=True)\n"
            )
        _, prj2 = self._create_repo_project("P12Zero", test_command=json.dumps([sys.executable, ro_script]))
        tsk2, _ = self._start_commit_submit_accept(prj2, "CleanZero", "clean2.txt")
        rc_m2, resp_m2 = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc_m2, 0, resp_m2)
        integ2 = os.path.join(self.harness.home, "workspaces", prj2, "integration")
        self.assertFalse(os.path.exists(os.path.join(integ2, "top000")))
        self.assertFalse(os.path.exists(os.path.join(integ2, "empty_leftover")))
        self.assertTrue(self.fixture.is_clean(integ2))
        self._doctor_and_audit_ok()

    def test_p2_config_set_workspaces_root_rejects_symlink_ancestor(self) -> None:
        real_parent = os.path.join(self.fixture.base, "real_ws_parent")
        ws_dir = os.path.join(real_parent, "ws_target")
        os.makedirs(ws_dir, mode=0o700)
        link_parent = os.path.join(self.fixture.base, "link_ws_parent")
        os.symlink(real_parent, link_parent)

        rc, resp = self.harness.cli(
            "config", "set", "workspaces.root", json.dumps(os.path.join(link_parent, "ws_target")), user=True
        )
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING, resp)


if __name__ == "__main__":
    unittest.main()
