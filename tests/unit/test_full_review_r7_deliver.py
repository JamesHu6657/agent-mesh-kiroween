"""Full review r7 (R2-P1-1): `project deliver` never overwrites an ignored file in the
base branch's worktree (git's ff merge does so silently by default, and `status
--porcelain` does not list ignored files)."""

import os
import subprocess
import tempfile
import unittest

import mesh.constants as c
from mesh.core import gitops
from mesh.core.repos import deliver_to_base
from mesh.errors import MeshError

INTEG = "mesh/prj_r7/integration"


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class DeliverIgnoredFilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(root, "repo")
        self.integ = os.path.join(root, "integration")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "r7@example.invalid")
        _git(self.repo, "config", "user.name", "r7")
        _write(os.path.join(self.repo, ".gitignore"), ".env\nbuild/\ncfg\n")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-qm", "init")
        _git(self.repo, "branch", INTEG)
        _git(self.repo, "worktree", "add", "-q", self.integ, INTEG)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_on_integration(self, files):
        for rel, text in files.items():
            _write(os.path.join(self.integ, rel), text)
            _git(self.integ, "add", "-f", rel)
        _git(self.integ, "commit", "-qm", "task work")

    def test_ignored_file_at_an_added_path_is_refused_and_kept(self):
        self._commit_on_integration({".env": "AGENT=placeholder\n"})
        _write(os.path.join(self.repo, ".env"), "REAL_SECRET=keep\n")
        old = _git(self.repo, "rev-parse", "refs/heads/main")
        with self.assertRaises(MeshError) as cm:
            deliver_to_base(self.repo, "main", INTEG)
        err = cm.exception
        self.assertEqual(err.code, c.WORKTREE_DIRTY)
        self.assertEqual(err.exit_code, c.EXIT_MERGE_FAILED)
        self.assertEqual(err.details["reason"], "ignored_files_would_be_overwritten")
        self.assertEqual(err.details["paths"], [".env"])
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"), old)
        self.assertEqual(_read(os.path.join(self.repo, ".env")), "REAL_SECRET=keep\n")

    def test_ignored_file_where_the_delivery_needs_a_directory_is_refused(self):
        self._commit_on_integration({"cfg/settings.json": "{}\n"})
        _write(os.path.join(self.repo, "cfg"), "local\n")
        with self.assertRaises(MeshError) as cm:
            deliver_to_base(self.repo, "main", INTEG)
        self.assertEqual(cm.exception.details["paths"], ["cfg/settings.json"])
        self.assertEqual(_read(os.path.join(self.repo, "cfg")), "local\n")

    def test_new_file_inside_an_ignored_directory_still_delivers(self):
        self._commit_on_integration({"build/out.js": "x\n"})
        _write(os.path.join(self.repo, "build", "local.js"), "mine\n")
        res = deliver_to_base(self.repo, "main", INTEG)
        self.assertEqual(res["method"], "merge_ff_only")
        self.assertEqual(_git(self.repo, "rev-parse", "refs/heads/main"),
                         _git(self.repo, "rev-parse", f"refs/heads/{INTEG}"))
        self.assertEqual(_read(os.path.join(self.repo, "build", "local.js")), "mine\n")

    def test_without_a_local_file_the_ff_lands(self):
        self._commit_on_integration({".env": "AGENT=placeholder\n", "src/a.py": "a = 1\n"})
        res = deliver_to_base(self.repo, "main", INTEG)
        self.assertEqual(res["method"], "merge_ff_only")
        self.assertEqual(_read(os.path.join(self.repo, ".env")), "AGENT=placeholder\n")

    def test_gitops_allows_exactly_the_deliver_form(self):
        gitops.validate_git_args(["merge", "--ff-only", "--no-overwrite-ignore", "0123abcd"])
        gitops.validate_git_args(["merge", "--ff-only", "0123abcd"])
        for bad in (["merge", "--ff-only", "--overwrite-ignore", "0123abcd"],
                    ["merge", "--ff-only", "--no-overwrite-ignore", "-x"],
                    ["merge", "--no-overwrite-ignore", "--ff-only", "0123abcd"],
                    ["merge", "--ff-only", "--no-overwrite-ignore", "a", "b"]):
            with self.assertRaises(AssertionError, msg=bad):
                gitops.validate_git_args(bad)


if __name__ == "__main__":
    unittest.main()
