"""Unit tests for the hermetic git fixture (checklist section K, fixture K0).

Supporting fixture for K1-K14 (fixture K0); it carries no ACC tag of its own.
"""

import os
import subprocess
import unittest
from unittest import mock

from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import MeshHarness


class TestGitFixture(unittest.TestCase):
    """K0 fixture: repo shape, hermetic env, no-push remote, path guard, cleanup."""

    def setUp(self):
        self.fixture = GitFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_k0_repo_shape_and_suite(self):
        """K0 repo is on main with a repo-local identity and a passing unittest suite."""
        repo = self.fixture.make_k0()
        self.assertTrue(repo.startswith("/tmp/mh-"))
        self.assertEqual(self.fixture.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")
        self.assertEqual(self.fixture.git(repo, "config", "--get", "user.name").stdout.strip(), "t")
        self.assertEqual(self.fixture.git(repo, "config", "--get", "user.email").stdout.strip(), "t@example.invalid")
        cp = subprocess.run(
            ["python3", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            cwd=repo,
            env=self.fixture.env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertTrue(self.fixture.is_clean(repo))
        self.assertFalse(self.fixture.has_merge_head(repo))

    def test_k0_without_identity_requires_explicit_identity(self):
        """make_k0(identity=False) leaves no identity and useConfigOnly blocks a bare commit."""
        repo = self.fixture.make_k0("R2", identity=False)
        cp = self.fixture.git(repo, "config", "--get", "user.name", check=False)
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stdout.strip(), "")
        self.assertEqual(self.fixture.git(repo, "config", "--get", "user.useConfigOnly").stdout.strip(), "true")
        with open(os.path.join(repo, "calc.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef sub(a, b):\n    return a - b\n")
        self.fixture.git(repo, "add", "-A")
        failed = self.fixture.git(repo, "commit", "-m", "no identity", check=False)
        self.assertNotEqual(failed.returncode, 0)
        # The same commit succeeds when the identity is passed explicitly.
        ok = self.fixture.git(repo, "commit", "-m", "explicit identity", identity=True)
        self.assertEqual(ok.returncode, 0)

    def test_remote_refs_without_push(self):
        """The bare origin is built locally: ls-remote lists main at R's head, nothing pushed."""
        repo = self.fixture.make_k0()
        refs = self.fixture.remote_refs(repo)
        self.assertIn("refs/heads/main", [line.split("\t")[1] for line in refs])
        main_lines = [line for line in refs if line.endswith("\trefs/heads/main")]
        self.assertEqual(len(main_lines), 1, refs)
        sha, ref = main_lines[0].split("\t")
        self.assertEqual(ref, "refs/heads/main")
        self.assertEqual(sha, self.fixture.head(repo, "main"))
        # Nothing was pushed: the bare repo has no other refs and R has no upstream.
        self.assertEqual(len(refs), 2, refs)  # HEAD + refs/heads/main
        self.assertNotEqual(self.fixture.git(repo, "rev-parse", "--abbrev-ref", "@{u}", check=False).returncode, 0)

    def test_guard_refuses_paths_outside_base_before_spawn(self):
        """A Mesh-checkout path (cwd or argument) is refused before any process starts."""
        repo = self.fixture.make_k0()
        with mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called")):
            with mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen must not be called")):
                with self.assertRaises(AssertionError):
                    self.fixture.git("/home/node/workspace/mesh-fresh-v1", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git("/tmp/not-a-repo", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.path("..", "escape")
                # Repository-selecting options and absolute path arguments are refused too.
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "-C", "/home/node/workspace/mesh-fresh-v1", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "--git-dir=/home/node/workspace/mesh-fresh-v1/.git", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "--git-dir", "/home/node/workspace/mesh-fresh-v1/.git", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "--work-tree=/home/node/workspace/mesh-fresh-v1", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "--work-tree", "/home/node/workspace/mesh-fresh-v1", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(repo, "status", "/home/node/workspace/mesh-fresh-v1")
        # The fixture is still usable afterwards.
        self.assertTrue(self.fixture.is_clean(repo))

    def test_env_drops_inherited_git_vars(self):
        """Inherited GIT_* variables never reach the fixture env or an identity-less commit."""
        repo = self.fixture.make_k0("R3", identity=False)
        other = self.fixture.path("other-repo")
        os.makedirs(other, exist_ok=True)
        leaked = {
            "GIT_DIR": other,
            "GIT_WORK_TREE": other,
            "GIT_AUTHOR_NAME": "leak",
            "GIT_COMMITTER_EMAIL": "leak@example.invalid",
        }
        with mock.patch.dict(os.environ, leaked):
            env = self.fixture.env()
            for key in leaked:
                self.assertNotIn(key, env)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], self.fixture.empty_gitconfig)
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_CEILING_DIRECTORIES"], self.fixture.base)
            # The hermetic keys are the only GIT_* keys present.
            self.assertEqual(
                sorted(k for k in env if k.startswith("GIT_")),
                ["GIT_CEILING_DIRECTORIES", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"],
            )
            # An identity-less commit is still refused with the leak in the outer env.
            with open(os.path.join(repo, "calc.py"), "a", encoding="utf-8") as f:
                f.write("\n\ndef sub(a, b):\n    return a - b\n")
            self.fixture.git(repo, "add", "-A")
            failed = self.fixture.git(repo, "commit", "-m", "no identity", check=False)
            self.assertNotEqual(failed.returncode, 0)

    def test_branches_returns_plain_names_for_worktree_branches(self):
        """branches() returns the bare name of a branch checked out in a fixture worktree."""
        repo = self.fixture.make_k0()
        self.fixture.git(repo, "branch", "mesh/prj_x/tsk_1", "main")
        wt = self.fixture.path("wt-tsk1")
        self.fixture.git(repo, "worktree", "add", wt, "mesh/prj_x/tsk_1")
        names = self.fixture.branches(repo)
        self.assertIn("mesh/prj_x/tsk_1", names)
        self.assertIn("main", names)
        self.assertNotIn("+ mesh/prj_x/tsk_1", names)
        self.assertEqual(self.fixture.branches(repo, "mesh/prj_x/*"), ["mesh/prj_x/tsk_1"])
        self.assertEqual(self.fixture.branches(repo, "mesh/prj_y/*"), [])
        # A `*` crosses `/`, so a "branch was deleted" check cannot pass falsely.
        self.assertEqual(self.fixture.branches(repo, "mesh/*"), ["mesh/prj_x/tsk_1"])

    def test_cleanup_removes_base(self):
        """cleanup() removes the base dir."""
        base = self.fixture.base
        self.assertTrue(os.path.isdir(base))
        self.fixture.cleanup()
        self.assertFalse(os.path.exists(base))


class TestGitMeshHarness(unittest.TestCase):
    """GitMeshHarness keeps the MeshHarness env and only adds the hermetic git keys."""

    def setUp(self):
        self.fixture = GitFixture()
        self.extra_path = self.fixture.path("stubs")
        os.makedirs(self.extra_path, exist_ok=True)

    def tearDown(self):
        self.fixture.cleanup()

    def test_get_env_keeps_harness_env_and_adds_git_keys(self):
        """PATH (shims + extra_path), MESH_HOME, MESH_TESTING and the tripwire survive."""
        harness = GitMeshHarness(
            self.fixture,
            extra_path=self.extra_path,
            check_invariants_on_teardown=False,
        )
        home = harness.home
        base = self.fixture.base
        # An inherited GIT_* set (as a git hook or wrapper would export) must not leak.
        leaked = {
            "GIT_DIR": self.extra_path,
            "GIT_WORK_TREE": self.extra_path,
            "GIT_AUTHOR_NAME": "leak",
            "GIT_COMMITTER_EMAIL": "leak@example.invalid",
        }
        try:
            with mock.patch.dict(os.environ, leaked):
                env = harness.get_env()
            expected = MeshHarness.get_env(harness)
            self.assertEqual(env["PATH"], expected["PATH"])
            self.assertTrue(env["PATH"].startswith(self.extra_path + ":"))
            self.assertIn(os.path.join(harness.repo_root, "tests", "shims"), env["PATH"])
            self.assertEqual(env["MESH_HOME"], harness.home)
            self.assertEqual(env["MESH_TESTING"], "1")
            self.assertEqual(env["MESH_TRIPWIRE_FILE"], harness.tripwire_file)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], self.fixture.empty_gitconfig)
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_CEILING_DIRECTORIES"], self.fixture.base)
            self.assertNotIn("GIT_AUTHOR_NAME", env)
            self.assertNotIn("GIT_COMMITTER_EMAIL", env)
            for key in leaked:
                self.assertNotIn(key, env)
            self.assertEqual(
                sorted(k for k in env if k.startswith("GIT_")),
                ["GIT_CEILING_DIRECTORIES", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"],
            )
        finally:
            harness.teardown()
        self.assertFalse(os.path.exists(home))
        self.assertFalse(os.path.exists(base))

    def test_harness_home_is_an_allowed_root(self):
        """GitMeshHarness registers its home (task/lead worktrees live there); other paths stay refused."""
        harness = GitMeshHarness(
            self.fixture,
            extra_path=self.extra_path,
            check_invariants_on_teardown=False,
        )
        try:
            self.assertEqual(self.fixture.allowed_roots, [self.fixture.base, harness.home])
            os.makedirs(os.path.join(harness.home, "workspaces"), exist_ok=True)
            repo = self.fixture.make_k0()
            wt = os.path.join(harness.home, "workspaces", "wt")
            self.fixture.git(repo, "worktree", "add", "--detach", wt, "main")
            self.assertEqual(self.fixture.head(wt), self.fixture.head(repo))
            self.assertEqual(self.fixture.path("x"), os.path.join(self.fixture.base, "x"))
            with mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called")):
                with self.assertRaises(AssertionError):
                    self.fixture.git("/tmp/mh-not-registered", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git(harness.home + "-sibling", "status")
                with self.assertRaises(AssertionError):
                    self.fixture.git("/home/node/workspace/mesh-fresh-v1", "status")
            for bad in ("/tmp/other", "/home/node/workspace/mesh-fresh-v1", "relative/mh-x"):
                with self.assertRaises(AssertionError):
                    self.fixture.allow_root(bad)
            self.assertEqual(self.fixture.allowed_roots, [self.fixture.base, harness.home])
        finally:
            harness.teardown()


if __name__ == "__main__":
    unittest.main()
