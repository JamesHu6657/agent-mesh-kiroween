"""m3-test-kdf round 2: doctor fails closed, SQLite URI escaping, one default-home
guard for every entry point, and an independent KDF cost oracle.

The doctor cases run the real CLI (`bin/mesh doctor` in a subprocess), not only
the helper, so the envelope and exit code users see are what is checked.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import paths as mesh_paths
from mesh.core.auth import derive_user_keys
from mesh.core.db import connect, sqlite_ro_uri
from mesh.core.init import init_mesh_home
from mesh.errors import MeshError
from tests.helpers import MeshHarness

PASS = "test-passphrase-at-least-16-chars-long"

# Home names a URI would break on: "?" starts the query, "#" the fragment, "%" an
# escape (%41 would decode to "A"), plus spaces.
HOSTILE_NAMES = ["h?mode=rw", "h#frag", "h%41b", "h with space", "h ?#%3F mixed"]

# Independent cost oracle: known-answer vectors computed once with hashlib.scrypt
# and frozen here as literals (not derived from mesh constants), so a change that
# silently derives with a different n / r / p / dklen fails even if metadata lies.
KV_PASS = b"known-vector-passphrase-m3"
KV_SALT = bytes(range(16))
KV_PROD_N, KV_TEST_N, KV_R, KV_P, KV_DKLEN = 32768, 1024, 8, 1, 64
KV_PROD = bytes.fromhex(
    "3c4dd3fe4de0f3a9d805d4b2f84f2b23254371b1b202696d83844afb8308ff30"
    "407ac37dc0aad0c0864ea75c9aa1be4088236b1687336750e40b4c4390d7fd19")
KV_TEST = bytes.fromhex(
    "3ad21369818718e3da39687477e20417e0012adb3f35c9be21b602749ed3c6fa"
    "2db829df95c36be28455da9158e9623402aecc666efa548923e0bf3064b17585")


def _prod_env() -> dict:
    # harness.get_env sets MESH_TESTING=1; "" means "not testing" (only "1" is).
    return {"MESH_TESTING": ""}


class TestSqliteUriEscaping(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-uri-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_only_connect_opens_the_exact_file(self) -> None:
        for name in HOSTILE_NAMES:
            with self.subTest(name=name):
                d = os.path.join(self.tmp, name)
                os.makedirs(d)
                real = os.path.join(d, "mesh.db")
                conn = sqlite3.connect(real)
                conn.execute("CREATE TABLE t (v TEXT);")
                conn.execute("INSERT INTO t VALUES ('real');")
                conn.commit()
                conn.close()
                # Decoy at the path an unescaped URI would be cut to.
                cut = os.path.join(self.tmp, name.split("?")[0].split("#")[0])
                if cut != d and not os.path.exists(cut):
                    dc = sqlite3.connect(cut)
                    dc.execute("CREATE TABLE t (v TEXT);")
                    dc.execute("INSERT INTO t VALUES ('decoy');")
                    dc.commit()
                    dc.close()
                ro = connect(real, read_only=True)
                try:
                    self.assertEqual(ro.execute("SELECT v FROM t;").fetchone()[0], "real")
                    with self.assertRaises(sqlite3.OperationalError):
                        ro.execute("INSERT INTO t VALUES ('x');")  # really read-only
                finally:
                    ro.close()

    def test_uri_has_no_raw_reserved_chars_in_path(self) -> None:
        uri = sqlite_ro_uri("/tmp/a?b#c%d e/mesh.db")
        path_part, _, query = uri.partition("?")
        self.assertEqual(query, "mode=ro")
        self.assertNotIn("#", path_part)
        self.assertIn("%3F", path_part)
        self.assertIn("%23", path_part)
        self.assertIn("%25", path_part)
        self.assertIn("%20", path_part)


class _HostileHomeCase(unittest.TestCase):
    """A harness whose MESH_HOME is a hostile name inside the harness's own dir."""

    def make_harness(self, name: str) -> MeshHarness:
        h = MeshHarness()
        base = h.home
        self.addCleanup(shutil.rmtree, base, True)
        self.addCleanup(h.teardown)
        h.home = os.path.join(base, name)
        os.makedirs(h.home, mode=0o700)
        os.chmod(h.home, 0o700)
        return h


