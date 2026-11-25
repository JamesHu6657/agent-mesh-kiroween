"""Unit regression tests for the Carbon merge-gate review, as ported to the private repo.

P1-3: the durable commit intent (``commit_ready.json``, fsynced before ``git commit``; a merge
that cannot record it is aborted) and recovery by parents, plus reverse I9.
P2-5: noop merges run ``test_command`` under the same guards and record the commit that
really carries the task.

Port decisions (see the PR): the private repo keeps its batch7 recovery rule on top of the
parent match (message and current branch tip must match as well; anything else is rolled
back to ``pre_head``, fail closed), and its own P1-2 snapshot/restore machinery instead of
the public ``restore_worktree_to_index`` helpers (covered by tests/unit/test_batch7_*).
"""

import json
import os
import stat
import sys
import unittest
from unittest import mock

from mesh.core.db import transaction
from mesh.core.invariants import check_invariants
from mesh.daemon import merge
from tests.gitfixture import GitFixture
from tests.unit.test_batch7_pr45_r4 import _MergeFixtureBase
from tests.unit.test_invariants import TestInvariantI9

# Runs the merge pipeline (real /proc scans) and decide_recovery.
MESH_PROC_LOCK = "shared"

PASS = "print('ok')\n"


class TestCommitReadyDurable(_MergeFixtureBase):
    PRE = "a" * 40
    TIP = "b" * 40

    def test_write_is_fsynced_file_then_directory_and_roundtrips(self) -> None:
        run_dir = os.path.join(self.fixture.base, "mrg_x")
        os.makedirs(run_dir, mode=0o700)
        fsynced = []
        real = os.fsync

        def spy(fd: int) -> None:
            fsynced.append(os.fstat(fd).st_mode)
            real(fd)

        with mock.patch("mesh.daemon.merge.os.fsync", side_effect=spy):
            merge._save_commit_ready(run_dir, task_id="tsk_1", pre_head=self.PRE, tip=self.TIP,
                                     symref="refs/heads/main", message="m")
        expected_fsyncs = [True] if sys.platform == "win32" else [True, False]
        self.assertEqual([stat.S_ISREG(m) for m in fsynced], expected_fsyncs)  # the file (and dir on POSIX)
        self.assertEqual([n for n in os.listdir(run_dir) if n != merge.COMMIT_READY_NAME], [])
        cr = merge._load_commit_ready(run_dir)
        self.assertEqual((cr["task_id"], cr["pre_head"], cr["tip"]), ("tsk_1", self.PRE, self.TIP))

    def test_write_without_run_dir_raises(self) -> None:
        with self.assertRaises(OSError):
            merge._save_commit_ready(None, task_id="t", pre_head=self.PRE, tip=self.TIP, symref=None, message="m")
        with self.assertRaises(OSError):
            merge._save_commit_ready(os.path.join(self.fixture.base, "missing"), task_id="t",
                                     pre_head=self.PRE, tip=self.TIP, symref=None, message="m")

    def test_merge_is_aborted_when_the_intent_cannot_be_made_durable(self) -> None:
        branch = "mesh/prj_1/tsk_dur"
        self._make_branch(branch, {"d.txt": "d\n"})
        with mock.patch.object(merge, "_save_commit_ready", side_effect=OSError("disk full")):
            out = merge.run_merge_pipeline(self._plan("run_dur", "tsk_dur", branch, self.base))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "git_failed"), out)
        self.assertEqual(out["details"]["step"], "write commit_ready", out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))


