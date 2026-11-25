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
from mesh.platform.paths import temp_root

MESH_PROC_LOCK = "shared"


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
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(recovery._identity_alive({"pid": proc.pid, "starttime": 0}))


class TestRunwrapDelay(unittest.TestCase):
    def _run(self, spec_extra: dict) -> float:
        run_dir = tempfile.mkdtemp(prefix="mh-rw-", dir=temp_root())
        self.addCleanup(shutil.rmtree, run_dir, True)
        spec = {"argv": [sys.executable, "-c", "pass"], "cwd": run_dir, "stdin": "devnull", "redact_env_names": []}
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
            with mock.patch.dict(os.environ, {"HOME": fake_home, "USERPROFILE": fake_home}):
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


class TestRecoveryLogLevels(unittest.TestCase):
    """Issue 2: clean start logs INFO; WARNING/ERROR only for real recovery problems."""

    def test_clean_start_logs_info_not_error(self) -> None:
        from mesh.core.db import connect, init_db

        with tempfile.TemporaryDirectory(prefix="mh-rec-log-") as home:
            os.makedirs(os.path.join(home, "logs"), mode=0o700, exist_ok=True)
            conn = init_db(os.path.join(home, "mesh.db"))
            conn.close()
            conn = connect(os.path.join(home, "mesh.db"))
            try:
                summary = recovery.recover(conn, home, "boot_clean_1", testing_mode=True)
            finally:
                conn.close()

            self.assertEqual(summary["run_errors"], 0)
            self.assertEqual(summary["merge_errors"], 0)
            log_path = os.path.join(home, "logs", "meshd.log")
            with open(log_path, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            self.assertEqual(len(lines), 1, lines)
            self.assertIn(" INFO recovery: ", lines[0])
            self.assertNotIn(" ERROR recovery: ", lines[0])
            self.assertNotIn(" WARNING recovery: ", lines[0])

    def test_summary_level_classification(self) -> None:
        zero_summary = {
            "boot_id": "boot_1",
            "runs_adopted": 0,
            "runs_adopted_uncertain": 0,
            "runs_adopted_past_deadline": 0,
            "runs_exited": 0,
            "runs_exit_deferred": 0,
            "runs_lost": 0,
            "runs_spawn_failed": 0,
            "runs_terminating": 0,
            "run_errors": 0,
            "merge_runs_killed": 0,
            "merge_runs_lost": 0,
            "merge_runs_held": 0,
            "merges_interrupted": 0,
            "merges_reconciled": 0,
            "merge_errors": 0,
            "workspace_deletes_finalized": 0,
            "workspace_deletes_rolled_back": 0,
            "workspace_prunes_finalized": 0,
            "workspace_prunes_rolled_back": 0,
            "deliveries_recovered": 0,
            "agent_leases_extended": 0,
            "outbox_requeued": 0,
            "tasks_cancelled": 0,
            "approvals_expired": 0,
        }
        self.assertEqual(recovery._recovery_summary_level(zero_summary), "INFO")
        self.assertEqual(
            recovery._recovery_summary_level({**zero_summary, "approvals_expired": 2, "runs_adopted": 1}),
            "INFO",
        )
        for warn_key in (
            "runs_lost",
            "runs_spawn_failed",
            "runs_adopted_uncertain",
            "runs_terminating",
            "merges_interrupted",
            "merge_runs_held",
            "workspace_deletes_rolled_back",
        ):
            self.assertEqual(
                recovery._recovery_summary_level({**zero_summary, warn_key: 1}),
                "WARNING",
                warn_key,
            )
        self.assertEqual(recovery._recovery_summary_level({**zero_summary, "run_errors": 1}), "ERROR")
        self.assertEqual(recovery._recovery_summary_level({**zero_summary, "merge_errors": 1}), "ERROR")
        self.assertEqual(
            recovery._recovery_summary_level(zero_summary, {"deliver_intent_errors": 1}),
            "ERROR",
        )

    def test_recovery_log_compat_with_two_arg_mock(self) -> None:
        captured = []
        with mock.patch.object(supervisor, "_log_meshd", lambda h, m: captured.append((h, m))):
            recovery._log("/tmp/fake", "clean summary", level="INFO")
            recovery._log("/tmp/fake", "problem", level="WARNING")
            recovery._log("/tmp/fake", "failure", level="ERROR")
        self.assertEqual(len(captured), 3)
        self.assertTrue(all(msg.startswith("recovery: ") for _, msg in captured))


if __name__ == "__main__":
    unittest.main()
