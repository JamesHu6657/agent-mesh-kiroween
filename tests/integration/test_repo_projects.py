"""Integration tests for repo projects (plan §10.1, §10.2, §8; checklist K1, K2, K13, K11).

Every git repo is a K0 fixture under /tmp/mh-*-git; meshd runs with the fixture's hermetic
git env (GitMeshHarness). Nothing here touches the Mesh checkout.
"""

import os
import unittest
from typing import Any, Dict, List

import mesh.constants as c
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.integration import test_projects as _projects

create_agent = _projects.create_agent  # the function only: importing the class would re-run its tests

TEST_ARGV = '["python3", "-m", "unittest", "discover", "-s", "tests"]'


class TestRepoProjects(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.harness = GitMeshHarness(self.fixture, init_db=True, start_daemon=True)
        self.addCleanup(self.harness.teardown)  # also removes the fixture
        self.tok_lead1 = create_agent(self.harness, "lead1", can_lead=True)
        self.tok_lead2 = create_agent(self.harness, "lead2", can_lead=True)
        self.tok_grok = create_agent(self.harness, "grok", capabilities=["intake", "relay"])
        rc, resp = self.harness.cli("config", "set", "lead.default_agent", '"lead1"', user=True)
        self.assertEqual(rc, 0, resp)
        self.repo = self.fixture.make_k0()
        self.main = self.fixture.head(self.repo, "main")
        self.remote_before = self.fixture.remote_refs(self.repo)
        self.ws = os.path.join(self.harness.home, "workspaces")

    # --- helpers -----------------------------------------------------------------

    def intake(self, *extra: str, title: str = "Repo project") -> Any:
        return self.harness.cli("intake", "submit", "--title", title, "--goal", "g", "--criterion", "c1",
                                *extra, token=self.tok_grok)

    def agent_id(self, name: str) -> str:
        rc, resp = self.harness.cli("agent", "show", name, user=True)
        self.assertEqual(rc, 0, resp)
        return resp["data"]["id"]

    def worktrees(self) -> Dict[str, Dict[str, Any]]:
        return {os.path.realpath(str(e["worktree"])): e for e in self.fixture.worktrees(self.repo)}

    def mesh_branches(self) -> List[str]:
        return self.fixture.branches(self.repo, "mesh/*")

    def project_count(self) -> int:
        rc, resp = self.harness.cli("project", "list", user=True)
        self.assertEqual(rc, 0, resp)
        return len(resp["data"]["items"])

    def assert_usage(self, rc: int, resp: Dict[str, Any], param: str) -> None:
        self.assertEqual(rc, c.EXIT_USAGE, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_ARGUMENT, resp)
        self.assertEqual(resp["error"]["details"]["param"], param, resp)

    # --- tests -------------------------------------------------------------------

    def test_repo_intake_integration_and_lead_worktrees(self) -> None:
        """ACC: K1, K2, K13 (partial: lead repo/ detached at creation and set-lead), K11 (partial: no remote ref changes) - repo intake end to end."""
        # K2: a repo project needs --test-command; nothing is created without it.
        rc, resp = self.intake("--repo", self.repo, "--base-branch", "main")
        self.assert_usage(rc, resp, "test_command")
        self.assertEqual(self.mesh_branches(), [])
        self.assertEqual(self.project_count(), 0)

        # A dry run validates only: no branch, no worktree, no project.
        rc, resp = self.intake("--repo", self.repo, "--base-branch", "main", "--test-command", "none", "--dry-run")
        self.assertEqual(rc, 0, resp)
        self.assertTrue(resp["dry_run"], resp)
        self.assertEqual(self.mesh_branches(), [])
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual(self.project_count(), 0)

        # K1 + K2: --test-command none.
        rc, resp = self.intake("--repo", self.repo, "--base-branch", "main", "--test-command", "none")
        self.assertEqual(rc, 0, resp)
        prj = resp["data"]["id"]
        branch = f"mesh/{prj}/integration"
        self.assertEqual(self.mesh_branches(), [branch])
        self.assertEqual(self.fixture.head(self.repo, branch), self.main)
        integ = os.path.join(self.ws, prj, "integration")
        wts = self.worktrees()
        self.assertIn(os.path.realpath(integ), wts)
        self.assertEqual(wts[os.path.realpath(integ)]["branch"], f"refs/heads/{branch}")
        self.assertEqual(wts[os.path.realpath(integ)]["head"], self.main)

        rc, show = self.harness.cli("project", "show", prj, user=True)
        self.assertEqual(rc, 0, show)
        d = show["data"]
        self.assertEqual(d["repo"], os.path.realpath(self.repo))
        self.assertEqual(d["base_branch"], "main")
        self.assertEqual(d["integration_branch"], branch)
        self.assertEqual(d["base_commit"], self.main)
        self.assertEqual(d["tests"], "none")

        # K13 (partial): lead1's repo/ is detached at the integration tip and is not the
        # integration worktree.
        lead1 = self.agent_id("lead1")
        lead1_repo = os.path.realpath(os.path.join(self.ws, prj, "lead", lead1, "repo"))
        wts = self.worktrees()
        self.assertIn(lead1_repo, wts)
        self.assertTrue(wts[lead1_repo].get("detached"), wts[lead1_repo])
        self.assertNotIn("branch", wts[lead1_repo])
        self.assertEqual(wts[lead1_repo]["head"], self.main)
        self.assertNotEqual(lead1_repo, os.path.realpath(integ))
        self.assertEqual(os.stat(os.path.join(self.ws, prj)).st_mode & 0o777, 0o700)

        # set-lead lead2: lead2 gets its own detached repo/, lead1's stays.
        rc, resp = self.harness.cli("project", "set-lead", prj, "lead2", "--reason", "handover", user=True)
        self.assertEqual(rc, 0, resp)
        lead2_repo = os.path.realpath(os.path.join(self.ws, prj, "lead", self.agent_id("lead2"), "repo"))
        wts = self.worktrees()
        self.assertIn(lead2_repo, wts)
        self.assertTrue(wts[lead2_repo].get("detached"), wts[lead2_repo])
        self.assertIn(lead1_repo, wts)
        self.assertEqual(sorted(wts), sorted([os.path.realpath(self.repo), os.path.realpath(integ), lead1_repo, lead2_repo]))
        self.assertEqual(self.mesh_branches(), [branch])

        # K1/K2 via `project create` with an argv test command.
        rc, resp = self.harness.cli("project", "create", "--title", "P2", "--goal", "g", "--criterion", "c",
                                    "--repo", self.repo, "--base-branch", "main", "--test-command", TEST_ARGV,
                                    user=True)
        self.assertEqual(rc, 0, resp)
        prj2 = resp["data"]["id"]
        self.assertEqual(resp["data"]["tests"], ["python3", "-m", "unittest", "discover", "-s", "tests"])
        self.assertEqual(self.mesh_branches(), sorted([branch, f"mesh/{prj2}/integration"]))
        rc, show2 = self.harness.cli("project", "show", prj2, user=True)
        self.assertEqual(rc, 0, show2)
        self.assertEqual(show2["data"]["tests"], ["python3", "-m", "unittest", "discover", "-s", "tests"])

        # A non-repo project's show has no repo fields.
        rc, resp = self.harness.cli("project", "create", "--title", "plain", "--goal", "g", "--criterion", "c",
                                    user=True)
        self.assertEqual(rc, 0, resp)
        rc, show3 = self.harness.cli("project", "show", resp["data"]["id"], user=True)
        self.assertEqual(rc, 0, show3)
        self.assertNotIn("repo", show3["data"])
        self.assertNotIn("tests", show3["data"])

        # K11 (partial): no ref changed on the fixture's remote.
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)

    def test_bad_repo_and_base_branch(self) -> None:
        """ACC: K1 (bad repo rc 2), K2 - bad repo paths and base branches are rc 2 and create nothing."""
        not_repo = self.fixture.path("not-a-repo")
        os.makedirs(not_repo)
        sub = os.path.join(self.repo, "tests")
        for path in (not_repo, sub, self.fixture.path("missing")):
            rc, resp = self.intake("--repo", path, "--base-branch", "main", "--test-command", "none")
            self.assert_usage(rc, resp, "repo")
        rc, resp = self.intake("--repo", self.repo, "--base-branch", "nope", "--test-command", "none")
        self.assert_usage(rc, resp, "base_branch")
        rc, resp = self.intake("--repo", self.repo, "--test-command", "none")
        self.assert_usage(rc, resp, "base_branch")
        for bad in ("[]", "make test", '[""]'):
            rc, resp = self.intake("--repo", self.repo, "--base-branch", "main", "--test-command", bad)
            self.assert_usage(rc, resp, "test_command")
        # --base-branch/--test-command without --repo, and repo flags on a follow-up message.
        rc, resp = self.intake("--base-branch", "main")
        self.assert_usage(rc, resp, "base_branch")
        rc, resp = self.harness.cli("project", "create", "--title", "plain", "--goal", "g", "--criterion", "c",
                                    user=True)
        self.assertEqual(rc, 0, resp)
        plain = resp["data"]["id"]
        rc, resp = self.harness.cli("intake", "submit", "--project", plain, "--message", "m",
                                    "--repo", self.repo, token=self.tok_grok)
        self.assert_usage(rc, resp, "repo")
        self.assertEqual(self.project_count(), 1)
        rc, resp = self.harness.cli("project", "show", plain, user=True)
        self.assertEqual(rc, 0, resp)
        self.assertNotIn("repo", resp["data"])
        self.assertEqual(self.mesh_branches(), [])
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual(self.project_count(), 1)  # only the plain project
        # Only the plain project's lead dir exists under the workspaces root.
        self.assertEqual(sorted(os.listdir(self.ws)) if os.path.exists(self.ws) else [], [plain])
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)

    def test_failed_intake_leaves_no_git_state(self) -> None:
        """ACC: K1 - a repo intake that fails on its own input (unknown --depends-on-project) creates no branch, worktree, project or workspace dir (review m2-r3 finding 1)."""
        for deps in (["prj_unknown0000"],):
            extra = []
            for d in deps:
                extra += ["--depends-on-project", d]
            rc, resp = self.harness.cli("project", "create", "--title", "P", "--goal", "g", "--criterion", "c",
                                        "--repo", self.repo, "--base-branch", "main", "--test-command", "none",
                                        *extra, user=True)
            self.assertNotEqual(rc, 0, resp)
            self.assertFalse(resp["ok"], resp)
        self.assertEqual(self.mesh_branches(), [])
        self.assertEqual(list(self.worktrees()), [os.path.realpath(self.repo)])
        self.assertEqual(self.project_count(), 0)
        self.assertFalse(os.path.exists(self.ws) and [d for d in os.listdir(self.ws) if d.startswith("prj_")])
        self.assertEqual(self.fixture.remote_refs(self.repo), self.remote_before)


if __name__ == "__main__":
    unittest.main()
