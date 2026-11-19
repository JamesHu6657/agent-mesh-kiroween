"""Integration regression tests for PR #45 review findings (r1)."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

import mesh.constants as c
from mesh.core.clock import now_iso_utc
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import MeshHarness
from tests.integration.test_execution_launch import create_agent as create_exec_agent
from tests.stubs import create_test_stubs


class TestBatch7PR45R1Integration(unittest.TestCase):
    """End-to-end CLI/daemon regression tests for P1-1, P1-2, P2-1, P2-3, P3-1."""

    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-pr45r1-")
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

    def _start_commit_submit_accept(self, prj: str, title: str, fname: str):
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
            f.write(fname + "\n")
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

    def test_p1_1_historical_workspaces_root_blocks_repo_registration(self) -> None:
        old_ws = os.path.join(self.fixture.base, "ws_old")
        new_ws = os.path.join(self.fixture.base, "ws_new")
        os.makedirs(old_ws, mode=0o700)
        os.makedirs(new_ws, mode=0o700)

        rc1, r1 = self.harness.cli("config", "set", "workspaces.root", f'"{old_ws}"', user=True)
        self.assertEqual(rc1, 0, r1)

        _, prj_old = self._create_repo_project("P11Old")
        tsk_old, ws_old_task = self._start_commit_submit_accept(prj_old, "OldWork", "a.txt")
        rc_m, m_resp = self.harness.cli("task", "merge", tsk_old, token=self.tok_lead1)
        self.assertEqual(rc_m, 0, m_resp)
        rc_comp, r_comp = self.harness.cli("project", "complete", prj_old, token=self.tok_lead1)
        if rc_comp != 0:
            # Cancel project if criteria aren't verified before switching root
            rc_can, r_can = self.harness.cli("project", "cancel", prj_old, user=True)
            self.assertEqual(rc_can, 0, r_can)

        rc2, r2 = self.harness.cli("config", "set", "workspaces.root", f'"{new_ws}"', user=True)
        self.assertEqual(rc2, 0, r2)

        # Registering the historical task worktree as a new project's repo must be refused!
        rc_bad, resp_bad = self.harness.cli(
            "project", "create",
            "--title", "EvilHijack",
            "--goal", "G",
            "--criterion", "C1",
            "--repo", ws_old_task,
            "--base-branch", f"mesh/{prj_old}/{tsk_old}",
            "--test-command", "none",
            "--lead", "lead1",
            user=True,
        )
        self.assertEqual(rc_bad, c.EXIT_USAGE, resp_bad)
        self.assertEqual(resp_bad["error"]["code"], c.INVALID_ARGUMENT)

        # Deleting the old task workspace afterward leaves nothing to hijack and doesn't touch any new repo.
        rc_del, resp_del = self.harness.cli("workspace", "delete", tsk_old, user=True)
        self.assertEqual(rc_del, 0, resp_del)
        self.assertFalse(os.path.exists(ws_old_task))

    def test_p1_2_online_merge_cleans_readonly_test_artifacts_before_commit(self) -> None:
        ro_script = os.path.join(self.stubs_dir, "mk_ro_cache.py")
        with open(ro_script, "w", encoding="utf-8") as f:
            f.write(
                "import os\n"
                "d = os.path.join(os.getcwd(), 'ro_cache')\n"
                "os.makedirs(d, mode=0o755, exist_ok=True)\n"
                "with open(os.path.join(d, 'artifact.bin'), 'w') as fp:\n"
                "    fp.write('1')\n"
                "os.chmod(d, 0o500)\n"
            )
        _, prj = self._create_repo_project("P12Online", test_command=json.dumps([sys.executable, ro_script]))
        tsk1, _ = self._start_commit_submit_accept(prj, "FirstRO", "f1.txt")
        rc1, resp1 = self.harness.cli("task", "merge", tsk1, token=self.tok_lead1)
        self.assertEqual(rc1, 0, resp1)

        integ_wt = os.path.join(self.harness.home, "workspaces", prj, "integration")
        self.assertFalse(os.path.exists(os.path.join(integ_wt, "ro_cache")))

        # Second merge on the same project must NOT fail with integration_dirty!
        tsk2, _ = self._start_commit_submit_accept(prj, "SecondOK", "f2.txt")
        rc2, resp2 = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc2, 0, resp2)

    def test_p2_1_workspace_delete_cleans_readonly_subdir_and_prune_works(self) -> None:
        _, prj = self._create_repo_project("P21WS")
        tsk1, ws1 = self._start_commit_submit_accept(prj, "RODelete", "d1.txt")
        ro_sub = os.path.join(ws1, "ro_build")
        os.makedirs(ro_sub, mode=0o755)
        with open(os.path.join(ro_sub, "out.o"), "w", encoding="utf-8") as f:
            f.write("obj\n")
        os.chmod(ro_sub, 0o500)
        self.addCleanup(lambda: os.path.isdir(ro_sub) and os.chmod(ro_sub, 0o700))

        rc_d, resp_d = self.harness.cli("workspace", "delete", tsk1, user=True)
        self.assertEqual(rc_d, 0, resp_d)
        self.assertTrue(resp_d["data"]["deleted"])
        self.assertFalse(os.path.exists(ws1))

        # Prune on a merged task continues to work cleanly off the event loop.
        tsk2, ws2 = self._start_commit_submit_accept(prj, "PruneAsync", "p2.txt")
        rc_m, resp_m = self.harness.cli("task", "merge", tsk2, token=self.tok_lead1)
        self.assertEqual(rc_m, 0, resp_m)
        rc_p, resp_p = self.harness.cli("workspace", "prune", "--project", prj, user=True)
        self.assertEqual(rc_p, 0, resp_p)
        self.assertEqual([r["task_id"] for r in resp_p["data"]["removed"]], [tsk2])
        self.assertFalse(os.path.exists(ws2))

    def test_p2_3_and_p3_1_cli_paths(self) -> None:
        # P2-3: rejected / cancelled approval stays non-expired after expires_at passes
        rc1, req_rej = self.harness.cli(
            "approval", "request", "--category", "deploy", "--summary", "Deploy 1",
            "--operation", '{"step":1}', user=True,
        )
        self.assertEqual(rc1, 0, req_rej)
        rc2, req_can = self.harness.cli(
            "approval", "request", "--category", "deploy", "--summary", "Deploy 2",
            "--operation", '{"step":2}', user=True,
        )
        self.assertEqual(rc2, 0, req_can)

        rc_r = self.harness.cli(
            "approval", "reject", req_rej["data"]["approval_id"], "--op-hash", req_rej["data"]["op_hash"], user=True,
        )
        self.assertEqual(rc_r[0], 0, rc_r)
        rc_c = self.harness.cli("approval", "cancel", req_can["data"]["approval_id"], user=True)
        self.assertEqual(rc_c[0], 0, rc_c)

        past = now_iso_utc(time.time() - 3600)
        conn = sqlite3.connect(os.path.join(self.harness.home, "mesh.db"), timeout=10.0)
        try:
            with conn:
                conn.execute("UPDATE approvals SET expires_at = ?;", (past,))
        finally:
            conn.close()

        for apr, expected in ((req_rej["data"], "rejected"), (req_can["data"], "cancelled")):
            rc_chk, resp_chk = self.harness.cli(
                "approval", "check", apr["approval_id"], "--op-hash", apr["op_hash"], user=True,
            )
            self.assertEqual(rc_chk, 0, resp_chk)
            self.assertEqual(resp_chk["data"]["status"], expected)

        # P3-1: project update rejects whitespace --goal and whitespace --criterion explicitly
        _, prj = self._create_repo_project("P31Upd")
        rc_g, resp_g = self.harness.cli("project", "update", prj, "--goal", "  \t ", user=True)
        self.assertEqual(rc_g, c.EXIT_USAGE, resp_g)
        self.assertIn("goal", resp_g["error"]["message"].lower())

        rc_c, resp_c = self.harness.cli("project", "update", prj, "--criterion", "   ", user=True)
        self.assertEqual(rc_c, c.EXIT_USAGE, resp_c)
        self.assertIn("criterion", resp_c["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
