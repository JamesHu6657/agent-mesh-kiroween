"""Unit tests for mesh/core/repos.py (plan §10.1, §10.2, §7.6 mitigation 7).

All git runs on K0 fixture repos under /tmp/mh-*; product git goes through gitops.run_git.
"""

import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

import mesh.constants as c
from mesh.core import repos
from mesh.core.paths import get_install_root
from mesh.errors import MeshError
from tests.gitfixture import GitFixture


class RepoFixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        # Hermetic: product git runs in-process with os.environ (review m2-r1 carry-in).
        env_patch = mock.patch.dict(os.environ, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            del os.environ[key]
        os.environ.update(self.fixture.git_env_vars())
        self.repo = self.fixture.make_k0()
        self.main = self.fixture.head(self.repo, "main")
        self.ws_root = self.fixture.path("ws")
        os.makedirs(self.ws_root, mode=0o700)

    def assert_mesh_error(self, exc: MeshError, code: str, rc: int, **details: object) -> None:
        self.assertEqual(exc.code, code, exc.details)
        self.assertEqual(exc.exit_code, rc)
        for k, v in details.items():
            self.assertEqual(exc.details.get(k), v, exc.details)


class TestValidateRepo(RepoFixtureCase):
    def test_valid_repo_returns_base_commit(self) -> None:
        """ACC: K1 - a repo top level and an existing base branch validate; base_commit = main's commit."""
        info = repos.validate_repo(self.repo, "main")
        self.assertEqual(info, {"repo": os.path.realpath(self.repo), "base_branch": "main", "base_commit": self.main})

    def test_bad_repo_paths_rc2(self) -> None:
        """ACC: K1 - subdir, non-repo dir, missing dir, relative path: rc 2 details.param repo."""
        sub = os.path.join(self.repo, "tests")
        plain = self.fixture.path("not-a-repo")
        os.makedirs(plain)
        cases = {
            sub: "not_toplevel",
            plain: "not_a_repo",
            self.fixture.path("missing"): "not_a_directory",
        }
        for path, reason in cases.items():
            with self.assertRaises(MeshError) as cm:
                repos.validate_repo(path, "main")
            self.assert_mesh_error(cm.exception, c.INVALID_ARGUMENT, c.EXIT_USAGE, param="repo", reason=reason)
        for bad in ("relative/path", "", None):
            with self.assertRaises(MeshError) as cm:
                repos.validate_repo(bad, "main")
            self.assert_mesh_error(cm.exception, c.INVALID_ARGUMENT, c.EXIT_USAGE, param="repo")

    def test_unknown_base_branch_rc2(self) -> None:
        """ACC: K1 - an unknown or malformed base branch is rc 2 details.param base_branch."""
        for bad in ("nope", "-main", "ma in", "", None):
            with self.assertRaises(MeshError) as cm:
                repos.validate_repo(self.repo, bad)
            self.assert_mesh_error(cm.exception, c.INVALID_ARGUMENT, c.EXIT_USAGE, param="base_branch")
        # A commit that is not a local branch is rc 2 reason not_a_branch (review m2-r3 finding 3).
        self.fixture.git(self.repo, "tag", "v-k1", "main")
        for rev in (self.main, "HEAD", "v-k1", "main~0"):
            with self.assertRaises(MeshError, msg=rev) as cm:
                repos.validate_repo(self.repo, rev)
            self.assert_mesh_error(cm.exception, c.INVALID_ARGUMENT, c.EXIT_USAGE,
                                   param="base_branch", reason="not_a_branch")


class TestParseTestCommand(unittest.TestCase):
    def test_parse_test_command(self) -> None:
        """ACC: K2 - JSON argv array of non-empty strings, or the literal none; anything else rc 2."""
        self.assertEqual(repos.parse_test_command("none"), "none")
        argv = repos.parse_test_command('["python3", "-m", "unittest"]')
        self.assertEqual(argv, ["python3", "-m", "unittest"])
        self.assertEqual(repos.encode_test_command(argv), '["python3", "-m", "unittest"]')
        self.assertEqual(repos.encode_test_command("none"), '"none"')
        self.assertEqual(repos.tests_view('"none"'), "none")
        self.assertEqual(repos.tests_view('["a"]'), ["a"])
        self.assertIsNone(repos.tests_view(None))
        for bad in (None, "", "  ", "[]", '[""]', '"x"', "[1]", "not json", '{"a": 1}', "None", "NONE", '"none"'):
            with self.assertRaises(MeshError, msg=repr(bad)) as cm:
                repos.parse_test_command(bad)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
            self.assertEqual(cm.exception.details["param"], "test_command")


class TestSetupIntegration(RepoFixtureCase):
    def test_integration_branch_and_worktree(self) -> None:
        """ACC: K1 - mesh/<prj>/integration at the base commit, worktree at <ws>/<prj>/integration."""
        out = repos.setup_integration(os.path.realpath(self.repo), "prj_k1", self.main, self.ws_root)
        wt = os.path.join(self.ws_root, "prj_k1", "integration")
        self.assertEqual(out, {"integration_branch": "mesh/prj_k1/integration", "integration_path": wt})
        self.assertEqual(self.fixture.branches(self.repo, "mesh/*"), ["mesh/prj_k1/integration"])
        self.assertEqual(self.fixture.head(self.repo, "mesh/prj_k1/integration"), self.main)
        entries = {os.path.realpath(e["worktree"]): e for e in self.fixture.worktrees(self.repo)}
        self.assertIn(os.path.realpath(wt), entries)
        self.assertEqual(entries[os.path.realpath(wt)]["branch"], "refs/heads/mesh/prj_k1/integration")
        self.assertEqual(os.stat(os.path.dirname(wt)).st_mode & 0o777, 0o700)

    def test_existing_branch_rc6(self) -> None:
        """ACC: K1 - (Q2, core level) a pre-created mesh/<prj>/integration is rc 6 ALREADY_EXISTS; nothing is created."""
        self.fixture.git(self.repo, "branch", "mesh/prj_dup/integration", "main")
        before_wt = self.fixture.worktrees(self.repo)
        with self.assertRaises(MeshError) as cm:
            repos.setup_integration(os.path.realpath(self.repo), "prj_dup", self.main, self.ws_root)
        self.assert_mesh_error(cm.exception, c.ALREADY_EXISTS, c.EXIT_CONFLICT, branch="mesh/prj_dup/integration")
        self.assertEqual(self.fixture.worktrees(self.repo), before_wt)
        self.assertFalse(os.path.exists(os.path.join(self.ws_root, "prj_dup")))
        self.assertEqual(self.fixture.branches(self.repo, "mesh/*"), ["mesh/prj_dup/integration"])

    def test_worktree_failure_keeps_branch(self) -> None:
        """Q3: a failed worktree step leaves the created branch and names it in details.branch."""
        blocker = os.path.join(self.ws_root, "prj_wf", "integration")
        os.makedirs(blocker)
        with open(os.path.join(blocker, "occupied"), "w", encoding="utf-8") as f:
            f.write("x")
        with self.assertRaises(MeshError) as cm:
            repos.setup_integration(os.path.realpath(self.repo), "prj_wf", self.main, self.ws_root)
        self.assert_mesh_error(cm.exception, c.INTERNAL_ERROR, c.EXIT_INTERNAL,
                               reason="git_failed", step="worktree add", branch="mesh/prj_wf/integration")
        self.assertEqual(self.fixture.branches(self.repo, "mesh/*"), ["mesh/prj_wf/integration"])


    def test_worktree_timeout_names_branch(self) -> None:
        """Review m2-r3 finding 2: a git timeout on the worktree step is rc 1 git_timeout naming the branch; the branch stays."""
        real_run_git = repos.run_git

        def fake(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            if args[:2] == ["worktree", "add"]:
                raise subprocess.TimeoutExpired(["git"] + list(args), 30.0)
            return real_run_git(args, cwd=cwd, **kw)

        with mock.patch("mesh.core.repos.run_git", side_effect=fake):
            with self.assertRaises(MeshError) as cm:
                repos.setup_integration(os.path.realpath(self.repo), "prj_to", self.main, self.ws_root)
        wt = os.path.join(self.ws_root, "prj_to", "integration")
        self.assert_mesh_error(cm.exception, c.INTERNAL_ERROR, c.EXIT_INTERNAL, reason="git_timeout",
                               step="worktree add", branch="mesh/prj_to/integration", worktree=wt)
        self.assertEqual(cm.exception.details["timeout_s"], 30.0)
        self.assertEqual(self.fixture.branches(self.repo, "mesh/*"), ["mesh/prj_to/integration"])

    def test_validate_timeout_is_git_timeout(self) -> None:
        """Review m2-r3 finding 2: a timeout during validation is rc 1 git_timeout, not an unclassified error."""
        with mock.patch("mesh.core.repos.run_git",
                        side_effect=subprocess.TimeoutExpired(["git", "rev-parse"], 30.0)):
            with self.assertRaises(MeshError) as cm:
                repos.validate_repo(self.repo, "main")
        self.assert_mesh_error(cm.exception, c.INTERNAL_ERROR, c.EXIT_INTERNAL, reason="git_timeout",
                               step="rev-parse --show-toplevel")


class TestLeadRepoWorktree(RepoFixtureCase):
    def test_lead_repo_is_detached_and_separate(self) -> None:
        """ACC: K13 (partial) - the lead's repo/ worktree is detached at the integration tip, not the integration worktree."""
        repo = os.path.realpath(self.repo)
        repos.setup_integration(repo, "prj_l", self.main, self.ws_root)
        path = repos.add_lead_repo_worktree(repo, "prj_l", self.ws_root, "agt_lead1")
        self.assertEqual(path, os.path.join(self.ws_root, "prj_l", "lead", "agt_lead1", "repo"))
        entries = {os.path.realpath(e["worktree"]): e for e in self.fixture.worktrees(self.repo)}
        lead = entries[os.path.realpath(path)]
        self.assertTrue(lead.get("detached"), lead)
        self.assertNotIn("branch", lead)
        self.assertEqual(lead["head"], self.main)
        self.assertNotEqual(os.path.realpath(path), os.path.realpath(os.path.join(self.ws_root, "prj_l", "integration")))
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        # Idempotent: an existing repo/ is kept, no second worktree.
        n = len(self.fixture.worktrees(self.repo))
        self.assertEqual(repos.add_lead_repo_worktree(repo, "prj_l", self.ws_root, "agt_lead1"), path)
        self.assertEqual(len(self.fixture.worktrees(self.repo)), n)


class TestInstallPathWiring(unittest.TestCase):
    def test_repo_equal_to_install_root_rejected_before_git(self) -> None:
        """ACC: K1 - intake validation with repo = install root: rc 2 INSTALL_PATH_INSECURE, and no git call."""
        from mesh.daemon.handlers import project as project_handlers

        install_root = get_install_root()
        ctx = SimpleNamespace(args={"repo": install_root, "base_branch": "main", "test_command": "none"})
        boom = AssertionError("git must not run for an insecure repo path")
        with mock.patch("mesh.core.gitops.run_git", side_effect=boom), \
                mock.patch("mesh.core.repos.run_git", side_effect=boom):
            for repo in (install_root, os.path.join(install_root, "mesh"), os.path.dirname(install_root)):
                ctx.args["repo"] = repo
                with self.assertRaises(MeshError) as cm:
                    project_handlers._prepare_repo_project(ctx)  # type: ignore[arg-type]
                self.assertEqual(cm.exception.code, c.INSTALL_PATH_INSECURE, repo)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
                self.assertEqual(cm.exception.details["install_root"], install_root)

    def test_repo_argument_checks_before_git(self) -> None:
        """ACC: K2 - --repo without --base-branch or --test-command is rc 2 before any git call."""
        from mesh.daemon.handlers import project as project_handlers

        boom = AssertionError("git must not run")
        cases = [
            ({"repo": "/tmp/mh-x", "test_command": "none"}, "base_branch"),
            ({"repo": "/tmp/mh-x", "base_branch": "main"}, "test_command"),
            ({"repo": "/tmp/mh-x", "base_branch": "main", "test_command": "[]"}, "test_command"),
            ({"repo": "rel/path", "base_branch": "main", "test_command": "none"}, "repo"),
            ({"base_branch": "main"}, "base_branch"),
            ({"test_command": "none"}, "test_command"),
        ]
        with mock.patch("mesh.core.gitops.run_git", side_effect=boom), \
                mock.patch("mesh.core.repos.run_git", side_effect=boom):
            for args, param in cases:
                with self.assertRaises(MeshError, msg=str(args)) as cm:
                    project_handlers._prepare_repo_project(SimpleNamespace(args=args))  # type: ignore[arg-type]
                self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT, args)
                self.assertEqual(cm.exception.details["param"], param, args)
            self.assertIsNone(project_handlers._prepare_repo_project(SimpleNamespace(args={})))  # type: ignore[arg-type]



class SubmissionGitInfoBoundsTest(RepoFixtureCase):
    """Review m2-r4 finding 3: `deliverable.git` is redacted (§15.2) and size-bounded."""

    def test_large_untracked_tree_is_capped_and_redacted(self) -> None:
        """ACC: K4 (deliverable clause) - 300 untracked files + a secret-looking name: capped at 200, redacted."""
        nm = os.path.join(self.repo, "node_modules")
        os.makedirs(nm)
        for i in range(300):
            with open(os.path.join(nm, f"f{i:03d}.js"), "w", encoding="utf-8") as f:
                f.write("x\n")
        secret = "sk-" + "A" * 24
        with open(os.path.join(self.repo, f"{secret}.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        info = repos.submission_git_info(self.repo, "main", self.main)
        self.assertNotIn("error", info)
        self.assertIs(info["dirty"], True)
        self.assertEqual(info["dirty_count"], 301)
        self.assertIs(info["dirty_files_truncated"], True)
        self.assertEqual(len(info["dirty_files"]), repos.GIT_DIRTY_FILES_CAP)
        self.assertIs(info["diff_stat_truncated"], False)
        self.assertNotIn(secret, repr(info))

    def test_small_tree_not_truncated(self) -> None:
        """ACC: K4 (deliverable clause) - below the caps nothing is cut; the flags are false."""
        with open(os.path.join(self.repo, "one.txt"), "w", encoding="utf-8") as f:
            f.write("x\n")
        info = repos.submission_git_info(self.repo, "main", self.main)
        self.assertEqual(info["dirty_files"], ["one.txt"])
        self.assertEqual(info["dirty_count"], 1)
        self.assertIs(info["dirty_files_truncated"], False)

    def test_bound_git_info_caps_diff_stat(self) -> None:
        """ACC: K4 (deliverable clause) - diff_stat over 16 KB is cut with a flag; error blocks pass through."""
        big = "é" * (repos.GIT_DIFF_STAT_CAP)  # 2 bytes each in UTF-8
        out = repos.bound_git_info({"diff_stat": big, "dirty_files": []})
        self.assertIs(out["diff_stat_truncated"], True)
        self.assertLessEqual(len(out["diff_stat"].encode("utf-8")), repos.GIT_DIFF_STAT_CAP)
        out = repos.bound_git_info({"branch": "b", "error": {"step": "workspace"}})
        self.assertEqual(out, {"branch": "b", "error": {"step": "workspace"}})


if __name__ == "__main__":
    unittest.main()
