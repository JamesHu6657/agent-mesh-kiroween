"""Phase 7b (T12) cross-platform and Windows port tests.

Covers every Win32 branch in ``scripts/coord/reap_daemons.py``, ``tests/gitfixture.py``,
``tests/helpers.py`` (``MeshHarness``, ``rmtree_deep``, test permission helpers), and
``mesh/platform/{_win32,proc,perms}.py`` via mocked Win32 APIs on Linux, plus real
Windows end-to-end tests under ``@unittest.skipUnless(sys.platform == "win32", ...)``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import io
import json
import ntpath
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from mesh.platform import _posix as plat_posix, _win32 as plat_win32, _win32_ctypes as w32
from mesh.platform import paths as plat_paths, perms as plat_perms, proc as plat_proc
from scripts.coord import reap_daemons as reap
from tests import gitfixture as gf_mod, helpers as helpers_mod
from tests.gitfixture import GitFixture, GitMeshHarness
from tests.helpers import (
    MeshHarness,
    restore_accessible_for_test,
    rmtree_deep,
    set_inaccessible_for_test,
    set_world_readable_for_test,
)
from tests.win_sim import SIMULATED_WIN32_PRELUDE

MESH_PROC_LOCK = "shared"


def _encode_cmd_bytes(text: str) -> bytes:
    return (text + "\x00").encode("utf-16-le")


class _FakeCmdProc:
    def __init__(
        self,
        pid: int,
        *,
        cmdline: str = "",
        cmd_len: int | None = None,
        cmd_null: bool = False,
        creation_time: int = 134000000000000000,
        alive: bool = True,
        wow64: bool = False,
        partial_params_copy: bool = False,
        access_denied_open: bool = False,
        access_denied_vm: bool = False,
        access_denied_term: bool = False,
        die_after_wait_checks: int | None = None,
    ) -> None:
        self.pid = pid
        self.cmdline = cmdline
        self.cmd_len = cmd_len
        self.cmd_null = cmd_null
        self.creation_time = creation_time
        self.alive = alive
        self.wow64 = wow64
        self.partial_params_copy = partial_params_copy
        self.access_denied_open = access_denied_open
        self.access_denied_vm = access_denied_vm
        self.access_denied_term = access_denied_term
        self.die_after_wait_checks = die_after_wait_checks


class _FakeWin32HarnessKernel:
    """In-memory kernel32 + ntdll mock for PEB CommandLine and OpenProcess/TerminateProcess."""

    def __init__(self, procs: dict[int, _FakeCmdProc], *, current_pid: int = 999) -> None:
        self.procs = procs
        self.current_pid = current_pid
        self.last_error = 0
        self.next_handle = 0x2000
        self.open_handles: list[int] = []
        self.closed_handles: list[int] = []
        self.handles: dict[int, tuple[_FakeCmdProc, int]] = {}
        self.terminated: list[tuple[int, int, int]] = []
        self.wait_calls: list[tuple[int, int]] = []
        self.mem: dict[int, dict[int, bytes]] = {}
        for pid, entry in procs.items():
            self._build_mem(pid, entry)

    def _build_mem(self, pid: int, entry: _FakeCmdProc) -> None:
        regions: dict[int, bytes] = {}
        raw_cmd = _encode_cmd_bytes(entry.cmdline) if entry.cmdline else b""
        rep_len = (len(entry.cmdline) * 2) if entry.cmd_len is None else entry.cmd_len
        cmd_addr = 0 if entry.cmd_null else 0x60000000
        if entry.wow64:
            peb_addr = 0x00400000
            params_addr = 0x00500000
            regions[peb_addr] = bytes(w32.PEB32(ProcessParameters=params_addr))
            params = w32.RTL_USER_PROCESS_PARAMETERS32(
                CommandLine=w32.UNICODE_STRING32(
                    Length=rep_len,
                    MaximumLength=max(rep_len, len(raw_cmd)),
                    Buffer=cmd_addr,
                )
            )
            full_params = bytes(params)
            if entry.partial_params_copy:
                # Provide only through CommandLine so full-struct read gets ERROR_PARTIAL_COPY.
                end = w32.RTL_USER_PROCESS_PARAMETERS32.CommandLine.offset + ctypes.sizeof(w32.UNICODE_STRING32)
                regions[params_addr] = full_params[:end]
            else:
                regions[params_addr] = full_params
        else:
            peb_addr = 0x00007FF700100000
            params_addr = 0x0000018000200000
            regions[peb_addr] = bytes(w32.PEB64(ProcessParameters=params_addr))
            params = w32.RTL_USER_PROCESS_PARAMETERS64(
                CommandLine=w32.UNICODE_STRING64(
                    Length=rep_len,
                    MaximumLength=max(rep_len, len(raw_cmd)),
                    Buffer=cmd_addr,
                )
            )
            full_params = bytes(params)
            if entry.partial_params_copy:
                end = w32.RTL_USER_PROCESS_PARAMETERS64.CommandLine.offset + ctypes.sizeof(w32.UNICODE_STRING64)
                regions[params_addr] = full_params[:end]
            else:
                regions[params_addr] = full_params
        if cmd_addr and raw_cmd:
            regions[cmd_addr] = raw_cmd
        self.mem[pid] = regions

    def get_last_error(self) -> int:
        return self.last_error

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> Any:
        _ = inherit
        entry = self.procs.get(pid)
        if entry is None:
            self.last_error = w32.ERROR_INVALID_PARAMETER
            return None
        if entry.access_denied_open:
            self.last_error = w32.ERROR_ACCESS_DENIED
            return None
        self.next_handle += 4
        h = self.next_handle
        self.open_handles.append(h)
        self.handles[h] = (entry, access)
        self.last_error = 0
        return h

    def CloseHandle(self, handle: int) -> int:
        self.closed_handles.append(handle)
        self.handles.pop(handle, None)
        return 1

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        self.wait_calls.append((int(handle), int(timeout_ms)))
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return w32.WAIT_FAILED
        entry, _ = item
        if entry.die_after_wait_checks is not None:
            entry.die_after_wait_checks -= 1
            if entry.die_after_wait_checks <= 0:
                entry.alive = False
        return w32.WAIT_TIMEOUT if entry.alive else w32.WAIT_OBJECT_0

    def GetExitCodeProcess(self, handle: int, out_ptr: Any) -> int:
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _ = item
        ctypes.cast(out_ptr, wintypes.LPDWORD)[0] = w32.STILL_ACTIVE if entry.alive else 0
        return 1

    def GetProcessTimes(self, handle: int, ct_ptr: Any, et_ptr: Any, kt_ptr: Any, ut_ptr: Any) -> int:
        _ = (et_ptr, kt_ptr, ut_ptr)
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _ = item
        ct = ctypes.cast(ct_ptr, ctypes.POINTER(w32.FILETIME)).contents
        ct.dwLowDateTime = entry.creation_time & 0xFFFFFFFF
        ct.dwHighDateTime = (entry.creation_time >> 32) & 0xFFFFFFFF
        return 1

    def TerminateProcess(self, handle: int, exit_code: int) -> int:
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _ = item
        if entry.access_denied_term:
            self.last_error = w32.ERROR_ACCESS_DENIED
            return 0
        self.terminated.append((handle, entry.pid, exit_code))
        entry.alive = False
        return 1

    def IsWow64Process2(self, handle: int, proc_m_ptr: Any, nat_m_ptr: Any) -> int:
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _ = item
        ctypes.cast(proc_m_ptr, ctypes.POINTER(ctypes.c_uint16))[0] = (
            w32.IMAGE_FILE_MACHINE_I386 if entry.wow64 else w32.IMAGE_FILE_MACHINE_UNKNOWN
        )
        ctypes.cast(nat_m_ptr, ctypes.POINTER(ctypes.c_uint16))[0] = 0x8664
        return 1

    def NtQueryInformationProcess(
        self, handle: int, info_class: int, buf_ptr: Any, buf_len: int, ret_ptr: Any
    ) -> int:
        _ = buf_len
        item = self.handles.get(handle)
        if item is None:
            return -1073741816
        entry, _ = item
        if info_class == w32.ProcessWow64Information:
            ctypes.cast(buf_ptr, ctypes.POINTER(ctypes.c_size_t))[0] = 0x00400000 if entry.wow64 else 0
            if ret_ptr:
                ctypes.cast(ret_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(ctypes.c_size_t)
            return w32.STATUS_SUCCESS
        if info_class == w32.ProcessBasicInformation:
            pbi = ctypes.cast(buf_ptr, ctypes.POINTER(w32.PROCESS_BASIC_INFORMATION)).contents
            pbi.PebBaseAddress = 0x00400000 if entry.wow64 else 0x00007FF700100000
            pbi.UniqueProcessId = entry.pid
            if ret_ptr:
                ctypes.cast(ret_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(
                    w32.PROCESS_BASIC_INFORMATION
                )
            return w32.STATUS_SUCCESS
        return w32.STATUS_INVALID_PARAMETER

    def RtlNtStatusToDosError(self, status: int) -> int:
        return w32.ERROR_ACCESS_DENIED if status == w32.STATUS_ACCESS_DENIED else w32.ERROR_INVALID_PARAMETER

    def ReadProcessMemory(
        self, handle: int, base_addr: int, buf_ptr: Any, size: int, nread_ptr: Any
    ) -> int:
        item = self.handles.get(handle)
        if item is None:
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, access = item
        if entry.access_denied_vm or not (access & w32.PROCESS_VM_READ):
            self.last_error = w32.ERROR_ACCESS_DENIED
            return 0
        for r_base, r_bytes in self.mem.get(entry.pid, {}).items():
            if r_base <= base_addr < r_base + len(r_bytes):
                off = base_addr - r_base
                if len(r_bytes) - off < size:
                    self.last_error = w32.ERROR_PARTIAL_COPY
                    return 0
                ctypes.memmove(buf_ptr, r_bytes[off : off + size], size)
                if nread_ptr:
                    ctypes.cast(nread_ptr, ctypes.POINTER(ctypes.c_size_t))[0] = size
                return 1
        self.last_error = w32.ERROR_PARTIAL_COPY
        return 0


class SimulatedWin32ImportHygieneTests(unittest.TestCase):
    def test_t12_modules_import_under_win32_without_fcntl_or_posix(self) -> None:
        script = (
            "import asyncio, shutil, subprocess, tempfile\n"
            + SIMULATED_WIN32_PRELUDE
            + "import scripts.coord.reap_daemons\n"
            + "import tests.helpers\n"
            + "import tests.gitfixture\n"
            + "import tests.unit.test_gitfixture\n"
            + "import tests.unit.test_reap_daemons\n"
            + "import tests.unit.test_repos\n"
            + "import tests.unit.test_repos_cas\n"
            + "import tests.unit.test_merge\n"
            + "print('OK')\n"
        )
        cp = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(cp.returncode, 0, f"stderr={cp.stderr}\nstdout={cp.stdout}")
        self.assertIn("OK", cp.stdout)

    def test_reap_daemons_unskipped_and_runs_policy_under_win32(self) -> None:
        from tests.unit import test_reap_daemons

        win_skips_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "win_skips.py",
        )
        self.assertFalse(os.path.exists(win_skips_file))

        script = (
            "import asyncio, shutil, subprocess, tempfile\n"
            + SIMULATED_WIN32_PRELUDE
            + "import tests.unit.test_reap_daemons as m\n"
            + "assert not getattr(m.TestPolicy, '__unittest_skip__', False)\n"
            + "for cls in (m.RegistryFixture, m.TestRegistry, m.TestGenerationsAndEdges, "
            + "m.TestHarnessPidFileFallback, m.TestPidFileDaemonWithoutIdentity):\n"
            + "    assert getattr(cls, '__unittest_skip__', False), cls.__name__\n"
            + "print('OK')\n"
        )
        cp = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(cp.returncode, 0, f"stderr={cp.stderr}\nstdout={cp.stdout}")
        self.assertIn("OK", cp.stdout)

        with mock.patch.object(test_reap_daemons.sys, "platform", "win32"), \
                mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(test_reap_daemons.os, "path", ntpath), \
                mock.patch.object(reap.os, "path", ntpath), \
                mock.patch.object(test_reap_daemons.plat_paths, "temp_root", return_value=r"C:\Temp"), \
                mock.patch.object(reap.plat_paths, "temp_root", return_value=r"C:\Temp"), \
                mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                mock.patch.object(ntpath, "realpath", side_effect=lambda p: ntpath.normpath(p)):
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_reap_daemons.TestPolicy)
            res = unittest.TestResult()
            suite.run(res)
        self.assertEqual(res.errors, [], res.errors)
        self.assertEqual(res.failures, [], res.failures)
        self.assertEqual(res.testsRun, 6)


class PlatformWin32CmdlineTests(unittest.TestCase):
    def test_parse_win_command_line_ms_crt_rules(self) -> None:
        self.assertEqual(plat_win32.parse_win_command_line(""), [])
        self.assertEqual(plat_win32.parse_win_command_line("   \t  "), [])
        self.assertEqual(
            plat_win32.parse_win_command_line(
                r'"C:\Program Files\Python312\python.exe" -P -s -m mesh --home "C:\dir with spaces\\" daemon run'
            ),
            [
                r"C:\Program Files\Python312\python.exe",
                "-P",
                "-s",
                "-m",
                "mesh",
                "--home",
                "C:\\dir with spaces\\",
                "daemon",
                "run",
            ],
        )
        self.assertEqual(
            plat_win32.parse_win_command_line(r'py "" "a""b" "c\"d" e\\f'),
            ["py", "", 'a"b', 'c"d', r"e\\f"],
        )

    def test_get_proc_cmdline_win_64_wow64_partial_and_errors(self) -> None:
        k = _FakeWin32HarnessKernel(
            {
                10: _FakeCmdProc(10, cmdline=r'"C:\Py\python.exe" -P -s -m mesh --home C:\mh-1 daemon run'),
                11: _FakeCmdProc(11, cmdline=r'python3.12.exe -m mesh daemon run', wow64=True),
                12: _FakeCmdProc(12, cmdline=r'python.exe --home "C:\a b"', partial_params_copy=True),
                13: _FakeCmdProc(13, cmdline="", cmd_len=0),
                14: _FakeCmdProc(14, alive=False, cmdline="python.exe"),
                15: _FakeCmdProc(15, access_denied_open=True),
                16: _FakeCmdProc(16, cmdline="x", cmd_null=True),
                17: _FakeCmdProc(17, cmdline="x", cmd_len=plat_win32._MAX_CMDLINE_BYTES + 1),
            }
        )
        self.assertEqual(
            plat_win32.get_proc_cmdline_win(10, k32=k, ntdll=k, get_last_error_fn=k.get_last_error),
            [r"C:\Py\python.exe", "-P", "-s", "-m", "mesh", "--home", r"C:\mh-1", "daemon", "run"],
        )
        self.assertEqual(
            plat_win32.get_proc_cmdline_win(11, k32=k, ntdll=k, get_last_error_fn=k.get_last_error),
            ["python3.12.exe", "-m", "mesh", "daemon", "run"],
        )
        self.assertEqual(
            plat_win32.get_proc_cmdline_win(12, k32=k, ntdll=k, get_last_error_fn=k.get_last_error),
            ["python.exe", "--home", r"C:\a b"],
        )
        self.assertEqual(
            plat_win32.get_proc_cmdline_win(13, k32=k, ntdll=k, get_last_error_fn=k.get_last_error),
            [],
        )
        self.assertIsNone(plat_win32.get_proc_cmdline_win(14, k32=k, ntdll=k, get_last_error_fn=k.get_last_error))
        self.assertIsNone(plat_win32.get_proc_cmdline_win(9999, k32=k, ntdll=k, get_last_error_fn=k.get_last_error))
        with self.assertRaises(PermissionError):
            plat_win32.get_proc_cmdline_win(15, k32=k, ntdll=k, get_last_error_fn=k.get_last_error)
        with self.assertRaises(OSError):
            plat_win32.get_proc_cmdline_win(16, k32=k, ntdll=k, get_last_error_fn=k.get_last_error)
        with self.assertRaises(OSError):
            plat_win32.get_proc_cmdline_win(17, k32=k, ntdll=k, get_last_error_fn=k.get_last_error)
        self.assertEqual(sorted(k.closed_handles), sorted(k.open_handles))

    @unittest.skipUnless(sys.platform != "win32", "POSIX get_proc_cmdline check")
    def test_posix_get_proc_cmdline(self) -> None:
        self.assertEqual(plat_proc.get_proc_cmdline(os.getpid()) is not None, True)
        self.assertIsNone(plat_posix.get_proc_cmdline(99999999))


class TestPermissionHelpersMockedAndPosix(unittest.TestCase):
    def test_win32_inaccessible_and_restore_mocked(self) -> None:
        calls: list[tuple[str, Any]] = []

        class _FakeK32:
            def CloseHandle(self, h: int) -> int:
                calls.append(("close", h))
                return 1

        fake_k32 = _FakeK32()
        fake_adv = object()
        with mock.patch.object(plat_perms, "_open_path_for_security_win", return_value=(0x111, True)) as open_sec, \
                mock.patch.object(plat_perms, "_apply_sddl_to_handle_win") as apply_sddl:
            plat_perms.set_inaccessible_for_test_win(r"C:\mh\secret", advapi=fake_adv, k32=fake_k32)
            open_sec.assert_called_once()
            self.assertEqual(apply_sddl.call_args.args[:2], (0x111, "D:P"))

        calls.clear()
        chmods: list[tuple[str, int]] = []
        with mock.patch.object(plat_perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001")), \
                mock.patch.object(plat_perms, "_open_path_for_security_win", return_value=(0x222, False)), \
                mock.patch.object(plat_perms, "_apply_sddl_to_handle_win") as apply_sddl:
            plat_perms.restore_accessible_for_test_win(
                r"C:\mh\secret.db",
                advapi=fake_adv,
                k32=fake_k32,
                chmod_fn=lambda p, m: chmods.append((p, m)),
            )
            self.assertEqual(apply_sddl.call_args.args[0], 0x222)
            self.assertIn("D:P", apply_sddl.call_args.args[1])
            self.assertIn("S-1-5-21-1-1001", apply_sddl.call_args.args[1])
            self.assertEqual(chmods, [(r"C:\mh\secret.db", stat.S_IWRITE | stat.S_IREAD)])

    def test_cross_platform_test_perm_dispatch_under_mocked_win32(self) -> None:
        with mock.patch.object(plat_perms.sys, "platform", "win32"), \
                mock.patch.object(plat_perms, "set_inaccessible_for_test_win") as m_inacc, \
                mock.patch.object(plat_perms, "restore_accessible_for_test_win") as m_rest, \
                mock.patch.object(plat_perms, "grant_everyone_read_for_test_win") as m_world:
            set_inaccessible_for_test(r"C:\x")
            restore_accessible_for_test(r"C:\x")
            set_world_readable_for_test(r"C:\x")
            m_inacc.assert_called_once_with(r"C:\x")
            m_rest.assert_called_once_with(r"C:\x")
            m_world.assert_called_once_with(r"C:\x")

    def test_win32_open_path_for_security_nt_open_file_fallback_mocked(self) -> None:
        self.assertEqual(plat_perms._dos_path_to_nt_path_win(r"C:\a\b"), r"\??\C:\a\b")
        self.assertEqual(plat_perms._dos_path_to_nt_path_win(r"\\?\C:\a\b"), r"\??\C:\a\b")
        self.assertEqual(plat_perms._dos_path_to_nt_path_win(r"\\?\UNC\srv\sh\f"), r"\??\UNC\srv\sh\f")
        self.assertEqual(plat_perms._dos_path_to_nt_path_win(r"\\srv\sh\f"), r"\??\UNC\srv\sh\f")

        nt_calls: list[tuple[int, str, int, int]] = []
        closed: list[int] = []
        nt_status = {"code": 0}

        class _FakeK32:
            def CreateFileW(self, *_args: object) -> int:
                return w32.INVALID_HANDLE_VALUE

            def GetFileType(self, _h: object) -> int:
                return w32.FILE_TYPE_DISK

            def CloseHandle(self, h: int) -> int:
                closed.append(h)
                return 1

        class _FakeNtdll:
            def NtOpenFile(self, h_out: object, desired: int, oa_arg: object, _iosb: object, share: int, opts: int) -> int:
                oa = oa_arg._obj
                us = oa.ObjectName.contents
                nt_name = ctypes.string_at(us.Buffer, us.Length).decode("utf-16-le")
                nt_calls.append((desired, nt_name, share, opts))
                if nt_status["code"] < 0:
                    return nt_status["code"]
                h_out._obj.value = 0x777
                return 0

            def RtlNtStatusToDosError(self, _st: int) -> int:
                return w32.ERROR_ACCESS_DENIED

        fake_k32 = _FakeK32()
        fake_nt = _FakeNtdll()
        sec_mask = w32.READ_CONTROL | w32.WRITE_DAC

        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "secret.txt")
            d = os.path.join(td, "perm_dir")
            os.makedirs(d)
            with open(f, "w", encoding="utf-8") as fp:
                fp.write("s")

            h_file, is_dir = plat_perms._open_path_for_security_win(
                f,
                desired_access=sec_mask,
                reject_reparse=True,
                expected_is_dir=False,
                k32=fake_k32,
                ntdll=fake_nt,
                get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
            )
            self.assertEqual((h_file, is_dir), (0x777, False))
            self.assertEqual(nt_calls[-1][0], sec_mask)
            self.assertEqual(nt_calls[-1][1], plat_perms._dos_path_to_nt_path_win(f))
            self.assertEqual(
                nt_calls[-1][3],
                w32.FILE_OPEN_FOR_BACKUP_INTENT | w32.FILE_OPEN_REPARSE_POINT,
            )

            h_dir, dir_flag = plat_perms._open_path_for_security_win(
                d,
                desired_access=sec_mask,
                reject_reparse=True,
                expected_is_dir=True,
                k32=fake_k32,
                ntdll=fake_nt,
                get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
            )
            self.assertEqual((h_dir, dir_flag), (0x777, True))
            self.assertTrue(nt_calls[-1][3] & w32.FILE_DIRECTORY_FILE)

            # Non-security access must NOT use NtOpenFile fallback
            before = len(nt_calls)
            with self.assertRaises(OSError) as cm_nonsec:
                plat_perms._open_path_for_security_win(
                    f,
                    desired_access=sec_mask | 0x80000000,
                    reject_reparse=True,
                    expected_is_dir=False,
                    k32=fake_k32,
                    ntdll=fake_nt,
                    get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
                )
            self.assertEqual(getattr(cm_nonsec.exception, "winerror", None), w32.ERROR_ACCESS_DENIED)
            self.assertEqual(len(nt_calls), before)

            # NT failure propagates mapped DOS error
            nt_status["code"] = -1073741790
            with self.assertRaises(OSError) as cm_nt:
                plat_perms._open_path_for_security_win(
                    f,
                    desired_access=sec_mask,
                    reject_reparse=True,
                    expected_is_dir=False,
                    k32=fake_k32,
                    ntdll=fake_nt,
                    get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
                )
            self.assertEqual(getattr(cm_nt.exception, "winerror", None), w32.ERROR_ACCESS_DENIED)
            nt_status["code"] = 0

            if hasattr(os, "symlink"):
                sym = os.path.join(td, "sym.txt")
                os.symlink(f, sym)
                before_sym = len(nt_calls)
                with self.assertRaises(OSError) as cm:
                    plat_perms._open_path_for_security_win(
                        sym,
                        desired_access=sec_mask,
                        reject_reparse=True,
                        expected_is_dir=False,
                        k32=fake_k32,
                        ntdll=fake_nt,
                        get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
                    )
                self.assertEqual(cm.exception.errno, errno.ELOOP)
                self.assertEqual(len(nt_calls), before_sym)

    def test_p3_2_open_security_nt_fallback_reopenfile_handle_attrs_mocked(self) -> None:
        nt_calls: list[str] = []
        closed: list[int] = []
        reopen_flags = {"attrs": w32.FILE_ATTRIBUTE_NORMAL, "next_h": 0x777}

        class _FakeK32Reopen:
            def GetFileType(self, _h: object) -> int:
                return w32.FILE_TYPE_DISK

            def ReOpenFile(self, h_val: object, access: int, share: int, flags: int) -> int:
                self.reopen_call = (h_val, access, share, flags)
                return 0x888

            def GetFileInformationByHandle(self, h: object, info_ptr: object) -> int:
                info = ctypes.cast(info_ptr, ctypes.POINTER(w32.BY_HANDLE_FILE_INFORMATION)).contents
                info.dwFileAttributes = reopen_flags["attrs"]
                return 1

            def CloseHandle(self, h: int) -> int:
                closed.append(int(h))
                return 1

        class _FakeNtdll:
            def NtOpenFile(self, h_out: object, _desired: int, oa_arg: object, _iosb: object, _share: int, _opts: int) -> int:
                oa = oa_arg._obj
                us = oa.ObjectName.contents
                nt_calls.append(ctypes.string_at(us.Buffer, us.Length).decode("utf-16-le"))
                h_out._obj.value = reopen_flags["next_h"]
                return 0

            def RtlNtStatusToDosError(self, _st: int) -> int:
                return w32.ERROR_ACCESS_DENIED

        fake_k32 = _FakeK32Reopen()
        fake_nt = _FakeNtdll()
        lstat_calls = 0

        def _pre_open_only_lstat(_p: str) -> os.stat_result:
            nonlocal lstat_calls
            lstat_calls += 1
            if lstat_calls > 1:
                raise AssertionError("post-open os.lstat must not run when ReOpenFile succeeds")
            return os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0))

        with mock.patch.object(plat_perms.os, "lstat", side_effect=_pre_open_only_lstat):
            h_out, is_dir = plat_perms._open_security_nt_fallback_win(
                r"C:\mh\secret.db",
                desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
                reject_reparse=True,
                expected_is_dir=False,
                k32=fake_k32,
                ntdll=fake_nt,
                err_fn=lambda: 0,
            )
            self.assertEqual(h_out, 0x777)
            self.assertFalse(is_dir)
            self.assertEqual(lstat_calls, 1)
            self.assertEqual(closed, [0x888])
            self.assertEqual(fake_k32.reopen_call[1], w32.FILE_READ_ATTRIBUTES)
            self.assertTrue(fake_k32.reopen_call[3] & w32.FILE_FLAG_OPEN_REPARSE_POINT)

            lstat_calls = 0
            closed.clear()
            reopen_flags["attrs"] = w32.FILE_ATTRIBUTE_REPARSE_POINT
            with self.assertRaises(OSError) as ctx_rep:
                plat_perms._open_security_nt_fallback_win(
                    r"C:\mh\raced_junc",
                    desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
                    reject_reparse=True,
                    expected_is_dir=False,
                    k32=fake_k32,
                    ntdll=fake_nt,
                    err_fn=lambda: 0,
                )
            self.assertEqual(ctx_rep.exception.errno, errno.ELOOP)
            self.assertEqual(lstat_calls, 1)
            self.assertEqual(sorted(closed), [0x777, 0x888])

    def test_win32_lead_and_worktree_paths_use_normalized_separators(self) -> None:
        from mesh.core import repos as repos_mod, workspaces as ws_mod

        with mock.patch.object(ws_mod.os, "path", ntpath), \
                mock.patch.object(ws_mod.os, "sep", "\\"), \
                mock.patch.object(repos_mod.os, "path", ntpath), \
                mock.patch.object(repos_mod.sys, "platform", "win32"):
            self.assertEqual(
                ws_mod.compute_lead_workspace_path(r"C:\tmp\run", "prj1", "agt_lead1"),
                "C:\\tmp\\run\\prj1\\lead\\agt_lead1\\",
            )
            self.assertEqual(
                ws_mod.compute_task_workspace_path(r"C:\tmp\run", "prj1", "t1"),
                "C:\\tmp\\run\\prj1\\tasks\\t1\\",
            )
            self.assertEqual(
                repos_mod.lead_repo_worktree_path(r"C:\tmp\run", "prj1", "agt_lead1"),
                r"C:\tmp\run\prj1\lead\agt_lead1\repo",
            )

            fake_porcelain = (
                "worktree C:/tmp/wt/R\0HEAD abcdef\0branch refs/heads/feat/x\0\0"
            )
            fake_proc = subprocess.CompletedProcess(
                args=["git"], returncode=0, stdout=fake_porcelain, stderr=""
            )
            with mock.patch.object(repos_mod, "run_git", return_value=fake_proc):
                self.assertEqual(
                    repos_mod._worktree_for_branch(r"C:\tmp\repo", "feat/x"),
                    r"C:\tmp\wt\R",
                )

    @unittest.skipUnless(sys.platform != "win32", "POSIX chmod checks")
    def test_posix_test_permission_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "sub")
            f = os.path.join(td, "tok")
            os.makedirs(d, mode=0o700)
            with open(f, "w", encoding="utf-8") as fp:
                fp.write("t\n")
            os.chmod(f, 0o600)

            set_inaccessible_for_test(d)
            set_inaccessible_for_test(f)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o000)
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o000)

            restore_accessible_for_test(d)
            restore_accessible_for_test(f)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)

            set_world_readable_for_test(d)
            set_world_readable_for_test(f)
            self.assertEqual(os.stat(d).st_mode & 0o777, 0o755)
            self.assertEqual(os.stat(f).st_mode & 0o777, 0o644)


class ReapDaemonsWin32MockedTests(unittest.TestCase):
    def test_registry_dir_prepare_registry_and_process_stat_win32(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "temp_root", return_value=td), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(reap.registry_dir(), os.path.join(td, "mesh-test-daemons"))

            reg = os.path.join(td, "reg")
            closed: list[int] = []

            class _FakeK32:
                def CloseHandle(self, h: int) -> int:
                    closed.append(h)
                    return 1

            fake_k32 = _FakeK32()
            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                    mock.patch.object(w32, "get_kernel32", return_value=fake_k32), \
                    mock.patch.object(w32, "get_advapi32", return_value=object()), \
                    mock.patch.object(
                        plat_perms,
                        "get_current_user_and_owner_sids_win",
                        return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001"),
                    ), \
                    mock.patch.object(plat_perms, "_open_path_for_security_win", return_value=(0x333, True)) as m_open, \
                    mock.patch.object(plat_perms, "_inspect_handle_security_win", return_value=("S-1-5-21-1-1001", [])) as m_insp, \
                    mock.patch.object(plat_perms, "_apply_sddl_to_handle_win") as m_apply:
                reap.prepare_registry(reg)
                self.assertTrue(os.path.isdir(reg))
                m_open.assert_called_once()
                self.assertEqual(m_open.call_args.kwargs["desired_access"], w32.READ_CONTROL | w32.WRITE_DAC)
                self.assertTrue(m_open.call_args.kwargs["reject_reparse"])
                self.assertTrue(m_open.call_args.kwargs["expected_is_dir"])
                self.assertEqual(m_insp.call_args.args[0], 0x333)
                self.assertEqual(m_apply.call_args.args[0], 0x333)
                self.assertEqual(closed, [0x333])

            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=True):
                with self.assertRaises(ValueError):
                    reap.prepare_registry(reg)

            closed.clear()
            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                    mock.patch.object(w32, "get_kernel32", return_value=fake_k32), \
                    mock.patch.object(w32, "get_advapi32", return_value=object()), \
                    mock.patch.object(
                        plat_perms,
                        "get_current_user_and_owner_sids_win",
                        return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001"),
                    ), \
                    mock.patch.object(plat_perms, "_open_path_for_security_win", return_value=(0x444, True)), \
                    mock.patch.object(plat_perms, "_inspect_handle_security_win", return_value=("S-1-5-21-999-9999", [])), \
                    mock.patch.object(plat_perms, "_apply_sddl_to_handle_win") as m_apply:
                with self.assertRaises(ValueError):
                    reap.prepare_registry(reg)
                m_apply.assert_not_called()
                self.assertEqual(closed, [0x444])

            for bad_errno in (errno.ELOOP, errno.ENOTDIR):
                with mock.patch.object(reap.sys, "platform", "win32"), \
                        mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                        mock.patch.object(w32, "get_kernel32", return_value=fake_k32), \
                        mock.patch.object(w32, "get_advapi32", return_value=object()), \
                        mock.patch.object(
                            plat_perms,
                            "get_current_user_and_owner_sids_win",
                            return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001"),
                        ), \
                        mock.patch.object(
                            plat_perms,
                            "_open_path_for_security_win",
                            side_effect=OSError(bad_errno, "swapped"),
                        ):
                    with self.assertRaises(ValueError):
                        reap.prepare_registry(reg)

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_proc, "get_proc_stat", return_value={"state": "R", "starttime": 99999}):
            self.assertEqual(reap.process_stat(42), ("R", 99999))
        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_proc, "get_proc_stat", return_value=None):
            with self.assertRaises(FileNotFoundError):
                reap.process_stat(42)

    def test_valid_home_canonical_mesh_args_and_effective_home_win32(self) -> None:
        win_tmp = r"C:\Users\runneradmin\AppData\Local\Temp"
        home_canon = r"C:\Users\runneradmin\AppData\Local\Temp\mh-abcd1234"
        home_short = r"C:\Users\RUNNER~1\AppData\Local\Temp\mh-abcd1234"

        def _fake_realpath(p: str) -> str:
            norm = p.replace("/", "\\").rstrip("\\.")
            if norm.lower().startswith(r"c:\users\runner~1"):
                norm = r"C:\Users\runneradmin" + norm[len(r"C:\Users\RUNNER~1"):]
            if norm.lower() == home_canon.lower():
                return home_canon
            return norm

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_paths, "temp_root", return_value=win_tmp), \
                mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                mock.patch.object(reap.os.path, "realpath", side_effect=_fake_realpath):
            self.assertTrue(reap.valid_home(home_canon))
            self.assertTrue(reap.valid_home(home_canon.lower()))
            self.assertFalse(reap.valid_home(home_short))
            self.assertFalse(reap.valid_home(home_canon.replace("\\", "/")))
            self.assertFalse(reap.valid_home(win_tmp + r"\mh-"))
            self.assertFalse(reap.valid_home(win_tmp + r"\other-1"))
            self.assertFalse(reap.valid_home(home_canon + r"\..\mh-abcd1234"))

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_paths, "temp_root", return_value=win_tmp), \
                mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=True), \
                mock.patch.object(reap.os.path, "realpath", return_value=home_canon):
            self.assertFalse(reap.valid_home(home_canon))

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.os.path, "realpath", side_effect=_fake_realpath):
            # Interpreter regex accepts python.exe / python3.exe / python3.12.exe case-insensitively
            for exe in (
                rb"C:\Python312\python.exe",
                rb"C:\Python312\PYTHON.EXE",
                rb"C:\Python312\python3.12.exe",
                b"python3",
            ):
                got = reap.effective_home(
                    [exe, b"-P", b"-s", b"-m", b"mesh", b"--home", os.fsencode(home_canon), b"daemon", b"run"],
                    [],
                )
                self.assertEqual(got, os.fsencode(home_canon.lower()), exe)

            # Case-insensitive MESH_HOME env key
            got_env = reap.effective_home(
                [rb"C:\Python312\python.exe", b"-P", b"-s", b"-m", b"mesh", b"daemon", b"run"],
                [b"mesh_home=" + os.fsencode(home_canon)],
            )
            self.assertEqual(got_env, os.fsencode(home_canon.lower()))

            # Reject relative, drive-relative, and root-relative paths
            for bad_home in (b"mh-rel", b"C:rel", rb"\rootrel", b"/rootrel"):
                self.assertIsNone(
                    reap.effective_home(
                        [rb"C:\Python312\python.exe", b"-m", b"mesh", b"--home", bad_home, b"daemon", b"run"],
                        [],
                    ),
                    bad_home,
                )
            # UNC root is accepted
            unc = rb"\\server\share\mh-unc"
            self.assertEqual(
                reap.effective_home(
                    [rb"C:\Python312\python.exe", b"-m", b"mesh", b"--home", unc, b"daemon", b"run"],
                    [],
                ),
                unc,
            )

    def test_win32_handle_pinned_pidfd_stop_verified_and_token(self) -> None:
        win_home = r"C:\Temp\mh-win1234"
        win_tmp = r"C:\Temp"
        tok = "ab" * 16
        k = _FakeWin32HarnessKernel(
            {
                101: _FakeCmdProc(101, creation_time=777777, alive=True),
                102: _FakeCmdProc(102, creation_time=777777, alive=True, die_after_wait_checks=99),
                103: _FakeCmdProc(103, creation_time=777777, alive=False),
                104: _FakeCmdProc(104, creation_time=777777, alive=True, access_denied_term=True),
            }
        )

        # Open + terminate -> "terminated"
        fd, st = reap._open_pidfd_win(101, k32=k, get_last_error_fn=k.get_last_error)
        self.assertIsNotNone(fd)
        self.assertEqual(st, ("R", 777777))
        self.assertEqual(reap._stop_pidfd_win(fd, 0.1, 0.1, k32=k, get_last_error_fn=k.get_last_error), "terminated")
        reap._close_pidfd_win(fd, k32=k)

        # Gone before stop -> "gone"
        fd3, st3 = reap._open_pidfd_win(103, k32=k, get_last_error_fn=k.get_last_error)
        self.assertIsNone(fd3)
        self.assertIsNone(st3)
        self.assertIsNone(reap._open_pidfd_win(9999, k32=k, get_last_error_fn=k.get_last_error)[0])

        # Access denied on live TerminateProcess raises PermissionError
        fd4, _ = reap._open_pidfd_win(104, k32=k, get_last_error_fn=k.get_last_error)
        with self.assertRaises(PermissionError):
            reap._stop_pidfd_win(fd4, 0.1, 0.1, k32=k, get_last_error_fn=k.get_last_error)
        reap._close_pidfd_win(fd4, k32=k)

        # Escalation path when process doesn't exit on first TerminateProcess wait
        k_esc = _FakeWin32HarnessKernel({201: _FakeCmdProc(201, creation_time=555)})
        orig_wait = k_esc.WaitForSingleObject
        wait_seq = iter([w32.WAIT_TIMEOUT, w32.WAIT_TIMEOUT, w32.WAIT_OBJECT_0])
        with mock.patch.object(k_esc, "WaitForSingleObject", side_effect=lambda h, ms: next(wait_seq)):
            self.assertEqual(
                reap._stop_pidfd_win(
                    k_esc.OpenProcess(w32.PROCESS_TERMINATE, False, 201),
                    0.05,
                    0.05,
                    k32=k_esc,
                    get_last_error_fn=k_esc.get_last_error,
                ),
                "killed",
            )
            self.assertEqual([c[2] for c in k_esc.terminated], [15, 137])

        # stop_verified & stop_verified_token routing on win32
        def _fake_real(p: str) -> str:
            return p.replace("/", "\\")

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_paths, "temp_root", return_value=win_tmp), \
                mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                mock.patch.object(reap.os.path, "realpath", side_effect=_fake_real), \
                mock.patch.object(reap, "open_pidfd", return_value=(501, ("R", 888))), \
                mock.patch.object(reap, "_close_pidfd") as m_close, \
                mock.patch.object(reap, "stop_pidfd", return_value="terminated") as m_stop, \
                mock.patch.object(reap.plat_proc, "get_proc_cmdline", return_value=[r"C:\Py\python.exe", "-P", "-s", "-m", "mesh", "--home", win_home, "daemon", "run"]), \
                mock.patch.object(reap.plat_proc, "get_proc_environ", return_value={"mesh_test_harness_token": tok, "MESH_TEST_HARNESS_TOKEN": tok}):
            self.assertEqual(reap.stop_verified(501, 889, win_home), "pid_reused")
            self.assertEqual(reap.stop_verified(501, 888, win_home), "terminated")
            self.assertEqual(reap.stop_verified_token(501, tok, win_home), "terminated")
            m_stop.assert_called()
            self.assertEqual(m_close.call_count, 3)

        with mock.patch.object(reap.sys, "platform", "win32"), \
                mock.patch.object(reap.plat_paths, "temp_root", return_value=win_tmp), \
                mock.patch.object(reap.plat_paths, "is_reparse_link", return_value=False), \
                mock.patch.object(reap.os.path, "realpath", side_effect=_fake_real), \
                mock.patch.object(reap, "open_pidfd", return_value=(501, ("R", 888))), \
                mock.patch.object(reap, "_close_pidfd"), \
                mock.patch.object(reap, "stop_pidfd") as m_stop, \
                mock.patch.object(reap.plat_proc, "get_proc_cmdline", return_value=[r"C:\Py\python.exe", "-P", "-s", "-m", "mesh", "--home", win_home, "daemon", "run"]), \
                mock.patch.object(reap.plat_proc, "get_proc_environ", return_value={"MESH_TEST_HARNESS_TOKEN": "cd" * 16}):
            self.assertEqual(reap.stop_verified_token(501, tok, win_home), "token_mismatch")
            m_stop.assert_not_called()

        self.assertEqual(sorted(k.closed_handles), sorted(k.open_handles))

    def test_win32_suite_alive_and_prune_stale_locks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock = os.path.join(td, "suite-s1.lock")
            with open(lock, "w", encoding="utf-8") as fp:
                fp.write("")
            old = 1.0
            os.utime(lock, (old, old))
            rec = {"suite_id": "s1"}
            open_nofollow_fn = (
                reap.plat_fs.open_regular_nofollow_win
                if sys.platform == "win32"
                else (lambda p, f, label="": os.open(p, os.O_RDWR))
            )

            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_stat", return_value=False), \
                    mock.patch.object(reap.plat_fs, "open_regular_nofollow", side_effect=open_nofollow_fn), \
                    mock.patch.object(reap.plat_locks, "acquire_lock", side_effect=BlockingIOError), \
                    mock.patch.object(reap.plat_locks, "release_lock") as m_rel:
                self.assertTrue(reap.suite_alive(rec, td))
                m_rel.assert_not_called()

            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_stat", return_value=False), \
                    mock.patch.object(reap.plat_fs, "open_regular_nofollow", side_effect=open_nofollow_fn), \
                    mock.patch.object(reap.plat_locks, "acquire_lock"), \
                    mock.patch.object(reap.plat_locks, "release_lock") as m_rel:
                self.assertFalse(reap.suite_alive(rec, td))
                m_rel.assert_called_once()

            with mock.patch.object(reap.sys, "platform", "win32"), \
                    mock.patch.object(reap.plat_paths, "is_reparse_stat", return_value=False), \
                    mock.patch.object(reap.plat_fs, "open_regular_nofollow", side_effect=open_nofollow_fn), \
                    mock.patch.object(reap.plat_locks, "acquire_lock"), \
                    mock.patch.object(reap.plat_locks, "release_lock") as m_rel, \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                reap.prune_stale_locks(td, max_age=10.0)
                m_rel.assert_called_once()
                self.assertFalse(os.path.exists(lock))


class GitFixtureAndHarnessWin32MockedTests(unittest.TestCase):
    def test_gitfixture_win32_branches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            win_prefix = os.path.join(td, "mh-")
            with mock.patch.object(gf_mod.sys, "platform", "win32"), \
                    mock.patch.object(gf_mod, "BASE_PREFIX", win_prefix), \
                    mock.patch.object(gf_mod.plat_paths, "temp_root", return_value=td), \
                    mock.patch.object(gf_mod.plat_perms, "set_private_dir_acl") as m_dir_acl:
                fx = GitFixture()
                try:
                    m_dir_acl.assert_called_once_with(fx.base, 0o700)
                    with open(fx.empty_gitconfig, "r", encoding="utf-8") as fp:
                        cfg = fp.read()
                    self.assertIn("longpaths = true", cfg)
                    self.assertIn("autocrlf = false", cfg)

                    # allow_root accepts case variations and 8.3 short paths under temp_root/mh- and rejects others
                    other_home = os.path.join(td, "mh-other")
                    os.makedirs(other_home)
                    fx.allow_root(other_home)
                    short_alias = os.path.join(td, "SHORT~1", "mh-short")
                    with mock.patch.object(
                        gf_mod.plat_paths,
                        "long_path_win",
                        side_effect=lambda p: other_home if p == short_alias else p,
                    ):
                        fx.allow_root(short_alias)
                    with self.assertRaises(AssertionError):
                        fx.allow_root(os.path.join(td, "not-mh"))
                    with self.assertRaises(AssertionError):
                        fx._assert_safe_args([r"C:\Windows\System32"])
                    with self.assertRaises(AssertionError):
                        fx._assert_safe_args([r"D:relative"])
                    with self.assertRaises(AssertionError):
                        fx._assert_safe_args(["/etc/passwd"])
                    # Path inside base passes even with forward slashes
                    fx._assert_safe_args([fx.path("sub").replace("\\", "/")])

                    # make_k0 on mocked win32 invokes core.longpaths & core.autocrlf config calls
                    git_calls: list[tuple[str, tuple[str, ...]]] = []

                    def _fake_git(repo_cwd: str, *args: str, **kw: object) -> subprocess.CompletedProcess:
                        git_calls.append((repo_cwd, args))
                        if args[:2] == ("init", "-b"):
                            os.makedirs(args[-1], exist_ok=True)
                        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

                    with mock.patch.object(fx, "git", side_effect=_fake_git):
                        repo = fx.make_k0("R")
                    self.assertIn((repo, ("config", "core.longpaths", "true")), git_calls)
                    self.assertIn((repo, ("config", "core.autocrlf", "false")), git_calls)
                finally:
                    with mock.patch.object(gf_mod, "rmtree_deep") as m_rm:
                        fx.cleanup()
                        m_rm.assert_called_once_with(fx.base)
                    shutil.rmtree(fx.base, ignore_errors=True)

    def test_gitfixture_win32_83_short_base_prefix_both_sides_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            short_td = os.path.join(td, "SHORT83~1")
            long_td = os.path.join(td, "longuser")
            os.makedirs(long_td)
            os.makedirs(short_td)
            short_prefix = os.path.join(short_td, "mh-")
            other_short = os.path.join(short_td, "mh-other")
            other_long = os.path.join(long_td, "mh-other")
            os.makedirs(other_short)
            os.makedirs(other_long)

            def _expand_83(p: str) -> str:
                return p.replace("SHORT83~1", "longuser").replace("short83~1", "longuser")

            with mock.patch.object(gf_mod.sys, "platform", "win32"), \
                    mock.patch.object(gf_mod, "BASE_PREFIX", short_prefix), \
                    mock.patch.object(gf_mod.plat_paths, "temp_root", return_value=long_td), \
                    mock.patch.object(gf_mod.plat_paths, "long_path_win", side_effect=_expand_83), \
                    mock.patch.object(gf_mod.plat_perms, "set_private_dir_acl"):
                fx = GitFixture()
                try:
                    fx.allow_root(other_short)
                    fx.allow_root(other_long)
                    sub = fx.path("sub")
                    fx._assert_safe_args([sub.replace("\\", "/")])
                    self.assertIn("SHORT83~1", fx.base)
                finally:
                    with mock.patch.object(gf_mod, "rmtree_deep") as m_rm:
                        fx.cleanup()
                        m_rm.assert_called_once_with(fx.base)
                    shutil.rmtree(fx.base, ignore_errors=True)

    def test_mesh_harness_win32_branches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(helpers_mod.sys, "platform", "win32"), \
                    mock.patch.object(helpers_mod.plat_paths, "temp_root", return_value=td), \
                    mock.patch.object(helpers_mod.plat_perms, "set_private_dir_acl") as m_dir_acl, \
                    mock.patch.object(helpers_mod.plat_perms, "set_private_file_acl") as m_file_acl:
                h = MeshHarness(extra_path=r"C:\tools")
                try:
                    self.assertTrue(h.bin_mesh.endswith(os.path.join("bin", "mesh.cmd")))
                    self.assertTrue(h.home.startswith(os.path.join(td, "mh-")))
                    self.assertTrue(h.tripwire_file.startswith(os.path.join(td, "mesh-tripwire-")))
                    self.assertEqual(m_dir_acl.call_count, 2)
                    self.assertEqual(m_file_acl.call_count, 1)

                    # PATH uses os.pathsep (not ':'!) and preserves windows drive letters when os.pathsep=';'
                    with mock.patch.object(helpers_mod.os, "pathsep", ";"), \
                            mock.patch.dict(os.environ, {"PATH": r"C:\Windows\System32"}, clear=False):
                        env = h.get_env()
                        self.assertEqual(
                            env["PATH"],
                            ";".join([r"C:\tools", os.path.join(h.repo_root, "tests", "shims"), os.path.join(h.repo_root, "bin"), r"C:\Windows\System32"]),
                        )

                    # cli() uses mesh_cli_argv + --passphrase-file (no pass_fds) on win32
                    seen_run: list[tuple[Any, dict]] = []

                    def _fake_run(cmd: Any, **kw: Any) -> subprocess.CompletedProcess:
                        seen_run.append((cmd, kw))
                        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true, "data": {}}', stderr="")

                    with mock.patch.object(helpers_mod.subprocess, "run", side_effect=_fake_run):
                        rc, resp = h.cli("whoami", user=True)
                    self.assertEqual(rc, 0)
                    self.assertTrue(resp["ok"])
                    cmd0, kw0 = seen_run[0]
                    self.assertNotIn("pass_fds", kw0)
                    self.assertIn("--passphrase-file", cmd0)
                    self.assertNotIn("--passphrase-fd", cmd0)
                    self.assertEqual(cmd0[:4], [sys.executable, "-P", "-s", "-m"])
                    self.assertEqual(kw0["env"]["PYTHONPATH"], h.repo_root)

                    # Custom .cmd bin_mesh wraps via prepare_win_cmd_args
                    h_orig_bin = h.bin_mesh
                    h.bin_mesh = os.path.join(td, "custom.cmd")
                    with mock.patch.object(helpers_mod.pipe_pump, "prepare_win_cmd_args", return_value="WRAPPED") as m_wrap, \
                            mock.patch.object(helpers_mod.subprocess, "run", side_effect=_fake_run):
                        h.cli("version")
                        m_wrap.assert_called_once()
                    self.assertEqual(seen_run[-1][0], "WRAPPED")
                    h.bin_mesh = h_orig_bin

                    # setup_cli() uses --passphrase-file on win32
                    seen_inproc: list[list[str]] = []
                    with mock.patch.object(helpers_mod, "mesh_cli_main", side_effect=lambda argv: (seen_inproc.append(argv), print('{"ok": true}'), 0)[2]):
                        rc2, _ = h.setup_cli("agent", "list", user=True)
                    self.assertEqual(rc2, 0)
                    self.assertIn("--passphrase-file", seen_inproc[0])

                    # start_daemon() uses mesh_cli_argv + detached_popen_kwargs
                    fake_proc = mock.MagicMock()
                    fake_proc.pid = 4321
                    fake_proc.poll.return_value = None
                    with mock.patch.object(helpers_mod.subprocess, "Popen", return_value=fake_proc) as m_popen, \
                            mock.patch.object(helpers_mod.plat_proc, "detached_popen_kwargs", return_value={"creationflags": 0x208, "close_fds": True}), \
                            mock.patch.object(helpers_mod.reap_daemons, "process_stat", return_value=("R", 12345)), \
                            mock.patch.object(helpers_mod.reap_daemons, "register_daemon", return_value=None):
                        h.start_daemon(wait_ready=False)
                        self.assertEqual(m_popen.call_args.kwargs["creationflags"], 0x208)
                        self.assertEqual(m_popen.call_args.args[0][:4], [sys.executable, "-P", "-s", "-m"])

                    # db() tracks connections and teardown() closes them before removing home
                    db_path = os.path.join(h.home, "mesh.db")
                    c_init = sqlite3.connect(db_path)
                    c_init.execute("CREATE TABLE runs (id TEXT);")
                    c_init.execute("INSERT INTO runs VALUES ('run_1');")
                    c_init.commit()
                    c_init.close()
                    ro_conn = h.db()
                    self.assertEqual(len(h._open_dbs), 1)
                    self.assertEqual(h.run_ids_in_db(), {"run_1"})
                    h.daemon_proc = None

                    # meshd_pid() matches case-insensitive MESH_HOME; kill9_daemon() uses _win_terminate_and_wait_pid
                    os.makedirs(os.path.join(h.home, "run"), exist_ok=True)
                    with open(os.path.join(h.home, "run", "meshd.pid"), "w", encoding="utf-8") as pf:
                        pf.write("4321\n")
                    with mock.patch.object(helpers_mod.plat_proc, "get_proc_environ", return_value={"mesh_home": h.home}):
                        self.assertEqual(h.meshd_pid(), 4321)
                        with mock.patch.object(helpers_mod, "_win_terminate_and_wait_pid") as m_term:
                            h.kill9_daemon()
                            m_term.assert_called_once_with(4321, timeout_s=10.0)

                    # kill_leftover_run_processes() uses Toolhelp + PEB env + handle-pinned kill
                    with mock.patch.object(helpers_mod.plat_proc, "list_toolhelp_processes", return_value={5555: 1, os.getpid(): 1}), \
                            mock.patch.object(helpers_mod.plat_proc, "get_proc_stat", return_value={"starttime": 9876}), \
                            mock.patch.object(helpers_mod.plat_proc, "get_proc_environ", return_value={"MESH_RUN_ID": "run_1"}), \
                            mock.patch.object(helpers_mod.plat_proc, "signal_if_same", return_value=True) as m_sig, \
                            mock.patch.object(helpers_mod.plat_proc, "is_process_alive", side_effect=[True, True]), \
                            mock.patch.object(helpers_mod.plat_proc, "kill_if_same", return_value=True) as m_kill, \
                            mock.patch.object(helpers_mod.time, "monotonic", side_effect=[0.0, 2.0]):
                        victims = h.kill_leftover_run_processes()
                    self.assertEqual(victims, [5555])
                    m_sig.assert_called_once()
                    m_kill.assert_called_once_with(5555, 9876)
                finally:
                    h.check_invariants_on_teardown = False
                    with mock.patch.object(h, "stop_daemon"), \
                            mock.patch.object(helpers_mod.plat_proc, "list_toolhelp_processes", return_value={}):
                        h.teardown()
                    # Tracked db connection was closed by teardown!
                    with self.assertRaises(sqlite3.ProgrammingError):
                        ro_conn.execute("SELECT 1;")
                    self.assertFalse(os.path.exists(h.home))
                    self.assertFalse(os.path.exists(h.fixtures_dir))

    def test_win_terminate_and_wait_pid_and_rmtree_deep_win(self) -> None:
        k = _FakeWin32HarnessKernel(
            {
                10: _FakeCmdProc(10, alive=True),
                11: _FakeCmdProc(11, alive=False),
                12: _FakeCmdProc(12, alive=True, access_denied_term=True),
            }
        )
        helpers_mod._win_terminate_and_wait_pid(10, timeout_s=2.0, k32=k, get_last_error_fn=k.get_last_error)
        self.assertEqual(k.terminated, [(k.terminated[0][0], 10, 137)])
        helpers_mod._win_terminate_and_wait_pid(11, timeout_s=2.0, k32=k, get_last_error_fn=k.get_last_error)
        helpers_mod._win_terminate_and_wait_pid(9999, timeout_s=2.0, k32=k, get_last_error_fn=k.get_last_error)
        with self.assertRaises(PermissionError):
            helpers_mod._win_terminate_and_wait_pid(12, timeout_s=2.0, k32=k, get_last_error_fn=k.get_last_error)
        self.assertEqual(sorted(k.closed_handles), sorted(k.open_handles))

        # Iterative _rmtree_deep_win removes nested tree + never traverses a simulated reparse dir
        with tempfile.TemporaryDirectory() as td:
            tree = os.path.join(td, "root")
            secret = os.path.join(td, "secret")
            os.makedirs(os.path.join(tree, "a", "b"))
            os.makedirs(secret)
            secret_file = os.path.join(secret, "keep.txt")
            with open(secret_file, "w", encoding="utf-8") as fp:
                fp.write("keep")
            with open(os.path.join(tree, "a", "b", "f.txt"), "w", encoding="utf-8") as fp:
                fp.write("x")
            reparse_dir = os.path.join(tree, "junc")
            os.makedirs(reparse_dir)

            reparse_ino = os.lstat(reparse_dir).st_ino
            orig_is_reparse = helpers_mod.plat_paths.is_reparse_stat_win

            def _fake_reparse(st: os.stat_result) -> bool:
                # Pretend reparse_dir inode is a reparse point
                if st.st_ino == reparse_ino:
                    return True
                return orig_is_reparse(st)

            with mock.patch.object(helpers_mod.plat_paths, "is_reparse_stat_win", side_effect=_fake_reparse):
                helpers_mod._rmtree_deep_win(tree)
            self.assertFalse(os.path.exists(tree))
            self.assertTrue(os.path.exists(secret_file))

    def test_rmtree_deep_win_directory_symlinks_mocked(self) -> None:
        # Windows directory symlinks report st_mode=S_IFLNK (S_ISDIR False) + FILE_ATTRIBUTE_DIRECTORY,
        # and os.unlink (DeleteFileW) raises PermissionError(EACCES).
        with tempfile.TemporaryDirectory() as td:
            tree = os.path.join(td, "root")
            secret = os.path.join(td, "secret")
            os.makedirs(tree)
            os.makedirs(secret)
            secret_file = os.path.join(secret, "keep.txt")
            with open(secret_file, "w", encoding="utf-8") as fp:
                fp.write("keep")

            dir_sym = os.path.join(tree, "dir_sym")
            dir_sym_noattr = os.path.join(tree, "dir_sym_noattr")
            root_sym = os.path.join(td, "root_dir_sym")
            for p in (dir_sym, dir_sym_noattr, root_sym):
                os.makedirs(p)

            orig_lstat = os.lstat
            orig_unlink = os.unlink

            class _FakeWinDirLinkStat:
                def __init__(self, real_st: os.stat_result, *, with_dir_attr: bool) -> None:
                    self.st_mode = stat.S_IFLNK | 0o777
                    self.st_ino = real_st.st_ino
                    dir_attr = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
                    rep_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    self.st_file_attributes = rep_attr | (dir_attr if with_dir_attr else 0)
                    self.st_reparse_tag = 0

            sym_set_dir_attr = {dir_sym, root_sym}
            sym_set_no_dir_attr = {dir_sym_noattr}

            def _fake_lstat(p: str) -> Any:
                st = orig_lstat(p)
                if p in sym_set_dir_attr:
                    return _FakeWinDirLinkStat(st, with_dir_attr=True)
                if p in sym_set_no_dir_attr:
                    return _FakeWinDirLinkStat(st, with_dir_attr=False)
                return st

            def _fake_unlink(p: str) -> None:
                if p in sym_set_dir_attr or p in sym_set_no_dir_attr:
                    raise PermissionError(errno.EACCES, "Access is denied", p)
                orig_unlink(p)

            with mock.patch.object(helpers_mod.os, "lstat", side_effect=_fake_lstat), \
                    mock.patch.object(helpers_mod.os, "unlink", side_effect=_fake_unlink), \
                    mock.patch("mesh.platform.fs.os.unlink", side_effect=_fake_unlink):
                helpers_mod._rmtree_deep_win(tree)
                helpers_mod._rmtree_deep_win(root_sym)

            self.assertFalse(os.path.exists(tree))
            self.assertFalse(os.path.exists(root_sym))
            self.assertTrue(os.path.exists(secret_file))

    def test_mesh_harness_r10_fixes_win32_mocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(helpers_mod.sys, "platform", "win32"), \
                    mock.patch.object(helpers_mod.plat_paths, "temp_root", return_value=td), \
                    mock.patch.object(helpers_mod.plat_perms, "set_private_dir_acl"), \
                    mock.patch.object(helpers_mod.plat_perms, "set_private_file_acl"):
                h = MeshHarness(use_shims=False, check_invariants_on_teardown=False)
                try:
                    # Case-insensitive and separator-insensitive shims_dir stripping in get_env(use_shims=False)
                    orig_root = h.repo_root
                    h.repo_root = r"C:\Work\Mesh"
                    with mock.patch.object(helpers_mod.os, "path", ntpath), \
                            mock.patch.object(helpers_mod.os, "pathsep", ";"), \
                            mock.patch.object(ntpath, "realpath", side_effect=lambda p: ntpath.normpath(p.replace("/", "\\"))), \
                            mock.patch.dict(
                                os.environ,
                                {"PATH": r"c:\work\mesh\tests\shims;C:\Work\Mesh\TESTS\SHIMS/;C:\Tools"},
                                clear=False,
                            ):
                        env = h.get_env()
                    h.repo_root = orig_root
                    self.assertEqual(env["PATH"], r"C:\Work\Mesh\bin;C:\Tools")

                    # kill_leftover_run_processes: case-insensitive MESH_RUN_ID + guarded OSError in wait loop
                    db_path = os.path.join(h.home, "mesh.db")
                    c_init = sqlite3.connect(db_path)
                    c_init.execute("CREATE TABLE runs (id TEXT);")
                    c_init.execute("INSERT INTO runs VALUES ('run_1');")
                    c_init.commit()
                    c_init.close()

                    def _fake_alive(pid: int, st: int) -> bool:
                        _ = st
                        if pid == 5555:
                            raise PermissionError(errno.EACCES, "protected reused pid")
                        return True

                    with mock.patch.object(helpers_mod.plat_proc, "list_toolhelp_processes", return_value={5555: 1, 5556: 1}), \
                            mock.patch.object(helpers_mod.plat_proc, "get_proc_stat", return_value={"starttime": 9876}), \
                            mock.patch.object(
                                helpers_mod.plat_proc,
                                "get_proc_environ",
                                side_effect=lambda pid: {"mesh_run_id": "run_1"} if pid == 5555 else {"MESH_RUN_ID": "run_1"},
                            ), \
                            mock.patch.object(helpers_mod.plat_proc, "signal_if_same", return_value=True) as m_sig, \
                            mock.patch.object(helpers_mod.plat_proc, "is_process_alive", side_effect=_fake_alive), \
                            mock.patch.object(helpers_mod.plat_proc, "kill_if_same", return_value=True) as m_kill, \
                            mock.patch.object(helpers_mod.time, "monotonic", side_effect=[0.0, 0.1, 2.0]):
                        victims = h.kill_leftover_run_processes()
                    self.assertEqual(victims, [5555, 5556])
                    self.assertEqual(m_sig.call_count, 2)
                    m_kill.assert_called_once_with(5556, 9876)
                finally:
                    with mock.patch.object(h, "stop_daemon"), \
                            mock.patch.object(helpers_mod.plat_proc, "list_toolhelp_processes", return_value={}):
                        h.teardown()


@unittest.skipUnless(sys.platform == "win32", "Windows-only")
class RealWindowsPhase7bTests(unittest.TestCase):
    def test_real_reap_daemons_lifecycle_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            reg = os.path.join(os.path.realpath(td), "reg")
            reap.prepare_registry(reg)
            self.assertTrue(plat_perms.is_owned_by_current_user(reg))

            home = os.path.join(plat_paths.temp_root(), f"mh-real7b-{os.getpid()}")
            os.makedirs(home, mode=0o700, exist_ok=True)
            self.addCleanup(rmtree_deep, home)
            self.assertTrue(reap.valid_home(home))

            fake_pkg_root = os.path.join(os.path.realpath(td), "pkg")
            os.makedirs(os.path.join(fake_pkg_root, "mesh"), exist_ok=True)
            with open(os.path.join(fake_pkg_root, "mesh", "__init__.py"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(fake_pkg_root, "mesh", "__main__.py"), "w", encoding="utf-8") as f:
                f.write("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")

            tok = "ab" * 16
            env = dict(os.environ)
            env["PYTHONPATH"] = fake_pkg_root
            env["MESH_HOME"] = home
            env[reap.HARNESS_TOKEN_VAR] = tok
            child = subprocess.Popen(
                [sys.executable, "-P", "-s", "-m", "mesh", "--home", home, "daemon", "run"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(timeout=5), child.stdout.close(), child.stderr.close()))
            self.assertEqual(child.stdout.readline().strip(), "ready")

            with mock.patch.dict(os.environ, {"MESH_TEST_DAEMON_REGISTRY": reg, "MESH_TEST_SUITE_ID": "win-suite"}):
                handle = reap.register_daemon(child.pid, home)
                self.assertIsNotNone(handle)
                rec_path, record = handle
                self.assertTrue(os.path.isfile(rec_path))
                self.assertTrue(reap.home_matches(child.pid, home))
                self.assertTrue(reap.token_matches(child.pid, tok))

                # With held FileLock, suite_alive is True and dry/real reaper skips it
                lock_path = os.path.join(reg, "suite-win-suite.lock")
                from mesh.platform.locks import FileLock

                with FileLock(lock_path, exclusive=True, blocking=True):
                    self.assertTrue(reap.suite_alive(record, reg))
                    buf = io.StringIO()
                    with mock.patch("sys.stderr", buf):
                        self.assertEqual(reap.main(["--registry", reg]), 0)
                    self.assertIn(f"skipped_live pid={child.pid}", buf.getvalue())
                    self.assertIsNone(child.poll())

                # Once lock released, reaper terminates via handle-pinned OpenProcess
                self.assertFalse(reap.suite_alive(record, reg))
                buf2 = io.StringIO()
                with mock.patch("sys.stderr", buf2):
                    self.assertEqual(reap.main(["--registry", reg]), 0)
                self.assertIn(f"terminated pid={child.pid}", buf2.getvalue())
                child.wait(timeout=5)
                self.assertFalse(os.path.exists(rec_path))

    def test_real_harness_db_kill9_perms_and_rmtree_junction_on_windows(self) -> None:
        h = MeshHarness(init_db=True, start_daemon=True)
        ro_conn = None
        try:
            ro_conn = h.db()
            self.assertGreaterEqual(ro_conn.execute("SELECT COUNT(*) FROM meta;").fetchone()[0], 1)
            pid = h.meshd_pid()
            self.assertIsNotNone(pid)
            h.kill9_daemon()
            self.assertIsNone(h.meshd_pid())
            h.restart_daemon()
            rc, resp = h.cli("whoami", user=True)
            self.assertEqual(rc, 0, resp)
            self.assertTrue(resp["ok"])

            # Cross-platform permission helpers on real NTFS
            probe_dir = os.path.join(h.home, "perm_dir")
            os.makedirs(probe_dir, exist_ok=True)
            probe_file = os.path.join(probe_dir, "secret.txt")
            with open(probe_file, "w", encoding="utf-8") as fp:
                fp.write("secret\n")
            set_inaccessible_for_test(probe_file)
            with self.assertRaises(PermissionError):
                open(probe_file, "r", encoding="utf-8").close()
            restore_accessible_for_test(probe_file)
            with open(probe_file, "r", encoding="utf-8") as fp:
                self.assertEqual(fp.read(), "secret\n")
            set_world_readable_for_test(probe_file)
            from mesh.errors import MeshError

            with self.assertRaises(MeshError):
                plat_perms.verify_private_owner_and_perms(probe_file, is_dir=False)
            restore_accessible_for_test(probe_file)
            plat_perms.verify_private_owner_and_perms(probe_file, is_dir=False)

            # Junction and directory symlink inside home must be unlinked without deleting targets
            with tempfile.TemporaryDirectory() as ext_td:
                target = os.path.join(os.path.realpath(ext_td), "keep_dir")
                os.makedirs(target)
                keep_file = os.path.join(target, "keep.txt")
                with open(keep_file, "w", encoding="utf-8") as fp:
                    fp.write("keep")
                junc = os.path.join(h.home, "ext_junc")
                cp = subprocess.run(["cmd", "/c", "mklink", "/J", junc, target], capture_output=True, text=True)

                sym_target = os.path.join(os.path.realpath(ext_td), "keep_sym_dir")
                os.makedirs(sym_target)
                sym_keep_file = os.path.join(sym_target, "keep_sym.txt")
                with open(sym_keep_file, "w", encoding="utf-8") as fp:
                    fp.write("keep-sym")
                dir_sym = os.path.join(h.home, "ext_dir_sym")
                sym_created = False
                try:
                    os.symlink(sym_target, dir_sym, target_is_directory=True)
                    sym_created = True
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 1314:
                        raise

                h.teardown()
                self.assertFalse(os.path.exists(h.home))
                if cp.returncode == 0:
                    self.assertTrue(os.path.isfile(keep_file))
                if sym_created:
                    self.assertTrue(os.path.isfile(sym_keep_file))
        finally:
            if os.path.exists(h.home):
                h.teardown()
        self.assertFalse(os.path.exists(h.home))
        if ro_conn is not None:
            with self.assertRaises(sqlite3.ProgrammingError):
                ro_conn.execute("SELECT 1;")

    def test_real_git_mesh_harness_cleanup_on_windows(self) -> None:
        fx = GitFixture()
        h = GitMeshHarness(fx, init_db=True, start_daemon=False)
        try:
            repo = fx.make_k0()
            self.assertEqual(fx.head(repo, "main") != "", True)
            # Mark a file read-only (simulating git packfiles) to prove cleanup handles FILE_ATTRIBUTE_READONLY
            ro_file = os.path.join(fx.base, "readonly.pack")
            with open(ro_file, "w", encoding="utf-8") as fp:
                fp.write("pack")
            os.chmod(ro_file, stat.S_IREAD)
        finally:
            h.teardown()
        self.assertFalse(os.path.exists(h.home))
        self.assertFalse(os.path.exists(fx.base))


if __name__ == "__main__":
    unittest.main()
