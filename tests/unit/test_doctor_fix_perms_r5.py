"""m3-test-kdf round 5: doctor --fix-perms order and fd-based walk.

- Every path / overlap / refusal check runs before --fix-perms writes anything: an
  install dir used as the home gives rc 2 INSTALL_PATH_INSECURE with its mode unchanged.
- The walk is fd-based (O_PATH|O_NOFOLLOW + fstat + chmod on the fd): a leaf or an
  ancestor swapped for a symlink between the check and the chmod (forced through the
  doctor._RACE_HOOK test hook) never gets an outside file chmodded.
- The walk is iterative and holds one directory fd at a time: a 1500-level tree is
  repaired (no RecursionError / INTERNAL_ERROR, no fd exhaustion).
- Files under workspaces/ keep the owner's bits (x included); group/other are removed.
- Entries skipped because they are symlinks or owned by another uid give fixed:false
  with the skipped entries and reasons (rc 0).
- MESH_HOME="" counts as unset and falls back to the default home.

The chmod cases need a non-root uid (root ignores file modes) and are skipped as root.
"""

import os
import shutil
import stat
import subprocess
import json
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import doctor
from tests.helpers import rmtree_deep
from tests.unit.test_home_resolution_r3 import _CwdCase, _prod_env

_IS_ROOT = os.getuid() == 0


