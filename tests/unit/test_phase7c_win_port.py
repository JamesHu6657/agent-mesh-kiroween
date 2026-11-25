"""Phase 7c (T13) Windows integration CI enablement and harness unit test coverage.

Covers every Win32 branch and helper added in T13 via mocked Win32 APIs on
Linux (``HungIpcServer``, ``assert_private_mode``, ``fake_peer_creds``,
``cli_popen_argv``, ``EXPECTED_CRASH_RETURNCODE``, ``_write_python_stub``),
verifies simulated Win32 import hygiene across all enabled unit + integration
modules, checks CI workflow / ``WIN_UNIT_SKIPS`` invariants, and includes
real-Windows verification tests under ``@unittest.skipUnless(sys.platform == "win32", ...)``.
"""

from __future__ import annotations

import glob
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mesh.platform import _win32_ctypes as w32, ipc as plat_ipc, paths as plat_paths, perms as plat_perms
from tests import helpers as helpers_mod, stubs as stubs_mod
from tests.win_sim import SIMULATED_WIN32_PRELUDE


class _FakePipeKernel32:
    """In-memory Win32 Kernel32 double for testing ``HungIpcServer`` Named Pipe loops."""

    def __init__(self, *, connect_behaviors: list[str] | None = None) -> None:
        self.next_handle = 0x5000
        self.next_event = 0x7000
        self.open_pipes: set[int] = set()
        self.open_events: set[int] = set()
        self.disconnected: list[int] = []
        self.canceled: list[int] = []
        self.closed: list[int] = []
        self.stop_signaled = False
        self.created_names: list[str] = []
        self.first_flags: list[int] = []
        self.overlapped_waits: list[tuple[int, bool]] = []
        self.last_error = 0
        self._behaviors = list(connect_behaviors or ["pending_connect", "pending_forever"])
        self._conn_index = 0

    def get_last_error(self) -> int:
        return self.last_error

    def LocalFree(self, ptr: object) -> int:
        return 0

    def CreateEventW(self, _sa: object, _manual: bool, _initial: bool, _name: object) -> int:
        h = self.next_event
        self.next_event += 4
        self.open_events.add(h)
        return h

    def CreateNamedPipeW(
        self,
        name: str,
        open_mode: int,
        _pipe_mode: int,
        _max_inst: int,
        _out_sz: int,
        _in_sz: int,
        _timeout: int,
        _sa: object,
    ) -> int:
        self.created_names.append(name)
        self.first_flags.append(open_mode)
        h = self.next_handle
        self.next_handle += 4
        self.open_pipes.add(h)
        return h

    def ConnectNamedPipe(self, h_pipe: int, _ov_ptr: object) -> int:
        if self._conn_index < len(self._behaviors):
            mode = self._behaviors[self._conn_index]
        else:
            mode = "pending_forever"
        self._conn_index += 1
        self._current_mode = mode
        if mode == "immediate_true":
            self.last_error = 0
            return 1
        if mode == "already_connected":
            self.last_error = w32.ERROR_PIPE_CONNECTED
            return 0
        if mode == "no_data":
            self.last_error = w32.ERROR_NO_DATA
            return 0
        self.last_error = w32.ERROR_IO_PENDING
        return 0

    def WaitForMultipleObjects(self, count: int, handles: object, _wait_all: bool, _ms: int) -> int:
        assert count == 2
        mode = getattr(self, "_current_mode", "pending_forever")
        if mode == "pending_connect" and not self.stop_signaled:
            return w32.WAIT_OBJECT_0
        deadline = time.monotonic() + 2.0
        while not self.stop_signaled and time.monotonic() < deadline:
            time.sleep(0.005)
        return w32.WAIT_OBJECT_0 + 1

    def GetOverlappedResult(self, _h: int, _ov: object, nbytes_ptr: object, _wait: bool) -> int:
        _ = nbytes_ptr
        self.overlapped_waits.append((_h, bool(_wait)))
        return 1

    def CancelIoEx(self, h_pipe: int, _ov: object) -> int:
        self.canceled.append(h_pipe)
        return 1

    def DisconnectNamedPipe(self, h_pipe: int) -> int:
        self.disconnected.append(h_pipe)
        return 1

    def SetEvent(self, _ev: int) -> int:
        self.stop_signaled = True
        return 1

    def CloseHandle(self, h: int) -> int:
        self.open_pipes.discard(h)
        self.open_events.discard(h)
        self.closed.append(h)
        return 1


