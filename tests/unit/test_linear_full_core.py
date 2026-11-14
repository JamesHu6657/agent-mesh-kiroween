"""Linear full-codebase review of main 1f78a4f, part A (mesh/core): regression tests.

A1 workspaces.root must be in the normal form runtime uses (abspath(strip)), so the
   directory validated is the one used.
A2 task.max_attempts_cap cannot drop below task.default_max_attempts.
A3 deliver: a concurrent switch of the base worktree never advances another branch.
A5 I3 grandfathering counts leases acquired while the global cap was unlimited (NULL).
(A4 is covered in tests/integration/test_linear_full_core.py.)"""

import json
import os
import sqlite3
import tempfile
import shutil
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import repos
from mesh.core.invariants import check_i3_concurrency_and_limits
from mesh.core.schema import INITIAL_SCHEMA_STATEMENTS
from mesh.core.settings import validate_setting
from mesh.errors import MeshError
from tests.unit.test_repos import RepoFixtureCase


class TestWorkspacesRootNormalForm(unittest.TestCase):
    def setUp(self) -> None:
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="mh-a1-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        os.chmod(self.base, 0o700)
        self.home = os.path.join(self.base, "home")
        os.makedirs(self.home, mode=0o700)
        self.ws = os.path.join(self.base, "ws")
        os.makedirs(self.ws, mode=0o700)
        self.install = os.path.join(self.base, "install")
        os.makedirs(self.install, mode=0o700)

    def validate(self, val):
        return validate_setting(key="workspaces.root", val=val, home=self.home,
                                current_settings={}, install_root=self.install)

    def test_normal_path_accepted(self) -> None:
        self.assertEqual(self.validate(self.ws), self.ws)

    def test_non_normal_forms_refused(self) -> None:
        # <install>/link -> <outside>/child: "<install>/link/.." is <outside> on the
        # filesystem but <install> for abspath(); never validate one and use the other.
        outside = os.path.join(self.base, "outside")
        os.makedirs(os.path.join(outside, "child"), mode=0o700)
        os.chmod(outside, 0o700)
        os.symlink(os.path.join(outside, "child"), os.path.join(self.install, "link"))
        for val in (os.path.join(self.install, "link") + "/..", self.ws + "/", self.ws + "/.",
                    self.ws.replace("/ws", "//ws"), self.ws + " ", " " + self.ws,
                    os.path.join(self.ws, "..", "ws")):
            with self.subTest(val=val):
                with self.assertRaises(MeshError) as cm:
                    self.validate(val)
                self.assertEqual(cm.exception.code, c.INVALID_SETTING)


class TestAttemptsCapVsDefault(unittest.TestCase):
    def v(self, key, val, cur):
        return validate_setting(key=key, val=val, home="/nonexistent", current_settings=cur)

    def test_cap_below_stored_or_default_default_refused(self) -> None:
        for cur, cap in (({}, 2), ({"task.default_max_attempts": 5}, 4)):
            with self.subTest(cur=cur):
                with self.assertRaises(MeshError) as cm:
                    self.v("task.max_attempts_cap", cap, cur)
                self.assertEqual(cm.exception.code, c.INVALID_SETTING)
                self.assertIn("task.default_max_attempts", cm.exception.message)

    def test_cap_at_or_above_default_ok(self) -> None:
        self.assertEqual(self.v("task.max_attempts_cap", 3, {}), 3)
        self.assertEqual(self.v("task.max_attempts_cap", 1, {"task.default_max_attempts": 1}), 1)
        self.assertEqual(self.v("task.default_max_attempts", 1, {"task.max_attempts_cap": 10}), 1)


class TestDeliverWorktreeSwitched(RepoFixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.realpath(self.repo)
        out = repos.setup_integration(self.repo, "prj_a3", self.main, self.ws_root)
        self.integ_branch = out["integration_branch"]
        wt = out["integration_path"]
        with open(os.path.join(wt, "i.txt"), "w", encoding="utf-8") as f:
            f.write("i\n")
        self.fixture.git(wt, "add", "i.txt")
        self.fixture.git(wt, "commit", "-q", "-m", "i", identity=True)
        self.b = self.fixture.head(wt, "HEAD")

    def test_switch_after_discovery_changes_nothing(self) -> None:
        self.assertEqual(repos._worktree_for_branch(self.repo, "main"), self.repo)
        real = repos._worktree_for_branch

        def found_then_switched(repo, branch):  # type: ignore[no-untyped-def]
            wt = real(repo, branch)
            self.fixture.git(self.repo, "switch", "-q", "-c", "other")
            return wt

        with mock.patch("mesh.core.repos._worktree_for_branch", side_effect=found_then_switched):
            with self.assertRaises(MeshError) as cm:
                repos.deliver_to_base(self.repo, "main", self.integ_branch)
        self.assert_mesh_error(cm.exception, c.VERSION_MISMATCH, c.EXIT_CONFLICT,
                               reason="worktree_switched", actual_ref="refs/heads/other")
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/other"), self.main)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), self.main)

    def test_normal_deliver_still_fast_forwards(self) -> None:
        out = repos.deliver_to_base(self.repo, "main", self.integ_branch)
        self.assertEqual(out["new"], self.b)
        self.assertEqual(self.fixture.head(self.repo, "refs/heads/main"), self.b)


class TestI3UnlimitedGrandfathered(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        for st in INITIAL_SCHEMA_STATEMENTS:
            self.conn.execute(st)
        self.conn.execute("PRAGMA foreign_keys = OFF;")

    def lease(self, n: int, total_limit) -> None:
        self.conn.execute(
            "INSERT INTO leases (key, run_id, holder_kind, holder, agent_id, fence, slots_in_use_at_acquire, "
            "limit_at_acquire, total_at_acquire, total_limit_at_acquire, acquired_at, expires_at, heartbeat_at) "
            "VALUES (?, ?, 'meshd', 'meshd', 'agt_x', 1, 0, 5, ?, ?, 't', 't', 't');",
            (f"task:tsk_{n}", f"run_{n}", n, total_limit))

    def set_cap(self, v: int) -> None:
        self.conn.execute("INSERT INTO settings (key, value_json, updated_at, updated_by) "
                          "VALUES ('scheduler.max_total_runs', ?, 't', 'usr');", (json.dumps(v),))

    def i3(self):
        return [v for v in check_i3_concurrency_and_limits(self.conn) if "exceed" in v.message]

    def test_unlimited_at_acquire_is_grandfathered(self) -> None:
        self.lease(0, None)
        self.lease(1, None)
        self.set_cap(1)
        self.assertEqual(self.i3(), [])

    def test_real_oversubscription_still_reported(self) -> None:
        self.lease(0, 1)
        self.lease(1, 1)
        self.set_cap(1)
        self.assertEqual(len(self.i3()), 1)


if __name__ == "__main__":
    unittest.main()
