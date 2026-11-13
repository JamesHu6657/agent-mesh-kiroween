"""m3-test-kdf round 3: one home resolution everywhere, strict doctor.

- Every entry point (init, meshd, doctor, CLI --home / MESH_HOME) resolves the home
  once with mesh.core.paths.resolve_home: absolute against the cwd, symlinks
  followed before ".." (no lexical folding), and every SQLite connection (read and
  write) is opened through an escaped file: URI.
- Doctor tells "really absent" (FileNotFoundError) from "cannot access" (rc 1).
- Doctor is strict about mesh.db, the meta table and schema_version, and runs
  quick_check.

The CLI cases run the real `bin/mesh` in a subprocess with an explicit cwd.
"""

import json
import os
import shutil
import sqlite3
import struct
import subprocess
import time
import unittest
from typing import Dict, Optional, Tuple

import mesh.constants as c
from mesh.core.db import sqlite_uri
from mesh.core.paths import resolve_home
from tests.helpers import MeshHarness, rmtree_deep

RELATIVE_HOMES = ["file:rel", "file:q?mode=memory"]


def _prod_env() -> dict:
    return {"MESH_TESTING": ""}


class _CwdCase(unittest.TestCase):
    """A harness whose dir is used as the cwd; homes are given relative to it."""

    def setUp(self) -> None:
        self.h = MeshHarness(check_invariants_on_teardown=False)
        self.base = self.h.home
        self.addCleanup(rmtree_deep, self.base)  # round 6: depth-safe on Python 3.11
        self.addCleanup(self.h.teardown)

    def run_cli(self, home: str, *args: str, user: bool = False,
                extra_env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                timeout: float = 15.0) -> Tuple[int, dict]:
        env = self.h.get_env()
        env.pop("MESH_HOME", None)  # only --home chooses the home
        if extra_env:
            env.update(extra_env)
        cmd = [self.h.bin_mesh, "--home", home]
        fds: Tuple[int, ...] = ()
        fd = None
        try:
            if user:
                fd = os.open(self.h.passphrase_file, os.O_RDONLY)
                fds = (fd,)
                cmd += ["--user", "--passphrase-fd", str(fd)]
            cmd += list(args)
            proc = subprocess.run(cmd, cwd=cwd or self.base, env=env, text=True,
                                  capture_output=True, timeout=timeout, pass_fds=fds)
        finally:
            if fd is not None:
                os.close(fd)
        try:
            out = json.loads(proc.stdout.strip())
        except ValueError:
            out = {"ok": proc.returncode == 0, "raw_stdout": proc.stdout, "raw_stderr": proc.stderr}
        return proc.returncode, out

    def assert_kdf_too_weak(self, home: str, cwd: Optional[str] = None) -> None:
        for args in (("doctor",), ("doctor", "--check-invariants")):
            with self.subTest(home=home, args=args):
                rc, env = self.run_cli(home, *args, extra_env=_prod_env(), cwd=cwd)
                self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, env)
                self.assertEqual(env["error"]["code"], c.KDF_TOO_WEAK, env)


