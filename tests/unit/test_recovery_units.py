"""Unit tests for the crash-recovery hooks (plan §11.7, §17.1, risk 6)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mesh.core.proc import get_proc_stat
from mesh.daemon import recovery, supervisor


class TestCrashHook(unittest.TestCase):
    def test_crash_at_only_in_testing_mode(self) -> None:
        with mock.patch.dict(os.environ, {"MESH_TEST_CRASH_AT": "after_tx_a"}, clear=False):
            os.environ.pop("MESH_TESTING", None)
            self.assertIsNone(supervisor._crash_at())
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                self.assertEqual(supervisor._crash_at(), "after_tx_a")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1", "MESH_TEST_CRASH_AT": "anywhere"}):
            self.assertIsNone(supervisor._crash_at())


class TestIdentity(unittest.TestCase):
    def test_identity_alive_requires_matching_starttime(self) -> None:
        me = get_proc_stat(os.getpid())
        self.assertTrue(recovery._identity_alive({"pid": os.getpid(), "starttime": me["starttime"]}))
        self.assertFalse(recovery._identity_alive({"pid": os.getpid(), "starttime": int(me["starttime"]) + 1}))
        self.assertFalse(recovery._identity_alive({"pid": 1, "starttime": 0}))
        self.assertFalse(recovery._identity_alive(None))
        self.assertFalse(recovery._identity_alive({"pid": "x"}))

    def test_identity_dead_pid(self) -> None:
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.assertFalse(recovery._identity_alive({"pid": proc.pid, "starttime": 0}))


class TestRunwrapDelay(unittest.TestCase):
    def _run(self, spec_extra: dict) -> float:
        run_dir = tempfile.mkdtemp(prefix="mh-rw-")
        self.addCleanup(shutil.rmtree, run_dir, True)
        spec = {"argv": ["true"], "cwd": run_dir, "stdin": "devnull", "redact_env_names": []}
        spec.update(spec_extra)
        with open(os.path.join(run_dir, "spec.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)
        start = time.time()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESH_")}
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env["PYTHONPATH"] = repo
        subprocess.run([sys.executable, "-P", "-s", "-m", "mesh.runwrap", run_dir], env=env, check=True, timeout=30)
        took = os.stat(os.path.join(run_dir, "wrapper.json")).st_mtime - start
        self.assertTrue(os.path.exists(os.path.join(run_dir, "exit.json")))
        return took

    def test_spec_delay_postpones_wrapper_json(self) -> None:
        self.assertGreaterEqual(self._run({"testing_wrapper_delay_s": 1.0}), 0.9)

    def test_out_of_range_delay_ignored(self) -> None:
        self.assertLess(self._run({"testing_wrapper_delay_s": 999}), 5.0)


class TestTestingModeDefaultHome(unittest.TestCase):
    """Plan §17.1 (review m1c-run4 §6): meshd refuses testing mode on the default home."""

    def test_testing_mode_refused_on_default_home(self) -> None:
        from mesh.daemon.server import DaemonServer
        from mesh.errors import MeshError

        with tempfile.TemporaryDirectory() as fake_home:
            with mock.patch.dict(os.environ, {"HOME": fake_home}):
                default = os.path.join(fake_home, ".agent-mesh")
                os.makedirs(default, mode=0o700)
                with self.assertRaises(MeshError) as cm:
                    DaemonServer(default, testing_mode=True).setup_storage()
                self.assertEqual(cm.exception.code, "INVALID_ARGUMENT")
                self.assertEqual(cm.exception.exit_code, 2)
                self.assertIn("default home", cm.exception.message)
                # Not in testing mode the default home passes this check (and fails later,
                # here on the missing database, not on the testing-mode guard).
                with mock.patch.dict(os.environ, {"MESH_TESTING": "0"}):
                    with self.assertRaises(MeshError) as cm2:
                        DaemonServer(default, testing_mode=False).setup_storage()
                    self.assertNotIn("Testing mode", cm2.exception.message)
                # Another home in testing mode passes the guard.
                other = os.path.join(fake_home, "mh")
                os.makedirs(other, mode=0o700)
                with self.assertRaises(MeshError) as cm3:
                    DaemonServer(other, testing_mode=True).setup_storage()
                self.assertNotIn("Testing mode", cm3.exception.message)

    def test_daemon_start_waits_for_recovery(self) -> None:
        from mesh.cli.commands import daemon as daemon_cmd

        self.assertGreaterEqual(daemon_cmd.DAEMON_START_WAIT_S, 30.0)


if __name__ == "__main__":
    unittest.main()
