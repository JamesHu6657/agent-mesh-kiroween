"""Unit tests for cross-platform scripts/run_suite.py and T14 Windows recovery helpers."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mesh.platform import locks as plat_locks, paths as plat_paths, perms as plat_perms
from scripts import run_suite
from scripts.coord import reap_daemons
from tests.recovery import test_recovery, test_recovery_adopt_seen
from tests.unit import test_docs_m19

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestRunSuiteScript(unittest.TestCase):
    """Cross-platform and mocked-Win32 unit coverage for ``scripts/run_suite.py``."""

    def test_check_flag_cli_and_inproc(self) -> None:
        buf = io.StringIO()
        rc = run_suite.run_suite(check_only=True, stdout=buf)
        self.assertEqual(rc, 0)
        self.assertIn("syntax and runner OK", buf.getvalue())

        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "run_suite.py"), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("syntax and runner OK", proc.stdout)

    def test_tripwire_paths_and_build_suite_env_win32_mocked(self) -> None:
        win_tmp = r"C:\Users\ci\AppData\Local\Temp"
        fake_repo = r"C:\work\agent-mesh"
        with mock.patch.object(plat_paths, "temp_root", return_value=win_tmp):
            suite_tw, def_tw = run_suite.tripwire_paths(fake_repo)
        self.assertTrue(suite_tw.startswith(win_tmp))
        self.assertIn("mesh-tripwire-suite-", suite_tw)
        self.assertEqual(def_tw, os.path.join(win_tmp, "mesh-tripwire-hit.log"))

        with mock.patch.object(run_suite.os, "pathsep", ";"):
            env = run_suite.build_suite_env(
                {"PATH": r"C:\Python312;C:\Windows\System32", "MESH_TEST_SUITE_ID": "stale"},
                REPO_ROOT,
                suite_tw,
                os.path.join(win_tmp, "mesh-test-daemons"),
                "suite123",
            )
            self.assertEqual(env["_TRIPWIRE_FILE"], suite_tw)
            self.assertEqual(env["_SUITE_TRIPWIRE_FILE"], suite_tw)
            self.assertEqual(env["MESH_TEST_SUITE_ID"], "suite123")
            expected_prefix = ";".join((os.path.join(REPO_ROOT, "tests", "shims"), os.path.join(REPO_ROOT, "bin")))
            self.assertTrue(env["PATH"].startswith(expected_prefix + ";"), env["PATH"])

            env_nolock = run_suite.build_suite_env(
                {"MESH_TEST_SUITE_ID": "stale"},
                REPO_ROOT,
                suite_tw,
                os.path.join(win_tmp, "mesh-test-daemons"),
                None,
            )
            self.assertNotIn("MESH_TEST_SUITE_ID", env_nolock)
            self.assertEqual(env_nolock["PATH"], expected_prefix)

    def test_acquire_and_release_suite_lock_win32_mocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = os.path.join(td, "reg")
            calls = []

            def fake_prepare(d: str) -> None:
                os.makedirs(d, exist_ok=True)
                calls.append(("prepare", d))

            with mock.patch.object(run_suite.sys, "platform", "win32"), \
                    mock.patch.object(reap_daemons, "prepare_registry", side_effect=fake_prepare), \
                    mock.patch.object(plat_locks, "open_lock_file", wraps=plat_locks.open_lock_file) as m_open, \
                    mock.patch.object(plat_perms, "set_private_file_acl") as m_acl, \
                    mock.patch.object(plat_locks, "acquire_lock") as m_acq, \
                    mock.patch.object(plat_locks, "release_lock") as m_rel, \
                    mock.patch.object(run_suite, "_run_reap") as m_reap:
                fd, lock_path = run_suite._acquire_suite_lock(registry, "abc123")
                self.assertIsNotNone(fd)
                self.assertTrue(os.path.isfile(lock_path))
                m_open.assert_called_once_with(lock_path, mode=0o600)
                m_acl.assert_called_once_with(fd)
                m_acq.assert_called_once_with(fd, exclusive=True, blocking=False)

                run_suite._release_and_cleanup_suite(fd, lock_path, REPO_ROOT, {})
                m_rel.assert_called_once_with(fd)
                m_reap.assert_called_once()
                self.assertFalse(os.path.exists(lock_path))

    def test_acquire_suite_lock_failure_falls_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with mock.patch.object(reap_daemons, "prepare_registry", side_effect=PermissionError("denied")):
                fd, path = run_suite._acquire_suite_lock(os.path.join(td, "reg"), "bad", stderr=err)
            self.assertIsNone(fd)
            self.assertIsNone(path)
            self.assertIn("adhoc process ownership", err.getvalue())

    def test_run_suite_happy_path_tier_failure_and_tripwire_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            seen_tiers = []

            def fake_subproc(cmd, cwd=None, env=None, **_kw):
                seen_tiers.append(cmd[-1])
                return SimpleNamespace(returncode=0)

            with mock.patch.object(plat_paths, "temp_root", return_value=td), \
                    mock.patch.dict(os.environ, {"MESH_TEST_DAEMON_REGISTRY": os.path.join(td, "reg")}), \
                    mock.patch.object(run_suite.subprocess, "run", side_effect=fake_subproc), \
                    mock.patch.object(run_suite, "_run_reap") as m_reap:
                rc = run_suite.run_suite(repo_root=REPO_ROOT, stdout=out)
            self.assertEqual(rc, 0)
            self.assertEqual(seen_tiers, [rel for _lbl, rel in run_suite.TIERS])
            self.assertEqual(m_reap.call_count, 2)
            self.assertIn("All test tiers passed successfully", out.getvalue())

            # Tier failure stops before later tiers and still runs cleanup reap.
            seen_tiers.clear()
            out_fail = io.StringIO()

            def fail_integration(cmd, **_kw):
                seen_tiers.append(cmd[-1])
                return SimpleNamespace(returncode=2 if "integration" in cmd[-1] else 0)

            with mock.patch.object(plat_paths, "temp_root", return_value=td), \
                    mock.patch.dict(os.environ, {"MESH_TEST_DAEMON_REGISTRY": os.path.join(td, "reg")}), \
                    mock.patch.object(run_suite.subprocess, "run", side_effect=fail_integration), \
                    mock.patch.object(run_suite, "_run_reap") as m_reap:
                rc = run_suite.run_suite(repo_root=REPO_ROOT, stdout=out_fail)
            self.assertEqual(rc, 2)
            self.assertEqual(
                seen_tiers,
                [os.path.join("tests", "unit"), os.path.join("tests", "integration")],
            )
            self.assertEqual(m_reap.call_count, 2)

            # Non-empty tripwire log fails the suite with rc=1.
            out_tw = io.StringIO()
            suite_tw, _def_tw = run_suite.tripwire_paths(REPO_ROOT, temp_dir=td)

            def trigger_tw(cmd, **_kw):
                if "recovery" in cmd[-1]:
                    with open(suite_tw, "w", encoding="utf-8") as fh:
                        fh.write("TRIPWIRE HIT: claude\n")
                return SimpleNamespace(returncode=0)

            with mock.patch.object(plat_paths, "temp_root", return_value=td), \
                    mock.patch.dict(os.environ, {"MESH_TEST_DAEMON_REGISTRY": os.path.join(td, "reg")}), \
                    mock.patch.object(run_suite.subprocess, "run", side_effect=trigger_tw), \
                    mock.patch.object(run_suite, "_run_reap"):
                rc = run_suite.run_suite(repo_root=REPO_ROOT, stdout=out_tw)
            self.assertEqual(rc, 1)
            self.assertIn("FAIL: Suite tripwire was triggered", out_tw.getvalue())
            self.assertIn("TRIPWIRE HIT: claude", out_tw.getvalue())


class TestRecoveryHelpersWin32Mocked(unittest.TestCase):
    """Mocked Win32 branch coverage on Linux for T14 recovery test helpers."""

    def test_carriers_and_group_helpers_win32_mocked(self) -> None:
        envs = {
            101: {"MESH_RUN_ID": "run_1"},
            102: {"MESH_RUN_ID": "run_other"},
            103: None,
        }
        stats = {
            101: {"state": "R", "starttime": 111},
            104: {"state": "R", "starttime": 222},
            999: {"state": "R", "starttime": 333},
        }
        fake_job = mock.MagicMock()
        with mock.patch.object(test_recovery.sys, "platform", "win32"), \
                mock.patch.object(test_recovery.plat_proc, "list_toolhelp_processes", return_value={101: 1, 102: 1, 103: 1}), \
                mock.patch.object(test_recovery, "get_proc_environ", side_effect=lambda p: envs.get(p)), \
                mock.patch.object(test_recovery, "get_proc_stat", side_effect=lambda p: stats.get(p)):
            self.assertEqual(test_recovery._carriers("run_1"), [101])

            case = test_recovery.RecoveryBase("run")
            case.harness = SimpleNamespace(home=r"C:\mh")
            with mock.patch.object(test_recovery.plat_proc, "open_run_job", return_value=fake_job), \
                    mock.patch.object(test_recovery.plat_proc, "query_job_pids", return_value=[101, 104]), \
                    mock.patch.object(test_recovery.plat_proc, "terminate_job") as m_term_job, \
                    mock.patch.object(test_recovery, "_win_terminate_and_wait_pid") as m_kill_wait:
                members = case._run_group_members({"id": "run_1", "pgid": 999, "wrapper_pid": 999})
                self.assertEqual(members, [101, 104])
                case._kill_run_group_pids({"id": "run_1", "pgid": 999, "wrapper_pid": 999})
                m_term_job.assert_called_once_with(fake_job, exit_code=137)
                self.assertEqual([c.args[0] for c in m_kill_wait.call_args_list], [101, 104, 999])
                case._kill_pid(555)
                m_kill_wait.assert_called_with(555)

    def test_win32_creationflags_and_docs_run_help_mocked(self) -> None:
        fake_proc = mock.MagicMock()
        fake_proc.pid = 777
        fake_proc.stdout.readline.return_value = b"r\n"
        with mock.patch.object(test_recovery_adopt_seen.sys, "platform", "win32"), \
                mock.patch.object(test_recovery_adopt_seen.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True), \
                mock.patch.object(test_recovery_adopt_seen.subprocess, "Popen", return_value=fake_proc) as m_popen:
            p = test_recovery_adopt_seen._spawn_session_leader()
            self.assertIs(p, fake_proc)
            self.assertEqual(m_popen.call_args.kwargs["creationflags"], 0x200)
            self.assertNotIn("start_new_session", m_popen.call_args.kwargs)

        with mock.patch.object(test_docs_m19.sys, "platform", "win32"), \
                mock.patch.object(test_docs_m19.plat_perms, "set_private_dir_acl") as m_acl, \
                mock.patch.object(test_docs_m19, "mesh_cli_argv", side_effect=lambda *a: ["py", "-m", "mesh", *a]), \
                mock.patch.object(test_docs_m19.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=0, stdout='{"ok":true,"data":{"text":"ok"}}')) as m_run:
            rc, env = test_docs_m19.run_help("auth")
            self.assertEqual((rc, env["data"]["text"]), (0, "ok"))
            m_acl.assert_called_once()
            self.assertEqual(m_run.call_args.args[0][:3], ["py", "-m", "mesh"])
            self.assertIn("PYTHONPATH", m_run.call_args.kwargs["env"])


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class TestRealWindowsRunSuiteLock(unittest.TestCase):
    """Real Windows checks for scripts/run_suite.py suite LockFileEx and ACL setup."""

    def test_real_win32_check_and_suite_lock(self) -> None:
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "run_suite.py"), "--check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("syntax and runner OK", proc.stdout)

        with tempfile.TemporaryDirectory(prefix="mh-suite-win-", dir=plat_paths.temp_root()) as td:
            reg = os.path.join(td, "reg")
            fd, lock_path = run_suite._acquire_suite_lock(reg, "winreal")
            self.assertIsNotNone(fd)
            self.assertIsNotNone(lock_path)
            try:
                self.assertTrue(os.path.isfile(lock_path))
            finally:
                with mock.patch.object(run_suite, "_run_reap"):
                    run_suite._release_and_cleanup_suite(fd, lock_path, REPO_ROOT, {})
            self.assertFalse(os.path.exists(lock_path))


class TestRunSuiteSignalsAndTripwire(unittest.TestCase):
    """Cross-platform tests for SIGBREAK signal handling and tripwire trailing newlines."""

    def test_verify_tripwire_clean_newline_handling(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            suite_log = os.path.join(td, "suite.log")
            default_log = os.path.join(td, "default.log")
            with open(suite_log, "w", encoding="utf-8") as fh:
                fh.write("triggered-no-newline")
            out = io.StringIO()
            self.assertFalse(run_suite.verify_tripwire_clean(suite_log, default_log, out=out))
            self.assertTrue(out.getvalue().endswith("\n"), repr(out.getvalue()))
            self.assertFalse(out.getvalue().endswith("\n\n"), repr(out.getvalue()))

            with open(suite_log, "w", encoding="utf-8") as fh:
                fh.write("triggered-with-newline\n")
            out2 = io.StringIO()
            self.assertFalse(run_suite.verify_tripwire_clean(suite_log, default_log, out=out2))
            self.assertTrue(out2.getvalue().endswith("triggered-with-newline\n"), repr(out2.getvalue()))
            self.assertFalse(out2.getvalue().endswith("\n\n"), repr(out2.getvalue()))

    def test_run_suite_traps_and_restores_sigbreak_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            installed = {}
            restored = []
            prev_term = object()
            prev_break = object()

            def fake_getsignal(sig):
                return prev_term if sig == 15 else prev_break

            def fake_signal(sig, handler):
                if callable(handler) and handler not in (prev_term, prev_break):
                    installed[sig] = handler
                else:
                    restored.append((sig, handler))

            def raise_sigbreak(*_args, **_kwargs):
                installed[21](21, None)
                return SimpleNamespace(returncode=0)

            out = io.StringIO()
            err = io.StringIO()
            with mock.patch.object(run_suite.signal, "SIGTERM", 15, create=True), \
                    mock.patch.object(run_suite.signal, "SIGBREAK", 21, create=True), \
                    mock.patch.object(run_suite.signal, "getsignal", side_effect=fake_getsignal), \
                    mock.patch.object(run_suite.signal, "signal", side_effect=fake_signal), \
                    mock.patch.object(run_suite, "_acquire_suite_lock", return_value=(123, os.path.join(td, "s.lock"))), \
                    mock.patch.object(run_suite, "_run_reap"), \
                    mock.patch.object(run_suite, "_release_and_cleanup_suite") as m_cleanup, \
                    mock.patch.object(run_suite.subprocess, "run", side_effect=raise_sigbreak):
                with self.assertRaises(SystemExit) as ctx:
                    run_suite.run_suite(repo_root=td, tiers=(("Unit", "tests/unit"),), stdout=out, stderr=err)
            self.assertEqual(ctx.exception.code, 130)
            m_cleanup.assert_called_once()
            self.assertIn((21, prev_break), restored)
            self.assertIn((15, prev_term), restored)


if __name__ == "__main__":
    unittest.main()