class TestRelativeHomes(_CwdCase):
    def test_init_lands_inside_the_real_dir_and_doctor_reads_it(self) -> None:
        for name in RELATIVE_HOMES:
            with self.subTest(name=name):
                before = set(os.listdir(self.base))
                rc, env = self.run_cli(name, "init", user=True)
                self.assertEqual(rc, 0, env)
                real = os.path.join(self.base, name)
                self.assertEqual(env["data"]["home"], real)
                self.assertTrue(os.path.isfile(os.path.join(real, "mesh.db")))
                # Nothing created outside the home (no ./rel, ./q, ...).
                self.assertEqual(set(os.listdir(self.base)) - before, {name})
                rc, env = self.run_cli(name, "doctor")
                self.assertEqual(rc, 0, env)
                self.assertEqual(env["data"]["status"], "healthy")
                self.assertEqual(env["data"]["home"], real)
                # Production doctor reads that same (testing, weak) DB: never healthy.
                self.assert_kdf_too_weak(name)

    def test_daemon_reads_the_same_db(self) -> None:
        name = RELATIVE_HOMES[1]
        real = os.path.join(self.base, name)
        rc, env = self.run_cli(name, "init", user=True)
        self.assertEqual(rc, 0, env)
        env_d = self.h.get_env()
        env_d.pop("MESH_HOME", None)
        proc = subprocess.Popen([self.h.bin_mesh, "--home", name, "daemon", "run"], cwd=self.base,
                                env=env_d, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        # Teardown stops it through the absolute path of the same home.
        self.h.home = real
        self.h.daemon_proc = proc
        sock = os.path.join(real, "run", "mesh.sock")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not os.path.exists(sock):
            self.assertIsNone(proc.poll(), "meshd exited early")
            time.sleep(0.05)
        self.assertTrue(os.path.exists(sock))
        rc, env = (None, None)
        while time.monotonic() < deadline:
            rc, env = self.run_cli(name, "whoami", user=True)
            if rc == 0:
                break
            time.sleep(0.1)
        self.assertEqual(rc, 0, env)
        # The user the daemon unlocked is the one init wrote into ./<name>/mesh.db.
        conn = sqlite3.connect(sqlite_uri(os.path.join(real, "mesh.db"), "ro"), uri=True)
        try:
            uid = conn.execute("SELECT id FROM principals WHERE kind = 'user';").fetchone()[0]
        finally:
            conn.close()
        self.assertIn(uid, json.dumps(env))

    def test_write_connect_opens_the_exact_relative_file(self) -> None:
        """db.connect / init_db (write path) on a relative "file:..." name: the file
        is created at that exact path under the cwd, never parsed as a URI."""
        from mesh.core.db import connect, init_db
        old = os.getcwd()
        os.chdir(self.base)
        try:
            for i, name in enumerate(RELATIVE_HOMES):
                with self.subTest(name=name):
                    os.makedirs(name, mode=0o700)
                    before = set(os.listdir(self.base))
                    rel = os.path.join(name, "w.db")
                    conn = connect(rel, read_only=False)
                    try:
                        conn.execute("CREATE TABLE t (v TEXT);")
                        conn.execute("INSERT INTO t VALUES ('x');")
                    finally:
                        conn.close()
                    self.assertTrue(os.path.isfile(os.path.join(self.base, name, "w.db")))
                    conn = init_db(os.path.join(name, "i.db"))
                    conn.close()
                    self.assertTrue(os.path.isfile(os.path.join(self.base, name, "i.db")))
                    self.assertEqual(set(os.listdir(self.base)), before)
        finally:
            os.chdir(old)

    def test_uri_for_relative_hostile_names(self) -> None:
        old = os.getcwd()
        os.chdir(self.base)
        try:
            for name in RELATIVE_HOMES:
                uri = sqlite_uri(os.path.join(name, "mesh.db"), "rwc")
                path_part, _, query = uri.partition("?")
                self.assertEqual(query, "mode=rwc")
                self.assertTrue(path_part.startswith("file:///"))
                self.assertIn(self.base.lstrip("/"), path_part)
                self.assertNotIn("?", path_part)
        finally:
            os.chdir(old)


class TestSymlinkDotDot(_CwdCase):
    """a/link -> b/child: `a/link/..` is b (what the kernel opens), never a."""

    def setUp(self) -> None:
        super().setUp()
        self.a = os.path.join(self.base, "a")
        self.b = os.path.join(self.base, "b")
        os.makedirs(os.path.join(self.b, "child"), mode=0o700)
        rc, env = self.run_cli("b", "init", user=True)  # testing mode: weak KDF
        self.assertEqual(rc, 0, env)
        rc, env = self.run_cli("a", "init", user=True, extra_env=_prod_env())  # strong
        self.assertEqual(rc, 0, env)
        os.symlink(os.path.join("..", "b", "child"), os.path.join(self.a, "link"))

    def test_resolve_home_follows_symlink_before_dotdot(self) -> None:
        self.assertEqual(resolve_home(os.path.join(self.a, "link", "..")), self.b)
        old = os.getcwd()
        os.chdir(self.base)
        try:
            self.assertEqual(resolve_home("a/link/.."), self.b)
        finally:
            os.chdir(old)
        from mesh.daemon.server import DaemonServer
        self.assertEqual(DaemonServer(os.path.join(self.a, "link", ".."), testing_mode=False).home, self.b)

    def test_doctor_reads_link_target_parent(self) -> None:
        for home in ("a/link/..", os.path.join(self.a, "link", "..")):
            with self.subTest(home=home):
                rc, env = self.run_cli(home, "doctor")
                self.assertEqual(rc, 0, env)
                self.assertEqual(env["data"]["home"], self.b)
                self.assert_kdf_too_weak(home)
        # The strong home itself is healthy in production mode (sanity).
        rc, env = self.run_cli("a", "doctor", extra_env=_prod_env())
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["home"], self.a)


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file modes")
class TestUnreadableHome(_CwdCase):
    def test_chmod_000_home_is_not_healthy(self) -> None:
        home = os.path.join(self.base, "h")
        rc, env = self.run_cli(home, "init", user=True, extra_env=_prod_env())
        self.assertEqual(rc, 0, env)
        os.chmod(home, 0o000)
        self.addCleanup(os.chmod, home, 0o700)
        for extra in ({}, _prod_env()):
            for args in (("doctor",), ("doctor", "--check-invariants")):
                with self.subTest(args=args, prod=bool(extra)):
                    rc, env = self.run_cli(home, *args, extra_env=extra)
                    self.assertNotEqual(rc, 0, env)
                    self.assertFalse(env.get("ok"), env)
                    self.assertNotIn("status", env.get("data") or {})
                    self.assertEqual(env["error"]["code"], c.DB_INTEGRITY_FAILED, env)
                    self.assertIn("PermissionError", env["error"]["details"]["reason"])

    def test_really_absent_home_is_still_uninitialised(self) -> None:
        rc, env = self.run_cli(os.path.join(self.base, "nope"), "doctor")
        self.assertEqual(rc, 0, env)
        # An uninitialised home is reported as such, never as healthy.
        self.assertEqual(env["data"]["status"], "not_initialized")
        rc, env = self.run_cli(os.path.join(self.base, "nope"), "doctor", "--check-invariants")
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["violations"], [])


