"""m3-test-kdf guard c: the production-cost KDF path stays covered end to end.

The harness homes are created in testing mode and so use the cheap testing KDF
(scrypt n=2**10). These tests keep the production path exercised through the real
CLI and daemon: `mesh init` outside testing mode stores n=2**15 and a user command
unlocks with it, and a non-testing meshd refuses to unlock a testing home.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import mesh.constants as c
from mesh.core.auth import SCRYPT_N, SCRYPT_N_TESTING
from tests.helpers import MeshHarness
from tests.stubs import create_test_stubs


def stored_kdf(h: MeshHarness) -> dict:
    conn = sqlite3.connect(os.path.join(h.home, "mesh.db"))
    try:
        row = conn.execute("SELECT kdf_params FROM credentials WHERE kind = 'passphrase';").fetchone()
    finally:
        conn.close()
    return json.loads(row[0])


class TestKdfProductionPath(unittest.TestCase):
    def setUp(self) -> None:
        self.stubs_dir = tempfile.mkdtemp(prefix="mh-stubs-")
        create_test_stubs(self.stubs_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.stubs_dir, ignore_errors=True)

    def _start_non_testing_daemon(self, h: MeshHarness) -> None:
        env = h.get_env()
        env.pop("MESH_TESTING", None)
        h.daemon_proc = subprocess.Popen([h.bin_mesh, "--home", h.home, "daemon", "run"], env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        h.wait_for(lambda: h.cli("status")[0] == 0, timeout=10.0)

    def test_full_params_init_and_unlock(self) -> None:
        """Production cost: init outside testing mode -> n=2**15, no marker; a non-testing meshd unlocks it."""
        h = MeshHarness(init_db=False, start_daemon=False, extra_path=self.stubs_dir)
        try:
            rc, resp = h.cli("init", user=True, extra_env={"MESH_TESTING": "0"})
            self.assertEqual(rc, 0, resp)
            params = stored_kdf(h)
            self.assertEqual(params["n"], SCRYPT_N)
            self.assertEqual(params["n"], 2**15)
            self.assertNotIn("testing", params)
            self._start_non_testing_daemon(h)
            rc, who = h.cli("whoami", user=True, timeout=20.0)
            self.assertEqual(rc, 0, who)
            self.assertEqual(who["data"]["kind"], "user")
            rc_bad, bad = h.cli("whoami", "--user", "--passphrase-fd", "0", stdin="wrong-passphrase-123\n")
            self.assertEqual(rc_bad, c.EXIT_UNAUTHENTICATED, bad)
        finally:
            h.teardown()

    def test_non_testing_daemon_refuses_testing_home(self) -> None:
        """Guard b end to end: a testing home (n=2**10, marker) is not unlocked by a non-testing meshd."""
        h = MeshHarness(init_db=True, start_daemon=False, extra_path=self.stubs_dir)
        try:
            params = stored_kdf(h)
            self.assertEqual(params["n"], SCRYPT_N_TESTING)
            self.assertIs(params["testing"], True)
            self._start_non_testing_daemon(h)
            rc, resp = h.cli("whoami", user=True)
            self.assertEqual(rc, c.EXIT_UNAUTHENTICATED, resp)
            self.assertEqual(resp["error"]["code"], c.KDF_TOO_WEAK)
            self.assertNotIn(h.passphrase, json.dumps(resp))
            # The refusal is not a passphrase failure: nothing counts toward the lockout.
            conn = sqlite3.connect(os.path.join(h.home, "mesh.db"))
            try:
                fails = conn.execute("SELECT COUNT(*) FROM auth_failures;").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(fails, 0)
        finally:
            h.teardown()


if __name__ == "__main__":
    unittest.main()
