"""Unit tests for gitops allow-list and wrapper per plan §7.5, §10.5, §11.3. ACC: A6"""

import os
import re
import unittest
from unittest.mock import patch

from mesh.core.gitops import (
    ALLOWED_SUBCOMMANDS,
    FORBIDDEN_SUBCOMMANDS,
    FORBIDDEN_TOKENS,
    run_git,
    validate_git_args,
)


class TestGitops(unittest.TestCase):
    """Test git operations allow-list and subprocess wrapper. ACC: A6"""

    def test_gitops_allowlist(self) -> None:
        """ACC: A6 - Test strict git allow-list validation and rejection of forbidden commands."""
        # 1. Assert forbidden constants contain required commands
        for cmd in ("push", "rebase", "reset", "checkout", "switch", "fetch", "pull", "clone", "config", "stash", "clean"):
            self.assertIn(cmd, FORBIDDEN_SUBCOMMANDS)

        # 1b. The explicit allow-list exists and never contains a forbidden subcommand
        self.assertIsInstance(ALLOWED_SUBCOMMANDS, (set, frozenset))
        for cmd in ("push", "rebase", "reset", "checkout", "switch", "fetch", "pull", "clone", "config", "stash", "clean", "rm", "mv"):
            self.assertNotIn(cmd, ALLOWED_SUBCOMMANDS)
        self.assertEqual(ALLOWED_SUBCOMMANDS & FORBIDDEN_SUBCOMMANDS, set())

        # 2. Assert validation rejects before subprocess spawn
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = AssertionError("subprocess.run should NOT be called on validation failure")

            # Empty arguments
            with self.assertRaises(AssertionError):
                validate_git_args([])

            # Forbidden subcommands
            for subcmd in ("push", "pull", "fetch", "clone", "reset", "checkout", "rebase", "rm", "config", "stash", "clean", "switch"):
                with self.assertRaises(AssertionError):
                    validate_git_args([subcmd])
                with self.assertRaises(AssertionError):
                    run_git([subcmd])

            # Forbidden tokens anywhere in argv
            forbidden_tokens = ["-X", "--strategy-option", "--force", "-f", "-Xours", "--strategy-option=theirs"]
            for tok in forbidden_tokens:
                with self.assertRaises(AssertionError):
                    validate_git_args(["diff", tok])
                with self.assertRaises(AssertionError):
                    validate_git_args(["status", tok])

            # Branch: create, or exactly `-d <ref>` for prune (§10.7). Every other flag,
            # and every forced/renaming form, stays rejected.
            with self.assertRaises(AssertionError):
                validate_git_args(["branch"])  # no name
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-D", "foo"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-m", "old", "new"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-M", "old", "new"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "--delete", "--force", "foo"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-df", "foo"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-d", "-f", "foo"])
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-d"])  # no ref
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-d", "foo", "bar"])  # one ref only
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-d", "-foo"])  # a ref never starts with '-'
            with self.assertRaises(AssertionError):
                validate_git_args(["branch", "-d", "foo bar"])  # no whitespace in a ref

            # Worktree: disallowed verbs or flags
            with self.assertRaises(AssertionError):
                validate_git_args(["worktree"])  # no verb
            with self.assertRaises(AssertionError):
                validate_git_args(["worktree", "lock"])
            with self.assertRaises(AssertionError):
                validate_git_args(["worktree", "add", "--force", "/path"])
            with self.assertRaises(AssertionError):
                validate_git_args(["worktree", "remove", "-f", "/path"])

            # Merge: only three allowed forms
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "other_branch"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--squash", "other_branch"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "-X", "ours", "other_branch"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--no-ff", "other_branch"])  # missing --no-commit

            # Merge: exact arity only (review m1c-run1 finding 5)
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--no-ff", "--no-commit", "-s", "ours", "ref"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--no-ff", "--no-commit", "--squash", "ref"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--ff-only", "ref", "extra"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--abort", "extra"])
            # A ref must not start with '-'
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--ff-only", "-b"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--no-ff", "--no-commit", "--squash"])
            with self.assertRaises(AssertionError):
                validate_git_args(["merge", "--ff-only", ""])

            # diff/log must not be able to write a file via --output/-o
            for out_flag in ("--output=/tmp/x", "--output", "-o", "-o/tmp/x"):
                with self.assertRaises(AssertionError):
                    validate_git_args(["diff", out_flag])
                with self.assertRaises(AssertionError):
                    validate_git_args(["log", out_flag])

            # Commit: no amend or fixup
            with self.assertRaises(AssertionError):
                validate_git_args(["commit", "--amend"])
            with self.assertRaises(AssertionError):
                validate_git_args(["commit", "--fixup", "HEAD"])

            # Disallowed unknown subcommands
            for bad_subcmd in ("daemon", "init", "remote", "cherry-pick", "bisect"):
                with self.assertRaises(AssertionError):
                    validate_git_args([bad_subcmd])

            # 3. Assert allowed forms validate successfully
            allowed_invocations = [
                ["rev-parse", "HEAD"],
                ["rev-parse", "--verify", "HEAD"],
                ["status", "--porcelain"],
                ["diff", "--name-only", "--diff-filter=U"],
                ["log", "-n", "5"],
                ["branch", "mesh/prj_1/task_1"],
                ["branch", "mesh/prj_1/task_1", "pre_head"],
                ["branch", "-d", "mesh/prj_1/task_1"],
                ["worktree", "add", "/tmp/wt", "mesh/prj_1/task_1"],
                ["worktree", "list", "--porcelain"],
                ["worktree", "remove", "/tmp/wt"],
                ["worktree", "prune"],
                ["merge", "--no-ff", "--no-commit", "mesh/prj_1/task_1"],
                ["merge", "--abort"],
                ["merge", "--ff-only", "mesh/prj_1/integration"],
                ["commit", "--no-edit", "-m", "Merge mesh task tsk_1: Foo"],
                ["commit", "-m", "Merge mesh task tsk_1: Foo"],
                ["update-ref", "refs/heads/main", "new_hash", "old_hash"],
                ["merge-base", "--is-ancestor", "base", "integration"],
                ["show-ref", "refs/heads/main"],
                ["cat-file", "-e", "HEAD"],
            ]
            for inv in allowed_invocations:
                # Should not raise AssertionError
                validate_git_args(inv)

    def test_git_never_spawned_outside_gitops(self) -> None:
        """Verify across mesh/core, mesh/daemon, and mesh/cli that git is never spawned directly."""
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        check_dirs = [
            os.path.join(repo_root, "mesh", "core"),
            os.path.join(repo_root, "mesh", "daemon"),
            os.path.join(repo_root, "mesh", "cli"),
        ]

        # Pattern matching subprocess invocation of git
        git_call_pattern = re.compile(r'subprocess\.(run|Popen|check_output|check_call|call)\(\s*\[\s*["\']git["\']')

        violations = []
        for d in check_dirs:
            for root, _, files in os.walk(d):
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    file_path = os.path.join(root, f)
                    if file_path.endswith(os.path.join("mesh", "core", "gitops.py")):
                        continue  # gitops.py is the designated module
                    with open(file_path, "r", encoding="utf-8") as fp:
                        content = fp.read()
                    if git_call_pattern.search(content):
                        violations.append(file_path)

        self.assertEqual(violations, [], f"Found unauthorized git subprocess call(s) in: {violations}")

    def test_run_git_execution(self) -> None:
        """Verify run_git executes an allow-listed command against the repo root."""
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cp = run_git(["rev-parse", "HEAD"], cwd=repo_root)
        self.assertEqual(cp.returncode, 0)
        output = cp.stdout.strip()
        self.assertEqual(len(output), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in output))

    def test_run_git_ignores_inherited_repo_location_env(self) -> None:
        """An inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE never redirects run_git away from its cwd."""
        from tests.gitfixture import GitFixture

        fixture = GitFixture()
        self.addCleanup(fixture.cleanup)
        repo_a = fixture.make_k0("A")
        repo_b = fixture.make_k0("B")
        fixture.git(repo_b, "commit", "--allow-empty", "-m", "second")
        hermetic = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        hermetic.update(fixture.git_env_vars())
        hermetic.update({
            "GIT_DIR": os.path.join(repo_a, ".git"),
            "GIT_WORK_TREE": repo_a,
            "GIT_INDEX_FILE": os.path.join(repo_a, ".git", "index"),
        })
        with patch.dict(os.environ, hermetic, clear=True):
            top = run_git(["rev-parse", "--show-toplevel"], cwd=repo_b)
            head = run_git(["rev-parse", "HEAD"], cwd=repo_b)
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertEqual(os.path.realpath(top.stdout.strip()), os.path.realpath(repo_b))
        self.assertEqual(head.stdout.strip(), fixture.head(repo_b))
        self.assertNotEqual(head.stdout.strip(), fixture.head(repo_a))


if __name__ == "__main__":
    unittest.main()


class TestCarbonGitAllowList(unittest.TestCase):
    def test_ref_update_requires_compare_and_swap(self):
        from mesh.core.gitops import validate_git_args
        for args in (["update-ref", "--stdin"], ["update-ref", "-d", "refs/heads/main"],
                     ["update-ref", "refs/heads/main", "a" * 40]):
            with self.assertRaises(AssertionError):
                validate_git_args(args)
        validate_git_args(["update-ref", "refs/heads/main", "a" * 40, "b" * 40])

    def test_combined_force_flags_are_rejected(self):
        from mesh.core.gitops import validate_git_args
        for flag in ("-ff", "-df", "--force=1"):
            with self.assertRaises(AssertionError):
                validate_git_args(["worktree", "remove", flag, "/tmp/worktree"])
        validate_git_args(["worktree", "add", "-b", "topic", "/tmp/worktree", "main"])