class TestDoctorStrict(_CwdCase):
    def _home(self, name: str) -> str:
        home = os.path.join(self.base, name)
        rc, env = self.run_cli(home, "init", user=True)
        self.assertEqual(rc, 0, env)
        return home

    def _sql(self, home: str, *stmts: str) -> None:
        conn = sqlite3.connect(sqlite_uri(os.path.join(home, "mesh.db"), "rw"), uri=True)
        try:
            for s in stmts:
                conn.execute(s)
            conn.commit()
        finally:
            conn.close()

    def _assert_failed(self, home: str, reason_part: str) -> None:
        for extra in ({}, _prod_env()):
            for args in (("doctor",), ("doctor", "--check-invariants")):
                with self.subTest(home=os.path.basename(home), args=args, prod=bool(extra)):
                    rc, env = self.run_cli(home, *args, extra_env=extra)
                    self.assertEqual(rc, c.EXIT_INTERNAL, env)
                    self.assertEqual(env["error"]["code"], c.DB_INTEGRITY_FAILED, env)
                    self.assertIn(reason_part, env["error"]["details"]["reason"])

    def test_missing_db_in_initialised_home(self) -> None:
        home = self._home("nodb")
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(home, "mesh.db" + suffix)
            if os.path.exists(p):
                os.remove(p)
        self._assert_failed(home, "mesh.db missing")

    def test_missing_meta_table(self) -> None:
        home = self._home("nometa")
        self._sql(home, "DROP TABLE meta;")
        self._assert_failed(home, "meta table missing")

    def test_missing_version_row(self) -> None:
        home = self._home("norow")
        self._sql(home, "DELETE FROM meta WHERE key = 'schema_version';")
        self._assert_failed(home, "schema_version missing")

    def test_non_integer_version(self) -> None:
        for i, bad in enumerate(("abc", "1.5", "", " 1")):
            with self.subTest(value=bad):
                home = self._home(f"badver{i}")
                self._sql(home, "UPDATE meta SET value = '%s' WHERE key = 'schema_version';" % bad)
                self._assert_failed(home, "schema_version is not an integer")

    def test_quick_check_failure(self) -> None:
        """A wrong freelist page count in the file header: the DB stays fully
        readable (every other doctor query succeeds), only PRAGMA quick_check
        reports it. A doctor that skipped quick_check would say healthy here.
        (A CHECK-constraint violation is not used: quick_check on a mode=ro
        connection does not verify CHECK constraints.)"""
        home = self._home("qc")
        db = os.path.join(home, "mesh.db")
        conn = sqlite3.connect(sqlite_uri(db, "rw"), uri=True)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
        with open(db, "r+b") as f:
            f.seek(32)
            self.assertEqual(f.read(8), b"\0" * 8)  # no freelist: trunk 0, count 0
            f.seek(36)
            f.write(struct.pack(">I", 7))  # header claims 7 free pages
        conn = sqlite3.connect(sqlite_uri(db, "ro"), uri=True)
        try:
            qc = conn.execute("PRAGMA quick_check;").fetchall()
            self.assertNotEqual(qc, [("ok",)])
            # SQLite 3.40 says "Main freelist: ...", newer ones "Freelist: ...":
            # match the word only, case-insensitively (round 4).
            self.assertIn("freelist", qc[0][0].lower())
            self.assertIsNotNone(conn.execute(
                "SELECT c.kdf_params FROM credentials c JOIN principals p ON c.principal_id = p.id "
                "WHERE p.kind = 'user' AND c.kind = 'passphrase';").fetchone())
            self.assertEqual(conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version';").fetchone()[0].isdigit(), True)
        finally:
            conn.close()
        rc, env = self.run_cli(home, "doctor")
        self.assertEqual(rc, c.EXIT_INTERNAL, env)
        self.assertEqual(env["error"]["code"], c.DB_INTEGRITY_FAILED, env)
        self.assertIn("quick_check failed", env["error"]["details"]["reason"])
        rc, env = self.run_cli(home, "doctor", "--check-invariants")
        self.assertEqual(rc, c.EXIT_INTERNAL, env)
        self.assertIn("quick_check failed", env["error"]["details"]["reason"])


if __name__ == "__main__":
    unittest.main()
