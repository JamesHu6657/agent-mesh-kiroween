"""Unit regression tests for the Carbon merge-gate review (P1-3: intent file and reverse I9)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from mesh.core.db import transaction
from mesh.core.invariants import check_invariants
from mesh.daemon import merge
from tests.gitfixture import GitFixture
from tests.unit.test_invariants import TestInvariantI9


class TestMergeIntentFile(unittest.TestCase):
    PRE = "a" * 40
    TIP = "b" * 40

    def test_write_is_fsynced_file_then_directory_and_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-intent-") as d:
            run_dir = os.path.join(d, "mrg_x")
            fsynced = []
            real = os.fsync

            def spy(fd: int) -> None:
                fsynced.append(os.fstat(fd).st_mode)
                real(fd)

            with mock.patch("mesh.daemon.merge.os.fsync", side_effect=spy):
                merge.write_merge_intent(run_dir, {"v": 1, "task_id": "tsk_1", "pre_head": self.PRE, "tip": self.TIP})
            import stat
            self.assertEqual([stat.S_ISREG(m) for m in fsynced], [True, False])  # the file, then its directory
            self.assertFalse(os.path.exists(os.path.join(run_dir, "merge_intent.json.tmp")))
            intent = merge.read_merge_intent(run_dir)
            self.assertEqual((intent["task_id"], intent["pre_head"], intent["tip"]), ("tsk_1", self.PRE, self.TIP))

    def test_read_rejects_missing_or_malformed_intents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-intent-") as d:
            self.assertIsNone(merge.read_merge_intent(d))
            self.assertIsNone(merge.read_merge_intent(None))
            path = merge.merge_intent_path(d)
            for bad in ("{trunc", "[]", json.dumps({"task_id": "t", "pre_head": "zz", "tip": self.TIP}),
                        json.dumps({"task_id": "t", "pre_head": self.PRE})):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(bad)
                self.assertIsNone(merge.read_merge_intent(d), bad)


class TestDecideRecoveryByParents(unittest.TestCase):
    """decide_recovery matches the merge commit by (pre_head, tip), not by message."""

    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0()
        self.fixture.git(self.repo, "branch", "mesh/prj_x/tsk_1", "main")
        self.pre = self.fixture.head(self.repo, "main")
        wt = self.fixture.path("wt-t")
        self.fixture.git(self.repo, "worktree", "add", wt, "mesh/prj_x/tsk_1")
        with open(os.path.join(wt, "t.txt"), "w", encoding="utf-8") as f:
            f.write("t\n")
        self.fixture.git(wt, "add", "t.txt")
        self.fixture.git(wt, "commit", "-m", "task", identity=True)
        self.tip = self.fixture.head(wt)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())

    def merge_commit(self, message: str) -> str:
        self.fixture.git(self.repo, "merge", "--no-ff", "-m", message, "mesh/prj_x/tsk_1", identity=True)
        return self.fixture.head(self.repo)

    def test_message_without_the_task_id_and_a_moved_branch_still_match(self) -> None:
        mc = self.merge_commit("chore: merge branch")
        # the task branch moved on after the tip was resolved
        wt = self.fixture.path("wt-more")
        self.fixture.git(self.repo, "worktree", "add", wt, "mesh/prj_x/tsk_1")
        with open(os.path.join(wt, "m.txt"), "w", encoding="utf-8") as f:
            f.write("m\n")
        self.fixture.git(wt, "add", "m.txt")
        self.fixture.git(wt, "commit", "-m", "more", identity=True)
        self.fixture.git(self.repo, "worktree", "remove", wt)
        intent = {"task_id": "tsk_1", "pre_head": self.pre, "tip": self.tip}
        out = merge.decide_recovery(self.repo, "tsk_1", "mesh/prj_x/tsk_1", self.pre, intent)
        self.assertEqual((out["outcome"], out["merge_commit"], out["pre_head"]), ("merged", mc, self.pre), out)
        self.assertEqual(out["matched_by"], "merge_intent")
        # without an intent the old rule needs message and current tip: it cannot prove the merge
        legacy = merge.decide_recovery(self.repo, "tsk_1", "mesh/prj_x/tsk_1", self.pre, None)
        self.assertEqual((legacy["outcome"], legacy["reason"]), ("failed", "interrupted"), legacy)

    def test_a_commit_with_other_parents_than_the_intent_is_not_matched(self) -> None:
        self.merge_commit("Merge mesh task tsk_1: x")  # a real merge ...
        other = {"task_id": "tsk_1", "pre_head": self.pre, "tip": "c" * 40}  # ... but not of the intended tip
        out = merge.decide_recovery(self.repo, "tsk_1", "mesh/prj_x/tsk_1", self.pre, other)
        self.assertEqual((out["outcome"], out["reason"]), ("failed", "interrupted"), out)

    def test_unmerged_branch_with_intent_is_interrupted_and_restored(self) -> None:
        intent = {"task_id": "tsk_1", "pre_head": self.pre, "tip": self.tip}
        out = merge.decide_recovery(self.repo, "tsk_1", "mesh/prj_x/tsk_1", self.pre, intent)
        self.assertEqual((out["outcome"], out["reason"], out["pre_head"]), ("failed", "interrupted", self.pre), out)
        self.assertIs(out["details"]["restored"], True, out)


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

    def test_a_merged_task_is_not_reported(self) -> None:
        self._create_repo_project()
        branch = self.make_task("tsk_ok", "merged", True)
        mc = self.merge_into_integration(branch)
        with transaction(self.conn):
            self.conn.execute("UPDATE tasks SET merge_json = ? WHERE id = 'tsk_ok';",
                              (json.dumps({"merge_commit": mc}),))
        self.assertEqual(check_invariants(self.conn), [])


if __name__ == "__main__":
    unittest.main()
