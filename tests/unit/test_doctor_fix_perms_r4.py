"""m3-test-kdf round 4: doctor --fix-perms repairs before the fail-closed checks,
and empty / "/" homes are refused at resolve_home.

- `mesh doctor --fix-perms` on a chmod 000 home or a chmod 000 mesh.db repairs the
  modes and exits 0 (as on main, plan §5.1); without --fix-perms the same home still
  fails (rc 1). The walk is owner-only and lstat-based: a symlink in the home is not
  followed, and files the home does not know about are left alone.
- `--home ""` / whitespace and `--home /` (or anything resolving to "/") give rc 2
  INVALID_ARGUMENT and create nothing.

The chmod cases need a non-root uid (root ignores file modes) and are skipped as root.
"""

import os
import stat
import unittest

import mesh.constants as c
from mesh.core.paths import resolve_home
from mesh.errors import MeshError
from tests.unit.test_home_resolution_r3 import _CwdCase, _prod_env

_IS_ROOT = os.getuid() == 0


def _mode(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestFixPermsRepairsFirst(_CwdCase):
    def _init(self, name: str) -> str:
        home = os.path.join(self.base, name)
        rc, env = self.run_cli(home, "init", user=True)
        self.assertEqual(rc, 0, env)
        return home

    def _assert_healthy(self, home: str) -> None:
        rc, env = self.run_cli(home, "doctor")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["status"], "healthy", env)

    def test_chmod_000_home(self) -> None:
        home = self._init("h000")
        os.chmod(home, 0o000)
        self.addCleanup(lambda: os.path.isdir(home) and os.chmod(home, 0o700))
        # Without --fix-perms an unreadable home still fails closed (rc 1).
        rc, env = self.run_cli(home, "doctor")
        self.assertEqual(rc, c.EXIT_INTERNAL, env)
        self.assertEqual(env["error"]["code"], c.DB_INTEGRITY_FAILED, env)
        self.assertEqual(_mode(home), 0o000)
        # With --fix-perms: repaired, then the checks pass.
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertTrue(env["data"]["fixed"], env)
        self.assertEqual(_mode(home), 0o700)
        self.assertEqual(_mode(os.path.join(home, "mesh.db")), 0o600)
        self._assert_healthy(home)

    def test_chmod_000_mesh_db(self) -> None:
        home = self._init("db000")
        db = os.path.join(home, "mesh.db")
        os.chmod(db, 0o000)
        self.addCleanup(lambda: os.path.exists(db) and os.chmod(db, 0o600))
        rc, env = self.run_cli(home, "doctor")
        self.assertEqual(rc, c.EXIT_INTERNAL, env)
        self.assertEqual(env["error"]["code"], c.DB_INTEGRITY_FAILED, env)
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertEqual(_mode(db), 0o600)
        self._assert_healthy(home)
        # The production doctor still refuses the (testing, weak) DB after the repair.
        rc, env = self.run_cli(home, "doctor", "--fix-perms", extra_env=_prod_env())
        self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, env)
        self.assertEqual(env["error"]["code"], c.KDF_TOO_WEAK, env)

    def test_chmod_000_known_subdir_and_insecure_modes(self) -> None:
        home = self._init("sub")
        runs = os.path.join(home, "runs")
        inner = os.path.join(runs, "r1")
        os.makedirs(inner)
        with open(os.path.join(inner, "out.log"), "w") as f:
            f.write("x")
        os.chmod(os.path.join(inner, "out.log"), 0o644)
        os.chmod(inner, 0o755)
        os.chmod(runs, 0o000)
        os.chmod(home, 0o755)
        self.addCleanup(lambda: os.path.isdir(runs) and os.chmod(runs, 0o700))
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertEqual(_mode(home), 0o700)
        self.assertEqual(_mode(runs), 0o700)
        self.assertEqual(_mode(inner), 0o700)
        self.assertEqual(_mode(os.path.join(inner, "out.log")), 0o600)

    def test_symlinks_not_followed_and_unknown_entries_left_alone(self) -> None:
        home = self._init("sym")
        outside = os.path.join(self.base, "outside.txt")
        with open(outside, "w") as f:
            f.write("not mesh")
        os.chmod(outside, 0o644)
        outside_dir = os.path.join(self.base, "outside_dir")
        os.mkdir(outside_dir, 0o755)
        os.chmod(outside_dir, 0o755)
        os.symlink(outside, os.path.join(home, "runs", "link-file"))
        os.symlink(outside_dir, os.path.join(home, "logs", "link-dir"))
        # A symlink in place of a known top-level directory is not followed either.
        outside_dir2 = os.path.join(self.base, "outside_dir2")
        os.mkdir(outside_dir2)
        os.chmod(outside_dir2, 0o755)
        os.rmdir(os.path.join(home, "merges"))
        os.symlink(outside_dir2, os.path.join(home, "merges"))
        unknown = os.path.join(home, "notes.txt")
        with open(unknown, "w") as f:
            f.write("user file")
        os.chmod(unknown, 0o640)
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertEqual(_mode(outside), 0o644)
        self.assertEqual(_mode(outside_dir), 0o755)
        self.assertEqual(_mode(outside_dir2), 0o755)
        self.assertEqual(_mode(unknown), 0o640)

    def test_absent_home_with_fix_perms_is_a_no_op(self) -> None:
        home = os.path.join(self.base, "nope")
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertFalse(os.path.exists(home))


class TestEmptyAndRootHome(_CwdCase):
    BAD = ["", " ", "\t", "/", "//", "/.", "/tmp/.."]

    def test_resolve_home_refuses(self) -> None:
        for home in self.BAD + [os.path.relpath("/", self.base)]:
            with self.subTest(home=home):
                with self.assertRaises(MeshError) as ctx:
                    old = os.getcwd()
                    os.chdir(self.base)
                    try:
                        resolve_home(home)
                    finally:
                        os.chdir(old)
                self.assertEqual(ctx.exception.code, c.INVALID_ARGUMENT)
                self.assertEqual(ctx.exception.exit_code, c.EXIT_USAGE)

    def test_cli_refuses_empty_and_root_home(self) -> None:
        up = os.path.relpath("/", self.base)
        for home in self.BAD + [up]:
            for args, user in ((("init",), True), (("doctor",), False),
                               (("doctor", "--fix-perms"), False)):
                with self.subTest(home=home, args=args):
                    before = set(os.listdir(self.base))
                    rc, env = self.run_cli(home, *args, user=user)
                    self.assertEqual(rc, c.EXIT_USAGE, env)
                    self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT, env)
                    self.assertEqual(env["error"]["details"]["param"], "home", env)
                    # Nothing written into the cwd.
                    self.assertEqual(set(os.listdir(self.base)), before)

    def test_init_core_refuses_empty_home(self) -> None:
        from mesh.core.init import init_mesh_home
        for home in ("", "  ", "/"):
            with self.subTest(home=home):
                with self.assertRaises(MeshError) as ctx:
                    init_mesh_home(home, "correct horse battery staple")
                self.assertEqual(ctx.exception.code, c.INVALID_ARGUMENT)


if __name__ == "__main__":
    unittest.main()