class _FakePipeAdvapi32:
    """In-memory Advapi32 double supplying a deterministic owner SID + SD."""

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(
        self, _sddl: str, _rev: int, sd_out: object, _sz_out: object
    ) -> int:
        import ctypes

        ptr = ctypes.cast(sd_out, ctypes.POINTER(ctypes.c_void_p))
        ptr.contents.value = 0x123400
        return 1


class Phase7cWinPortTests(unittest.TestCase):
    """Mocked-Win32 and repository hygiene checks for T13."""

    def test_simulated_win32_import_hygiene_enabled_unit_and_integration(self) -> None:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        unit_mods = sorted(
            "tests.unit." + os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(repo_root, "tests", "unit", "test_*.py"))
        )
        int_mods = sorted(
            "tests.integration." + os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(repo_root, "tests", "integration", "test_*.py"))
        )
        rec_mods = sorted(
            "tests.recovery." + os.path.splitext(os.path.basename(p))[0]
            for p in glob.glob(os.path.join(repo_root, "tests", "recovery", "test_*.py"))
        )
        all_mods = unit_mods + int_mods + rec_mods
        code = (
            "import asyncio, os, subprocess, sys, tempfile\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            + SIMULATED_WIN32_PRELUDE
            + f"import importlib\n"
            + f"mods = {all_mods!r}\n"
            + "bad = []\n"
            + "for m in mods:\n"
            + "    try:\n"
            + "        importlib.import_module(m)\n"
            + "    except BaseException as exc:\n"
            + "        bad.append((m, type(exc).__name__, str(exc)))\n"
            + "if bad:\n"
            + "    for item in bad: print('FAIL:', item)\n"
            + "    sys.exit(1)\n"
            + "print(f'OK count={len(mods)}')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"OK count={len(all_mods)}", proc.stdout)

    @unittest.skipIf(not (pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").is_file(), "CI workflow not included in this snapshot")
    def test_win_ci_workflow_and_win_skips_invariants(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        ci_text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for expected in (
            "windows-latest",
            'MESH_TEST_JOBS: "6"',
            "Add-MpPreference -ExclusionPath $env:TEMP",
            "continue-on-error: true",
            "python tests/run_tier.py tests/unit",
        ):
            self.assertIn(expected, ci_text)

        self.assertFalse((repo_root / "tests" / "win_skips.py").exists())

    def test_expected_crash_and_peer_creds_and_assert_private_mode_win32_mocked(self) -> None:
        self.assertEqual(helpers_mod.EXPECTED_CRASH_RETURNCODE, -signal.SIGKILL if sys.platform != "win32" else 137)

        with mock.patch.object(helpers_mod.sys, "platform", "win32"):
            self.assertEqual(helpers_mod.fake_peer_creds(4321), (4321, 0, 0))
            with mock.patch.object(helpers_mod.plat_perms, "verify_private_owner_and_perms") as m_ver:
                helpers_mod.assert_private_mode(self, r"C:\tmp\tok", is_dir=False, posix_mode=0o600)
                helpers_mod.assert_private_mode(self, r"C:\tmp\dir", is_dir=True, posix_mode=0o700)
            self.assertEqual(
                m_ver.call_args_list,
                [
                    mock.call(r"C:\tmp\tok", is_dir=False),
                    mock.call(r"C:\tmp\dir", is_dir=True),
                ],
            )

        with mock.patch.object(helpers_mod.sys, "platform", "linux"), \
                mock.patch.object(helpers_mod.os, "getuid", return_value=1000, create=True), \
                mock.patch.object(helpers_mod.os, "getgid", return_value=1000, create=True), \
                mock.patch.object(os, "getuid", return_value=1000, create=True), \
                mock.patch.object(os, "getgid", return_value=1000, create=True):
            self.assertEqual(helpers_mod.fake_peer_creds(4321), (4321, os.getuid(), os.getgid()))
            with tempfile.TemporaryDirectory() as td:
                f = os.path.join(td, "secret.tok")
                with open(f, "w", encoding="utf-8") as fp:
                    fp.write("x")
                os.chmod(f, 0o600)
                with mock.patch.object(
                    helpers_mod.os,
                    "stat",
                    return_value=os.stat_result((0o100600, 1, 1, 1, 1000, 1000, 1, 0, 0, 0)),
                ):
                    helpers_mod.assert_private_mode(self, f, is_dir=False, posix_mode=0o600)

    def test_cli_popen_argv_win32_mocked_direct_and_custom(self) -> None:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        fake_h = mock.MagicMock()
        fake_h.home = r"C:\Users\ci\AppData\Local\Temp\mh-1"
        fake_h._uses_default_bin_mesh.return_value = True
        fake_h.get_env.return_value = {"MESH_TESTING": "1"}
        fake_h._with_direct_pythonpath.side_effect = lambda e: e.update({"PYTHONPATH": repo_root}) or e

        with mock.patch.object(helpers_mod.sys, "platform", "win32"):
            cmd, env = helpers_mod.cli_popen_argv(fake_h, "task", "keepalive", "tsk_1")
            self.assertEqual(cmd[:6], [sys.executable, "-P", "-s", "-m", "mesh", "--home"])
            self.assertEqual(cmd[6:], [fake_h.home, "task", "keepalive", "tsk_1"])
            self.assertEqual(env["PYTHONPATH"], repo_root)

            fake_h._uses_default_bin_mesh.return_value = False
            fake_h.bin_mesh = r"C:\custom\mesh.cmd"
            cmd2, _ = helpers_mod.cli_popen_argv(fake_h, "status")
            self.assertEqual(cmd2, [r"C:\custom\mesh.cmd", "--home", fake_h.home, "status"])

    def test_hung_ipc_server_win32_named_pipe_accept_and_close_mocked(self) -> None:
        fake_k32 = _FakePipeKernel32(connect_behaviors=["pending_connect", "pending_forever"])
        fake_advapi = _FakePipeAdvapi32()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-0123456789abcdef0123456789abcdef"

        with mock.patch.object(plat_perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1000", "S-1-5-21-1000")):
            srv = helpers_mod.HungIpcServer(
                pipe_name,
                k32=fake_k32,
                advapi=fake_advapi,
                get_last_error_fn=fake_k32.get_last_error,
            )
            try:
                self.assertTrue(srv.accepted.wait(2.0), "HungIpcServer failed to accept mocked pipe connection")
                deadline = time.monotonic() + 2.0
                while len(fake_k32.created_names) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(fake_k32.created_names), 2)
                self.assertTrue(fake_k32.first_flags[0] & w32.FILE_FLAG_FIRST_PIPE_INSTANCE)
                self.assertFalse(fake_k32.first_flags[1] & w32.FILE_FLAG_FIRST_PIPE_INSTANCE)
            finally:
                srv.close()

        self.assertTrue(fake_k32.stop_signaled)
        self.assertEqual(len(fake_k32.disconnected), 1)
        self.assertGreaterEqual(len(fake_k32.canceled), 1)
        self.assertIn((fake_k32.canceled[0], True), fake_k32.overlapped_waits)
        self.assertEqual(fake_k32.open_pipes, set())
        self.assertEqual(fake_k32.open_events, set())

    def test_hung_ipc_server_init_pipe_failure_closes_stop_event_mocked(self) -> None:
        fake_k32 = _FakePipeKernel32()
        fake_advapi = _FakePipeAdvapi32()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-11111111111111111111111111111111"
        with mock.patch.object(plat_ipc, "_create_named_pipe_instance_win", side_effect=OSError("pipe failed")):
            with self.assertRaises(OSError):
                helpers_mod.HungIpcServer(
                    pipe_name,
                    k32=fake_k32,
                    advapi=fake_advapi,
                    get_last_error_fn=fake_k32.get_last_error,
                )
        self.assertEqual(fake_k32.open_events, set())

    def test_hung_ipc_server_close_keeps_stop_event_if_thread_alive_mocked(self) -> None:
        fake_k32 = _FakePipeKernel32(connect_behaviors=["pending_forever"])
        fake_advapi = _FakePipeAdvapi32()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-22222222222222222222222222222222"
        with mock.patch.object(plat_perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1000", "S-1-5-21-1000")):
            srv = helpers_mod.HungIpcServer(
                pipe_name,
                k32=fake_k32,
                advapi=fake_advapi,
                get_last_error_fn=fake_k32.get_last_error,
            )
            fake_k32.SetEvent(0)
            srv.thread.join(timeout=2.0)
            stop_ev = srv._stop_ev
            with mock.patch.object(srv.thread, "is_alive", return_value=True):
                srv.close()
            self.assertIn(stop_ev, fake_k32.open_events)
            fake_k32.CloseHandle(stop_ev)

    def test_read_log_tail_strips_cr_only_on_win32(self) -> None:
        from mesh.daemon.handlers import run as run_handlers

        with tempfile.TemporaryDirectory() as td:
            log_path = os.path.join(td, "stdout.log")
            with open(log_path, "wb") as f:
                f.write(b"line1\r\nline2\r\n")
            with mock.patch.object(run_handlers.sys, "platform", "win32"):
                data_win = run_handlers._read_log_tail(log_path, tail=10)
            self.assertEqual(data_win["lines"], ["line1", "line2"])
            with mock.patch.object(run_handlers.sys, "platform", "linux"):
                data_linux = run_handlers._read_log_tail(log_path, tail=10)
            self.assertEqual(data_linux["lines"], ["line1\r", "line2\r"])

    def test_hung_ipc_server_already_connected_pipe_mocked(self) -> None:
        fake_k32 = _FakePipeKernel32(connect_behaviors=["already_connected", "pending_forever"])
        fake_advapi = _FakePipeAdvapi32()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-fedcba9876543210fedcba9876543210"

        with mock.patch.object(plat_perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1000", "S-1-5-21-1000")):
            srv = helpers_mod.HungIpcServer(
                pipe_name,
                k32=fake_k32,
                advapi=fake_advapi,
                get_last_error_fn=fake_k32.get_last_error,
            )
            try:
                self.assertTrue(srv.accepted.wait(2.0))
                self.assertEqual(len(srv.conns), 1)
            finally:
                srv.close()
        self.assertEqual(fake_k32.open_pipes, set())
        self.assertEqual(fake_k32.open_events, set())

    def test_hung_ipc_server_no_data_continues_to_next_pipe_mocked(self) -> None:
        fake_k32 = _FakePipeKernel32(connect_behaviors=["no_data", "already_connected", "pending_forever"])
        fake_advapi = _FakePipeAdvapi32()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-00112233445566778899aabbccddeeff"

        with mock.patch.object(plat_perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1000", "S-1-5-21-1000")):
            srv = helpers_mod.HungIpcServer(
                pipe_name,
                k32=fake_k32,
                advapi=fake_advapi,
                get_last_error_fn=fake_k32.get_last_error,
            )
            try:
                self.assertTrue(srv.accepted.wait(2.0), "HungIpcServer exited on ERROR_NO_DATA instead of continuing")
                self.assertEqual(len(srv.conns), 1)
            finally:
                srv.close()
        self.assertEqual(fake_k32.open_pipes, set())
        self.assertEqual(fake_k32.open_events, set())

    def test_write_python_stub_and_stub_path_win32_mocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(stubs_mod.sys, "platform", "win32"):
                self.assertEqual(stubs_mod.stub_path(td, "codex"), os.path.join(td, "codex.cmd"))
                cmd_file = stubs_mod._write_python_stub(td, "codex", "import sys; print('ok')\n")
                self.assertEqual(cmd_file, os.path.join(td, "codex.cmd"))
                self.assertTrue(os.path.isfile(os.path.join(td, "codex.py")))
                self.assertTrue(os.path.isfile(cmd_file))
                with open(cmd_file, "r", encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("%~dp0codex.py", content)
                self.assertIn("%*", content)


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class RealWindowsPhase7cTests(unittest.TestCase):
    """Real Win32 Named Pipe / DACL checks for Phase 7c helpers."""

    def test_real_hung_ipc_server_accepts_and_closes_named_pipe(self) -> None:
        home = tempfile.mkdtemp(prefix="mh-win7c-", dir=plat_paths.temp_root())
        self.addCleanup(helpers_mod.rmtree_deep, home)
        plat_perms.set_private_dir_acl(home)
        endpoint = plat_ipc.daemon_endpoint_path(home)
        srv = helpers_mod.HungIpcServer(endpoint)
        try:
            self.assertTrue(plat_ipc.probe_daemon_endpoint(home, timeout=5.0))
            client = plat_ipc.connect_endpoint(endpoint, timeout=5.0)
            try:
                self.assertTrue(srv.accepted.wait(5.0))
            finally:
                client.close()
        finally:
            srv.close()

    def test_real_assert_private_mode_and_inaccessible_toggle(self) -> None:
        with tempfile.TemporaryDirectory(dir=plat_paths.temp_root()) as td:
            f = os.path.join(td, "tok.txt")
            with open(f, "w", encoding="utf-8") as fp:
                fp.write("tok\n")
            plat_perms.set_private_file_acl(f)
            helpers_mod.assert_private_mode(self, f, is_dir=False)
            helpers_mod.set_inaccessible_for_test(f)
            with self.assertRaises(PermissionError):
                with open(f, "rb"):
                    pass
            helpers_mod.restore_accessible_for_test(f)
            with open(f, "r", encoding="utf-8") as fp:
                self.assertEqual(fp.read(), "tok\n")


if __name__ == "__main__":
    unittest.main()