class TestDecideRecoveryByParents(_MergeFixtureBase):
    """decide_recovery reconciles a crash after `git commit` from the durable commit_ready."""

    BRANCH = "mesh/prj_1/tsk_r"

    def setUp(self) -> None:
        super().setUp()
        self.tip = self._make_branch(self.BRANCH, {"r.txt": "r\n"})

    def crash_after_commit(self, run_id: str = "run_r") -> dict:
        plan = self._plan(run_id, "tsk_r", self.BRANCH, self.base)
        with mock.patch.object(merge, "_maybe_crash_after_commit", side_effect=RuntimeError("crash")):
            merge.run_merge_pipeline(plan)
        self.assertNotEqual(self.fixture.head(self.repo, "main"), self.base)
        return plan

    def test_genuine_merge_is_reconciled_by_commit_ready(self) -> None:
        plan = self.crash_after_commit()
        mc = self.fixture.head(self.repo, "main")
        out = merge.decide_recovery(self.repo, "tsk_r", self.BRANCH, self.base, run_dir=plan["run_dir"])
        self.assertEqual((out["outcome"], out["merge_commit"], out["pre_head"]), ("merged", mc, self.base), out)
        self.assertEqual(out["matched_by"], "commit_ready")

    def test_other_parents_than_the_intent_are_not_matched_and_rolled_back(self) -> None:
        plan = self.crash_after_commit()
        cr = merge._load_commit_ready(plan["run_dir"])
        cr["tip"] = "c" * 40
        merge._save_commit_ready(plan["run_dir"], **{k: cr[k] for k in ("task_id", "pre_head", "tip", "symref",
                                                                           "message")})
        out = merge.decide_recovery(self.repo, "tsk_r", self.BRANCH, self.base, run_dir=plan["run_dir"])
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"), out)
        self.assertIs(out["details"]["restored"], True, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)

    def test_moved_branch_fails_closed_never_leaving_unrecorded_work(self) -> None:
        """Port decision: public reconciles a merge whose task branch moved on; the private
        repo keeps batch7's stricter rule and rolls it back (task merge_failed, nothing of
        the task left in integration, its branch intact)."""
        plan = self.crash_after_commit()
        self.fixture.git(self.repo, "checkout", "-q", self.BRANCH)
        with open(os.path.join(self.repo, "m.txt"), "w", encoding="utf-8") as f:
            f.write("m\n")
        self.fixture.git(self.repo, "add", "m.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "more")
        self.fixture.git(self.repo, "checkout", "-q", "main")
        out = merge.decide_recovery(self.repo, "tsk_r", self.BRANCH, self.base, run_dir=plan["run_dir"])
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"), out)
        self.assertIs(out["details"]["restored"], True, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), self.base)
        self.assertNotEqual(self.fixture.head(self.repo, self.BRANCH), self.base)

    def test_legacy_run_without_snapshot_matches_by_message(self) -> None:
        self.fixture.git(self.repo, "merge", "--no-ff", "-q", "-m", "Merge mesh task tsk_r: x", self.BRANCH)
        mc = self.fixture.head(self.repo)
        out = merge.decide_recovery(self.repo, "tsk_r", self.BRANCH, self.base)
        self.assertEqual((out["outcome"], out["merge_commit"], out["matched_by"]), ("merged", mc, "message"), out)


