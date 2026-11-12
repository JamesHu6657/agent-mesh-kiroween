"""m3-test-kdf round 6: doctor --fix-perms binding gaps (Linear/Quest REVISE on dfdabd0).

1. Deep trees left by tests are removed iteratively (tests.helpers.rmtree_deep), so the
   suite is green on Python 3.11, whose shutil.rmtree recurses once per level.
2. The home is opened from "/" one component at a time with O_NOFOLLOW and must be the
   inode the precheck recorded: a symlinked ANCESTOR swapped in after the precheck
   never gets an outside directory chmodded.
3. Hardlinked regular files (st_nlink > 1) are skipped with reason "hardlinked".
4. After the walk every repaired path is re-verified (same inode, target mode); a dir
   swapped mid-walk gives fixed:false and doctor fails (rc 2 INSECURE_PERMISSIONS).
5. No by-name chmod fallback: without an fd-bound chmod the entry is skipped with
   reason "no fd-bound chmod".
6. No fd leaks on error paths (every fstat / open / listdir call failing in turn).
7. The per-entry uid check is exercised by faking fstat's st_uid; an install root
   nested anywhere in the home is refused.

The chmod cases need a non-root uid (root ignores file modes) and are skipped as root.
"""

import argparse
import os
import stat
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.cli.commands import doctor
from mesh.core.paths import check_install_path_secure
from mesh.errors import MeshError
from tests.helpers import rmtree_deep
from tests.unit.test_home_resolution_r3 import _CwdCase

_IS_ROOT = os.getuid() == 0


def _mode(path: str) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _write(path: str, mode: int, data: str = "x") -> None:
    with open(path, "w") as f:
        f.write(data)
    os.chmod(path, mode)


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="mesh-r6-"))
        self.addCleanup(rmtree_deep, self.base)
        self.addCleanup(setattr, doctor, "_RACE_HOOK", None)
        self.home = os.path.join(self.base, "a", "home")
        os.makedirs(os.path.join(self.home, "runs", "r1"))
        os.chmod(self.home, 0o755)
        os.chmod(os.path.join(self.home, "runs"), 0o755)
        os.chmod(os.path.join(self.home, "runs", "r1"), 0o755)
        self.leaf = os.path.join(self.home, "runs", "r1", "f")
        _write(self.leaf, 0o644)

    def hook_once(self, stage: str, rel: str, action) -> list:
        fired: list = []

        def hook(s: str, r: str) -> None:
            if s == stage and r == rel and not fired:
                fired.append(1)
                action()
        doctor._RACE_HOOK = hook
        return fired

    def fix(self):
        return doctor._fix_perms(self.home, doctor._home_identity(self.home))