def _mode(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _write(path: str, mode: int, data: str = "x") -> None:
    with open(path, "w") as f:
        f.write(data)
    os.chmod(path, mode)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestOrderInstallDirAsHome(_CwdCase):
    def setUp(self) -> None:
        super().setUp()
        # A private copy of the install (bin/ + mesh/), so chmodding it is harmless.
        self.install = os.path.join(self.base, "install")
        os.mkdir(self.install)
        repo = self.h.repo_root
        shutil.copytree(os.path.join(repo, "bin"), os.path.join(self.install, "bin"))
        shutil.copytree(os.path.join(repo, "mesh"), os.path.join(self.install, "mesh"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.bin = os.path.join(self.install, "bin", "mesh")

    def _run(self, home: str, *args: str):
        env = self.h.get_env()
        env.pop("MESH_HOME", None)
        proc = subprocess.run([self.bin, "--home", home, *args], cwd=self.base, env=env,
                              text=True, capture_output=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout.strip())

    def test_install_dir_as_home_is_refused_before_any_chmod(self) -> None:
        for rel in ("install/mesh", "install"):
            target = os.path.join(self.base, rel)
            with self.subTest(home=rel):
                os.chmod(target, 0o500)
                try:
                    for args in (("doctor", "--fix-perms"), ("doctor",)):
                        rc, env = self._run(rel, *args)
                        self.assertEqual(rc, c.EXIT_USAGE, env)
                        self.assertEqual(env["error"]["code"], c.INSTALL_PATH_INSECURE, env)
                        self.assertEqual(_mode(target), 0o500, args)
                finally:
                    os.chmod(target, 0o755)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestFdBasedWalkRaces(unittest.TestCase):
    """In-process: doctor._fix_perms with the race hook swapping components."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="mesh-r5-")
        self.addCleanup(rmtree_deep, self.base)
        self.home = os.path.join(self.base, "home")
        os.makedirs(os.path.join(self.home, "runs", "r1"))
        os.chmod(self.home, 0o755)
        self.leaf = os.path.join(self.home, "runs", "r1", "f")
        _write(self.leaf, 0o644)
        # Outside the home: must never be touched.
        self.out_dir = os.path.join(self.base, "outside")
        os.mkdir(self.out_dir)
        self.out_file = os.path.join(self.out_dir, "f")
        _write(self.out_file, 0o400)
        os.chmod(self.out_dir, 0o755)
        self.addCleanup(lambda: os.path.isdir(self.out_dir) and os.chmod(self.out_dir, 0o755))
        self.addCleanup(setattr, doctor, "_RACE_HOOK", None)

    def _hook_once(self, stage: str, rel: str, action) -> None:
        fired = []

        def hook(s: str, r: str) -> None:
            if s == stage and r == rel and not fired:
                fired.append(1)
                action()
        doctor._RACE_HOOK = hook
        self.fired = fired

    def _assert_outside_untouched(self) -> None:
        self.assertEqual(_mode(self.out_file), 0o400)
        self.assertEqual(_mode(self.out_dir), 0o755)

    def test_leaf_swapped_for_symlink(self) -> None:
        moved = self.leaf + ".moved"

        def swap() -> None:
            os.rename(self.leaf, moved)
            os.symlink(self.out_file, self.leaf)
        self._hook_once("before-chmod", "runs/r1/f", swap)
        doctor._fix_perms(self.home, doctor._home_identity(self.home))
        self.assertTrue(self.fired)
        self._assert_outside_untouched()
        self.assertEqual(_mode(moved), 0o600)  # the inode that was checked got the chmod

    def test_ancestor_swapped_for_symlink(self) -> None:
        r1 = os.path.join(self.home, "runs", "r1")

        def swap() -> None:
            os.rename(r1, r1 + ".moved")
            os.symlink(self.out_dir, r1)
        self._hook_once("before-chmod", "runs/r1/f", swap)
        doctor._fix_perms(self.home, doctor._home_identity(self.home))
        self.assertTrue(self.fired)
        self._assert_outside_untouched()
        self.assertEqual(_mode(os.path.join(r1 + ".moved", "f")), 0o600)

    def test_dir_swapped_between_chmod_and_listing(self) -> None:
        r1 = os.path.join(self.home, "runs", "r1")

        def swap() -> None:
            os.rename(r1, r1 + ".moved")
            os.symlink(self.out_dir, r1)
        self._hook_once("before-open-dir", "runs/r1", swap)
        doctor._fix_perms(self.home, doctor._home_identity(self.home))
        self.assertTrue(self.fired)
        self._assert_outside_untouched()

    def test_known_dir_swapped_for_symlink(self) -> None:
        runs = os.path.join(self.home, "runs")

        def swap() -> None:
            os.rename(runs, runs + ".moved")
            os.symlink(self.out_dir, runs)
        self._hook_once("before-chmod", "runs", swap)
        doctor._fix_perms(self.home, doctor._home_identity(self.home))
        self.assertTrue(self.fired)
        self._assert_outside_untouched()

    def test_other_uid_reported(self) -> None:
        with mock.patch.object(doctor.os, "getuid", return_value=os.getuid() + 1):
            skipped = doctor._fix_perms(self.home, doctor._home_identity(self.home)).skipped
        self.assertEqual(skipped[0]["path"], ".")
        self.assertIn("owned by uid", skipped[0]["reason"])
        self.assertEqual(_mode(self.home), 0o755)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestWalkShapeAndModes(_CwdCase):
    def _init(self, name: str) -> str:
        home = os.path.join(self.base, name)
        rc, env = self.run_cli(home, "init", user=True)
        self.assertEqual(rc, 0, env)
        return home

    def test_deep_tree_is_walked_iteratively(self) -> None:
        home = self._init("deep")
        depth = 1500
        fd = os.open(os.path.join(home, "runs"), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(depth):
                os.mkdir("d", 0o755, dir_fd=fd)
                nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = nfd
            leaf = os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
            os.close(leaf)
            os.chmod("leaf", 0o644, dir_fd=fd)
            os.chmod(fd, 0o000)  # the deepest dir is unreadable
            rc, env = self.run_cli(home, "doctor", "--fix-perms", timeout=60)
            self.assertEqual(rc, 0, env)
            self.assertTrue(env["data"]["fixed"], env)
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat("leaf", dir_fd=fd, follow_symlinks=False).st_mode), 0o600)
        finally:
            os.chmod(fd, 0o700)
            os.close(fd)
            # Round 6: remove the 1500-level tree here, iteratively; shutil.rmtree in a
            # later cleanup would raise RecursionError on Python 3.11.
            rmtree_deep(os.path.join(home, "runs"))
            self.assertFalse(os.path.lexists(os.path.join(home, "runs")))

    def test_workspaces_keep_owner_bits_runs_get_0600(self) -> None:
        home = self._init("ws")
        ws = os.path.join(home, "workspaces", "p")
        os.makedirs(ws)
        _write(os.path.join(ws, "run.sh"), 0o755)
        _write(os.path.join(ws, "data"), 0o666)
        _write(os.path.join(ws, "locked"), 0o000)
        _write(os.path.join(ws, "ro"), 0o444)
        r = os.path.join(home, "runs", "r")
        os.makedirs(r)
        _write(os.path.join(r, "x.sh"), 0o755)
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertTrue(env["data"]["fixed"], env)
        self.assertEqual(_mode(os.path.join(ws, "run.sh")), 0o700)
        self.assertEqual(_mode(os.path.join(ws, "data")), 0o600)
        self.assertEqual(_mode(os.path.join(ws, "locked")), 0o600)
        self.assertEqual(_mode(os.path.join(ws, "ro")), 0o600)
        self.assertEqual(_mode(ws), 0o700)
        self.assertEqual(_mode(os.path.join(r, "x.sh")), 0o600)  # as on main

    def test_symlink_reported_fixed_false(self) -> None:
        home = self._init("symrep")
        outside = os.path.join(self.base, "o.txt")
        _write(outside, 0o644)
        os.symlink(outside, os.path.join(home, "runs", "link"))
        rc, env = self.run_cli(home, "doctor", "--fix-perms")
        self.assertEqual(rc, 0, env)
        self.assertFalse(env["data"]["fixed"], env)
        self.assertIn({"path": "runs/link", "reason": "symlink, not followed"}, env["data"]["skipped"])
        self.assertTrue(env["data"]["reason"])
        self.assertEqual(_mode(outside), 0o644)


class TestEmptyMeshHomeFallsBack(_CwdCase):
    def test_empty_mesh_home_uses_default(self) -> None:
        fake_home = os.path.join(self.base, "user")
        os.mkdir(fake_home)
        env = self.h.get_env()
        env.update(_prod_env())
        env["MESH_HOME"] = ""
        env["HOME"] = fake_home
        proc = subprocess.run([self.h.bin_mesh, "doctor"], cwd=self.base, env=env,
                              text=True, capture_output=True, timeout=15)
        out = json.loads(proc.stdout.strip())
        self.assertEqual(proc.returncode, 0, out)
        self.assertEqual(out["data"]["home"], os.path.join(fake_home, ".agent-mesh"))
        self.assertFalse(os.path.exists(os.path.join(self.base, "mesh.db")))

    def test_documented_in_help_env(self) -> None:
        from mesh.cli.help import HELP_TOPICS
        text = " ".join(HELP_TOPICS["env"].split())
        self.assertIn('An empty MESH_HOME (MESH_HOME="") counts as unset and uses the default', text)

if __name__ == "__main__":
    unittest.main()
