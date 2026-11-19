"""Unit regression tests for the second, independent PR #45 round-5 review.

P1 split index: the shared index the pre-merge index links to is backed up and
   put back on restore (git may expire it during the test).
P1 `git add -N` / truncated index: abort that cannot clear MERGE_HEAD still
   restores the pre-merge snapshot.
P2 multi-line titles: the merge commit message is written verbatim, so crash
   recovery recognises Mesh's own merge.
P2 workspace.delete short-circuit: moved ``tasks`` parent / non-directory target
   are refused without consuming the approval; an already-missing worktree dir
   still gets ``git worktree prune``.
P2 workspace.prune: historical, already-recorded prunes stay out of the plan;
   branch existence is read with one ``show-ref --heads`` per integration worktree.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.errors import MeshError
from mesh.core.clock import now_iso_utc
from mesh.core.db import transaction
from mesh.core.events import record_event
from mesh.daemon import merge
from mesh.daemon.handlers import run as run_handlers
from tests.gitfixture import GitFixture
from tests.unit.test_batch7_pr45_r4 import _ctx, _setup_home_and_db
from tests.unit.test_batch7_pr45_r5 import TestR5WorkspaceDeleteAndPrune, _Srv, _pipeline

MESH_PROC_LOCK = "shared"


class TestR5bMergeRestore(unittest.TestCase):
    def setUp(self):
        self.fx = GitFixture()

    def tearDown(self):
        self.fx.cleanup()

    def _branch(self, repo, base, name, fname="f.txt"):
        self.fx.git(repo, "checkout", "-q", "-b", f"mesh/prj_1/{name}", base)
        with open(os.path.join(repo, fname), "w") as f:
            f.write(name + "\n")
        self.fx.git(repo, "add", fname)
        self.fx.git(repo, "commit", "-q", "-m", name)
        self.fx.git(repo, "checkout", "-q", "main")
        return f"mesh/prj_1/{name}"

    def _assert_restored(self, repo, base, out):
        self.assertNotEqual(out["outcome"], "merged", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertFalse(os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD")))
        self.assertEqual(self.fx.head(repo, "main"), base)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(st.returncode, 0, st.stderr)
        self.assertEqual(st.stdout, "", st.stdout)

    def test_intent_to_add_from_test_is_rolled_back(self):
        repo = self.fx.make_k0("Ita")
        base = self.fx.head(repo, "main")
        br = self._branch(repo, base, "tsk_ita")
        out = _pipeline(self.fx, repo, "run_ita", "tsk_ita", br, base, "custom",
                        [sys.executable, "-c",
                         "import subprocess; open('ita.txt','w').write('x\\n'); "
                         "subprocess.run(['git','add','-N','ita.txt'], check=True)"])
        self._assert_restored(repo, base, out)
        self.assertFalse(os.path.exists(os.path.join(repo, "ita.txt")))

    def test_intent_to_add_flag_counts_as_dirty(self):
        repo = self.fx.make_k0("ItaDirty")
        with open(os.path.join(repo, "n.txt"), "w") as f:
            f.write("n\n")
        self.fx.git(repo, "add", "-N", "n.txt")
        self.assertIn("n.txt", merge._dirty_paths(repo, "t"))

    def test_truncated_index_from_test_is_rolled_back(self):
        repo = self.fx.make_k0("Trunc")
        base = self.fx.head(repo, "main")
        br = self._branch(repo, base, "tsk_tr")
        out = _pipeline(self.fx, repo, "run_tr", "tsk_tr", br, base, "custom",
                        [sys.executable, "-c",
                         "open('.git/index','wb').write(b'DIRC\\x00\\x00\\x00\\x02truncated')"])
        self._assert_restored(repo, base, out)

    def test_split_index_repo_shared_file_expired_by_test_is_put_back(self):
        repo = self.fx.make_k0("SplitOn")
        self.fx.git(repo, "update-index", "--split-index")
        base = self.fx.head(repo, "main")
        br = self._branch(repo, base, "tsk_sp")
        self.fx.git(repo, "update-index", "--split-index")
        shared_before = merge._index_link_shared_name(os.path.join(repo, ".git", "index"))
        self.assertTrue(shared_before)
        out = _pipeline(self.fx, repo, "run_sp", "tsk_sp", br, base, "custom",
                        [sys.executable, "-c",
                         "import subprocess, sys, glob, os\n"
                         "subprocess.run(['git','-c','splitIndex.maxPercentChange=0','-c',"
                         "'splitIndex.sharedIndexExpire=now','update-index','--split-index',"
                         "'--force-write-index'], check=True)\n"
                         "for p in glob.glob('.git/sharedindex.*'):\n"
                         "    if not p.endswith(" + repr(shared_before) + "): continue\n"
                         "    os.unlink(p)\n"
                         "sys.exit(1)\n"])
        self._assert_restored(repo, base, out)
        self.assertTrue(os.path.isfile(os.path.join(repo, ".git", shared_before)))

    def test_multiline_title_merge_is_reconciled_after_crash(self):
        repo = self.fx.make_k0("Multi")
        base = self.fx.head(repo, "main")
        for n, title in enumerate(("Fix parser \nDetails", "Fix parser\r\nDetails", "Fix parser\n\n\nDetails")):
            self.fx.git(repo, "checkout", "-q", "main")
            self.fx.git(repo, "reset", "-q", "--hard", base)
            tid = f"tsk_ml{n}"
            br = self._branch(repo, base, tid, fname=f"m{n}.txt")
            rdir = os.path.join(self.fx.base, f"run_ml{n}")
            os.makedirs(rdir)
            with mock.patch.object(merge, "_maybe_crash_after_commit", side_effect=RuntimeError("crash")):
                merge.run_merge_pipeline({
                    "run_id": f"run_ml{n}", "task_id": tid, "title": title,
                    "integration": repo, "integration_branch": "main", "worktree": repo,
                    "branch": br, "pre_head": base, "test_command": "none",
                    "test_timeout_s": 30, "kill_grace_s": 1, "run_dir": rdir,
                })
            mc = self.fx.head(repo, "main")
            self.assertNotEqual(mc, base, title)
            rec = merge.decide_recovery(repo, tid, br, base, run_dir=rdir)
            self.assertEqual(rec["outcome"], "merged", (title, rec))
            self.assertEqual(self.fx.head(repo, "main"), mc)


class TestR5bWorkspaceDelete(TestR5WorkspaceDeleteAndPrune):
    # Reuse _setup_task/_delete/_approval_status/_deleted_events; run only the new tests.
    def _r5_skip(self):
        self.skipTest("covered in test_batch7_pr45_r5")

    test_tasks_dir_000_is_reopened_and_really_deleted = _r5_skip
    test_unreadable_target_never_consumes_approval = _r5_skip
    test_recovery_without_rmtree_marker_rolls_back = _r5_skip
    test_recovery_tasks_dir_000_is_unknown_not_deleted = _r5_skip
    test_recovery_with_marker_and_enoent_finalizes = _r5_skip
    test_prune_reconcile_without_active_list_ignores_history = _r5_skip

    def test_moved_tasks_parent_is_refused_and_approval_kept(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5b-mv-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_moved")
            tasks_dir = os.path.dirname(ws_dir)
            saved = tasks_dir + ".saved"
            os.rename(tasks_dir, saved)
            with self.assertRaises(MeshError) as cm:
                self._delete(conn, home, "tsk_moved", apr, "req_mv")
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(self._approval_status(conn, apr), "approved")
            self.assertEqual(self._deleted_events(conn, "tsk_moved"), 0)
            self.assertTrue(os.path.isfile(os.path.join(saved, "tsk_moved", "keep.txt")))
            conn.close()

    def test_non_directory_target_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5b-file-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_file")
            shutil.rmtree(ws_dir)
            with open(ws_dir, "w") as f:
                f.write("not a dir\n")
            with self.assertRaises(MeshError) as cm:
                self._delete(conn, home, "tsk_file", apr, "req_file")
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.details.get("reason"), "not_a_directory")
            self.assertEqual(self._approval_status(conn, apr), "approved")
            self.assertEqual(self._deleted_events(conn, "tsk_file"), 0)
            self.assertTrue(os.path.isfile(ws_dir))
            conn.close()

    def test_already_missing_dir_still_prunes_worktrees(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5b-gone-") as tmp:
            home, conn, ws_dir, apr = self._setup_task(tmp, "tsk_gone2")
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            with transaction(conn):
                conn.execute("UPDATE projects SET repo_path = ? WHERE id = 'prj_1';", (repo,))
            shutil.rmtree(ws_dir)
            with mock.patch.object(run_handlers, "_worktree_prune_quiet") as wp:
                res = self._delete(conn, home, "tsk_gone2", apr, "req_gone2")
            self.assertFalse(res.data["deleted"])
            wp.assert_called_once_with(repo)
            self.assertEqual(self._approval_status(conn, apr), "consumed")
            conn.close()


class TestR5bPrunePlan(unittest.TestCase):
    def test_recorded_historical_prunes_stay_out_of_plan(self):
        with tempfile.TemporaryDirectory(prefix="mh-r5b-plan-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            now = now_iso_utc()
            gone = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_old")
            live = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_new")
            os.makedirs(live)
            with transaction(conn):
                for tid, wp in (("tsk_old", gone), ("tsk_new", live), ("tsk_unrec", gone + "x")):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, "
                        "max_attempts, workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES (?, 'prj_1', 'T', 'S', 'code', 'merged', 'dev', 600, 3, ?, ?, 'usr_1', ?, ?);",
                        (tid, wp, f"mesh/prj_1/{tid}", now, now),
                    )
                record_event(conn=conn, type_="workspace.pruned", actor_id="usr_1", actor_kind="user",
                             via="direct", project_id="prj_1", task_id="tsk_old",
                             payload={"task_id": "tsk_old"}, request_id="req_old")
            plan = run_handlers.plan_workspace_prune(_ctx(conn, home, "workspace.prune", {"project": "prj_1"}))
            ids = [it["task"]["id"] for it in plan["items"]]
            self.assertNotIn("tsk_old", ids)
            self.assertIn("tsk_new", ids)
            # Never recorded as pruned: still checked (branch may still exist).
            self.assertIn("tsk_unrec", ids)
            conn.close()

    def test_already_pruned_uses_one_show_ref_per_integration_worktree(self):
        fx = GitFixture()
        try:
            repo = fx.make_k0("Heads")
            fx.git(repo, "branch", "mesh/prj_1/keep")
            calls = []
            real = run_handlers._local_heads

            def _count(wt):
                calls.append(wt)
                return real(wt)

            cache = {}
            with mock.patch.object(run_handlers, "_local_heads", side_effect=_count):
                res = [run_handlers._already_pruned({"workspace_path": "/nonexistent/x", "branch": b}, repo, cache)
                       for b in ("mesh/prj_1/keep", "mesh/prj_1/gone1", "mesh/prj_1/gone2")]
            self.assertEqual(res, [False, True, True])
            self.assertEqual(calls, [repo])
        finally:
            fx.cleanup()


if __name__ == "__main__":
    unittest.main()
