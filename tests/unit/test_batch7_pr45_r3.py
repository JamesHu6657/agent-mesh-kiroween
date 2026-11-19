"""Unit regression tests for PR #45 round-3 findings and audit fixes."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.invariants import check_i7_event_hash_chain, check_i8_cancelling_and_leases
from mesh.daemon import merge, recovery
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.run import (
    _prune_skip_reason,
    execute_workspace_prune_plan,
    plan_workspace_prune,
    prepare_workspace_prune,
)
from tests.gitfixture import GitFixture

# Calls merge.run_merge_pipeline scanning real /proc.
MESH_PROC_LOCK = "shared"


def _setup_home_and_db(tmp: str):
    home = os.path.join(tmp, "home")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "workspaces"), mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "run"), mode=0o700, exist_ok=True)
    os.makedirs(os.path.join(home, "runs"), mode=0o700, exist_ok=True)
    conn = init_db(os.path.join(home, "mesh.db"))
    now = "2026-01-01T00:00:00.000Z"
    with transaction(conn):
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO principals (id, kind, name, created_at) VALUES ('agt_lead', 'agent', 'lead1', ?);",
            (now,),
        )
        conn.execute(
            "INSERT INTO agents (id, adapter, delivery, can_lead, capabilities_json, max_concurrency, created_at, updated_at) "
            "VALUES ('agt_lead', 'none', 'inbox', 1, '[\"intake\"]', 2, ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
            "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', ?, ?);",
            (now, now),
        )
        conn.execute(
            "INSERT INTO lead_assignments (id, project_id, agent_id, assigned_by, via, started_at) "
            "VALUES ('lead_1', 'prj_1', 'agt_lead', 'usr_1', 'direct', ?);",
            (now,),
        )
    return home, conn


def _ctx(
    conn,
    home: str,
    cmd: str,
    args: dict,
    *,
    principal_id: str = "usr_1",
    actor_kind: str = "user",
    via: str = "direct",
    k_user=None,
    approval: str = None,
    testing_mode: bool = True,
    dry_run: bool = False,
    request_id: str = "req_r3",
    idempotency: dict = None,
) -> HandlerContext:
    pname = "user" if actor_kind == "user" else "lead1"
    ctx = HandlerContext(
        conn=conn,
        principal={"id": principal_id, "kind": actor_kind, "name": pname},
        actor_kind=actor_kind,
        via=via,
        request_id=request_id,
        home=home,
        cmd=cmd,
        args=args,
        dry_run=dry_run,
        k_user=k_user,
        testing_mode=testing_mode,
        approval=approval,
    )
    if idempotency is not None:
        ctx.idempotency = idempotency  # type: ignore[attr-defined]
    return ctx


class TestP1_1MergeStagedIndexDetection(unittest.TestCase):
    """P1-1: test_command staging/unstaging/committing in integ must abort as integration_dirty."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P11Idx")
        self.base = self.fixture.head(self.repo, "main")

    def _make_branch(self, branch: str, files: dict, base: str = None) -> None:
        start = base or self.fixture.head(self.repo, "main")
        self.fixture.git(self.repo, "checkout", "-b", branch, start)
        for rel, content in files.items():
            full = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            self.fixture.git(self.repo, "add", rel)
        self.fixture.git(self.repo, "commit", "-q", "-m", f"branch {branch}")
        self.fixture.git(self.repo, "checkout", "-q", "main")

    def _plan(self, run_id: str, task_id: str, branch: str, pre_head: str, script: str = None) -> dict:
        run_dir = os.path.join(self.fixture.base, run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        plan = {
            "run_id": run_id,
            "task_id": task_id,
            "title": task_id,
            "integration": self.repo,
            "worktree": self.repo,
            "branch": branch,
            "pre_head": pre_head,
            "test_command": "custom" if script else "none",
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        }
        if script is not None:
            plan["test_argv"] = [sys.executable, "-c", script]
        return plan

    def test_post_test_git_add_new_file_aborted_as_integration_dirty(self) -> None:
        branch = "mesh/prj_x/tsk_add"
        self._make_branch(branch, {"feat.txt": "ok\n"})
        script = (
            "import subprocess\n"
            "open('leaked_staged.txt', 'w').write('secret_artifact\\n')\n"
            "subprocess.run(['git', 'add', 'leaked_staged.txt'], check=True)\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_add", "tsk_add", branch, self.base, script))
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "integration_dirty", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertIn("leaked_staged.txt", out["details"].get("dirty_files", []), out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "leaked_staged.txt")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # A clean re-merge must succeed without leaked_staged.txt in commit.
        ok = merge.run_merge_pipeline(self._plan("run_add_ok", "tsk_add", branch, self.base, "pass"))
        self.assertEqual(ok["outcome"], "merged", ok)
        ls = self.fixture.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertIn("feat.txt", ls)
        self.assertNotIn("leaked_staged.txt", ls)

    def test_post_test_git_rm_cached_uninvolved_and_involved_aborted(self) -> None:
        b1 = "mesh/prj_x/tsk_rm_uninv"
        self._make_branch(b1, {"feat1.txt": "f1\n"})
        script1 = (
            "import subprocess\n"
            "subprocess.run(['git', 'rm', '--cached', '-q', 'calc.py'], check=True)\n"
        )
        out1 = merge.run_merge_pipeline(self._plan("run_rm1", "tsk_rm1", b1, self.base, script1))
        self.assertEqual(out1["outcome"], "failed", out1)
        self.assertEqual(out1["reason"], "integration_dirty", out1)
        self.assertIs(out1["details"].get("restored"), True, out1)
        self.assertIn("calc.py", out1["details"].get("dirty_files", []), out1)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "calc.py")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Also git rm --cached on a file newly added/staged by the merged task branch itself.
        script2 = (
            "import subprocess\n"
            "subprocess.run(['git', 'rm', '--cached', '-q', 'feat1.txt'], check=True)\n"
        )
        out2 = merge.run_merge_pipeline(self._plan("run_rm2", "tsk_rm2", b1, self.base, script2))
        self.assertEqual(out2["outcome"], "failed", out2)
        self.assertEqual(out2["reason"], "integration_dirty", out2)
        self.assertIs(out2["details"].get("restored"), True, out2)
        self.assertIn("feat1.txt", out2["details"].get("dirty_files", []), out2)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_post_test_modify_and_add_or_chmod_aborted(self) -> None:
        b = "mesh/prj_x/tsk_mod_add"
        self._make_branch(b, {"feat_m.txt": "expected_from_branch\n"})

        # 1. Modify involved staged file feat_m.txt and re-stage (oid change on already-staged A entry)
        script_oid = (
            "import subprocess\n"
            "open('feat_m.txt', 'w').write('tampered_by_test\\n')\n"
            "subprocess.run(['git', 'add', 'feat_m.txt'], check=True)\n"
        )
        out1 = merge.run_merge_pipeline(self._plan("run_oid", "tsk_oid", b, self.base, script_oid))
        self.assertEqual(out1["outcome"], "failed", out1)
        self.assertEqual(out1["reason"], "integration_dirty", out1)
        self.assertIs(out1["details"].get("restored"), True, out1)
        self.assertIn("feat_m.txt", out1["details"].get("dirty_files", []), out1)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertTrue(self.fixture.is_clean(self.repo))

        # 2. Modify mode in index via git update-index --chmod=+x calc.py (mode change only)
        script_mode = (
            "import subprocess\n"
            "subprocess.run(['git', 'update-index', '--chmod=+x', 'calc.py'], check=True)\n"
        )
        out2 = merge.run_merge_pipeline(self._plan("run_mode", "tsk_mode", b, self.base, script_mode))
        self.assertEqual(out2["outcome"], "failed", out2)
        self.assertEqual(out2["reason"], "integration_dirty", out2)
        self.assertIs(out2["details"].get("restored"), True, out2)
        self.assertIn("calc.py", out2["details"].get("dirty_files", []), out2)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_pre_commit_bypass_guard_and_test_commit_rejected(self) -> None:
        b = "mesh/prj_x/tsk_bypass"
        self._make_branch(b, {"clean.txt": "ok\n"})

        # Even if _post_test_dirty_paths were bypassed, the pre-commit index snapshot guard catches git add.
        script_add = (
            "import subprocess\n"
            "open('bypass.txt', 'w').write('bypassed\\n')\n"
            "subprocess.run(['git', 'add', 'bypass.txt'], check=True)\n"
        )
        with mock.patch.object(merge, "_post_test_dirty_paths", return_value=[]):
            out = merge.run_merge_pipeline(self._plan("run_byp", "tsk_byp", b, self.base, script_add))
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "integration_dirty", out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "bypass.txt")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Test command making an empty commit moves HEAD and is rejected as integration_dirty.
        script_commit = (
            "import subprocess\n"
            "subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@e.invalid', "
            "'commit', '--allow-empty', '-m', 'rogue'], check=True)\n"
        )
        out_c = merge.run_merge_pipeline(self._plan("run_cmt", "tsk_cmt", b, self.base, script_commit))
        self.assertEqual(out_c["outcome"], "failed", out_c)
        self.assertEqual(out_c["reason"], "integration_dirty", out_c)
        self.assertNotIn("merge_commit", out_c)
        self.assertIs(out_c["details"].get("restored"), True, out_c)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))