class TestHostileHomeCli(_HostileHomeCase):
    def test_init_daemon_doctor_on_hostile_homes(self) -> None:
        """Linear repro: testing home with "?" etc. -> production doctor rc 3, never healthy."""
        for i, name in enumerate(HOSTILE_NAMES):
            with self.subTest(name=name):
                h = self.make_harness(name)
                rc, env = h.init()
                self.assertEqual(rc, 0, env)
                self.assertTrue(os.path.exists(os.path.join(h.home, "mesh.db")))
                if i in (0, len(HOSTILE_NAMES) - 1):  # daemon on the "?" and the mixed home
                    h.start_daemon()
                    rc, env = h.cli("whoami", user=True)
                    self.assertEqual(rc, 0, env)
                    h.stop_daemon()
                rc, env = h.cli("doctor")
                self.assertEqual(rc, 0, env)
                self.assertEqual(env["data"]["status"], "healthy")
                self.assertEqual(os.path.realpath(env["data"]["home"]), os.path.realpath(h.home))
                for args in (("doctor",), ("doctor", "--check-invariants")):
                    rc, env = h.cli(*args, extra_env=_prod_env())
                    self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, env)
                    self.assertEqual(env["error"]["code"], c.KDF_TOO_WEAK)

    def test_production_daemon_refuses_hostile_testing_home(self) -> None:
        from mesh.core.db import connect_db
        from mesh.daemon.server import DaemonServer

        h = self.make_harness(HOSTILE_NAMES[-1])
        self.assertEqual(h.init()[0], 0)
        srv = DaemonServer(h.home, testing_mode=False)
        srv.db_conn = connect_db(os.path.join(h.home, "mesh.db"))
        try:
            with mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None):
                with self.assertRaises(MeshError) as cm:
                    asyncio.run(srv.authenticate_and_authorize(
                        peer_pid=os.getpid(), auth_info={"type": "user", "passphrase": PASS},
                        cmd="whoami", request_id="req-uri"))
            self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
        finally:
            srv.db_conn.close()

    def test_production_home_with_hostile_name_is_healthy(self) -> None:
        h = self.make_harness(HOSTILE_NAMES[-1])
        rc, env = h.cli("init", user=True, extra_env=_prod_env())
        self.assertEqual(rc, 0, env)
        rc, env = h.cli("doctor", extra_env=_prod_env())
        self.assertEqual(rc, 0, env)
        self.assertEqual(env["data"]["status"], "healthy")