class TestP25NoopMergeGate(_MergeFixtureBase):
    """Carbon P2-5: a branch already in integration is not merged, but test_command still runs."""

    BRANCH = "mesh/prj_1/tsk_n"

    def merged_branch(self) -> str:
        self._make_branch(self.BRANCH, {"n.txt": "n\n"})
        self.fixture.git(self.repo, "merge", "--no-ff", "-q", "-m", "earlier unrecorded merge", self.BRANCH)
        return self.fixture.head(self.repo)

    def test_noop_without_tests_records_the_integrating_merge_commit(self) -> None:
        mc = self.merged_branch()
        with open(os.path.join(self.repo, "later.txt"), "w", encoding="utf-8") as f:
            f.write("later\n")
        self.fixture.git(self.repo, "add", "later.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "later unrelated")
        pre = self.fixture.head(self.repo)
        out = merge.run_merge_pipeline(self._plan("run_n0", "tsk_n", self.BRANCH, pre))
        self.assertEqual((out["outcome"], out["noop"], out["merge_commit"], out["tests"]),
                         ("merged", True, mc, "none"), out)

    def test_zero_commit_task_records_its_tip(self) -> None:
        self.fixture.git(self.repo, "branch", self.BRANCH, self.base)
        with open(os.path.join(self.repo, "x.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        self.fixture.git(self.repo, "add", "x.txt")
        self.fixture.git(self.repo, "commit", "-q", "-m", "other task")
        out = merge.run_merge_pipeline(self._plan("run_nz", "tsk_n", self.BRANCH, self.fixture.head(self.repo)))
        self.assertEqual((out["outcome"], out["merge_commit"]), ("merged", self.base), out)

    def test_noop_runs_tests_and_passes(self) -> None:
        mc = self.merged_branch()
        out = merge.run_merge_pipeline(self._plan("run_np", "tsk_n", self.BRANCH, mc, PASS))
        self.assertEqual((out["outcome"], out["noop"], out["tests"], out["merge_commit"]),
                         ("merged", True, "passed", mc), out)
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_noop_failing_test_fails_and_leaves_tree_clean(self) -> None:
        mc = self.merged_branch()
        script = "open('cov.xml','w').write('x')\nopen('n.txt','w').write('changed')\nraise SystemExit(1)\n"
        out = merge.run_merge_pipeline(self._plan("run_nf", "tsk_n", self.BRANCH, mc, script))
        self.assertEqual((out["outcome"], out["reason"], out["tests"]), ("failed", "tests_failed", "failed"), out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertNotIn("abort_stderr", out["details"])
        self.assertEqual(self.fixture.head(self.repo, "main"), mc)
        self.assertTrue(self.fixture.is_clean(self.repo))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "cov.xml")))

    def test_noop_test_that_commits_or_merges_is_rejected_and_restored(self) -> None:
        mc = self.merged_branch()
        commit = ("import subprocess\nopen('evil.txt','w').write('e')\n"
                  "subprocess.run(['git','add','evil.txt'],check=True)\n"
                  "subprocess.run(['git','-c','user.name=t','-c','user.email=t@e.invalid','commit','-qm','rogue'],"
                  "check=True)\n")
        out = merge.run_merge_pipeline(self._plan("run_nc", "tsk_n", self.BRANCH, mc, commit))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertIs(out["details"].get("restored"), True, out)
        self.assertEqual(self.fixture.head(self.repo, "main"), mc)
        self.assertTrue(self.fixture.is_clean(self.repo))
        other = self._make_branch("mesh/prj_1/other", {"o.txt": "o\n"}, base=mc)
        mrg = ("import subprocess\nsubprocess.run(['git','merge','--no-ff','--no-commit','%s'],check=True)\n" % other)
        out = merge.run_merge_pipeline(self._plan("run_nm", "tsk_n", self.BRANCH, mc, mrg))
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "integration_dirty"), out)
        self.assertFalse(self.fixture.has_merge_head(self.repo))
        self.assertTrue(self.fixture.is_clean(self.repo))

    def test_crash_during_noop_test_is_cleared_by_recovery(self) -> None:
        mc = self.merged_branch()
        plan = self._plan("run_nx", "tsk_n", self.BRANCH, mc)
        plan["test_argv"] = [sys.executable, "-c", PASS]

        def crash(_plan):
            with open(os.path.join(self.repo, "cov.xml"), "w", encoding="utf-8") as f:
                f.write("x")
            raise KeyboardInterrupt  # stands in for a meshd crash mid-test

        with mock.patch.object(merge, "run_test_step", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                merge.run_merge_pipeline(plan)
        out = merge.decide_recovery(self.repo, "tsk_n", self.BRANCH, mc, run_dir=plan["run_dir"])
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"), out)
        self.assertIs(out["details"]["restored"], True, out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "cov.xml")))


