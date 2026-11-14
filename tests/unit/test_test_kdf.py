"""m3-test-kdf: cheap scrypt params for testing homes, strictly gated (guards a-c).

a) only `mesh init` in testing mode (MESH_TESTING=1) on a non-default home gets the
   cheap KDF; `mesh init` refuses testing mode on the default home.
b) the home records a testing marker with its kdf params; outside testing mode the
   unlock (and `mesh doctor`) refuse a home whose KDF is below the production cost.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import mesh.constants as c
from mesh.core import auth
from mesh.core import paths as mesh_paths
from mesh.core.auth import (
    SCRYPT_DKLEN,
    SCRYPT_MAXMEM,
    SCRYPT_N,
    SCRYPT_N_TESTING,
    derive_user_keys,
    kdf_params_weak,
    parse_kdf_params,
    verify_user_passphrase,
)
from mesh.core.init import init_mesh_home
from mesh.errors import MeshError

PASS = "test-passphrase-at-least-16-chars-long"


def _stored_kdf(home: str) -> dict:
    conn = sqlite3.connect(os.path.join(home, "mesh.db"))
    try:
        row = conn.execute("SELECT kdf_params FROM credentials WHERE kind = 'passphrase';").fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


def _env_without_testing() -> dict:
    env = dict(os.environ)
    env.pop("MESH_TESTING", None)
    return env


class TestDeriveParams(unittest.TestCase):
    def test_default_is_production_cost(self) -> None:
        _, _, _, params = derive_user_keys(PASS)
        self.assertEqual(params["n"], SCRYPT_N)
        self.assertEqual(SCRYPT_N, 2**15)
        self.assertNotIn("testing", params)
        self.assertFalse(kdf_params_weak(params))

    def test_testing_kdf_is_cheap_and_marked(self) -> None:
        verifier, k_user, salt, params = derive_user_keys(PASS, testing_kdf=True)
        self.assertEqual(params["n"], SCRYPT_N_TESTING)
        self.assertEqual(SCRYPT_N_TESTING, 2**10)
        self.assertIs(params["testing"], True)
        self.assertTrue(kdf_params_weak(params))
        ok, k2 = verify_user_passphrase(PASS, salt, params, verifier, testing_mode=True)
        self.assertTrue(ok)
        self.assertEqual(k2, k_user)


class TestUnlockRefusal(unittest.TestCase):
    """Guard b: weak params + testing_mode=False -> KDF_TOO_WEAK, whatever the env says."""

    def setUp(self) -> None:
        self.verifier, _, self.salt, self.weak = derive_user_keys(PASS, testing_kdf=True)

    def _assert_refused(self, testing_mode: bool = False) -> MeshError:
        with self.assertRaises(MeshError) as cm:
            verify_user_passphrase(PASS, self.salt, self.weak, self.verifier, testing_mode=testing_mode)
        self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
        self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)
        self.assertIn("testing mode", cm.exception.message)
        self.assertNotIn(PASS, cm.exception.message)
        return cm.exception

    def test_refused_outside_testing_mode(self) -> None:
        exc = self._assert_refused()
        self.assertEqual(exc.details["kdf_n"], SCRYPT_N_TESTING)
        self.assertEqual(exc.details["min_n"], SCRYPT_N)
        self.assertTrue(exc.details["testing_marker"])

    def test_helpers_never_read_the_env(self) -> None:
        # Mode authority: the caller's mode is the only input; MESH_TESTING=1 in the
        # env does not relax an explicit False, and an unset env does not tighten True.
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            self._assert_refused(testing_mode=False)
            self.assertTrue(kdf_params_weak(self.weak))
        with mock.patch.dict(os.environ, _env_without_testing(), clear=True):
            ok, _ = verify_user_passphrase(PASS, self.salt, self.weak, self.verifier, testing_mode=True)
            self.assertTrue(ok)

    def test_testing_mode_is_required(self) -> None:
        with self.assertRaises(TypeError):
            verify_user_passphrase(PASS, self.salt, self.weak, self.verifier)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            parse_kdf_params(self.weak)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            parse_kdf_params(self.weak, None)  # type: ignore[arg-type]

    def test_refused_before_any_scrypt_work(self) -> None:
        with mock.patch.object(auth.hashlib, "scrypt", side_effect=AssertionError("scrypt ran")):
            self._assert_refused()

    def test_weak_without_marker_is_refused(self) -> None:
        # Hand-made, otherwise valid params (no marker) below the floor: still too weak.
        base = {"n": 2**14, "r": 8, "p": 1, "dklen": SCRYPT_DKLEN, "maxmem": SCRYPT_MAXMEM}
        for n in (2**10, 2**12, 2**14):
            with self.subTest(n=n):
                with self.assertRaises(MeshError) as cm:
                    parse_kdf_params(dict(base, n=n), testing_mode=False)
                self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
                self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)
                parse_kdf_params(dict(base, n=n), testing_mode=True)

    def test_marker_alone_is_refused(self) -> None:
        params = {"n": 2**15, "r": 8, "p": 1, "dklen": SCRYPT_DKLEN, "maxmem": SCRYPT_MAXMEM, "testing": True}
        with self.assertRaises(MeshError) as cm:
            parse_kdf_params(params, testing_mode=False)
        self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)

    def test_production_params_unlock_outside_testing(self) -> None:
        verifier, k_user, salt, params = derive_user_keys(PASS)
        ok, k2 = verify_user_passphrase(PASS, salt, params, verifier, testing_mode=False)
        bad, _ = verify_user_passphrase("wrong-passphrase-here", salt, params, verifier, testing_mode=False)
        self.assertTrue(ok)
        self.assertEqual(k2, k_user)
        self.assertFalse(bad)
        # Stored text form (what meshd reads from the DB) is accepted the same way.
        ok, _ = verify_user_passphrase(PASS, salt, json.dumps(params), verifier, testing_mode=False)
        self.assertTrue(ok)
        # The production params pin exactly what init writes.
        self.assertEqual(parse_kdf_params(json.dumps(params), testing_mode=False),
                         {"n": 2**15, "r": 8, "p": 1, "dklen": 64, "maxmem": 64 * 1024 * 1024})


class TestKdfParamsValidation(unittest.TestCase):
    """MAJOR 2 + MINOR: one parse+validate helper, fail closed in every mode."""

    GOOD = {"n": 2**15, "r": 8, "p": 1, "dklen": 64, "maxmem": 64 * 1024 * 1024}

    def _assert_invalid(self, raw) -> None:
        for mode in (False, True):
            with self.subTest(raw=raw, testing_mode=mode):
                with self.assertRaises(MeshError) as cm, \
                        mock.patch.object(auth.hashlib, "scrypt", side_effect=AssertionError("scrypt ran")):
                    verify_user_passphrase(PASS, b"s" * 16, raw, b"v" * 32, testing_mode=mode)
                self.assertEqual(cm.exception.code, c.KDF_PARAMS_INVALID)
                self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)
                self.assertNotIn(PASS, cm.exception.message)

    def test_unreadable_stored_values(self) -> None:
        for raw in (None, "", "   ", "{", "{\"n\": 32768,", "null", "[]", "\"x\"", "32768", "true",
                    b"\xff\xfe", 12345, [], "NaN",
                    '{"n": NaN, "r": 8, "p": 1, "dklen": 64, "maxmem": 67108864}',
                    '{"n": Infinity, "r": 8, "p": 1, "dklen": 64, "maxmem": 67108864}'):
            self._assert_invalid(raw)

    def test_missing_and_unknown_keys(self) -> None:
        for key in self.GOOD:
            bad = dict(self.GOOD)
            del bad[key]
            self._assert_invalid(json.dumps(bad))
        self._assert_invalid(json.dumps(dict(self.GOOD, salt_len=16)))
        self._assert_invalid({"r": 8, "p": 1})  # pre-validation "old home" shape: no such home exists

    def test_out_of_range_values(self) -> None:
        cases = [
            {"n": 3 * 2**14}, {"n": 2**15 + 1}, {"n": 2**9}, {"n": 2**21}, {"n": 0}, {"n": -32768},
            {"n": "32768"}, {"n": 32768.0}, {"n": True}, {"n": None},
            {"r": 7}, {"r": 4}, {"r": 33}, {"r": "8"}, {"r": False},
            {"p": 0}, {"p": -1}, {"p": 17}, {"p": 1.5},
            {"dklen": 32}, {"dklen": 65}, {"dklen": 128}, {"dklen": "64"},
            {"maxmem": 0}, {"maxmem": 1024}, {"maxmem": 512 * 1024 * 1024}, {"maxmem": 64.5},
            {"n": 2**20},  # power of two in range but needs 1 GiB > the 256 MiB maxmem cap
            {"testing": False}, {"testing": 1}, {"testing": "true"}, {"testing": None},
        ]
        for patch in cases:
            self._assert_invalid(json.dumps(dict(self.GOOD, **patch)))

    def test_accepted_shapes(self) -> None:
        self.assertEqual(parse_kdf_params(json.dumps(self.GOOD), testing_mode=False), self.GOOD)
        self.assertEqual(parse_kdf_params(json.dumps(self.GOOD).encode(), testing_mode=False), self.GOOD)
        strong = dict(self.GOOD, n=2**16, r=16, p=2, maxmem=256 * 1024 * 1024)
        self.assertEqual(parse_kdf_params(json.dumps(strong), testing_mode=False), strong)
        testing = dict(self.GOOD, n=2**10, testing=True)
        self.assertEqual(parse_kdf_params(json.dumps(testing), testing_mode=True), testing)


class TestInitGate(unittest.TestCase):
    """Guard a: the cheap KDF only for testing-mode init on a non-default home."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-kdf-")
        os.chmod(self.tmp, 0o700)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_testing_init_on_other_home_is_cheap_and_marked(self) -> None:
        home = os.path.join(self.tmp, "h")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            res = init_mesh_home(home, PASS)
        self.assertTrue(res["initialized"])
        params = _stored_kdf(home)
        self.assertEqual(params["n"], SCRYPT_N_TESTING)
        self.assertIs(params["testing"], True)

    def test_non_testing_init_is_production_cost(self) -> None:
        for env in (_env_without_testing(), dict(_env_without_testing(), MESH_TESTING="0")):
            home = os.path.join(self.tmp, f"h{len(env)}-{env.get('MESH_TESTING', 'unset')}")
            with self.subTest(testing=env.get("MESH_TESTING")), mock.patch.dict(os.environ, env, clear=True):
                init_mesh_home(home, PASS)
                params = _stored_kdf(home)
                self.assertEqual(params["n"], SCRYPT_N)
                self.assertNotIn("testing", params)

    def test_testing_init_on_default_home_refused(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "MESH_TESTING": "1"}):
            default = os.path.join(self.tmp, ".agent-mesh")
            with self.assertRaises(MeshError) as cm:
                init_mesh_home(default, PASS)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertEqual(cm.exception.exit_code, c.EXIT_USAGE)
            self.assertIn("default home", cm.exception.message)
            self.assertFalse(os.path.exists(default))
            # Same refusal through a symlink to the default home.
            os.makedirs(default, mode=0o700)
            link = os.path.join(self.tmp, "alias")
            os.symlink(default, link)
            with self.assertRaises(MeshError):
                init_mesh_home(link, PASS)
            self.assertFalse(os.path.exists(os.path.join(default, "mesh.db")))

    def _fake_identity(self, alias_of: dict):
        """os.stat stand-in: paths in alias_of report the identity of their target."""
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            path = os.fspath(path)
            return real_stat(alias_of.get(path, path), *a, **kw)
        return mock.patch.object(mesh_paths.os, "stat", side_effect=fake_stat)

    def test_testing_init_on_alias_by_identity_refused(self) -> None:
        # A second path to the default home that realpath cannot see (bind mount);
        # simulated by making os.stat report the default home's (st_dev, st_ino).
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "MESH_TESTING": "1"}):
            default = os.path.join(self.tmp, ".agent-mesh")
            os.makedirs(default, mode=0o700)
            alias = os.path.join(self.tmp, "bind-alias")
            os.makedirs(alias, mode=0o700)
            self.assertNotEqual(os.path.realpath(alias), os.path.realpath(default))
            with self._fake_identity({os.path.realpath(alias): os.path.realpath(default)}):
                self.assertTrue(mesh_paths.is_default_home(alias))
                with self.assertRaises(MeshError) as cm:
                    init_mesh_home(alias, PASS)
            self.assertEqual(cm.exception.code, c.INVALID_ARGUMENT)
            self.assertIn("default home", cm.exception.message)
            self.assertFalse(os.path.exists(os.path.join(alias, "mesh.db")))
            self.assertFalse(os.path.exists(os.path.join(default, "mesh.db")))
            # Without the alias the same directory is an ordinary other home.
            self.assertFalse(mesh_paths.is_default_home(alias))

    def test_testing_init_on_alias_of_parent_refused(self) -> None:
        # Neither home exists yet: an alias of the default home's PARENT plus the same
        # basename is the default home too (parent identity + basename).
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "MESH_TESTING": "1"}):
            other_parent = os.path.join(self.tmp, "mnt")
            os.makedirs(other_parent, mode=0o700)
            candidate = os.path.join(other_parent, ".agent-mesh")
            with self._fake_identity({os.path.realpath(other_parent): os.path.realpath(self.tmp)}):
                self.assertTrue(mesh_paths.is_default_home(candidate))
                with self.assertRaises(MeshError):
                    init_mesh_home(candidate, PASS)
                # Same parent alias but a different basename is a different home.
                self.assertFalse(mesh_paths.is_default_home(os.path.join(other_parent, "mh")))
            self.assertFalse(os.path.exists(candidate))

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("mount"), "bind mount needs root")
    def test_testing_init_on_real_bind_mount_refused(self) -> None:  # pragma: no cover - root only
        with mock.patch.dict(os.environ, {"HOME": self.tmp, "MESH_TESTING": "1"}):
            default = os.path.join(self.tmp, ".agent-mesh")
            os.makedirs(default, mode=0o700)
            alias = os.path.join(self.tmp, "bind-alias")
            os.makedirs(alias, mode=0o700)
            if subprocess.run(["mount", "--bind", default, alias]).returncode != 0:
                self.skipTest("bind mount not permitted here")
            try:
                with self.assertRaises(MeshError):
                    init_mesh_home(alias, PASS)
            finally:
                subprocess.run(["umount", alias])

    def test_default_home_without_testing_is_production(self) -> None:
        env = dict(_env_without_testing(), HOME=self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            default = os.path.join(self.tmp, ".agent-mesh")
            init_mesh_home(default, PASS)
            self.assertEqual(_stored_kdf(default)["n"], SCRYPT_N)


class TestDoctorRefusesWeakHome(unittest.TestCase):
    def test_doctor(self) -> None:
        from mesh.cli.commands.doctor import handle_doctor

        tmp = tempfile.mkdtemp(prefix="mh-kdf-")
        try:
            os.chmod(tmp, 0o700)
            weak = os.path.join(tmp, "weak")
            strong = os.path.join(tmp, "strong")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(weak, PASS)
            with mock.patch.dict(os.environ, _env_without_testing(), clear=True):
                init_mesh_home(strong, PASS)
                with self.assertRaises(MeshError) as cm:
                    handle_doctor(argparse.Namespace(home=weak, check_invariants=True, fix_perms=False))
                self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
                res = handle_doctor(argparse.Namespace(home=strong, check_invariants=True, fix_perms=False))
                self.assertEqual(res.get("violations"), [])
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                res = handle_doctor(argparse.Namespace(home=weak, check_invariants=True, fix_perms=False))
                self.assertEqual(res.get("violations"), [])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _set_stored_kdf(home: str, raw) -> None:
    conn = sqlite3.connect(os.path.join(home, "mesh.db"))
    try:
        conn.execute("UPDATE credentials SET kdf_params = ? WHERE kind = 'passphrase';", (raw,))
        conn.commit()
    finally:
        conn.close()


class TestDoctorFailClosed(unittest.TestCase):
    """MAJOR 2: doctor never reports a home with unreadable KDF params as healthy."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-kdf-")
        os.chmod(self.tmp, 0o700)
        self.home = os.path.join(self.tmp, "h")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(self.home, PASS)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_garbled_null_empty_fail_in_both_modes(self) -> None:
        from mesh.cli.commands.doctor import handle_doctor

        for raw in ("{not json", None, "", "null", "[]", '{"n": 1024}'):
            _set_stored_kdf(self.home, raw)
            for env in ({"MESH_TESTING": "1"}, {"MESH_TESTING": "0"}):
                with self.subTest(raw=raw, env=env), mock.patch.dict(os.environ, env):
                    with self.assertRaises(MeshError) as cm:
                        handle_doctor(argparse.Namespace(home=self.home, check_invariants=False, fix_perms=False))
                    self.assertEqual(cm.exception.code, c.KDF_PARAMS_INVALID)
                    self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)


class TestDaemonModeAuthority(unittest.TestCase):
    """MAJOR 3 + MINOR (order): daemon-level, through DaemonServer.authenticate_and_authorize."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mh-kdf-")
        os.chmod(self.tmp, 0o700)
        self.weak_home = os.path.join(self.tmp, "weak")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(self.weak_home, PASS)
        self.servers = []

    def tearDown(self) -> None:
        for srv in self.servers:
            if srv.db_conn is not None:
                srv.db_conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _server(self, home: str, **kw):
        from mesh.core.db import connect_db
        from mesh.daemon.server import DaemonServer

        srv = DaemonServer(home, **kw)
        srv.db_conn = connect_db(os.path.join(home, "mesh.db"))
        self.servers.append(srv)
        return srv

    def _auth(self, srv, passphrase: str = PASS):
        import asyncio

        with mock.patch("mesh.daemon.server.classify_peer_run_session", return_value=None):
            return asyncio.run(srv.authenticate_and_authorize(
                peer_pid=os.getpid(), auth_info={"type": "user", "passphrase": passphrase},
                cmd="whoami", request_id="req-kdf"))

    def _failures(self, home: str) -> int:
        conn = sqlite3.connect(os.path.join(home, "mesh.db"))
        try:
            return conn.execute("SELECT COUNT(*) FROM auth_failures;").fetchone()[0]
        finally:
            conn.close()

    def test_explicit_false_beats_env(self) -> None:
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            srv = self._server(self.weak_home, testing_mode=False)
            self.assertFalse(srv.testing_mode)
            with self.assertRaises(MeshError) as cm:
                self._auth(srv)
        self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
        self.assertEqual(self._failures(self.weak_home), 0)

    def test_mode_is_decided_once_at_startup(self) -> None:
        # Changing the env after construction changes nothing in either direction.
        with mock.patch.dict(os.environ, _env_without_testing(), clear=True):
            srv_off = self._server(self.weak_home)
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            self.assertFalse(srv_off.testing_mode)
            with self.assertRaises(MeshError) as cm:
                self._auth(srv_off)
            self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
            srv_on = self._server(self.weak_home)
        self.assertTrue(srv_on.testing_mode)
        with mock.patch.dict(os.environ, _env_without_testing(), clear=True):
            principal, kind, via, k_user = self._auth(srv_on)
        self.assertEqual(kind, "user")
        self.assertIsNotNone(k_user)

    def test_explicit_true_without_env(self) -> None:
        with mock.patch.dict(os.environ, _env_without_testing(), clear=True):
            srv = self._server(self.weak_home, testing_mode=True)
            self.assertTrue(srv.testing_mode)
            _, kind, _, _ = self._auth(srv)
        self.assertEqual(kind, "user")

    def test_kdf_check_before_lockout(self) -> None:
        conn = sqlite3.connect(os.path.join(self.weak_home, "mesh.db"))
        try:
            from mesh.core.clock import now_iso_utc
            for _ in range(5):
                conn.execute("INSERT INTO auth_failures (kind, at, peer_pid) VALUES ('passphrase', ?, 1);",
                             (now_iso_utc(),))
            conn.commit()
        finally:
            conn.close()
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            srv = self._server(self.weak_home, testing_mode=False)
            with self.assertRaises(MeshError) as cm:
                self._auth(srv)
            self.assertEqual(cm.exception.code, c.KDF_TOO_WEAK)
            # A testing daemon on the same home is past the KDF check and hits the lockout.
            srv_t = self._server(self.weak_home, testing_mode=True)
            with self.assertRaises(MeshError) as cm2:
                self._auth(srv_t)
            self.assertEqual(cm2.exception.code, c.LOCKED_OUT)
        self.assertEqual(self._failures(self.weak_home), 5)

    def test_invalid_params_same_code_as_doctor(self) -> None:
        from mesh.cli.commands.doctor import handle_doctor

        for raw in ("{garbled", None, "", "null"):
            _set_stored_kdf(self.weak_home, raw)
            for mode in (False, True):
                with self.subTest(raw=raw, testing_mode=mode):
                    srv = self._server(self.weak_home, testing_mode=mode)
                    with self.assertRaises(MeshError) as cm:
                        self._auth(srv)
                    self.assertEqual(cm.exception.code, c.KDF_PARAMS_INVALID)
                    self.assertEqual(cm.exception.exit_code, c.EXIT_UNAUTHENTICATED)
                    with mock.patch.dict(os.environ, {"MESH_TESTING": "1" if mode else "0"}), \
                            self.assertRaises(MeshError) as dcm:
                        handle_doctor(argparse.Namespace(home=self.weak_home, check_invariants=False,
                                                         fix_perms=False))
                    self.assertEqual(dcm.exception.code, cm.exception.code)
        self.assertEqual(self._failures(self.weak_home), 0)


if __name__ == "__main__":
    unittest.main()
