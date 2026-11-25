"""Unit regression tests for PR #45 round-4 findings (P1-1, P1-2, P2-3, P2-4, P2-5)."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import gitops
from mesh.core.approvals import create_or_get_pending_approval
from mesh.core.clock import now_iso_utc
from mesh.core.db import init_db, transaction
from mesh.core.invariants import check_i7_event_hash_chain, check_i8_cancelling_and_leases
from mesh.daemon import merge, recovery
from mesh.daemon.handlers import run as run_handlers
from mesh.daemon.handlers.approval import handle_approval_approve
from mesh.daemon.handlers.base import HandlerContext
from mesh.daemon.handlers.run import (
    _remove_worktree_and_branch_with_roots,
    execute_workspace_prune_plan,
    plan_workspace_prune,
    prepare_workspace_prune,
)
from mesh.errors import MeshError
from tests.gitfixture import GitFixture
from tests.helpers import fake_peer_creds, restore_accessible_for_test, set_inaccessible_for_test

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    request_id: str = "req_r4",
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


class _MergeFixtureBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("PR45R4")
        self.base = self.fixture.head(self.repo, "main")

    def _make_branch(self, branch: str, files: dict, base: str = None) -> str:
        start = base or self.fixture.head(self.repo, "main")
        self.fixture.git(self.repo, "checkout", "-b", branch, start)
        for rel, content in files.items():
            full = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            self.fixture.git(self.repo, "add", rel)
        self.fixture.git(self.repo, "commit", "-q", "-m", f"branch {branch}")
        tip = self.fixture.head(self.repo)
        self.fixture.git(self.repo, "checkout", "-q", "main")
        return tip

    def _plan(self, run_id: str, task_id: str, branch: str, pre_head: str, script: str = None) -> dict:
        run_dir = os.path.join(self.fixture.base, run_id)
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        plan = {
            "run_id": run_id,
            "task_id": task_id,
            "title": task_id,
            "integration": self.repo,
            "integration_branch": "main",
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


class TestP1_1TestCommitAndClearedMergeHead(_MergeFixtureBase):
    """P1-1: test_command commits, branch/detach switches, cleared MERGE_HEAD, and crash recovery."""

    def test_rogue_commit_with_added_file_resets_branch_restores_tree_and_allows_next_merge(self) -> None:
        branch = "mesh/prj_1/tsk_1"
        self._make_branch(branch, {"clean.txt": "ok\n"})
        script = (
            "import subprocess\n"
            "open('evil.txt', 'w').write('unreviewed\\n')\n"
            "subprocess.run(['git', 'add', 'evil.txt'], check=True)\n"
            "subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@e.invalid', "
            "'commit', '-m', 'rogue'], check=True)\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_rogue1", "tsk_1", branch, self.base, script))
        self.assertEqual(out["outcome"], "failed", out)
        self.assertEqual(out["reason"], "integration_dirty", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "clean.txt")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Subsequent merge of the same task succeeds cleanly without evil.txt!
        ok = merge.run_merge_pipeline(self._plan("run_clean1", "tsk_1", branch, self.base, "pass"))
        self.assertEqual(ok["outcome"], "merged", ok)
        ls = self.fixture.git(self.repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines()
        self.assertIn("clean.txt", ls)
        self.assertNotIn("evil.txt", ls)

    def test_rogue_commit_naming_task_and_crash_before_commit_ready_not_reconciled(self) -> None:
        branch = "mesh/prj_1/tsk_rogue_rec"
        tip = self._make_branch(branch, {"clean.txt": "ok\n"})
        run_dir = os.path.join(self.fixture.base, "run_rogue_rec")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)

        # Simulate pre-merge step + git merge --no-ff --no-commit where test runs rogue commit and daemon crashes.
        with mock.patch.object(
            merge,
            "run_test_step",
            side_effect=RuntimeError("crash_after_rogue_commit"),
        ):
            pass  # First verify live DecideRecovery path with snapshots written before test commit:

        # Drive real pipeline up to run_test_step by writing rogue commit inside run_test_step mock then simulating crash.
        for idx, msg in enumerate((f"Merge branch '{branch}'", "Merge mesh task tsk_rogue_rec: pwned")):
            with self.subTest(msg=msg):
                repo_sub = self.fixture.make_k0(f"RogueRec_{idx}")
                base_sub = self.fixture.head(repo_sub, "main")
                self.fixture.git(repo_sub, "checkout", "-b", branch, base_sub)
                with open(os.path.join(repo_sub, "clean.txt"), "w", encoding="utf-8") as f:
                    f.write("ok\n")
                self.fixture.git(repo_sub, "add", "clean.txt")
                self.fixture.git(repo_sub, "commit", "-q", "-m", "clean")
                tip_sub = self.fixture.head(repo_sub)
                self.fixture.git(repo_sub, "checkout", "-q", "main")

                rdir = os.path.join(self.fixture.base, f"rdir_{idx}")
                os.makedirs(rdir, mode=0o700, exist_ok=True)

                def _rogue_then_kill(_plan):
                    with open(os.path.join(repo_sub, "evil.txt"), "w", encoding="utf-8") as ef:
                        ef.write("unreviewed\n")
                    self.fixture.git(repo_sub, "add", "evil.txt")
                    self.fixture.git(repo_sub, "commit", "-m", msg, identity=True)
                    raise BaseException("simulated_sigkill_before_tx2")

                with mock.patch.object(merge, "run_test_step", side_effect=_rogue_then_kill):
                    with self.assertRaises(BaseException):
                        merge.run_merge_pipeline({
                            "run_id": "run_rr",
                            "task_id": "tsk_rogue_rec",
                            "title": "Legit Title",
                            "integration": repo_sub,
                            "integration_branch": "main",
                            "worktree": repo_sub,
                            "branch": branch,
                            "pre_head": base_sub,
                            "test_command": "custom",
                            "test_argv": [sys.executable, "-c", "pass"],
                            "test_timeout_s": 30,
                            "kill_grace_s": 1,
                            "run_dir": rdir,
                        })

                rec = merge.decide_recovery(repo_sub, "tsk_rogue_rec", branch, base_sub, run_dir=rdir)
                self.assertEqual((rec["outcome"], rec["reason"]), ("failed", "interrupted"), rec)
                self.assertIs(rec["details"].get("restored"), True, rec)
                self.assertEqual(self.fixture.head(repo_sub, "main"), base_sub)
                self.assertFalse(self.fixture.has_merge_head(repo_sub))
                self.assertFalse(os.path.exists(os.path.join(repo_sub, "evil.txt")))
                self.assertFalse(os.path.exists(os.path.join(repo_sub, "clean.txt")))
                self.assertTrue(self.fixture.is_clean(repo_sub))
                self.assertEqual(
                    self.fixture.git(repo_sub, "rev-parse", f"refs/heads/{branch}").stdout.strip(),
                    tip_sub,
                )

    def test_merge_abort_then_stage_and_checkout_or_detach_and_exception_cleanup(self) -> None:
        branch = "mesh/prj_1/tsk_abort_stage"
        self._make_branch(branch, {"clean.txt": "ok\n"})

        # 1. Test runs `git merge --abort` + `git add extra.txt` and exits 1 -> tests_failed & restored=True
        script_fail = (
            "import subprocess, sys\n"
            "subprocess.run(['git', 'merge', '--abort'], check=True)\n"
            "open('extra.txt', 'w').write('leaked\\n')\n"
            "subprocess.run(['git', 'add', 'extra.txt'], check=True)\n"
            "sys.exit(1)\n"
        )
        out1 = merge.run_merge_pipeline(self._plan("run_as1", "tsk_as1", branch, self.base, script_fail))
        self.assertEqual((out1["outcome"], out1["reason"]), ("failed", "tests_failed"), out1)
        self.assertIs(out1["details"].get("restored"), True, out1)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "extra.txt")))
        self.assertTrue(self.fixture.is_clean(self.repo))

        # 2. Test switches branch via `git checkout -b stray` and commits -> reverted back to main at base!
        script_branch = (
            "import subprocess\n"
            "subprocess.run(['git', 'checkout', '-b', 'stray_branch'], check=True)\n"
            "open('stray.txt', 'w').write('stray\\n')\n"
            "subprocess.run(['git', 'add', 'stray.txt'], check=True)\n"
            "subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@e.invalid', "
            "'commit', '-m', 'stray'], check=True)\n"
        )
        out2 = merge.run_merge_pipeline(self._plan("run_sb", "tsk_sb", branch, self.base, script_branch))
        self.assertEqual((out2["outcome"], out2["reason"]), ("failed", "integration_dirty"), out2)
        self.assertIs(out2["details"].get("restored"), True, out2)
        self.assertEqual(
            self.fixture.git(self.repo, "rev-parse", "--symbolic-full-name", "HEAD").stdout.strip(),
            "refs/heads/main",
        )
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertTrue(self.fixture.is_clean(self.repo))

        # 3. Test runs `git commit` then an unexpected Exception occurs before _post_test_dirty_paths finishes:
        #    _run_merge_pipeline except block still cleans up even though MERGE_HEAD is gone!
        def _commit_then_boom(_plan):
            with open(os.path.join(self.repo, "evil_exc.txt"), "w", encoding="utf-8") as f:
                f.write("evil\n")
            self.fixture.git(self.repo, "add", "evil_exc.txt")
            self.fixture.git(self.repo, "commit", "-m", "rogue", identity=True)
            return {
                "passed": True,
                "log_path": os.path.join(_plan["run_dir"], "test.log"),
                "timeout": False,
                "timeout_s": 30,
                "exit_code": 0,
                "signal": None,
            }

        with mock.patch.object(merge, "run_test_step", side_effect=_commit_then_boom), \
             mock.patch.object(merge, "_post_test_dirty_paths", side_effect=RuntimeError("boom_post")):
            out3 = merge.run_merge_pipeline(self._plan("run_exc", "tsk_exc", branch, self.base, "pass"))
        self.assertEqual(out3["outcome"], "failed", out3)
        self.assertIs(out3["details"].get("restored"), True, out3)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "evil_exc.txt")))
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_commit_ready_reconciles_legitimate_merge_and_rejects_mismatched_title(self) -> None:
        branch = "mesh/prj_1/tsk_cready"
        self._make_branch(branch, {"ok.txt": "ok\n"})
        run_dir = os.path.join(self.fixture.base, "run_cready")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        # 1. Genuine Mesh merge with commit_ready.json present (after_merge_commit crash).
        with mock.patch.object(merge, "_maybe_crash_after_commit", side_effect=RuntimeError("crash_here")):
            out = merge.run_merge_pipeline({
                "run_id": "run_cready",
                "task_id": "tsk_cready",
                "title": "Real Title",
                "integration": self.repo,
                "integration_branch": "main",
                "worktree": self.repo,
                "branch": branch,
                "pre_head": self.base,
                "test_command": "none",
                "test_timeout_s": 30,
                "kill_grace_s": 1,
                "run_dir": run_dir,
            })
        self.assertEqual(out["outcome"], "failed", out)
        # Because commit succeeded before _maybe_crash_after_commit raised, commit stays on HEAD and decide_recovery reconciles it.
        mc = self.fixture.head(self.repo, "main")
        self.assertNotEqual(mc, self.base)
        rec = merge.decide_recovery(self.repo, "tsk_cready", branch, self.base, run_dir=run_dir)
        self.assertEqual(rec["outcome"], "merged", rec)
        self.assertEqual(rec["merge_commit"], mc)
        self.assertEqual(rec["pre_head"], self.base)

        # 2. Mismatched title / message against commit_ready.json must NOT reconcile; rolls back to pre_head!
        repo2 = self.fixture.make_k0("CReadyMismatch")
        base2 = self.fixture.head(repo2, "main")
        b2 = "mesh/prj_1/tsk_cr_bad"
        self.fixture.git(repo2, "checkout", "-b", b2, base2)
        with open(os.path.join(repo2, "ok2.txt"), "w", encoding="utf-8") as f:
            f.write("ok2\n")
        self.fixture.git(repo2, "add", "ok2.txt")
        self.fixture.git(repo2, "commit", "-q", "-m", "ok2")
        tip2 = self.fixture.head(repo2)
        self.fixture.git(repo2, "checkout", "-q", "main")
        rdir2 = os.path.join(self.fixture.base, "run_cready_bad")
        os.makedirs(rdir2, mode=0o700, exist_ok=True)
        with open(os.path.join(repo2, ".git", "HEAD"), "r", encoding="utf-8") as hf:
            head_raw = hf.read()
        with open(os.path.join(repo2, ".git", "index"), "rb") as ixf:
            idx_bytes = ixf.read()
        merge._save_pre_untracked(rdir2, set())
        merge._save_pre_empty_dirs(rdir2, set())
        merge._save_pre_merge_state(
            rdir2, task_id="tsk_cr_bad", pre_head=base2, tip=tip2, symref="refs/heads/main",
            head_raw=head_raw, index_bytes=idx_bytes,
        )
        merge._save_commit_ready(
            rdir2, task_id="tsk_cr_bad", pre_head=base2, tip=tip2, symref="refs/heads/main",
            message="Merge mesh task tsk_cr_bad: Expected Title",
        )
        # Rogue merge commit using wrong title (same parents + task_id prefix)
        self.fixture.git(repo2, "merge", "--no-ff", "-q", "-m", "Merge mesh task tsk_cr_bad: Spoofed Title", tip2)
        rec_bad = merge.decide_recovery(repo2, "tsk_cr_bad", b2, base2, run_dir=rdir2)
        self.assertEqual((rec_bad["outcome"], rec_bad["reason"]), ("failed", "interrupted"), rec_bad)
        self.assertIs(rec_bad["details"].get("restored"), True, rec_bad)
        self.assertEqual(self.fixture.head(repo2, "main"), base2)
        self.assertTrue(self.fixture.is_clean(repo2))


class TestP1_2IndexHideFlagsBypass(_MergeFixtureBase):
    """P1-2: --skip-worktree and --assume-unchanged must not bypass dirty checks or break future merges."""

    def test_dirc_index_flag_parser_and_clearer_v2_v3_v4(self) -> None:
        for idx_ver in (2, 3, 4):
            with self.subTest(idx_ver=idx_ver):
                repo = self.fixture.make_k0(f"DircV{idx_ver}")
                long_rel = "sub/" + ("a" * 70) + ".py"
                os.makedirs(os.path.join(repo, "sub"), exist_ok=True)
                with open(os.path.join(repo, long_rel), "w", encoding="utf-8") as f:
                    f.write("x = 1\n")
                self.fixture.git(repo, "add", long_rel)
                self.fixture.git(repo, "commit", "-q", "-m", "add long")
                self.fixture.git(repo, "update-index", "--index-version", str(idx_ver))
                self.fixture.git(repo, "update-index", "--assume-unchanged", "calc.py")
                self.fixture.git(repo, "update-index", "--skip-worktree", long_rel)

                flags = merge._read_index_flags(repo)
                self.assertEqual(flags.get("calc.py"), (True, False, False))
                self.assertEqual(flags.get(long_rel), (False, True, False))

                cleared = merge._clear_index_hide_flags(repo)
                self.assertTrue(cleared)
                self.assertEqual(merge._read_index_flags(repo), {})

    def test_skip_worktree_and_assume_unchanged_aborted_restored_and_next_merge_ok(self) -> None:
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()

        for flag in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(flag=flag):
                b1 = f"mesh/prj_1/tsk_2a_{flag.lstrip('-')}"
                b2 = f"mesh/prj_1/tsk_2b_{flag.lstrip('-')}"
                self._make_branch(b1, {f"f_{flag.lstrip('-')}.txt": "2a\n"})
                self._make_branch(b2, {"calc.py": f"# updated cleanly {flag}\ndef add(a, b):\n    return a + b\n"})

                # 1. Passing test modifies calc.py and hides it via flag -> integration_dirty & restored!
                script_pass = (
                    "import subprocess\n"
                    "open('calc.py', 'w').write('# CORRUPTED\\n')\n"
                    f"subprocess.run(['git', 'update-index', {flag!r}, 'calc.py'], check=True)\n"
                )
                out1 = merge.run_merge_pipeline(self._plan(f"run_{flag}_p", "tsk_2a", b1, self.base, script_pass))
                self.assertEqual(out1["outcome"], "failed", out1)
                self.assertEqual(out1["reason"], "integration_dirty", out1)
                self.assertIs(out1["details"].get("restored"), True, out1)
                self.assertIn("calc.py", out1["details"].get("dirty_files", []), out1)
                with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), orig_calc)
                self.assertEqual(merge._read_index_flags(self.repo), {})
                self.assertTrue(self.fixture.is_clean(self.repo))

                # 2. Subsequent merge modifying calc.py MUST succeed (not fail git_failed!).
                out2 = merge.run_merge_pipeline(self._plan(f"run_{flag}_next", "tsk_2b", b2, self.base, "pass"))
                self.assertEqual(out2["outcome"], "merged", out2)
                self.assertTrue(self.fixture.is_clean(self.repo))

                # Reset repo base reference for next subTest cleanly using fixture repo.
                self.base = self.fixture.head(self.repo, "main")
                with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
                    orig_calc = f.read()

    def test_failing_test_and_pre_commit_guard_and_recovery_with_hide_flags(self) -> None:
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            orig_calc = f.read()
        b = "mesh/prj_1/tsk_inv_sw"
        self._make_branch(b, {"inv.txt": "from_branch\n"})

        # 1. Failing test hides involved file edit AND uninvolved calc.py edit -> tests_failed & restored!
        script_fail = (
            "import subprocess, sys\n"
            "open('inv.txt', 'w').write('corrupt_inv\\n')\n"
            "open('calc.py', 'w').write('corrupt_calc\\n')\n"
            "subprocess.run(['git', 'update-index', '--skip-worktree', 'inv.txt', 'calc.py'], check=True)\n"
            "sys.exit(1)\n"
        )
        out_f = merge.run_merge_pipeline(self._plan("run_swf", "tsk_swf", b, self.base, script_fail))
        self.assertEqual((out_f["outcome"], out_f["reason"]), ("failed", "tests_failed"), out_f)
        self.assertIs(out_f["details"].get("restored"), True, out_f)
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "inv.txt")))
        self.assertEqual(merge._read_index_flags(self.repo), {})
        self.assertTrue(self.fixture.is_clean(self.repo))

        # 2. Pre-commit guard catches flag even if _post_test_dirty_paths mocked empty.
        script_guard = (
            "import subprocess\n"
            "open('calc.py', 'w').write('guard_bypass\\n')\n"
            "subprocess.run(['git', 'update-index', '--assume-unchanged', 'calc.py'], check=True)\n"
        )
        with mock.patch.object(merge, "_post_test_dirty_paths", return_value=[]):
            out_g = merge.run_merge_pipeline(self._plan("run_swg", "tsk_swg", b, self.base, script_guard))
        self.assertEqual((out_g["outcome"], out_g["reason"]), ("failed", "integration_dirty"), out_g)
        self.assertIs(out_g["details"].get("restored"), True, out_g)
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertEqual(merge._read_index_flags(self.repo), {})

        # 3. Crash after merge --abort with --skip-worktree on calc.py -> decide_recovery clears & restores!
        run_dir = os.path.join(self.fixture.base, "run_rec_sw")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        merge._save_pre_untracked(run_dir, set())
        merge._save_pre_empty_dirs(run_dir, set())
        with open(os.path.join(self.repo, "calc.py"), "w", encoding="utf-8") as f:
            f.write("# crash_sw\n")
        self.fixture.git(self.repo, "update-index", "--skip-worktree", "calc.py")
        rec = merge.decide_recovery(self.repo, "tsk_rec_sw", b, self.base, run_dir=run_dir)
        self.assertIs(rec["details"].get("restored"), True, rec)
        with open(os.path.join(self.repo, "calc.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), orig_calc)
        self.assertEqual(merge._read_index_flags(self.repo), {})
        self.assertTrue(self.fixture.is_clean(self.repo))


class TestP2_3GitlinkSymlinkAndEmptyCleanup(_MergeFixtureBase):
    """P2-3: Symlinked gitlinks must not pollute skip_paths_real; uninitialized empty gitlinks must clean test junk."""

    def setUp(self) -> None:
        super().setUp()
        self.fixture.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor_sub")
        os.makedirs(os.path.join(self.repo, "vendor_sub"), exist_ok=True)
        self.fixture.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},deps/nested_sub")
        os.makedirs(os.path.join(self.repo, "deps", "nested_sub"), exist_ok=True)
        self.fixture.git(self.repo, "commit", "-q", "-m", "add gitlinks")
        self.base = self.fixture.head(self.repo, "main")
        self.addCleanup(lambda: os.path.isdir(os.path.join(self.repo, "tests")) and os.chmod(os.path.join(self.repo, "tests"), 0o755))

    def test_symlinked_gitlink_and_ancestor_fail_and_pass_do_not_bypass_chmod_or_crash(self) -> None:
        with open(os.path.join(self.repo, "tests", "test_calc.py"), encoding="utf-8") as f:
            orig_test = f.read()
        b = "mesh/prj_1/tsk_gl_sym"
        self._make_branch(b, {"ok_sym.txt": "ok\n"})

        for exit_code, exp_reason in ((1, "tests_failed"), (0, "integration_dirty")):
            with self.subTest(exit_code=exit_code):
                script = (
                    "import os, sys\n"
                    f"sys.path.insert(0, {_REPO_ROOT!r})\n"
                    "from tests.helpers import set_inaccessible_for_test\n"
                    "open('tests/test_calc.py', 'w').write('# dirtied\\n')\n"
                    "set_inaccessible_for_test('tests')\n"
                    "os.rmdir('vendor_sub')\n"
                    "os.symlink('.', 'vendor_sub', target_is_directory=True)\n"
                    "os.rmdir('deps/nested_sub')\n"
                    "os.rmdir('deps')\n"
                    "os.symlink('.', 'deps', target_is_directory=True)\n"
                    f"sys.exit({exit_code})\n"
                )
                out = merge.run_merge_pipeline(self._plan(f"run_gl_{exit_code}", "tsk_gl", b, self.base, script))
                self.assertEqual((out["outcome"], out["reason"]), ("failed", exp_reason), out)
                self.assertIs(out["details"].get("restored"), True, out)
                self.assertFalse(os.path.islink(os.path.join(self.repo, "vendor_sub")))
                self.assertTrue(os.path.isdir(os.path.join(self.repo, "vendor_sub")))
                self.assertFalse(os.path.islink(os.path.join(self.repo, "deps")))
                self.assertTrue(os.path.isdir(os.path.join(self.repo, "deps", "nested_sub")))
                with open(os.path.join(self.repo, "tests", "test_calc.py"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), orig_test)
                self.assertTrue(self.fixture.is_clean(self.repo))

    def test_uninitialized_empty_gitlink_cleaned_while_populated_submodule_preserved(self) -> None:
        # Populate deps/nested_sub BEFORE merge (simulating initialized submodule), leave vendor_sub empty (uninitialized).
        nested_dir = os.path.join(self.repo, "deps", "nested_sub")
        keep_file = os.path.join(nested_dir, "keep_inside.txt")
        with open(keep_file, "w", encoding="utf-8") as f:
            f.write("keep\n")
        os.chmod(keep_file, 0o444)
        os.chmod(nested_dir, 0o555)
        self.addCleanup(lambda: os.path.isdir(nested_dir) and os.chmod(nested_dir, 0o755))

        b = "mesh/prj_1/tsk_gl_leak"
        self._make_branch(b, {"clean_gl.txt": "ok\n"})
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {_REPO_ROOT!r})\n"
            "from tests.helpers import set_inaccessible_for_test\n"
            "os.makedirs('vendor_sub/subcache/d000', exist_ok=True)\n"
            "open('vendor_sub/leaked.txt', 'w').write('leftover\\n')\n"
            "open('vendor_sub/subcache/d000/inner.txt', 'w').write('x')\n"
            "set_inaccessible_for_test('vendor_sub/subcache/d000')\n"
            "os.makedirs('vendor_sub/.git', exist_ok=True)\n"
            "open('vendor_sub/.git/HEAD', 'w').write('ref: refs/heads/main\\n')\n"
        )
        out = merge.run_merge_pipeline(self._plan("run_gl_leak", "tsk_gl_leak", b, self.base, script))
        self.assertEqual(out["outcome"], "merged", out)
        self.assertEqual(os.listdir(os.path.join(self.repo, "vendor_sub")), [])
        self.assertTrue(os.path.isfile(keep_file))
        if sys.platform != "win32":
            self.assertEqual(os.stat(nested_dir).st_mode & 0o777, 0o555)
        self.assertTrue(self.fixture.is_clean(self.repo))

        # Direct _post_test_dirty_paths guard flags non-empty empty-snapshot gitlink if uncleaned.
        os.makedirs(os.path.join(self.repo, "vendor_sub", "residual"), exist_ok=True)
        try:
            dirty = merge._post_test_dirty_paths(
                self.repo, pre_untracked=set(), pre_empty_dirs={"vendor_sub"}, pre_head=self.fixture.head(self.repo, "main")
            )
            self.assertIn("vendor_sub", dirty)
        finally:
            shutil.rmtree(os.path.join(self.repo, "vendor_sub", "residual"), ignore_errors=True)


class TestP2_4PruneOffLoopNoTxGitAndBranchCheck(unittest.TestCase):
    """P2-4: plan_workspace_prune / dry-run / except BaseException never run git in TX or on event loop; missing integ_wt fails branch delete."""

    def test_plan_dry_run_live_and_except_never_call_git_on_loop_or_in_transaction(self) -> None:
        from tests.unit.test_batch7_pr45_r2 import _win_safe_tempdir

        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo = fixture.make_k0("P24Prn")
        base_sha = fixture.head(repo, "main")

        with _win_safe_tempdir(prefix="mh-r4-p24-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                fixture.allow_root(home)
                now = "2026-01-01T00:00:00.000Z"
                integ_wt = os.path.join(home, "workspaces", "prj_1", "integration")
                os.makedirs(os.path.dirname(integ_wt), mode=0o700, exist_ok=True)
                fixture.git(repo, "branch", "mesh/prj_1/integration", base_sha)
                fixture.git(repo, "worktree", "add", integ_wt, "mesh/prj_1/integration")

                # Historical already-pruned task (branch deleted, dir deleted, still in DB as merged)
                ws_old = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_old")
                # Active merged task still needing prune
                ws_act = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_act")
                fixture.git(repo, "worktree", "add", "-b", "mesh/prj_1/tsk_act", ws_act, base_sha)

                with transaction(conn):
                    conn.execute(
                        "UPDATE projects SET repo_path = ?, base_branch = 'main', integration_branch = 'mesh/prj_1/integration' WHERE id = 'prj_1';",
                        (repo,),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                        "VALUES ('tsk_old', 'prj_1', 'Old', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_old', ?, 'usr_1', ?, ?);",
                        (ws_old, base_sha, now, now),
                    )
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                        "VALUES ('tsk_act', 'prj_1', 'Act', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_act', ?, 'usr_1', ?, ?);",
                        (ws_act, base_sha, now, now),
                    )

                calls = []
                loop_tid = [None]
                orig_run_git = gitops.run_git

                def _spy_run_git(args, *a, **kw):
                    # conn.in_transaction is safe to read only from owning thread; record False when off-thread.
                    on_loop = threading.get_ident() == loop_tid[0]
                    in_tx = bool(conn.in_transaction) if on_loop else False
                    calls.append((list(args), on_loop, in_tx))
                    return orig_run_git(args, *a, **kw)

                # 1. plan_workspace_prune (even inside a TX!) makes ZERO git calls.
                with mock.patch.object(gitops, "run_git", side_effect=_spy_run_git):
                    loop_tid[0] = threading.get_ident()
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        _ = plan_workspace_prune(_ctx(conn, home, "workspace.prune", {"project": "prj_1"}))
                    finally:
                        conn.execute("ROLLBACK;")
                self.assertEqual(calls, [], f"plan_workspace_prune made git calls: {calls}")

                # 2. server dry-run dispatches plan + execute via to_thread with zero git on loop/TX.
                from mesh.daemon.server import DaemonServer
                from tests.unit.test_batch7_pr45_r2 import _drive_server

                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn

                async def _drive_dry_prune(request_id: str) -> dict:
                    reader = asyncio.StreamReader()
                    req = {"v": 1, "cmd": "workspace.prune", "args": {"project": "prj_1"},
                           "dry_run": True, "request_id": request_id}
                    reader.feed_data((json.dumps(req) + "\n").encode("utf-8"))
                    reader.feed_eof()
                    buf = bytearray()

                    class _W:
                        def get_extra_info(self, *_a):
                            return None

                        def write(self, data: bytes) -> None:
                            buf.extend(data)

                        async def drain(self) -> None:
                            return None

                        def close(self) -> None:
                            pass

                        async def wait_closed(self) -> None:
                            pass

                    with mock.patch("mesh.daemon.server.get_peer_credentials", return_value=fake_peer_creds()), \
                         mock.patch.object(srv, "authenticate_and_authorize", return_value=({"id": "usr_1", "kind": "user", "name": "user"}, "user", "direct", None)):
                        await srv.handle_client(reader, _W())
                    return json.loads(buf.decode("utf-8").splitlines()[0])

                async def _run_cases():
                    loop_tid[0] = threading.get_ident()
                    r_dry = await _drive_dry_prune("req_dry")
                    self.assertTrue(r_dry["ok"], r_dry)
                    dry_removed = [t["task_id"] for t in r_dry["data"]["would"]["transitions"]]
                    self.assertEqual(dry_removed, ["tsk_act"], r_dry)

                    r_live = await _drive_server(srv, "workspace.prune", {"project": "prj_1"}, request_id="req_live")
                    self.assertTrue(r_live["ok"], r_live)
                    self.assertEqual([x["task_id"] for x in r_live["data"]["removed"]], ["tsk_act"], r_live)

                with mock.patch.object(gitops, "run_git", side_effect=_spy_run_git):
                    asyncio.run(_run_cases())

                bad = [c_item for c_item in calls if c_item[1] or c_item[2]]
                self.assertEqual(bad, [], f"git calls ran on event loop or inside TX: {bad}")
                self.assertFalse(os.path.exists(ws_act))

                # 3. Phase 2 except BaseException branch also runs git off-loop and outside TX,
                #    and never emits duplicate workspace.pruned for historical tsk_old!
                ws_act2 = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_act2")
                fixture.git(repo, "worktree", "add", "-b", "mesh/prj_1/tsk_act2", ws_act2, base_sha)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, base_commit, created_by, created_at, updated_at) "
                        "VALUES ('tsk_act2', 'prj_1', 'A2', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_act2', ?, 'usr_1', ?, ?);",
                        (ws_act2, base_sha, now, now),
                    )
                calls.clear()
                orig_rm = run_handlers._remove_worktree_and_branch_with_roots
                removed_once = [False]

                def _rm_wrap(*a, **kw):
                    r = orig_rm(*a, **kw)
                    removed_once[0] = True
                    return r

                orig_write = run_handlers._write_ws_prune_checkpoint

                def _write_boom(path, doc):
                    if removed_once[0]:
                        raise RuntimeError("boom_in_phase2")
                    return orig_write(path, doc)

                async def _run_boom():
                    loop_tid[0] = threading.get_ident()
                    with self.assertRaises(RuntimeError):
                        await run_handlers.run_workspace_prune(
                            srv, _ctx(conn, home, "workspace.prune", {"project": "prj_1"}, request_id="req_boom")
                        )

                with mock.patch.object(gitops, "run_git", side_effect=_spy_run_git), \
                     mock.patch.object(run_handlers, "_remove_worktree_and_branch_with_roots", side_effect=_rm_wrap), \
                     mock.patch.object(run_handlers, "_write_ws_prune_checkpoint", side_effect=_write_boom):
                    asyncio.run(_run_boom())

                bad2 = [c_item for c_item in calls if c_item[1] or c_item[2]]
                self.assertEqual(bad2, [], f"except BaseException ran git on loop or inside TX: {bad2}")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = 'tsk_old';").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned' AND task_id = 'tsk_act2';").fetchone()[0], 1)
            finally:
                import gc

                if "srv" in locals():
                    srv.db_conn = None
                conn.close()
                gc.collect()

    def test_remove_worktree_missing_integ_wt_with_nonempty_branch_returns_branch_delete_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r4-p24miss-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = "2026-01-01T00:00:00.000Z"
                ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_no_wt")
                os.makedirs(ws_dir, mode=0o700)
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_no_wt', 'prj_1', 'NoWT', 'S', 'code', 'merged', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_no_wt', 'usr_1', ?, ?);",
                        (ws_dir, now, now),
                    )
                task = dict(conn.execute("SELECT * FROM tasks WHERE id = 'tsk_no_wt';").fetchone())
                reason = _remove_worktree_and_branch_with_roots(
                    [os.path.join(home, "workspaces")], task, integ_wt=None
                )
                self.assertEqual(reason, "branch_delete_failed")
                self.assertTrue(os.path.isdir(ws_dir))

                res = prepare_workspace_prune(_ctx(conn, home, "workspace.prune", {"project": "prj_1"}))
                self.assertEqual(res["removed"], [])
                self.assertEqual([(s["task_id"], s["reason"]) for s in res["skipped"]], [("tsk_no_wt", "branch_delete_failed")])
                self.assertTrue(os.path.isdir(ws_dir))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.pruned';").fetchone()[0], 0)
            finally:
                conn.close()


class TestP2_5WorkspaceDeleteSymlinkParentRecovery(unittest.TestCase):
    """P2-5: workspace.delete Phase 2 & recovery must never consume approval when parent dir is symlinked or moved."""

    def test_run_workspace_delete_swapped_or_moved_parent_preserves_approval_and_original_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r4-p25-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_del")
                os.makedirs(ws_dir, mode=0o700)
                secret_file = os.path.join(ws_dir, "keep.txt")
                with open(secret_file, "w", encoding="utf-8") as f:
                    f.write("must survive\n")
                outside_empty = os.path.join(tmp, "outside_empty")
                os.makedirs(outside_empty, mode=0o700)
                tasks_dir = os.path.dirname(ws_dir)
                saved_tasks = os.path.join(tmp, "saved_tasks")

                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_del', 'prj_1', 'Del', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_del', 'usr_1', ?, ?);",
                        (ws_dir, now, now),
                    )
                    apr = create_or_get_pending_approval(
                        conn=conn, category="delete", op="workspace.delete", requested_by="agt_lead",
                        params={"task_id": "tsk_del"}, summary="Del", project_id="prj_1", target_id="tsk_del",
                        testing_mode=True,
                    )
                    handle_approval_approve(_ctx(
                        conn, home, "approval.approve",
                        {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        k_user=b"\x01" * 32,
                    ))

                orig_del = run_handlers._delete_workspace_dir_sync

                def _swap_parent_to_symlink_before_del(*args, **kwargs):
                    os.rename(tasks_dir, saved_tasks)
                    os.symlink(outside_empty, tasks_dir)
                    return orig_del(*args, **kwargs)

                class _Srv:
                    def track_merge_job(self, job):
                        pass

                ctx = _ctx(
                    conn, home, "workspace.delete", {"task_id": "tsk_del"},
                    principal_id="agt_lead", actor_kind="agent", via="token",
                    approval=apr["approval_id"], request_id="req_swap_del",
                )
                with mock.patch.object(run_handlers, "_delete_workspace_dir_sync", side_effect=_swap_parent_to_symlink_before_del):
                    with self.assertRaises(MeshError) as cm:
                        asyncio.run(run_handlers.run_workspace_delete(_Srv(), ctx))
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
                # Original workspace dir is untouched in saved_tasks, approval stays approved, zero deleted events!
                self.assertTrue(os.path.isfile(os.path.join(saved_tasks, "tsk_del", "keep.txt")))
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()[0],
                    "approved",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_del';").fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'ws_res:%';").fetchone()[0], 0)

                # Restore real tasks_dir and retry -> now legitimately deletes & consumes approval!
                os.unlink(tasks_dir)
                os.rename(saved_tasks, tasks_dir)
                res_ok = asyncio.run(run_handlers.run_workspace_delete(
                    _Srv(),
                    _ctx(
                        conn, home, "workspace.delete", {"task_id": "tsk_del"},
                        principal_id="agt_lead", actor_kind="agent", via="token",
                        approval=apr["approval_id"], request_id="req_retry_del",
                    ),
                ))
                self.assertTrue(res_ok.data["deleted"], res_ok.data)
                self.assertFalse(os.path.exists(ws_dir))
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()[0],
                    "consumed",
                )
            finally:
                conn.close()

    def test_recovery_step_ws_reservations_swapped_parent_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-r4-p25rec-") as tmp:
            home, conn = _setup_home_and_db(tmp)
            try:
                now = now_iso_utc()
                ws_dir = os.path.join(home, "workspaces", "prj_1", "tasks", "tsk_rec")
                tasks_dir = os.path.dirname(ws_dir)
                outside_empty = os.path.join(tmp, "outside_empty")
                os.makedirs(tasks_dir, mode=0o700)
                os.makedirs(outside_empty, mode=0o700)
                # Replace tasks dir with a symlink to outside_empty (so ws_dir does not exist via tasks/)
                os.rmdir(tasks_dir)
                os.symlink(outside_empty, tasks_dir)

                with transaction(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, project_id, title, spec, kind, status, profile, timeout_s, max_attempts, "
                        "workspace_path, branch, created_by, created_at, updated_at) "
                        "VALUES ('tsk_rec', 'prj_1', 'Rec', 'S', 'code', 'accepted', 'dev', 600, 3, ?, 'mesh/prj_1/tsk_rec', 'usr_1', ?, ?);",
                        (ws_dir, now, now),
                    )
                    apr = create_or_get_pending_approval(
                        conn=conn, category="delete", op="workspace.delete", requested_by="agt_lead",
                        params={"task_id": "tsk_rec"}, summary="Del rec", project_id="prj_1", target_id="tsk_rec",
                        testing_mode=True,
                    )
                    handle_approval_approve(_ctx(
                        conn, home, "approval.approve",
                        {"approval_id": apr["approval_id"], "op_hash": apr["op_hash"]},
                        k_user=b"\x02" * 32,
                    ))
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?);",
                        ("ws_res:task:tsk_rec", json.dumps({
                            "kind": "delete",
                            "task_id": "tsk_rec",
                            "project_id": "prj_1",
                            "path": ws_dir,
                            "stripped": ws_dir,
                            "real": ws_dir,
                            "matched_root": os.path.join(home, "workspaces"),
                            "branch": "mesh/prj_1/tsk_rec",
                            "approval_id": apr["approval_id"],
                            "requested_by": "agt_lead",
                            "request_id": "req_rec_swap",
                            "idempotency": {
                                "principal_id": "agt_lead",
                                "key": "k-rec-swap",
                                "cmd": "workspace.delete",
                                "request_hash": "h_rec_swap",
                            },
                        })),
                    )

                counts = {}
                recovery._step_ws_reservations(conn, home, counts)
                self.assertEqual(counts.get("workspace_deletes_rolled_back"), 1, counts)
                self.assertEqual(counts.get("workspace_deletes_finalized", 0), 0, counts)
                self.assertEqual(
                    conn.execute("SELECT status FROM approvals WHERE id = ?;", (apr["approval_id"],)).fetchone()[0],
                    "approved",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.deleted' AND task_id = 'tsk_rec';").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM events WHERE type = 'workspace.delete_rolled_back' AND task_id = 'tsk_rec';").fetchone()[0],
                    1,
                )
                self.assertIsNone(conn.execute("SELECT 1 FROM idempotency WHERE key = 'k-rec-swap';").fetchone())
                self.assertEqual(check_i7_event_hash_chain(conn), [])
                self.assertEqual(check_i8_cancelling_and_leases(conn), [])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