class TestDoctorCliFailClosed(_HostileHomeCase):
    """Doctor is never healthy on a DB it cannot fully read (both modes, real CLI)."""

    def _broken_home(self, name: str, breaker) -> MeshHarness:
        h = self.make_harness(name)
        self.assertEqual(h.init()[0], 0)
        breaker(os.path.join(h.home, "mesh.db"))
        h.check_invariants_on_teardown = False
        return h

    def _assert_not_healthy(self, h: MeshHarness, code: str = c.DB_INTEGRITY_FAILED) -> None:
        for extra in ({}, _prod_env()):
            for args in (("doctor",), ("doctor", "--check-invariants")):
                with self.subTest(args=args, prod=bool(extra)):
                    rc, env = h.cli(*args, extra_env=extra)
                    self.assertNotEqual(rc, 0, env)
                    self.assertFalse(env.get("ok"), env)
                    self.assertNotIn("status", env.get("data") or {})
                    self.assertEqual(env["error"]["code"], code, env)
                    self.assertEqual(rc, c.EXIT_INTERNAL)

    @staticmethod
    def _sql(db: str, *stmts: str) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF;")
            for s in stmts:
                conn.execute(s)
            conn.commit()
        finally:
            conn.close()

    def test_missing_credential_row(self) -> None:
        h = self._broken_home("h?cred", lambda db: self._sql(
            db, "DELETE FROM credentials WHERE kind = 'passphrase';"))
        self._assert_not_healthy(h)

    def test_renamed_params_column(self) -> None:
        h = self._broken_home("h#col", lambda db: self._sql(
            db, "ALTER TABLE credentials RENAME COLUMN kdf_params TO kdf_params_old;"))
        self._assert_not_healthy(h)

    def test_missing_params_column(self) -> None:
        def drop(db: str) -> None:
            conn = sqlite3.connect(db)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(credentials);")]
                keep = [col for col in cols if col != "kdf_params"]
                conn.execute("PRAGMA foreign_keys=OFF;")
                conn.execute("CREATE TABLE credentials_new AS SELECT %s FROM credentials;" % ", ".join(keep))
                conn.execute("DROP TABLE credentials;")
                conn.execute("ALTER TABLE credentials_new RENAME TO credentials;")
                conn.commit()
            finally:
                conn.close()
        h = self._broken_home("h%col", drop)
        self._assert_not_healthy(h)

    def test_missing_settings_or_projects_table(self) -> None:
        for name, table in (("h?set", "settings"), ("h?prj", "projects")):
            with self.subTest(table=table):
                h = self._broken_home(name, lambda db, t=table: self._sql(db, f"DROP TABLE {t};"))
                self._assert_not_healthy(h)

    def test_garbage_db_file(self) -> None:
        def garbage(db: str) -> None:
            for suffix in ("-wal", "-shm"):
                if os.path.exists(db + suffix):
                    os.remove(db + suffix)
            with open(db, "wb") as f:
                f.write(b"this is not an sqlite database" * 200)
        h = self._broken_home("h corrupt", garbage)
        self._assert_not_healthy(h)

    def test_corrupt_pages(self) -> None:
        def corrupt(db: str) -> None:
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.execute("PRAGMA journal_mode=DELETE;")
            conn.close()
            size = os.path.getsize(db)
            self.assertGreater(size, 4 * 4096)
            with open(db, "r+b") as f:
                for off in range(4096, size, 4096):  # every page but the header page
                    f.seek(off + 8)
                    f.write(os.urandom(64))
        h = self._broken_home("h#pages", corrupt)
        self._assert_not_healthy(h)

    def test_db_open_error(self) -> None:
        def make_dir(db: str) -> None:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(db + suffix):
                    os.remove(db + suffix)
            os.makedirs(db, mode=0o700)
        h = self._broken_home("h?open", make_dir)
        self._assert_not_healthy(h)

    def test_null_params_still_kdf_invalid(self) -> None:
        h = self._broken_home("h?null", lambda db: self._sql(
            db, "UPDATE credentials SET kdf_params = NULL WHERE kind = 'passphrase';"))
        for extra in ({}, _prod_env()):
            rc, env = h.cli("doctor", extra_env=extra)
            self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, env)
            self.assertEqual(env["error"]["code"], c.KDF_PARAMS_INVALID)


class TestDefaultHomeGuardEverywhere(unittest.TestCase):
    """Doctor, init and meshd share mesh.core.paths.refuse_testing_on_default_home."""

    def setUp(self) -> None:
        self.harness = MeshHarness(check_invariants_on_teardown=False)
        self.addCleanup(self.harness.teardown)
        self.fake_user_home = self.harness.home  # stands in for $HOME
        self.default = os.path.join(self.fake_user_home, ".agent-mesh")
        # A weak testing home copied onto the default path (init itself refuses that).
        src = os.path.join(self.fake_user_home, "src")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(src, PASS)
        shutil.copytree(src, self.default)
        os.chmod(self.default, 0o700)

    def test_doctor_testing_mode_on_default_home_refused(self) -> None:
        link = os.path.join(self.fake_user_home, "link")
        os.symlink(self.default, link)
        for home in (self.default, link):
            with self.subTest(home=home):
                self.harness.home = home
                rc, env = self.harness.cli("doctor", extra_env={"HOME": self.fake_user_home})
                self.assertEqual(rc, c.EXIT_USAGE, env)
                self.assertEqual(env["error"]["code"], c.INVALID_ARGUMENT)
                self.assertIn("default home", env["error"]["message"])
        self.harness.home = self.fake_user_home

    def test_doctor_testing_mode_on_identity_alias_refused(self) -> None:
        from mesh.cli.commands.doctor import handle_doctor

        alias = os.path.join(self.fake_user_home, "bind-alias")
        shutil.copytree(self.default, alias)
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            path = os.fspath(path)
            return real_stat(self.default if path == os.path.realpath(alias) else path, *a, **kw)

        with mock.patch.dict(os.environ, {"HOME": self.fake_user_home, "MESH_TESTING": "1"}), \
                mock.patch.object(mesh_paths.os, "stat", side_effect=fake_stat):
            with self.assertRaises(MeshError) as cm:
                handle_doctor(argparse.Namespace(home=alias, check_invariants=False, fix_perms=False))
        self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)

    def test_daemon_uses_identity_helper(self) -> None:
        from mesh.daemon.server import DaemonServer

        alias = os.path.join(self.fake_user_home, "bind-alias2")
        os.makedirs(alias, mode=0o700)
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            path = os.fspath(path)
            return real_stat(self.default if path == os.path.realpath(alias) else path, *a, **kw)

        with mock.patch.dict(os.environ, {"HOME": self.fake_user_home}):
            srv = DaemonServer(alias, testing_mode=True)
            with mock.patch.object(mesh_paths.os, "stat", side_effect=fake_stat):
                with self.assertRaises(MeshError) as cm:
                    srv.setup_storage()
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            # Without the alias the same directory is not the default home.
            self.assertFalse(mesh_paths.is_default_home(alias))

    def test_entry_points_share_one_guard(self) -> None:
        import inspect
        from mesh.cli.commands import doctor
        from mesh.core import init
        from mesh.daemon import server

        for mod in (doctor, init, server):
            src = inspect.getsource(mod)
            self.assertIn("refuse_testing_on_default_home(", src, mod.__name__)
            self.assertNotIn("== default_home_path()", src, mod.__name__)


