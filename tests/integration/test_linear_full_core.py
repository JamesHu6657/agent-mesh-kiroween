"""Linear full-codebase review of main 1f78a4f, A4: workspaces.root cannot move while repo
projects are active or paused (their integration worktrees live under the old root)."""

import os
import tempfile
import shutil
import unittest

import mesh.constants as c
from tests.integration import test_merge as tm


class TestRootChangeWithLiveProjects(unittest.TestCase):
    setUp = tm.TestMerge.setUp
    make_project = tm.TestMerge.make_project

    def new_root(self) -> str:
        d = tempfile.mkdtemp(prefix="mh-root-")
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        return os.path.realpath(d)

    def test_refused_while_a_repo_project_is_active(self) -> None:
        rc, resp = self.harness.cli("config", "set", "workspaces.root", self.new_root(), user=True)
        self.assertEqual(rc, c.EXIT_CONFLICT, resp)
        self.assertEqual(resp["error"]["code"], c.INVALID_SETTING)
        self.assertIn(self.prj, resp["error"]["details"]["projects"])

    def test_same_root_is_still_accepted(self) -> None:
        rc, resp = self.harness.cli("config", "get", "workspaces.root", user=True)
        cur = resp["data"]["value"]
        if isinstance(cur, str):
            rc, resp = self.harness.cli("config", "set", "workspaces.root", cur, user=True)
            self.assertEqual(rc, 0, resp)


if __name__ == "__main__":
    unittest.main()