class TestReverseI9(unittest.TestCase):
    """doctor --check-invariants reports non-merged tasks whose work is already in integration."""

    setUp = TestInvariantI9.setUp
    tearDown = TestInvariantI9.tearDown
    _create_repo_project = TestInvariantI9._create_repo_project

    def make_task(self, task_id: str, status: str, branch_commit: bool) -> str:
        """A code task with a branch based on main; ``branch_commit`` gives it one commit."""
        branch = f"mesh/prj_x/{task_id}"
        base = self.fixture.head(self.repo, "main")
        self.fixture.git(self.repo, "branch", branch, "main")
        if branch_commit:
            wt = self.fixture.path(f"wt-{task_id}")
            self.fixture.git(self.repo, "worktree", "add", wt, branch)
            with open(os.path.join(wt, f"{task_id}.txt"), "w", encoding="utf-8") as f:
                f.write("x\n")
            self.fixture.git(wt, "add", "-A")
            self.fixture.git(wt, "commit", "-m", task_id, identity=True)
            self.fixture.git(self.repo, "worktree", "remove", wt)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, fence_seq, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES (?, 'prj_x', 'T', 'S', 'code', ?, 'agt_lead', 'edit', 3600, 3, 1, ?, ?, 'usr_1', "
                "'2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z');", (task_id, status, branch, base))
            from mesh.core.events import record_event
            record_event(self.conn, type_="task.created", actor_id="usr_1", actor_kind="user", via="direct",
                         project_id="prj_x", task_id=task_id, to_state=status)
        return branch

    def merge_into_integration(self, branch: str) -> str:
        integ = self.fixture.path("wt-integ")
        self.fixture.git(self.repo, "worktree", "add", integ, self.integration)
        self.fixture.git(integ, "merge", "--no-ff", "-m", "chore: merge branch", branch, identity=True)
        sha = self.fixture.head(integ)
        self.fixture.git(self.repo, "worktree", "remove", integ)
        return sha

    def reverse_violations(self):
        return [v for v in check_invariants(self.conn) if v.details.get("reason") == "integrated_but_not_merged"]

    def test_cancelled_and_merge_failed_tasks_that_were_merged_are_reported(self) -> None:
        self._create_repo_project()
        failed = self.make_task("tsk_f", "merge_failed", True)
        cancelled = self.make_task("tsk_c", "cancelled", True)
        mc_f = self.merge_into_integration(failed)
        mc_c = self.merge_into_integration(cancelled)
        got = {v.details["task_id"]: v for v in self.reverse_violations()}
        self.assertEqual(sorted(got), ["tsk_c", "tsk_f"])
        self.assertEqual(got["tsk_f"].details["merge_commit"], mc_f)
        self.assertEqual(got["tsk_c"].details["merge_commit"], mc_c)
        self.assertEqual(got["tsk_c"].details["status"], "cancelled")
        self.assertEqual({v.invariant for v in got.values()}, {"I9"})

    def test_clean_states_are_not_reported(self) -> None:
        self._create_repo_project()
        self.make_task("tsk_z", "cancelled", False)      # zero-commit task: its base is in integration
        self.make_task("tsk_u", "accepted", True)        # work not integrated
        merging = self.make_task("tsk_m", "merging", True)
        self.merge_into_integration(merging)             # the merge in flight is not a violation
        self.assertEqual(self.reverse_violations(), [])

    def _close_superseded(self, task_id: str, other: str) -> None:
        from mesh.core.events import record_event
        with transaction(self.conn):
            record_event(self.conn, type_="task.closed", actor_id="usr_1", actor_kind="user", via="direct",
                         project_id="prj_x", task_id=task_id, from_state="merge_failed", to_state="cancelled",
                         payload={"superseded_by": other, "from_state": "merge_failed"})

    def test_task_superseded_by_a_merged_task_is_not_reported(self) -> None:
        """`task close TSK --superseded-by TSK2`: TSK (cancelled) carries the work TSK2 merged
        (here TSK2's branch is TSK's tip): not a violation while TSK2 is merged; reported
        again when the named task is not merged."""
        self._create_repo_project()
        old = self.make_task("tsk_s", "cancelled", True)
        new_branch = "mesh/prj_x/tsk_n"
        self.fixture.git(self.repo, "branch", new_branch, old)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, title, spec, kind, status, assignee_agent_id, profile, "
                "timeout_s, max_attempts, fence_seq, branch, base_commit, created_by, created_at, updated_at) "
                "VALUES ('tsk_n', 'prj_x', 'T2', 'S', 'code', 'merged', 'agt_lead', 'edit', 3600, 3, 1, ?, ?, "
                "'usr_1', '2026-11-07T00:00:00.000Z', '2026-11-07T00:00:00.000Z');",
                (new_branch, self.fixture.head(self.repo, "main")))
        mc = self.merge_into_integration(new_branch)
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = 'tsk_n';",
                              (json.dumps({"merge_commit": mc}),))
        # Not closed as superseded yet: a plain cancelled task with integrated work is reported.
        self.assertEqual([v.details["task_id"] for v in self.reverse_violations()], ["tsk_s"])
        self._close_superseded("tsk_s", "tsk_n")
        self.assertEqual(self.reverse_violations(), [])
        from mesh.core.invariants import check_i9_integrated_unmerged_tasks
        self.assertEqual(check_i9_integrated_unmerged_tasks(self.conn, "prj_x"), [])  # the deliver gate
        # The superseding task is no longer merged: the exemption no longer applies.
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'merge_failed' WHERE id = 'tsk_n';")
        self.assertIn("tsk_s", [v.details["task_id"] for v in self.reverse_violations()])

    def test_a_merged_task_is_not_reported(self) -> None:
        self._create_repo_project()
        branch = self.make_task("tsk_ok", "merged", True)
        mc = self.merge_into_integration(branch)
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = 'tsk_ok';",
                              (json.dumps({"merge_commit": mc}),))
        self.assertEqual(check_invariants(self.conn), [])