class TestKdfCostOracle(unittest.TestCase):
    """Stored verifiers equal scrypt computed independently with the expected cost."""

    def test_known_vectors_match_hashlib(self) -> None:
        for n, expected in ((KV_PROD_N, KV_PROD), (KV_TEST_N, KV_TEST)):
            dk = hashlib.scrypt(KV_PASS, salt=KV_SALT, n=n, r=KV_R, p=KV_P, dklen=KV_DKLEN,
                                maxmem=64 * 1024 * 1024)
            self.assertEqual(dk, expected)
        self.assertNotEqual(KV_PROD, KV_TEST)

    def test_derive_production_matches_vector(self) -> None:
        verifier, k_user, salt, params = derive_user_keys(KV_PASS.decode(), salt=KV_SALT)
        self.assertEqual(salt, KV_SALT)
        self.assertEqual(verifier, KV_PROD[:32])
        self.assertEqual(k_user, KV_PROD[32:])
        self.assertEqual((params["n"], params["r"], params["p"], params["dklen"]),
                         (KV_PROD_N, KV_R, KV_P, KV_DKLEN))
        self.assertNotIn("testing", params)

    def test_derive_testing_matches_vector(self) -> None:
        verifier, k_user, _salt, params = derive_user_keys(KV_PASS.decode(), salt=KV_SALT, testing_kdf=True)
        self.assertEqual(verifier, KV_TEST[:32])
        self.assertEqual(k_user, KV_TEST[32:])
        self.assertEqual(params["n"], KV_TEST_N)
        self.assertIs(params.get("testing"), True)

    def test_stored_verifier_equals_independent_scrypt(self) -> None:
        tmp = tempfile.mkdtemp(prefix="mh-oracle-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.chmod(tmp, 0o700)
        cases = (("prod", {"MESH_TESTING": "0"}, KV_PROD_N), ("test", {"MESH_TESTING": "1"}, KV_TEST_N))
        for name, env, n in cases:
            with self.subTest(home=name):
                home = os.path.join(tmp, name)
                with mock.patch.dict(os.environ, env):
                    init_mesh_home(home, PASS)
                conn = sqlite3.connect(os.path.join(home, "mesh.db"))
                try:
                    secret_hash, salt, raw = conn.execute(
                        "SELECT secret_hash, salt, kdf_params FROM credentials WHERE kind = 'passphrase';"
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(json.loads(raw)["n"], n)
                dk = hashlib.scrypt(PASS.encode(), salt=salt, n=n, r=KV_R, p=KV_P, dklen=KV_DKLEN,
                                    maxmem=64 * 1024 * 1024)
                self.assertEqual(bytes(secret_hash), dk[:32])
                other = KV_TEST_N if n == KV_PROD_N else KV_PROD_N
                dk_other = hashlib.scrypt(PASS.encode(), salt=salt, n=other, r=KV_R, p=KV_P,
                                          dklen=KV_DKLEN, maxmem=64 * 1024 * 1024)
                self.assertNotEqual(bytes(secret_hash), dk_other[:32])


if __name__ == "__main__":
    unittest.main()
