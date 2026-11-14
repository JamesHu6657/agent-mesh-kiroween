"""Linear full-codebase review of main 1f78a4f, part B (mesh/daemon), unit level.

B2 an unexpected error after `merge --no-commit` aborts the merge (integration clean).
B4 run_registered's timeout holds even when a descendant keeps the output pipes open."""

import errno
import os
import subprocess
import time
import unittest
from unittest import mock

from mesh.core import repos
from mesh.daemon import merge
from mesh.daemon.children import run_registered
from tests.unit.test_repos import RepoFixtureCase


class TestUnexpectedMergeErrorCleansUp(RepoFixtureCase):
    def test_spawn_failure_after_no_commit_merge_aborts(self) -> None:
        repo = os.path.realpath(self.repo)
        out = repos.setup_integration(repo, "prj_b2", self.main, self.ws_root)
        integ = out["integration_path"]
        task_wt = os.path.join(self.ws_root, "task")
        self.fixture.git(repo, "worktree", "add", "-q", "-b", "feat", task_wt, self.main)
        with open(os.path.join(task_wt, "f.txt"), "w", encoding="utf-8") as f:
            f.write("f\n")
        self.fixture.git(task_wt, "add", "f.txt")
        self.fixture.git(task_wt, "commit", "-q", "-m", "f", identity=True)
        pre = self.fixture.head(integ, "HEAD")
        plan = {"integration": integ, "worktree": task_wt, "branch": "feat", "test_argv": ["true"],
                "task_id": "tsk_b2", "title": "b2", "merge_id": "mrg_b2", "run_dir": self.ws_root}
        with mock.patch("mesh.daemon.merge.run_test_step",
                        side_effect=OSError(errno.EAGAIN, "Resource temporarily unavailable")):
            res = merge.run_merge_pipeline(plan)
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("Errno 11", res["details"]["error"])
        self.assertFalse(merge._merge_head_present(integ))
        self.assertEqual(self.fixture.git(integ, "status", "--porcelain", "--untracked-files=all").stdout, "")
        self.assertEqual(self.fixture.head(integ, "HEAD"), pre)


class TestRunRegisteredTimeoutBounded(unittest.TestCase):
    def test_descendant_holding_pipes_does_not_block(self) -> None:
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_registered("probe", ["sh", "-c", "sleep 30 & echo started; sleep 30"],
                           timeout=0.2, capture_output=True, text=True)
        self.assertLess(time.monotonic() - t0, 15.0)

    def test_own_group_is_killed(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            run_registered("probe", ["sh", "-c", "sleep 30 & echo $! > /dev/null; sleep 30"],
                           timeout=0.2, capture_output=True, text=True, start_new_session=True)

    def test_normal_run_unchanged(self) -> None:
        cp = run_registered("probe", ["sh", "-c", "echo hi"], timeout=5, capture_output=True, text=True)
        self.assertEqual((cp.returncode, cp.stdout), (0, "hi\n"))


if __name__ == "__main__":
    unittest.main()