class TestPR51R1ReleaseWhileActive(unittest.TestCase):
    """R1 P1-1: --force-merge-held is refused while the merge pipeline still runs, even
    in the windows where no test wrapper process is alive."""

    def test_release_refused_while_pipeline_runs(self) -> None:
        from mesh.daemon.handlers import run as run_handlers
        from mesh.errors import MeshError

        run = {"id": "run_active01", "kind": "merge_test", "status": "running",
               "project_id": "prj_x", "task_id": "tsk_x"}
        merge._ACTIVE_MERGE_RUNS.add(run["id"])
        self.addCleanup(merge._ACTIVE_MERGE_RUNS.discard, run["id"])
        with mock.patch.object(run_handlers, "_merge_wrapper_alive", return_value=False):
            with self.assertRaises(MeshError) as cm:
                run_handlers._release_merge_held(mock.Mock(), run)
        self.assertEqual(cm.exception.details["reason"], "merge_in_progress")


class TestPR51R3StoredOpJsonStable(unittest.TestCase):
    """R3 P2-2: an untouched redacted row stays valid when meshd restarts with a new
    secret-looking environment variable."""

    def test_env_exact_values_do_not_invalidate_a_redacted_row(self) -> None:
        from mesh.core import redact as redact_mod
        from mesh.core.approvals import stored_op_json, verify_stored_op_json
        from mesh.core.canonical import compute_op_hash

        secret = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
        op_doc, op_hash = compute_op_hash(op="project.update", requested_by="agt_1",
                                          params={"goal": f"use /opt/app/creds.json with {secret}"},
                                          project_id="prj_1", category="scope_change")
        row = {"op_json": stored_op_json(op_doc, op_hash), "op_type": "project.update",
               "requested_by": "agt_1", "project_id": "prj_1", "category": "scope_change"}
        self.assertIn("/opt/app/creds.json", row["op_json"])
        self.assertEqual(verify_stored_op_json(row), op_hash)
        env = [("GOOGLE_APPLICATION_CREDENTIALS", "/opt/app/creds.json")]
        with mock.patch.object(redact_mod, "default_exact_values", return_value=env):
            self.assertNotEqual(redact_mod.redact_value(json.loads(row["op_json"])["op"]),
                                json.loads(row["op_json"])["op"])  # the env value would now match
            self.assertEqual(verify_stored_op_json(row), op_hash)
