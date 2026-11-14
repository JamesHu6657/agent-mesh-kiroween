"""m3-r9b: worktree paths use refs/heads, never a short name a tag could shadow (review linear-m3-r9).

- lead `repo/` (§10.1): detached at refs/heads/<integration>, not at a same-name tag;
- first-start recovery (§10.3): base_commit is merge-base of the two full heads refs;
- fresh attempt (§6.2): the new branch forks from refs/heads/<task branch>;
- reuse: the worktree's branch is read as the full ref (`--abbrev-ref` says `heads/<b>`).
Git runs in GitFixture repos under /tmp/mh-*; fixture reads use full refs.
"""

import os
import unittest
from typing import Any, Dict

from mesh.core import repos
from mesh.core.workspaces import run_code_workspace_plan
from tests.unit.test_repos import RepoFixtureCase


class TestWorktreeRefs(RepoFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.realpath(self.repo)
        self.prj = "prj_wr"
        out = repos.setup_integration(self.repo, self.prj, self.main, self.ws_root)
        self.integ_branch = out["integration_branch"]
        self.integ_wt = out["integration_path"]
        self.b = self.commit(self.integ_wt, "integ.txt", "integ\n")
        self.task_branch = f"mesh/{self.prj}/tsk_1"

    def commit(self, cwd: str, name: str, content: str) -> str:
        with open(os.path.join(cwd, name), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(cwd, "add", name)
        self.fixture.git(cwd, "commit", "-q", "-m", name, identity=True)
        return self.fixture.head(cwd, "HEAD")

    def plan(self, ws_dir: str, branch: Any = None, base_commit: Any = None) -> Dict[str, Any]:
        return {"task_id": "tsk_1", "project_id": self.prj, "repo": self.repo,
                "integration": self.integ_branch,
                "workspace_path": os.path.join(self.ws_root, self.prj, "tasks", ws_dir) + "/",
                "branch": branch, "base_commit": base_commit, "fence_seq": 1}

    def tag(self, name: str, sha: str) -> None:
        self.fixture.git(self.repo, "tag", name, sha)
        self.assertEqual(self.fixture.head(self.repo, f"refs/tags/{name}"), sha)

    def test_lead_repo_ignores_tag_named_like_integration(self) -> None:
        """ACC: K4 - with the integration branch at B and a tag of the same name at A, the lead's
        detached repo/ worktree is at B."""
        self.tag(self.integ_branch, self.main)
        path = repos.add_lead_repo_worktree(self.repo, self.prj, self.ws_root, "agt_lead1")
        entries = {os.path.realpath(e["worktree"]): e for e in self.fixture.worktrees(self.repo)}
        lead = entries[os.path.realpath(path)]
        self.assertTrue(lead.get("detached"), lead)
        self.assertEqual(lead["head"], self.b)

    def test_recovered_base_commit_ignores_tag_named_like_integration(self) -> None:
        """ACC: K4 - first-start recovery (worktree created, T5 not committed): with the integration
        branch at B and a same-name tag at its ancestor A, the recovered base_commit is B."""
        first = run_code_workspace_plan(self.plan("tsk_1"))
        self.assertEqual((first["action"], first["base_commit"]), ("created", self.b))
        self.tag(self.integ_branch, self.main)
        again = run_code_workspace_plan(self.plan("tsk_1"))
        self.assertEqual(again["action"], "reused")
        self.assertEqual(again["branch"], self.task_branch)
        self.assertEqual(again["base_commit"], self.b)

    def test_fresh_attempt_forks_from_branch_not_same_name_tag(self) -> None:
        """ACC: K4 - fresh workspace after lease expiry: with a tag named like the task branch at A,
        `<tsk>-a2` forks from refs/heads/<task branch>'s tip X."""
        first = run_code_workspace_plan(self.plan("tsk_1"))
        x = self.commit(first["workspace_path"], "work.txt", "work\n")
        self.tag(self.task_branch, self.main)
        out = run_code_workspace_plan(self.plan("tsk_1.a2", branch=self.task_branch, base_commit=self.b))
        self.assertEqual(out["action"], "fresh")
        self.assertEqual(out["branch"], f"{self.task_branch}-a2")
        self.assertEqual(out["previous_branch"], self.task_branch)
        self.assertEqual(self.fixture.head(self.repo, f"refs/heads/{self.task_branch}-a2"), x)
        self.assertEqual(self.fixture.head(out["workspace_path"], "HEAD"), x)

    def test_reuse_ignores_tag_named_like_task_branch(self) -> None:
        """ACC: K4 - rework reuse: a tag named like the task branch must not turn the worktree's
        branch into `heads/<branch>` and a false wrong_branch; the worktree is reused."""
        first = run_code_workspace_plan(self.plan("tsk_1"))
        self.tag(self.task_branch, self.main)
        out = run_code_workspace_plan(self.plan("tsk_1", branch=self.task_branch, base_commit=self.b))
        self.assertEqual(out, {"workspace_path": first["workspace_path"], "branch": self.task_branch,
                               "base_commit": self.b, "action": "reused"})


if __name__ == "__main__":
    unittest.main()