class TestRmtreeDeep(unittest.TestCase):
    def test_removes_deep_unreadable_tree_iteratively(self) -> None:
        base = tempfile.mkdtemp(prefix="mesh-r6-deep-")
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for _ in range(1500):
                os.mkdir("d", 0o755, dir_fd=fd)
                nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = nfd
            os.close(os.open("leaf", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
            os.symlink("/", "link", dir_fd=fd)  # never followed
            os.chmod(fd, 0o000)
        finally:
            os.close(fd)
        before = _fd_count()
        rmtree_deep(base)
        self.assertFalse(os.path.lexists(base))
        self.assertEqual(_fd_count(), before)
        self.assertTrue(os.path.isdir("/"))


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestHomeBoundToPrecheck(_Base):
    def _outside_twin(self) -> str:
        """A real directory outside, shaped like base/a, with a 0755 'home'."""
        out = os.path.join(self.base, "outside_a")
        os.makedirs(os.path.join(out, "home", "runs"))
        _write(os.path.join(out, "home", "runs", "f"), 0o400)
        for p in (out, os.path.join(out, "home"), os.path.join(out, "home", "runs")):
            os.chmod(p, 0o755)
        return out

    def test_ancestor_swapped_between_precheck_and_open(self) -> None:
        out = self._outside_twin()
        a = os.path.join(self.base, "a")

        def swap() -> None:
            os.rename(a, a + ".moved")
            os.symlink(out, a)
        fired = self.hook_once("before-open-home", ".", swap)
        report = self.fix()
        self.assertTrue(fired)
        self.assertEqual(_mode(os.path.join(out, "home")), 0o755)
        self.assertEqual(_mode(os.path.join(out, "home", "runs")), 0o755)
        self.assertEqual(_mode(os.path.join(out, "home", "runs", "f")), 0o400)
        self.assertEqual(report.skipped[0]["path"], ".")
        self.assertEqual(report.repaired, {})

    def test_home_replaced_by_other_dir_after_precheck(self) -> None:
        # Not a symlink: a different real directory now at the same path.
        out = self._outside_twin()

        def swap() -> None:
            os.rename(self.home, self.home + ".moved")
            os.rename(os.path.join(out, "home"), self.home)
        self.hook_once("before-open-home", ".", swap)
        report = self.fix()
        self.assertEqual(_mode(self.home), 0o755)
        self.assertEqual(_mode(os.path.join(self.home, "runs", "f")), 0o400)
        self.assertIn("precheck", report.skipped[0]["reason"])

    def test_handle_doctor_passes_the_precheck_identity(self) -> None:
        seen = {}
        real = doctor._fix_perms

        def spy(home, expected=None):
            seen["args"] = (home, expected)
            return real(home, expected)
        args = argparse.Namespace(home=self.home, fix_perms=True, check_invariants=False)
        with mock.patch.object(doctor, "_fix_perms", spy):
            with self.assertRaises(MeshError):  # runs/ exists but no mesh.db (rc 1)
                doctor.handle_doctor(args)
        st = os.stat(self.home)
        self.assertEqual(seen["args"], (self.home, (st.st_dev, st.st_ino)))

    def test_no_expected_identity_means_no_repair(self) -> None:
        report = doctor._fix_perms(self.home, None)
        self.assertEqual(_mode(self.home), 0o755)
        self.assertEqual(report.skipped[0]["path"], ".")


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestHardlinks(_Base):
    def test_hardlinked_file_is_skipped(self) -> None:
        out = os.path.join(self.base, "outside.txt")
        for mode in (0o400, 0o444):
            with self.subTest(mode=oct(mode)):
                _write(out, mode)
                hl = os.path.join(self.home, "runs", "r1", "hl")
                os.link(out, hl)
                try:
                    report = self.fix()
                    self.assertEqual(_mode(out), mode)
                    self.assertIn({"path": "runs/r1/hl", "reason": "hardlinked"}, report.skipped)
                    self.assertEqual(_mode(self.leaf), 0o600)  # the rest is still repaired
                finally:
                    os.unlink(hl)
                    os.chmod(out, 0o600)
                    os.unlink(out)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestReverifyAfterWalk(_Base):
    def test_dir_swapped_mid_walk_is_unsafe(self) -> None:
        runs = os.path.join(self.home, "runs")

        def swap() -> None:
            os.rename(runs, runs + ".old")
            os.mkdir(runs)
            os.chmod(runs, 0o777)
        fired = self.hook_once("before-chmod", "runs/r1/f", swap)
        report = self.fix()
        self.assertTrue(fired)
        self.assertEqual(_mode(runs), 0o777)
        self.assertIn("runs", [u["path"] for u in report.unsafe])
        # Children of the swapped dir are covered by the parent entry, not repeated.
        self.assertNotIn("runs/r1", [u["path"] for u in report.unsafe])

    def test_mode_changed_after_repair_is_unsafe(self) -> None:
        def loosen() -> None:
            os.chmod(self.leaf, 0o666)
        self.hook_once("before-verify", ".", loosen)
        report = self.fix()
        self.assertEqual([u["path"] for u in report.unsafe], ["runs/r1/f"])
        self.assertIn("mode 0666", report.unsafe[0]["reason"])

    def test_clean_run_has_nothing_unsafe(self) -> None:
        report = self.fix()
        self.assertEqual(report.unsafe, [])
        self.assertEqual(report.skipped, [])
        self.assertEqual(set(report.repaired), {".", "runs", "runs/r1", "runs/r1/f"})


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestReverifyCli(_CwdCase):
    def test_swap_mid_walk_makes_doctor_fail(self) -> None:
        # Out of process the hook is not reachable, so drive handle_doctor in process.
        home = os.path.join(self.base, "cli")
        rc, env = self.run_cli(home, "init", user=True)
        self.assertEqual(rc, 0, env)
        runs = os.path.join(home, "runs")
        os.makedirs(os.path.join(runs, "r"))
        _write(os.path.join(runs, "r", "f"), 0o644)
        fired: list = []

        def hook(s: str, r: str) -> None:
            if s == "before-chmod" and r == "runs/r/f" and not fired:
                fired.append(1)
                os.rename(runs, runs + ".old")
                os.mkdir(runs)
                os.chmod(runs, 0o777)
        doctor._RACE_HOOK = hook
        self.addCleanup(setattr, doctor, "_RACE_HOOK", None)
        args = argparse.Namespace(home=home, fix_perms=True, check_invariants=False)
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            with self.assertRaises(MeshError) as cm:
                doctor.handle_doctor(args)
        self.assertTrue(fired)
        self.assertEqual(cm.exception.code, c.INSECURE_PERMISSIONS)
        self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
        self.assertFalse(cm.exception.details["fixed"])
        self.assertIn("runs", [u["path"] for u in cm.exception.details["unsafe"]])


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestNoNameFallback(_Base):
    def test_without_proc_chmod_entries_are_skipped(self) -> None:
        real_chmod = doctor.os.chmod
        calls = []

        def chmod(path, mode, *a, **kw):
            calls.append((path, kw))
            if isinstance(path, str) and path.startswith("/proc/self/fd/"):
                raise OSError(2, "no /proc")
            return real_chmod(path, mode, *a, **kw)

        def fchmod(fd, mode):
            raise OSError(9, "EBADF on O_PATH")
        with mock.patch.object(doctor.os, "chmod", chmod), mock.patch.object(doctor.os, "fchmod", fchmod):
            report = self.fix()
        self.assertEqual(report.skipped, [{"path": ".", "reason": "no fd-bound chmod"}])
        self.assertTrue(all(p.startswith("/proc/self/fd/") for p, _ in calls), calls)
        self.assertTrue(all("dir_fd" not in kw for _, kw in calls), calls)
        self.assertEqual(_mode(self.home), 0o755)
        self.assertEqual(_mode(self.leaf), 0o644)

    def test_entry_without_fd_chmod_is_skipped(self) -> None:
        real_chmod = doctor.os.chmod
        leaf_ino = os.lstat(self.leaf).st_ino

        def chmod(path, mode, *a, **kw):
            if "dir_fd" in kw:
                raise AssertionError("by-name chmod fallback used")
            if isinstance(path, str) and path.startswith("/proc/self/fd/") and os.stat(path).st_ino == leaf_ino:
                raise OSError(2, "no /proc")
            return real_chmod(path, mode, *a, **kw)
        with mock.patch.object(doctor.os, "chmod", chmod):
            report = self.fix()
        self.assertIn({"path": "runs/r1/f", "reason": "no fd-bound chmod"}, report.skipped)
        self.assertEqual(_mode(self.leaf), 0o644)


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestNoFdLeaks(_Base):
    """Make the Nth call of fstat / open / listdir raise, for every N, and check the
    fd count is unchanged after each run (whatever the run returned or raised)."""

    def setUp(self) -> None:
        super().setUp()
        os.makedirs(os.path.join(self.home, "runs", "r2", "deep"))
        _write(os.path.join(self.home, "runs", "r2", "deep", "g"), 0o644)
        _write(os.path.join(self.home, "mesh.db"), 0o644)

    def _count_calls(self, fname: str) -> int:
        real = getattr(doctor.os, fname)
        n = [0]

        def counting(*a, **kw):
            n[0] += 1
            return real(*a, **kw)
        with mock.patch.object(doctor.os, fname, counting):
            self.fix()
        return n[0]

    def test_every_failure_point_closes_its_fds(self) -> None:
        for fname in ("fstat", "open", "listdir"):
            total = self._count_calls(fname)
            self.assertGreater(total, 3, fname)
            for k in range(1, total + 1):
                real = getattr(doctor.os, fname)
                n = [0]

                def failing(*a, _k=k, _real=real, **kw):
                    n[0] += 1
                    if n[0] == _k:
                        raise OSError(5, f"injected {fname} #{_k}")
                    return _real(*a, **kw)
                before = _fd_count()
                with mock.patch.object(doctor.os, fname, failing):
                    try:
                        self.fix()
                    except OSError:
                        pass
                self.assertEqual(_fd_count(), before, f"{fname} failing at call {k}")


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestPerEntryUidCheck(_Base):
    def test_entry_owned_by_other_uid_is_left_alone(self) -> None:
        real_fstat = doctor.os.fstat
        leaf_ino = os.lstat(self.leaf).st_ino
        other = os.getuid() + 1

        def fstat(fd):
            st = real_fstat(fd)
            if st.st_ino == leaf_ino:
                fields = list(st)
                fields[stat.ST_UID] = other
                return os.stat_result(fields)
            return st
        with mock.patch.object(doctor.os, "fstat", fstat):
            report = self.fix()
        self.assertEqual(_mode(self.leaf), 0o644)
        self.assertIn({"path": "runs/r1/f", "reason": f"owned by uid {other}, not {os.getuid()}"},
                      report.skipped)
        self.assertEqual(_mode(os.path.join(self.home, "runs", "r1")), 0o700)  # siblings repaired


class TestInstallRootInsideHome(unittest.TestCase):
    def test_install_root_nested_in_home_is_refused(self) -> None:
        base = os.path.realpath(tempfile.mkdtemp(prefix="mesh-r6-inst-"))
        self.addCleanup(rmtree_deep, base)
        home = os.path.join(base, "home")
        for rel in ("logs/install", "install", "runs/x/install", "workspaces/p/install"):
            inst = os.path.join(home, rel)
            os.makedirs(inst, exist_ok=True)
            with self.subTest(install=rel):
                with self.assertRaises(MeshError) as cm:
                    check_install_path_secure(install_root=inst, home=home)
                self.assertEqual(cm.exception.code, c.INSTALL_PATH_INSECURE)
                self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)

    def test_home_inside_install_still_refused_and_disjoint_ok(self) -> None:
        base = os.path.realpath(tempfile.mkdtemp(prefix="mesh-r6-inst-"))
        self.addCleanup(rmtree_deep, base)
        inst = os.path.join(base, "inst")
        os.makedirs(os.path.join(inst, "home"))
        with self.assertRaises(MeshError):
            check_install_path_secure(install_root=inst, home=os.path.join(inst, "home"))
        os.makedirs(os.path.join(base, "home2"))
        check_install_path_secure(install_root=inst, home=os.path.join(base, "home2"))


@unittest.skipIf(_IS_ROOT, "root ignores file modes; needs a non-root uid")
class TestInstallRootInHomeLogsCli(_CwdCase):
    def test_doctor_refuses_install_in_home_logs(self) -> None:
        import shutil
        import subprocess
        import json
        home = os.path.join(self.base, "h")
        rc, env = self.run_cli(home, "init", user=True)
        self.assertEqual(rc, 0, env)
        inst = os.path.join(home, "logs", "install")
        os.makedirs(inst)
        repo = self.h.repo_root
        shutil.copytree(os.path.join(repo, "bin"), os.path.join(inst, "bin"))
        shutil.copytree(os.path.join(repo, "mesh"), os.path.join(inst, "mesh"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        os.chmod(home, 0o755)
        e = self.h.get_env()
        e.pop("MESH_HOME", None)
        for args in (("doctor", "--fix-perms"), ("doctor",)):
            proc = subprocess.run([os.path.join(inst, "bin", "mesh"), "--home", home, *args], cwd=self.base,
                                  env=e, text=True, capture_output=True, timeout=30)
            out = json.loads(proc.stdout.strip())
            self.assertEqual(proc.returncode, c.EXIT_USAGE, out)
            self.assertEqual(out["error"]["code"], c.INSTALL_PATH_INSECURE, out)
            self.assertEqual(_mode(home), 0o755, args)  # refused before any chmod


if __name__ == "__main__":
    unittest.main()