class TestP1_2GitlinkAndPreMergeSnapshots(unittest.TestCase):
    """P1-2: 160000 gitlink dirs must never be rmdir'd or wiped during abort/cleanup/recovery."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P12Gitlink")
        self.base_no_sub = self.fixture.head(self.repo, "main")
        # Commit two real 160000 gitlinks (top-level and nested) plus a regular file.
        self._add_gitlink("vendor_sub", self.base_no_sub)
        self._add_gitlink("deps/nested_sub", self.base_no_sub)
        self.base = self.fixture.head(self.repo, "main")

    def _add_gitlink(self, rel: str, target_sha: str) -> None:
        self.fixture.git(
            self.repo,
            "update-index", "--add", "--cacheinfo", f"160000,{target_sha},{rel}",
        )
        os.makedirs(os.path.join(self.repo, rel), exist_ok=True)
        self.fixture.git(self.repo, "commit", "-q", "-m", f"add gitlink {rel}")

    def test_task_deletes_gitlink_and_test_fails_restores_gitlink_cleanly(self) -> None:
        branch = "mesh/prj_x/tsk_del_sub"
        self.fixture.git(self.repo, "checkout", "-b", branch, self.base)
        self.fixture.git(self.repo, "rm", "-q", "vendor_sub")
        with open(os.path.join(self.repo, "note.txt"), "w", encoding="utf-8") as f:
            f.write("removed vendor_sub\n")
        self.fixture.git(self.repo, "add", "note.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "drop vendor_sub")
        self.fixture.git(self.repo, "checkout", "-q", "main")

        run_dir = os.path.join(self.fixture.base, "run_del_sub")
        os.makedirs(run_dir, mode=0o700)
        out = merge.run_merge_pipeline({
            "run_id": "run_del_sub",
            "task_id": "tsk_del_sub",
            "title": "drop sub",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": branch,
            "pre_head": self.base,
            "test_command": "custom",
            "test_argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        })
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "tests_failed", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "deps", "nested_sub")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Subsequent merge on main succeeds rather than wedging on D vendor_sub.
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_ok2", self.base)
        with open(os.path.join(self.repo, "ok2.txt"), "w", encoding="utf-8") as f:
            f.write("ok2\n")
        self.fixture.git(self.repo, "add", "ok2.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "ok2")
        self.fixture.git(self.repo, "checkout", "-q", "main")
        run_dir2 = os.path.join(self.fixture.base, "run_ok2")
        os.makedirs(run_dir2, mode=0o700)
        out2 = merge.run_merge_pipeline({
            "run_id": "run_ok2",
            "task_id": "tsk_ok2",
            "title": "ok2",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": "mesh/prj_x/tsk_ok2",
            "pre_head": self.base,
            "test_command": "none",
            "test_argv": None,
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir2,
        })
        self.assertEqual(out2["outcome"], "merged", out2)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_decide_recovery_with_merge_head_and_missing_pre_empty_dirs_preserves_gitlinks_and_empty_dirs(self) -> None:
        os.makedirs(os.path.join(self.repo, "user_empty"), mode=0o700)
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_rec_sub", self.base)
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return a - b\n")
        self.fixture.git(self.repo, "commit", "-am", "edit calc")
        self.fixture.git(self.repo, "checkout", "-q", "main")

        # Start merge --no-ff --no-commit with no pre_empty_dirs.json (simulating test_command="none" or crash before test snapshot)
        self.fixture.git(self.repo, "merge", "--no-ff", "--no-commit", "mesh/prj_x/tsk_rec_sub")
        self.assertTrue(self.fixture.has_merge_head(self.repo))

        rec = merge.decide_recovery(self.repo, "tsk_rec_sub", "mesh/prj_x/tsk_rec_sub", self.base, run_dir=None)
        self.assertEqual(rec["outcome"], "failed", rec)
        self.assertIs(rec["details"].get("restored"), True, rec)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "deps", "nested_sub")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "user_empty")))
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_pre_merge_snapshots_persisted_for_test_command_none(self) -> None:
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_none_snap", self.base)
        with open(os.path.join(self.repo, "n.txt"), "w", encoding="utf-8") as f:
            f.write("n\n")
        self.fixture.git(self.repo, "add", "n.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "n")
        self.fixture.git(self.repo, "checkout", "-q", "main")
        run_dir = os.path.join(self.fixture.base, "run_none_snap")
        os.makedirs(run_dir, mode=0o700)
        out = merge.run_merge_pipeline({
            "run_id": "run_none_snap",
            "task_id": "tsk_none_snap",
            "title": "none snap",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": "mesh/prj_x/tsk_none_snap",
            "pre_head": self.base,
            "test_command": "none",
            "test_argv": None,
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        })
        self.assertEqual(out["outcome"], "merged", out)
        self.assertTrue(os.path.isfile(os.path.join(run_dir, merge.PRE_UNTRACKED_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(run_dir, merge.PRE_EMPTY_DIRS_NAME)))
        with open(os.path.join(run_dir, merge.PRE_EMPTY_DIRS_NAME), encoding="utf-8") as f:
            empties = json.load(f)
        self.assertIn("vendor_sub", empties)

    def test_abort_and_verify_restore_preserves_recreated_gitlink_and_nested_gitlink_contents(self) -> None:
        # Simulate initialized/custom contents inside deps/nested_sub with custom 0o555 perms,
        # and vendor_sub deleted on disk before _abort_and_verify restores from pre_head.
        nested_dir = os.path.join(self.repo, "deps", "nested_sub")
        sub_file = os.path.join(nested_dir, "keep_inside.txt")
        with open(sub_file, "w", encoding="utf-8") as f:
            f.write("submodule content\n")
        os.chmod(sub_file, 0o444)
        os.chmod(nested_dir, 0o555)
        self.addCleanup(lambda: os.path.isdir(nested_dir) and os.chmod(nested_dir, 0o755))

        os.rmdir(os.path.join(self.repo, "vendor_sub"))
        # Empty pre_empty_dirs excludes vendor_sub (e.g. if snapshot lacked it); gitlink check still protects it!
        res = merge._abort_and_verify(self.repo, self.base, pre_untracked=set(), pre_empty_dirs=set())
        self.assertIs(res.get("restored"), True, res)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
        self.assertTrue(os.path.isfile(sub_file))
        self.assertEqual(os.stat(nested_dir).st_mode & 0o777, 0o555)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_task_adds_new_gitlink_and_passing_tests_preserves_gitlink(self) -> None:
        branch = "mesh/prj_x/tsk_add_sub"
        self.fixture.git(self.repo, "checkout", "-b", branch, self.base)
        self._add_gitlink("new_vendor_sub", self.base_no_sub)
        self.fixture.git(self.repo, "checkout", "-q", "main")

        run_dir = os.path.join(self.fixture.base, "run_add_sub")
        os.makedirs(run_dir, mode=0o700)
        out = merge.run_merge_pipeline({
            "run_id": "run_add_sub",
            "task_id": "tsk_add_sub",
            "title": "add sub",
            "integration": self.repo,
            "worktree": self.repo,
            "branch": branch,
            "pre_head": self.base,
            "test_command": "custom",
            "test_argv": [sys.executable, "-c", "import os; assert os.path.isdir('new_vendor_sub')"],
            "test_timeout_s": 30,
            "kill_grace_s": 1,
            "run_dir": run_dir,
        })
        self.assertEqual(out["outcome"], "merged", out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "new_vendor_sub")))
        self.assertTrue(self.fixture.is_clean(self.repo))


class TestP2_1MergeAbortCrashWindowRecovery(unittest.TestCase):
    """P2-1: crash after git merge --abort clears MERGE_HEAD must still restore tracked/untracked/000 dirs."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("P21AbortCrash")
        self.base = self.fixture.head(self.repo, "main")
        # Add a tracked 160000 gitlink too to ensure recovery protects it alongside restore.
        self.fixture.git(
            self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor_sub"
        )
        os.makedirs(os.path.join(self.repo, "vendor_sub"), exist_ok=True)
        self.fixture.git(self.repo, "commit", "-q", "-m", "add gitlink")
        self.base = self.fixture.head(self.repo, "main")

    def test_no_merge_head_recovery_restores_dirty_tracked_and_000_dirs_idempotently(self) -> None:
        run_dir = os.path.join(self.fixture.base, "run_p21")
        os.makedirs(run_dir, mode=0o700)
        merge._save_pre_untracked(run_dir, set())
        merge._save_pre_empty_dirs(run_dir, {"vendor_sub"})

        # Task branch touches unrelated file only; tests dirtied calc.py (uninvolved),
        # deleted tests/test_calc.py, wrote artifact.tmp, created 0o000 dir & empty dir,
        # and then `git merge --abort` cleared MERGE_HEAD right before crash.
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_p21", self.base)
        with open(os.path.join(self.repo, "branch_only.txt"), "w", encoding="utf-8") as f:
            f.write("b\n")
        self.fixture.git(self.repo, "add", "branch_only.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "branch")
        self.fixture.git(self.repo, "checkout", "-q", "main")

        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("# tampered during test before abort crash\n")
        os.unlink(os.path.join(self.repo, "tests", "test_calc.py"))
        with open(os.path.join(self.repo, "artifact.tmp"), "w", encoding="utf-8") as f:
            f.write("leftover\n")
        top000 = os.path.join(self.repo, "top000")
        os.makedirs(top000, mode=0o700)
        with open(os.path.join(top000, "f"), "w", encoding="utf-8") as f:
            f.write("locked\n")
        os.chmod(top000, 0o000)
        self.addCleanup(lambda: os.path.isdir(top000) and os.chmod(top000, 0o700))
        os.makedirs(os.path.join(self.repo, "crash_empty"), mode=0o700)

        self.assertFalse(self.fixture.has_merge_head(self.repo))
        rec = merge.decide_recovery(self.repo, "tsk_p21", "mesh/prj_x/tsk_p21", self.base, run_dir=run_dir)
        self.assertEqual((rec["outcome"], rec["reason"]), ("failed", "interrupted"), rec)
        self.assertIs(rec["details"].get("restored"), True, rec)
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "tests", "test_calc.py")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "artifact.tmp")))
        self.assertFalse(os.path.exists(top000))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "crash_empty")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Second call (idempotent re-recovery) stays restored & preserves gitlink.
        rec2 = merge.decide_recovery(self.repo, "tsk_p21", "mesh/prj_x/tsk_p21", self.base, run_dir=run_dir)
        self.assertIs(rec2["details"].get("restored"), True, rec2)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))

        # Pre-existing user artifacts saved in pre_untracked.json / pre_empty_dirs.json must stay untouched
        # even when new crash artifacts alongside them are cleaned up.
        run_dir_keep = os.path.join(self.fixture.base, "run_p21_keep")
        os.makedirs(run_dir_keep, mode=0o700)
        pre_keep = os.path.join(self.repo, "preexisting.txt")
        with open(pre_keep, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        pre_empty = os.path.join(self.repo, "pre_empty")
        os.makedirs(pre_empty, mode=0o700)
        merge._save_pre_untracked(run_dir_keep, {"preexisting.txt"})
        merge._save_pre_empty_dirs(run_dir_keep, {"pre_empty", "vendor_sub"})
        with open(os.path.join(self.repo, "artifact2.tmp"), "w", encoding="utf-8") as f:
            f.write("leftover2\n")
        os.makedirs(os.path.join(self.repo, "crash_empty2"), mode=0o700)
        rec_k = merge.decide_recovery(self.repo, "tsk_p21", "mesh/prj_x/tsk_p21", self.base, run_dir=run_dir_keep)
        self.assertEqual(rec_k["details"].get("dirty_files"), ["preexisting.txt"], rec_k)
        self.assertTrue(os.path.isfile(pre_keep))
        self.assertTrue(os.path.isdir(pre_empty))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "artifact2.tmp")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "crash_empty2")))
        self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))

    def test_no_merge_head_respects_missing_snapshot_and_head_moved_guard(self) -> None:
        # 1. Without pre_untracked.json (run_dir=None), user untracked file is NOT deleted,
        # while dirty tracked calc.py IS still restored from true_pre.
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("# dirty\n")
        user_file = os.path.join(self.repo, "user_note.tmp")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write("preserve me\n")
        rec = merge.decide_recovery(self.repo, "tsk_x", None, self.base, run_dir=None)
        self.assertIs(rec["details"].get("restored"), False, rec)
        self.assertEqual(rec["details"].get("dirty_files"), ["user_note.tmp"], rec)
        self.assertTrue(os.path.isfile(user_file))
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        os.unlink(user_file)

        # 2. When head_moved=True (mismatched commit), tracked/untracked cleanup must NOT alter tree.
        self.fixture.git(self.repo, "checkout", "-b", "mesh/prj_x/tsk_m", self.base)
        with open(os.path.join(self.repo, "m.txt"), "w", encoding="utf-8") as f:
            f.write("m\n")
        self.fixture.git(self.repo, "add", "m.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "m")
        tip = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "main")
        self.fixture.git(self.repo, "merge", "--no-ff", "-q", "-m", "wrong msg without task id", tip)
        run_dir = os.path.join(self.fixture.base, "run_hm")
        os.makedirs(run_dir, mode=0o700)
        merge._save_pre_untracked(run_dir, set())
        with open(os.path.join(self.repo, "keep_on_hm.tmp"), "w", encoding="utf-8") as f:
            f.write("hm\n")
        rec_hm = merge.decide_recovery(self.repo, "tsk_m", "mesh/prj_x/tsk_m", self.base, run_dir=run_dir)
        self.assertIs(rec_hm["details"].get("head_moved"), True, rec_hm)
        self.assertIs(rec_hm["details"].get("restored"), False, rec_hm)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "keep_on_hm.tmp")))


