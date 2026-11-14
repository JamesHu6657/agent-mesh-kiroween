"""m3-r9b: `deliver_to_base` pins the base and integration shas once (§10.6, review linear-m3-r9 MAJOR).

The ancestry test and the compare-and-swap use the same pinned `old`; a base advanced by
someone else between them must make the deliver fail, never roll the base back. The
interleave is deterministic: a wrapper around `repos.run_git` moves the base right after
the `merge-base --is-ancestor` call returns. All git runs in GitFixture repos under /tmp/mh-*.
"""

import os
import subprocess
import unittest
from typing import Any, Callable
from unittest import mock

import mesh.constants as c
from mesh.core import repos
from mesh.errors import MeshError
from tests.unit.test_repos import RepoFixtureCase


class TestDeliverCas(RepoFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.realpath(self.repo)
        self.prj = "prj_cas"
        out = repos.setup_integration(self.repo, self.prj, self.main, self.ws_root)
        self.integ_branch = out["integration_branch"]
        self.integ_wt = out["integration_path"]
        self.b = self.commit(self.integ_wt, "integ.txt", "integ\n")

    def commit(self, cwd: str, name: str, content: str) -> str:
        with open(os.path.join(cwd, name), "w", encoding="utf-8") as f:
            f.write(content)
        self.fixture.git(cwd, "add", name)
        self.fixture.git(cwd, "commit", "-q", "-m", name, identity=True)
        return self.fixture.head(cwd, "HEAD")

    def commit_on(self, parent: str, message: str) -> str:
        """A commit with `parent` as its only parent, made without touching any worktree."""
        tree = self.fixture.git(self.repo, "rev-parse", f"{parent}^{{tree}}").stdout.strip()
        return self.fixture.git(self.repo, "commit-tree", tree, "-p", parent, "-m", message,
                                identity=True).stdout.strip()

    def deliver_with_hook(self, base: str, hook: Callable[[], None]) -> Any:
        real = repos.run_git
        fired = []

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            cp = real(args, cwd=cwd, **kw)
            if args[:2] == ["merge-base", "--is-ancestor"] and not fired:
                fired.append(True)
                hook()
            return cp

        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError) as cm:
                repos.deliver_to_base(self.repo, base, self.integ_branch)
        self.assertEqual(fired, [True])
        return cm.exception

    def test_base_advanced_between_ancestry_and_cas_is_not_rolled_back(self) -> None:
        """ACC: K4 - update-ref path: the base moves A -> C (C diverges from the integration tip B)
        after the ancestry check; the CAS uses the pinned old A, so it fails with rc 6
        VERSION_MISMATCH reason base_moved and the base stays at C (it is never set to B)."""
        self.fixture.git(self.repo, "branch", "rel", self.main)
        c_sha = self.commit_on(self.main, "external C")
        exc = self.deliver_with_hook(
            "rel", lambda: self.fixture.git(self.repo, "update-ref", "refs/heads/rel", c_sha))
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/rel"), c_sha)
        self.assert_mesh_error(exc, c.VERSION_MISMATCH, c.EXIT_CONFLICT, reason="base_moved",
                               base_branch="rel", expected_old=self.main, actual=c_sha, new=self.b)

    def test_base_advanced_past_integration_is_not_rolled_back(self) -> None:
        """ACC: K4 - update-ref path: the base moves A -> C where C is a child of the integration
        tip B; the deliver must not move it back to B."""
        self.fixture.git(self.repo, "branch", "rel", self.main)
        c_sha = self.commit_on(self.b, "external C on B")
        exc = self.deliver_with_hook(
            "rel", lambda: self.fixture.git(self.repo, "update-ref", "refs/heads/rel", c_sha))
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/rel"), c_sha)
        self.assert_mesh_error(exc, c.VERSION_MISMATCH, c.EXIT_CONFLICT, reason="base_moved",
                               expected_old=self.main, actual=c_sha)

    def test_worktree_base_advanced_reports_base_moved(self) -> None:
        """ACC: K4 - merge --ff-only path (main is checked out in the repo): a commit C made in that
        worktree after the ancestry check makes the fast-forward impossible; rc 6
        VERSION_MISMATCH reason base_moved, and main stays at C."""
        state = {}

        def advance() -> None:
            state["c"] = self.commit(self.repo, "external.txt", "external\n")

        exc = self.deliver_with_hook("main", advance)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), state["c"])
        self.assert_mesh_error(exc, c.VERSION_MISMATCH, c.EXIT_CONFLICT, reason="base_moved",
                               expected_old=self.main, actual=state["c"])

    def test_worktree_base_advanced_past_integration_reports_base_moved(self) -> None:
        """ACC: K4 - a checked-out base moves A -> C, a descendant of pinned B.
        `merge --ff-only B` succeeds without moving C; report rc 6, not a git failure."""
        c_sha = self.commit_on(self.b, "external C on B")

        def advance() -> None:
            self.fixture.git(self.repo, "merge", "--ff-only", c_sha)

        exc = self.deliver_with_hook("main", advance)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), c_sha)
        self.assertEqual(self.fixture.head(self.repo, "HEAD"), c_sha)
        self.assertEqual(self.fixture.head(self.repo, f"refs/heads/{self.integ_branch}"), self.b)
        self.assertEqual(self.fixture.git(self.repo, "status", "--porcelain").stdout, "")
        self.assert_mesh_error(exc, c.VERSION_MISMATCH, c.EXIT_CONFLICT, reason="base_moved",
                               expected=self.b, actual=c_sha, base_branch="main")

    def test_post_ff_rev_parse_failure_reports_git_error(self) -> None:
        """ACC: K4 - a failed post-ff read is a git error, not evidence of base movement."""
        real = repos.run_git
        failed_calls = []
        stderr = "fatal: bad config line 1 in file .git/config\n"

        def wrapped(args, cwd=None, **kw):  # type: ignore[no-untyped-def]
            if args == ["rev-parse", "--verify", "refs/heads/main"]:
                failed_calls.append((args, cwd))
                return subprocess.CompletedProcess(args, 128, stdout="", stderr=stderr)
            return real(args, cwd=cwd, **kw)

        with mock.patch("mesh.core.repos.run_git", side_effect=wrapped):
            with self.assertRaises(MeshError) as cm:
                repos.deliver_to_base(self.repo, "main", self.integ_branch)
        self.assertEqual(len(failed_calls), 1)
        self.assertEqual(failed_calls[0][1], self.repo)
        exc = cm.exception
        self.assert_mesh_error(exc, c.INTERNAL_ERROR, c.EXIT_INTERNAL, reason="git_failed",
                               step="rev-parse base after ff", git_rc=128, stderr_tail=stderr,
                               repo=self.repo, worktree=self.repo, base_branch="main")
        self.assertIn("rc 128", exc.message)
        self.assertNotEqual(exc.details["reason"], "base_moved")
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), self.b)
        self.assertEqual(self.fixture.head(self.repo, "HEAD"), self.b)

if __name__ == "__main__":
    unittest.main()
