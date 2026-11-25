"""m3-r9: refs/heads hygiene. A tag named like a branch must never stand in for it.

- merge (§10.5, m2-r7 finding 6): the task branch is resolved as ``refs/heads/<branch>``.
- validate_repo (§10.2, m2-r3b finding 2): ``base_commit`` is ``refs/heads/<base>``'s commit.
All git runs in GitFixture repos under /tmp/mh-*; fixture reads use full refs (a bare name
would itself be shadowed by the tag).
"""

import unittest

from mesh.core import repos
from tests.gitfixture import GitFixture
from tests.integration import test_merge as tm


class _MergeHelpers(unittest.TestCase):
    """TestMerge's setUp and helpers without its tests (subclassing would rerun them)."""


for _name, _value in vars(tm.TestMerge).items():
    if callable(_value) and not _name.startswith("test"):
        setattr(_MergeHelpers, _name, _value)


class TestMergeRefs(_MergeHelpers):
    def test_merge_ignores_tag_named_like_task_branch(self) -> None:
        """ACC: K4 - a tag named like the task branch, pointing at main, does not shadow refs/heads/<branch>: the merge is a real --no-ff merge of the branch tip, not a noop on the tag."""
        tsk, _ = self.accepted_task(self.prj, "Tagged", "tagged.txt", "tagged\n")
        branch = f"mesh/{self.prj}/{tsk}"
        tip = self.fixture.head(self.repo, f"refs/heads/{branch}")
        self.fixture.git(self.repo, "tag", branch, self.main)
        self.assertEqual(self.fixture.head(self.repo, f"refs/tags/{branch}"), self.main)
        rc, resp = self.merge(tsk)
        self.assertEqual(rc, 0, resp)
        data = resp["data"]
        self.assertEqual(data["status"], "merged", data)
        self.assertIs(data["noop"], False, data)
        mc = self.fixture.head(self.repo, f"refs/heads/{self.integ_branch(self.prj)}")
        self.assertEqual(data["merge_commit"], mc)
        parents = self.fixture.git(self.repo, "rev-list", "--parents", "-n", "1", mc).stdout.split()
        self.assertEqual(parents[1:], [self.main, tip])


class TestValidateRepoRefs(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.repo = self.fixture.make_k0("V")

    def test_base_commit_ignores_tag_named_like_base(self) -> None:
        """ACC: K4 - with a tag `main` at an older commit, validate_repo(repo, "main") records refs/heads/main's commit, not the tag's."""
        old = self.fixture.head(self.repo, "refs/heads/main")
        self.fixture.git(self.repo, "tag", "main", old)
        with open(f"{self.repo}/newer.txt", "w", encoding="utf-8") as f:
            f.write("newer\n")
        self.fixture.git(self.repo, "add", "newer.txt")
        self.fixture.git(self.repo, "commit", "-m", "newer", identity=True)
        new = self.fixture.head(self.repo, "refs/heads/main")
        self.assertNotEqual(new, old)
        self.assertEqual(self.fixture.git(self.repo, "rev-parse", "main^{commit}").stdout.strip(), old)  # the trap
        got = repos.validate_repo(self.repo, "main")
        self.assertEqual(got["base_commit"], new)
        self.assertEqual(got["base_branch"], "main")

    def test_base_commit_with_non_commit_tag_named_like_base(self) -> None:
        """ACC: K4 - a tag `main` at a tree or a blob does not break validate_repo(repo, "main"):
        base_commit is refs/heads/main's commit (no bare `main^{commit}` is resolved first)."""
        head = self.fixture.head(self.repo, "refs/heads/main")
        objects = {
            "tree": self.fixture.head(self.repo, "refs/heads/main^{tree}"),
            "blob": self.fixture.head(self.repo, "refs/heads/main:calc.py"),
        }
        for kind, sha in objects.items():
            with self.subTest(kind=kind):
                self.fixture.git(self.repo, "tag", "-f", "main", sha)
                self.assertEqual(self.fixture.git(self.repo, "cat-file", "-t", "refs/tags/main").stdout.strip(), kind)
                got = repos.validate_repo(self.repo, "main")
                self.assertEqual(got["base_commit"], head)
                self.assertEqual(got["base_branch"], "main")


if __name__ == "__main__":
    unittest.main()