class TestP2_2WorkspacePruneCommitsAndCheckpoints(unittest.TestCase):
    """P2-2: _commits_beyond_base fallback after dir removal & prune checkpoint in_flight/planned recovery."""

    def test_commits_beyond_base_fallback_and_stale_worktree_prune(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("P22Prune")
        base_sha = fixture.head(repo, "main")

        home = os.path.join(fixture.base, "home")
        os.makedirs(os.path.join(home, "workspaces", "prj_1", "tasks"), mode=0o700)
        integ_wt = os.path.join(home, "workspaces", "prj_1", "integration")
        fixture.git(repo, "branch", "mesh/prj_1/integration", base_sha)
        fixture.git(repo, "worktree", "add", integ_wt, "mesh/prj_1/integration")

        # 1. Cancelled task with 0 commits whose workspace dir was removed (like after `workspace delete`)
        ws_c0 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_c0")
        fixture.git(repo, "branch", "mesh/prj_1/tsk_c0", base_sha)
        fixture.git(repo, "worktree", "add", ws_c0, "mesh/prj_1/tsk_c0")
        # Remove directory manually without worktree prune (even exercises stale .git/worktrees cleanup!)
        import shutil
        shutil.rmtree(ws_c0)

        # 2. Cancelled task with 1 commit whose workspace dir was removed via `worktree remove`
        ws_c1 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_c1")
        fixture.git(repo, "branch", "mesh/prj_1/tsk_c1", base_sha)
        fixture.git(repo, "worktree", "add", ws_c1, "mesh/prj_1/tsk_c1")
        with open(os.path.join(ws_c1, "unmerged.txt"), "w", encoding="utf-8") as f:
            f.write("keep me\n")
        fixture.git(ws_c1, "add", "unmerged.txt")
        fixture.git(ws_c1, "commit", "-q", "-m", "work")
        fixture.git(integ_wt, "worktree", "remove", ws_c1)

        # 3. Cancelled task whose branch is gone -> actually check missing branch + missing dir via bad base -> commits_unknown
        ws_bad = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_cbad")
        fixture.git(repo, "branch", "mesh/prj_1/tsk_cbad", base_sha)

        conn = init_db(os.path.join(home, "mesh.db"))
        self.addCleanup(conn.close)
        now = "2026-01-01T00:00:00.000Z"
        with transaction(conn):
            conn.execute(
                "INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);",
                (now,),
            )
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', ?, 'main', 'mesh/prj_1/integration', 'usr_1', 'direct', ?, ?);",
                (repo, now, now),
            )
            for tid, wsp, br, bc in (
                ("tsk_c0", ws_c0, "mesh/prj_1/tsk_c0", base_sha),
                ("tsk_c1", ws_c1, "mesh/prj_1/tsk_c1", base_sha),
                ("tsk_cbad", ws_bad, "mesh/prj_1/tsk_cbad", "0" * 40),
            ):
                conn.execute(
                    "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                    "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                    "VALUES (?, 'prj_1', ?, 'S', 'code', 'cancelled', 'dev', 600, 3, ?, ?, ?, 'usr_1', ?, ?);",
                    (tid, tid, wsp, br, bc, now, now),
                )

        ctx = _ctx(conn, home, "workspace.prune", {"project": "prj_1"})
        r_c0 = conn.execute("SELECT * FROM tasks WHERE id = 'tsk_c0';").fetchone()
        r_c1 = conn.execute("SELECT * FROM tasks WHERE id = 'tsk_c1';").fetchone()
        r_bad = conn.execute("SELECT * FROM tasks WHERE id = 'tsk_cbad';").fetchone()
        self.assertIsNone(_prune_skip_reason(ctx, r_c0))
        self.assertEqual(_prune_skip_reason(ctx, r_c1), "unmerged_commits")
        self.assertEqual(_prune_skip_reason(ctx, r_bad), "commits_unknown")

        res = prepare_workspace_prune(ctx)
        self.assertEqual([r["task_id"] for r in res["removed"]], ["tsk_c0"], res)
        self.assertEqual(
            {s["task_id"]: s["reason"] for s in res["skipped"]},
            {"tsk_c1": "unmerged_commits", "tsk_cbad": "commits_unknown"},
            res,
        )
        branches = fixture.branches(repo, "mesh/*")
        self.assertNotIn("mesh/prj_1/tsk_c0", branches)
        self.assertIn("mesh/prj_1/tsk_c1", branches)

    def test_step_ws_reservations_reconciles_in_flight_planned_and_prevents_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3-p22ckpt-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws1 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_1")
                ws2 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_2")
                # Case A: Single-item in_flight already physically removed (no post-delete ckpt yet!)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_1', 'prj_1', 'T1', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_1', 'usr_1', ?, ?);",
                        (ws1, now, now),
                    )
                    ckpt_a = os.path.join(home, "run", "ws_prune_a.json")
                    with open(ckpt_a, "w", encoding="utf-8") as cf:
                        json.dump({
                            "root": os.path.join(home, "workspaces"),
                            "completed": False,
                            "removed": [],
                            "skipped": [],
                            "in_flight": {
                                "task_id": "tsk_1",
                                "project_id": "prj_1",
                                "status": "merged",
                                "path": ws1,
                                "branch": "mesh/prj_1/tsk_1",
                            },
                            # Real checkpoints always carry the worker's active list (review PR45 R5 P2).
                            "active_planned_ids": ["tsk_1"],
                        }, cf)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:prune_proj:prj_1", json.dumps({
                            "kind": "prune",
                            "project_id": "prj_1",
                            "projects": ["prj_1"],
                            "task_ids": ["tsk_1"],
                            "planned": [{
                                "task_id": "tsk_1",
                                "project_id": "prj_1",
                                "status": "merged",
                                "path": ws1,
                                "branch": "mesh/prj_1/tsk_1",
                                "repo_path": None,
                                "integration_branch": None,
                                "matched_root": os.path.join(home, "workspaces"),
                            }],
                            "checkpoint_path": ckpt_a,
                            "requested_by": "usr_1",
                            "request_id": "req_a",
                            "idempotency": {
                                "principal_id": "usr_1",
                                "key": "k-r3-a",
                                "cmd": "workspace.prune",
                                "request_hash": "h_a",
                            },
                        })),
                    )
                # Note: wait, tsk_1 has non-None branch and repo_path=None so let's make sure a real git repo or no-branch
                # reflects _already_pruned! Let's use no-branch OR real integ_wt! Updating task & planned with empty branch
                # simulates branch+dir gone; next test case uses real git repo too.
                with transaction(conn):
                    conn.execute("UPDATE tasks SET branch = '' WHERE id = 'tsk_1';")
                    res_val = json.loads(conn.execute("SELECT value FROM meta WHERE key = 'ws_res:prune_proj:prj_1';").fetchone()[0])
                    res_val["planned"][0]["branch"] = ""
                    conn.execute("UPDATE meta SET value = ? WHERE key = 'ws_res:prune_proj:prj_1';", (json.dumps(res_val),))
                    with open(ckpt_a, "r+", encoding="utf-8") as cf:
                        d = json.load(cf)
                        d["in_flight"]["branch"] = ""
                        cf.seek(0)
                        json.dump(d, cf)
                        cf.truncate()

                counts_a = {}
                recovery._step_ws_reservations(conn, home, counts_a)
                self.assertEqual(counts_a.get("workspace_prunes_finalized"), 1, counts_a)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = 'tsk_1';").fetchone()[0],
                    1,
                )
                idem_row = conn.execute("SELECT response_json FROM idempotency WHERE key = 'k-r3-a';").fetchone()
                self.assertIsNotNone(idem_row)
                idem_env = json.loads(idem_row[0])
                self.assertEqual([r["task_id"] for r in idem_env["data"]["removed"]], ["tsk_1"])

                # Next plan_workspace_prune excludes already-pruned tsk_1!
                plan_next = plan_workspace_prune(_ctx(conn, home, "workspace.prune", {"project": "prj_1"}))
                self.assertEqual(plan_next["items"], [])

                # Case B: 0-item prune crashing at after_ws_prune_reserve must roll back, NEVER finalize!
                ckpt_empty = os.path.join(home, "run", "ws_prune_empty.json")
                with open(ckpt_empty, "w", encoding="utf-8") as cf:
                    json.dump({"root": os.path.join(home, "workspaces"), "completed": False, "removed": [], "skipped": [], "in_flight": None}, cf)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:prune_proj:prj_1", json.dumps({
                            "kind": "prune",
                            "project_id": "prj_1",
                            "projects": ["prj_1"],
                            "task_ids": [],
                            "planned": [],
                            "checkpoint_path": ckpt_empty,
                            "requested_by": "usr_1",
                            "request_id": "req_empty",
                            "idempotency": {
                                "principal_id": "usr_1",
                                "key": "k-r3-empty",
                                "cmd": "workspace.prune",
                                "request_hash": "h_empty",
                            },
                        })),
                    )
                counts_e = {}
                recovery._step_ws_reservations(conn, home, counts_e)
                self.assertEqual(counts_e.get("workspace_prunes_rolled_back"), 1, counts_e)
                self.assertIsNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-r3-empty';").fetchone())

                # Case C: Two-item [tsk_1(removed), tsk_2(still on disk)] -> emits pruned for tsk_1 AND rolls back!
                os.makedirs(ws2, mode=0o700)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_2', 'prj_1', 'T2', 'S', 'code', 'merged', 'dev', 600, 3, ?, '', 'usr_1', ?, ?);",
                        (ws2, now, now),
                    )
                    ckpt_b = os.path.join(home, "run", "ws_prune_b.json")
                    with open(ckpt_b, "w", encoding="utf-8") as cf:
                        json.dump({
                            "root": os.path.join(home, "workspaces"),
                            "completed": False,
                            "removed": [],
                            "skipped": [],
                            "in_flight": {"task_id": "tsk_1", "project_id": "prj_1", "status": "merged", "path": ws1, "branch": ""},
                        }, cf)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:prune_proj:prj_1", json.dumps({
                            "kind": "prune",
                            "project_id": "prj_1",
                            "projects": ["prj_1"],
                            "task_ids": ["tsk_1", "tsk_2"],
                            "planned": [
                                {"task_id": "tsk_1", "project_id": "prj_1", "status": "merged", "path": ws1, "branch": "", "repo_path": None, "integration_branch": None, "matched_root": os.path.join(home, "workspaces")},
                                {"task_id": "tsk_2", "project_id": "prj_1", "status": "merged", "path": ws2, "branch": "", "repo_path": None, "integration_branch": None, "matched_root": os.path.join(home, "workspaces")},
                            ],
                            "checkpoint_path": ckpt_b,
                            "requested_by": "usr_1",
                            "request_id": "req_b",
                            "idempotency": {"principal_id": "usr_1", "key": "k-r3-b", "cmd": "workspace.prune", "request_hash": "h_b"},
                        })),
                    )
                counts_b = {}
                recovery._step_ws_reservations(conn, home, counts_b)
                self.assertEqual(counts_b.get("workspace_prunes_rolled_back"), 1, counts_b)
                # Notice request_id req_b got 1 event for tsk_1 (previously 1 under req_a)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = 'tsk_1' AND request_id = 'req_b';").fetchone()[0],
                    1,
                )
                self.assertIsNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-r3-b';").fetchone())
                self.assertEqual(check_i7_event_hash_chain(conn), [])
                self.assertEqual(check_i8_cancelling_and_leases(conn), [])
            finally:
                conn.close()


class TestAuditFixesDeleteAndPruneExceptionWindows(unittest.TestCase):
    """Audit pass fixes: exceptions after physical disk delete in delete & prune must still audit/consume."""

    def test_run_workspace_delete_exception_after_disk_delete_records_audit_and_consumes_approval(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r3-audel-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_d")
                os.makedirs(ws_dir, mode=0o700)
                with open(os.path.join(ws_dir, "f.txt"), "w", encoding="utf-8") as f:
                    f.write("data\n")
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_d', 'prj_1', 'D', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_d', 'usr_1', ?, ?);",
                        (ws_dir, now, now),
                    )
                    apr = create_or_get_pending_approval(
                        conn=conn, category="delete", op="workspace.delete", requested_by="agt_lead",
                        params={"task_id": "tsk_d"}, summary="Del", project_id="prj_1", target_id="tsk_d",
                        testing_mode=True,
                    )
                    handle_approval_approve(_ctx(
                        conn, home, "approval.approve",
                        {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        k_user=b"\x01" * 32,
                    ))

                orig_del = run_handlers._delete_workspace_dir_sync

                def _fail_after_rmtree(*args, **kwargs):
                    orig_del(*args, **kwargs)
                    raise RuntimeError("boom_after_rmtree")

                class _Srv:
                    def track_merge_job(self, job):
                        pass

                ctx = _ctx(
                    conn, home, "workspace.delete", {"task_id": "tsk_d"},
                    principal_id="agt_lead", actor_kind="agent", via="token",
                    approval=apr["approval_id"], request_id="req_audel",
                )
                with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_fail_after_rmtree):
                    with self.assertRaises(RuntimeError):
                        asyncio.run(run_handlers.run_workspace_delete(_Srv(), ctx))

                self.assertFalse(os.path.exists(ws_dir))
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()[0],
                    "consumed",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_d';").fetchone()[0],
                    1,
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)
            finally:
                conn.close()

    def test_run_workspace_prune_exception_with_in_flight_records_audit(self) -> None:
        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("AudPrn")
        base_sha = fixture.head(repo, "main")

        home = os.path.join(fixture.base, "home")
        os.makedirs(os.path.join(home, "workspaces", "prj_1", "tasks"), mode=0o700)
        os.makedirs(os.path.join(home, "run"), mode=0o700)
        integ_wt = os.path.join(home, "workspaces", "prj_1", "integration")
        fixture.git(repo, "branch", "mesh/prj_1/integration", base_sha)
        fixture.git(repo, "worktree", "add", integ_wt, "mesh/prj_1/integration")
        ws1 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_p1")
        fixture.git(repo, "branch", "mesh/prj_1/tsk_p1", base_sha)
        fixture.git(repo, "worktree", "add", ws1, "mesh/prj_1/tsk_p1")

        conn = init_db(os.path.join(home, "mesh.db"))
        self.addCleanup(conn.close)
        now = "2026-01-01T00:00:00.000Z"
        with transaction(conn):
            conn.execute("INSERT INTO principals (id, kind, name, created_at) VALUES ('usr_1', 'user', 'user', ?);", (now,))
            conn.execute(
                "INSERT INTO projects (id, name, goal, status, repo_path, base_branch, integration_branch, created_by, created_via, created_at, updated_at) "
                "VALUES ('prj_1', 'P', 'G', 'active', ?, 'main', 'mesh/prj_1/integration', 'usr_1', 'direct', ?, ?);",
                (repo, now, now),
            )
            conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES ('tsk_p1', 'prj_1', 'P1', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_p1', ?, 'usr_1', ?, ?);",
                (ws1, base_sha, now, now),
            )

        orig_rm = run_handlers._remove_worktree_and_branch_with_roots
        orig_write = run_handlers._write_ws_prune_checkpoint
        removed_once = [False]

        def _rm_wrap(*args, **kwargs):
            res = orig_rm(*args, **kwargs)
            removed_once[0] = True
            return res

        def _write_boom(path, doc):
            # Trigger on the post-delete checkpoint write right after physical removal completes.
            if removed_once[0]:
                raise RuntimeError("boom_before_post_delete_ckpt")
            return orig_write(path, doc)

        class _Srv:
            def track_merge_job(self, job):
                pass

        ctx = _ctx(conn, home, "workspace.prune", {"project": "prj_1"}, request_id="req_auprn")
        with mock.patch.object(run_handlers, "_write_ws_prune_checkpoint", side_effect=_write_boom), \
             mock.patch.object(run_handlers, "_remove_worktree_and_branch_with_roots", side_effect=_rm_wrap):
            with self.assertRaises(RuntimeError):
                asyncio.run(run_handlers.run_workspace_prune(_Srv(), ctx))

        self.assertFalse(os.path.exists(ws1))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = 'tsk_p1';").fetchone()[0],
            1,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
