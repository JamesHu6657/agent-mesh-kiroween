"""Unit tests for mesh.platform.proc and mesh.platform._win32 (Phase 3a / T06)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import errno
import os
import pathlib
import queue
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from mesh.core import proc as core_proc
from mesh.platform import _win32, _win32_ctypes as w32, proc as plat_proc
from tests import win_sim


def _encode_win_env(items: list[tuple[str, str]]) -> bytes:
    """Encode a list of (key, value) pairs as a UTF-16LE double-NUL Windows env block."""
    if not items:
        return b"\x00\x00"
    joined = "\x00".join(f"{k}={v}" for k, v in items) + "\x00\x00"
    return joined.encode("utf-16-le")


@dataclass
class _FakeProcEntry:
    pid: int
    ppid: int
    creation_time: int
    alive: bool = True
    exit_code: int = w32.STILL_ACTIVE
    access_denied_open: bool = False
    access_denied_vm: bool = False
    access_denied_pbi: bool = False
    wow64: bool = False
    peb_null: bool = False
    params_null: bool = False
    env_null: bool = False
    env_items: list[tuple[str, str]] = field(default_factory=list)
    env_raw: bytes | None = None
    env_base_addr: int = 0x50000000
    env_reported_size: int | None = None
    unmapped_pages: set[int] = field(default_factory=set)
    die_after_vm_reads: int | None = None
    exe_name: str = "python.exe"


class _FakeWinProcKernelAndNt:
    """In-memory fake kernel32 + ntdll simulating Win32 handles, Toolhelp32, PEB, and VM."""

    def __init__(self, procs: dict[int, _FakeProcEntry], *, current_pid: int = 100) -> None:
        self.procs = procs
        self.current_pid = current_pid
        self.last_error = 0
        self.next_handle = 1000
        self.open_handles: list[int] = []
        self.closed_handles: list[int] = []
        self.handle_table: dict[int, tuple[str, Any]] = {}
        self.snapshot_errors: list[int] = []
        self.get_process_times_error: int | None = None
        self.terminated_calls: list[tuple[int, int]] = []
        self.vm_read_count = 0

        # Build virtual memory layouts per process
        self.mem: dict[int, dict[int, bytes]] = {}
        for pid, entry in procs.items():
            self._build_proc_memory(pid, entry)

    def _build_proc_memory(self, pid: int, entry: _FakeProcEntry) -> None:
        regions: dict[int, bytes] = {}
        if entry.peb_null:
            self.mem[pid] = regions
            return

        if entry.wow64:
            peb_addr = 0x00400000
            params_addr = 0 if entry.params_null else 0x00500000
            peb = w32.PEB32(ProcessParameters=params_addr)
            regions[peb_addr] = bytes(peb)
            if params_addr:
                env_addr = 0 if entry.env_null else entry.env_base_addr
                raw = entry.env_raw if entry.env_raw is not None else _encode_win_env(entry.env_items)
                rep_size = len(raw) if entry.env_reported_size is None else entry.env_reported_size
                params = w32.RTL_USER_PROCESS_PARAMETERS32(
                    Environment=env_addr,
                    EnvironmentSize=rep_size,
                )
                regions[params_addr] = bytes(params)
                if env_addr:
                    # Windows virtual memory commits whole 4 KiB pages; pad to page boundary.
                    end_addr = env_addr + len(raw)
                    padded_end = (end_addr + 4095) & ~4095
                    if padded_end == end_addr:
                        padded_end += 4096
                    regions[env_addr] = raw + b"\xaa" * (padded_end - end_addr)
        else:
            peb_addr = 0x00007FF700100000
            params_addr = 0 if entry.params_null else 0x0000018000200000
            peb = w32.PEB64(ProcessParameters=params_addr)
            regions[peb_addr] = bytes(peb)
            if params_addr:
                env_addr = 0 if entry.env_null else entry.env_base_addr
                raw = entry.env_raw if entry.env_raw is not None else _encode_win_env(entry.env_items)
                rep_size = len(raw) if entry.env_reported_size is None else entry.env_reported_size
                params = w32.RTL_USER_PROCESS_PARAMETERS64(
                    Environment=env_addr,
                    EnvironmentSize=rep_size,
                )
                regions[params_addr] = bytes(params)
                if env_addr:
                    end_addr = env_addr + len(raw)
                    padded_end = (end_addr + 4095) & ~4095
                    if padded_end == end_addr:
                        padded_end += 4096
                    regions[env_addr] = raw + b"\xaa" * (padded_end - end_addr)
        self.mem[pid] = regions

    def get_last_error(self) -> int:
        return self.last_error

    def _alloc_handle(self, kind: str, payload: Any) -> int:
        self.next_handle += 4
        h = self.next_handle
        self.open_handles.append(h)
        self.handle_table[h] = (kind, payload)
        return h

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> Any:
        _ = inherit
        entry = self.procs.get(pid)
        if entry is None:
            self.last_error = w32.ERROR_INVALID_PARAMETER
            return None
        if entry.access_denied_open:
            self.last_error = w32.ERROR_ACCESS_DENIED
            return None
        self.last_error = 0
        return self._alloc_handle("proc", (entry, access))

    def CloseHandle(self, handle: int) -> int:
        self.closed_handles.append(handle)
        self.handle_table.pop(handle, None)
        return 1

    def GetCurrentProcess(self) -> int:
        return -1

    def GetCurrentProcessId(self) -> int:
        return self.current_pid

    def TerminateProcess(self, handle: int, exit_code: int) -> int:
        self.terminated_calls.append((handle, exit_code))
        return 1

    def WaitForSingleObject(self, handle: int, timeout_ms: int) -> int:
        _ = timeout_ms
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return w32.WAIT_FAILED
        entry, _access = item[1]
        return w32.WAIT_TIMEOUT if entry.alive else w32.WAIT_OBJECT_0

    def GetExitCodeProcess(self, handle: int, out_ptr: Any) -> int:
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _access = item[1]
        code = w32.STILL_ACTIVE if entry.alive else entry.exit_code
        ctypes.cast(out_ptr, wintypes.LPDWORD)[0] = code
        return 1

    def GetProcessTimes(self, handle: int, ct_ptr: Any, et_ptr: Any, kt_ptr: Any, ut_ptr: Any) -> int:
        _ = (et_ptr, kt_ptr, ut_ptr)
        if self.get_process_times_error is not None:
            self.last_error = self.get_process_times_error
            return 0
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _access = item[1]
        ct = ctypes.cast(ct_ptr, ctypes.POINTER(w32.FILETIME)).contents
        ct.dwLowDateTime = entry.creation_time & 0xFFFFFFFF
        ct.dwHighDateTime = (entry.creation_time >> 32) & 0xFFFFFFFF
        return 1

    def IsWow64Process2(self, handle: int, proc_m_ptr: Any, nat_m_ptr: Any) -> int:
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, _access = item[1]
        pm = w32.IMAGE_FILE_MACHINE_I386 if entry.wow64 else w32.IMAGE_FILE_MACHINE_UNKNOWN
        ctypes.cast(proc_m_ptr, ctypes.POINTER(ctypes.c_uint16))[0] = pm
        ctypes.cast(nat_m_ptr, ctypes.POINTER(ctypes.c_uint16))[0] = 0x8664
        return 1

    def NtQueryInformationProcess(
        self, handle: int, info_class: int, buf_ptr: Any, buf_len: int, ret_ptr: Any
    ) -> int:
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            return -1073741816  # STATUS_INVALID_HANDLE
        entry, _access = item[1]
        if entry.access_denied_pbi:
            return w32.STATUS_ACCESS_DENIED
        if info_class == w32.ProcessWow64Information:
            val = 0x00400000 if (entry.wow64 and not entry.peb_null) else 0
            ctypes.cast(buf_ptr, ctypes.POINTER(ctypes.c_size_t))[0] = val
            if ret_ptr:
                ctypes.cast(ret_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(ctypes.c_size_t)
            return w32.STATUS_SUCCESS
        if info_class == w32.ProcessBasicInformation:
            _ = buf_len
            pbi = ctypes.cast(buf_ptr, ctypes.POINTER(w32.PROCESS_BASIC_INFORMATION)).contents
            pbi.ExitStatus = w32.STILL_ACTIVE if entry.alive else entry.exit_code
            pbi.PebBaseAddress = None if entry.peb_null else (0x00400000 if entry.wow64 else 0x00007FF700100000)
            pbi.UniqueProcessId = entry.pid
            pbi.InheritedFromUniqueProcessId = entry.ppid
            if ret_ptr:
                ctypes.cast(ret_ptr, ctypes.POINTER(ctypes.c_uint32))[0] = ctypes.sizeof(
                    w32.PROCESS_BASIC_INFORMATION
                )
            return w32.STATUS_SUCCESS
        return w32.STATUS_INVALID_PARAMETER

    def RtlNtStatusToDosError(self, status: int) -> int:
        if status == w32.STATUS_ACCESS_DENIED:
            return w32.ERROR_ACCESS_DENIED
        if status == w32.STATUS_PARTIAL_COPY:
            return w32.ERROR_PARTIAL_COPY
        return w32.ERROR_INVALID_PARAMETER

    def ReadProcessMemory(
        self, handle: int, base_addr: int, buf_ptr: Any, size: int, nread_ptr: Any
    ) -> int:
        item = self.handle_table.get(handle)
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        entry, access = item[1]
        self.vm_read_count += 1
        if entry.die_after_vm_reads is not None and self.vm_read_count >= entry.die_after_vm_reads:
            entry.alive = False
            entry.exit_code = 0
            self.last_error = w32.ERROR_PARTIAL_COPY
            return 0
        if entry.access_denied_vm or not (access & w32.PROCESS_VM_READ):
            self.last_error = w32.ERROR_ACCESS_DENIED
            return 0

        regions = self.mem.get(entry.pid, {})
        for r_base, r_bytes in regions.items():
            if r_base <= base_addr < r_base + len(r_bytes):
                off = base_addr - r_base
                avail = len(r_bytes) - off
                # Simulate unmapped page boundaries
                for page in entry.unmapped_pages:
                    if base_addr <= page < base_addr + size:
                        self.last_error = w32.ERROR_PARTIAL_COPY
                        return 0
                    if base_addr < page and base_addr + size > page:
                        self.last_error = w32.ERROR_PARTIAL_COPY
                        return 0
                if avail < size:
                    self.last_error = w32.ERROR_PARTIAL_COPY
                    return 0
                chunk = r_bytes[off : off + size]
                ctypes.memmove(buf_ptr, chunk, size)
                if nread_ptr:
                    ctypes.cast(nread_ptr, ctypes.POINTER(ctypes.c_size_t))[0] = size
                return 1
        self.last_error = w32.ERROR_PARTIAL_COPY
        return 0

    def CreateToolhelp32Snapshot(self, flags: int, pid: int) -> int:
        _ = (flags, pid)
        if self.snapshot_errors:
            self.last_error = self.snapshot_errors.pop(0)
            return w32.INVALID_HANDLE_VALUE
        entries = [e for e in self.procs.values() if e.alive]
        return self._alloc_handle("snap", {"entries": entries, "idx": 0})

    def Process32FirstW(self, snap: int, pe_ptr: Any) -> int:
        item = self.handle_table.get(snap)
        if item is None or item[0] != "snap":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        pe = ctypes.cast(pe_ptr, ctypes.POINTER(w32.PROCESSENTRY32W)).contents
        if pe.dwSize != ctypes.sizeof(w32.PROCESSENTRY32W):
            self.last_error = w32.ERROR_BAD_LENGTH
            return 0
        state = item[1]
        state["idx"] = 0
        if not state["entries"]:
            self.last_error = w32.ERROR_NO_MORE_FILES
            return 0
        return self._fill_pe(pe, state["entries"][0])

    def Process32NextW(self, snap: int, pe_ptr: Any) -> int:
        item = self.handle_table.get(snap)
        if item is None or item[0] != "snap":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        state = item[1]
        state["idx"] += 1
        if state["idx"] >= len(state["entries"]):
            self.last_error = w32.ERROR_NO_MORE_FILES
            return 0
        pe = ctypes.cast(pe_ptr, ctypes.POINTER(w32.PROCESSENTRY32W)).contents
        return self._fill_pe(pe, state["entries"][state["idx"]])

    def _fill_pe(self, pe: w32.PROCESSENTRY32W, entry: _FakeProcEntry) -> int:
        pe.th32ProcessID = entry.pid
        pe.th32ParentProcessID = entry.ppid
        pe.cntThreads = 1
        raw_exe = entry.exe_name.encode("utf-16-le")
        for i in range(w32.MAX_PATH):
            pe.szExeFile[i] = 0
        for i in range(min(len(raw_exe) // 2, w32.MAX_PATH - 1)):
            pe.szExeFile[i] = struct.unpack_from("<H", raw_exe, i * 2)[0]
        return 1


class TestPlatformProcImportHygiene(unittest.TestCase):
    """Ensure importing mesh.platform.proc stays cleanly isolated per OS."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX import-hygiene check")
    def test_linux_import_never_touches_windows_modules(self) -> None:
        code = (
            "import ctypes, sys\n"
            "assert not hasattr(ctypes, 'windll')\n"
            "import mesh.platform\n"
            "from mesh.platform import proc\n"
            "for mod in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32_ctypes', 'mesh.platform._win32'):\n"
            "    assert mod not in sys.modules, f'unexpectedly imported {mod}'\n"
        )
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root)
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}\nstdout={res.stdout}")

    def test_proc_import_under_win32_never_requires_fcntl_or_posix(self) -> None:
        code = (
            win_sim.SIMULATED_WIN32_PRELUDE
            + "from mesh.platform import proc\n"
            "assert 'mesh.platform._posix' not in sys.modules\n"
        )
        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        env = dict(os.environ, PYTHONPATH=repo_root)
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, f"stderr={res.stderr}\nstdout={res.stdout}")


class TestPosixProcDelegation(unittest.TestCase):
    """Verify POSIX delegation preserves existing mesh.core.proc behavior."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only behavior")
    def test_posix_delegates_to_core_proc_unchanged(self) -> None:
        run_id = f"run_posix_t06_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        env = dict(os.environ)
        env["MESH_RUN_ID"] = run_id
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; print('ready', flush=True); time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            st = plat_proc.get_proc_stat(child.pid)
            self.assertIsNotNone(st)
            self.assertEqual(st, core_proc.get_proc_stat(child.pid))
            self.assertTrue(plat_proc.is_tgid_alive(child.pid, st))
            self.assertTrue(plat_proc.is_process_alive(child.pid, st["starttime"]))
            self.assertFalse(plat_proc.is_process_alive(child.pid, st["starttime"] + 1))
            self.assertFalse(plat_proc.is_process_alive(child.pid, 0))
            self.assertEqual(plat_proc.get_proc_ancestry(child.pid, {os.getpid()}), [os.getpid()])
            c_env = plat_proc.get_proc_environ(child.pid)
            self.assertIsNotNone(c_env)
            self.assertEqual(c_env.get("MESH_RUN_ID"), run_id)

            child.kill()
            child.wait(timeout=10)
            self.assertFalse(plat_proc.is_tgid_alive(child.pid))
            self.assertFalse(plat_proc.is_process_alive(child.pid, st["starttime"]))
            self.assertIsNone(plat_proc.get_proc_stat(child.pid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()

        with mock.patch("mesh.platform._posix.os.kill") as mock_kill:
            plat_proc.crash_self_now()
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGKILL)


class TestWin32ProcStructsAndPureLogic(unittest.TestCase):
    """Cross-platform ABI layout, DLL binding, and pure-logic tests."""

    def test_win32_proc_struct_layouts_and_bindings(self) -> None:
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8

        self.assertEqual(ctypes.sizeof(w32.FILETIME), 8)
        self.assertEqual(w32.FILETIME.dwLowDateTime.offset, 0)
        self.assertEqual(w32.FILETIME.dwHighDateTime.offset, 4)

        self.assertEqual(ctypes.sizeof(w32.PROCESSENTRY32W), 568 if is_64 else 556)
        self.assertEqual(w32.PROCESSENTRY32W.dwSize.offset, 0)
        self.assertEqual(w32.PROCESSENTRY32W.th32ProcessID.offset, 8)
        self.assertEqual(w32.PROCESSENTRY32W.th32DefaultHeapID.offset, 16 if is_64 else 12)
        self.assertEqual(w32.PROCESSENTRY32W.th32ParentProcessID.offset, 32 if is_64 else 24)
        self.assertEqual(w32.PROCESSENTRY32W.szExeFile.offset, 44 if is_64 else 36)

        self.assertEqual(ctypes.sizeof(w32.PROCESS_BASIC_INFORMATION), 48 if is_64 else 24)
        self.assertEqual(w32.PROCESS_BASIC_INFORMATION.ExitStatus.offset, 0)
        self.assertEqual(w32.PROCESS_BASIC_INFORMATION.PebBaseAddress.offset, 8 if is_64 else 4)
        self.assertEqual(w32.PROCESS_BASIC_INFORMATION.UniqueProcessId.offset, 32 if is_64 else 16)
        self.assertEqual(
            w32.PROCESS_BASIC_INFORMATION.InheritedFromUniqueProcessId.offset,
            40 if is_64 else 20,
        )

        self.assertEqual(ctypes.sizeof(w32.PEB64), 0x28)
        self.assertEqual(w32.PEB64.ProcessParameters.offset, 0x20)
        self.assertEqual(ctypes.sizeof(w32.PEB32), 0x14)
        self.assertEqual(w32.PEB32.ProcessParameters.offset, 0x10)

        self.assertEqual(ctypes.sizeof(w32.RTL_USER_PROCESS_PARAMETERS64), 0x3F8)
        self.assertEqual(w32.RTL_USER_PROCESS_PARAMETERS64.Environment.offset, 0x80)
        self.assertEqual(w32.RTL_USER_PROCESS_PARAMETERS64.EnvironmentSize.offset, 0x3F0)

        self.assertEqual(ctypes.sizeof(w32.RTL_USER_PROCESS_PARAMETERS32), 0x294)
        self.assertEqual(w32.RTL_USER_PROCESS_PARAMETERS32.Environment.offset, 0x48)
        self.assertEqual(w32.RTL_USER_PROCESS_PARAMETERS32.EnvironmentSize.offset, 0x290)

        class _FakeFn:
            pass

        class _FakeK32:
            def __init__(self) -> None:
                for name in (
                    "OpenProcess",
                    "GetCurrentProcessId",
                    "WaitForSingleObject",
                    "GetExitCodeProcess",
                    "GetProcessTimes",
                    "TerminateProcess",
                    "CreateToolhelp32Snapshot",
                    "Process32FirstW",
                    "Process32NextW",
                    "ReadProcessMemory",
                    "IsWow64Process2",
                ):
                    setattr(self, name, _FakeFn())

        class _FakeNt:
            def __init__(self) -> None:
                self.NtQueryInformationProcess = _FakeFn()

        k32 = w32.bind_kernel32_proc(_FakeK32())
        self.assertEqual(k32.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(len(k32.OpenProcess.argtypes), 3)
        self.assertEqual(k32.WaitForSingleObject.restype, wintypes.DWORD)
        self.assertEqual(k32.GetExitCodeProcess.restype, wintypes.BOOL)
        self.assertEqual(k32.GetProcessTimes.restype, wintypes.BOOL)
        self.assertEqual(len(k32.GetProcessTimes.argtypes), 5)
        self.assertEqual(k32.TerminateProcess.restype, wintypes.BOOL)
        self.assertEqual(k32.CreateToolhelp32Snapshot.restype, wintypes.HANDLE)
        self.assertEqual(k32.Process32FirstW.restype, wintypes.BOOL)
        self.assertEqual(k32.Process32NextW.restype, wintypes.BOOL)
        self.assertEqual(k32.ReadProcessMemory.restype, wintypes.BOOL)
        self.assertEqual(len(k32.ReadProcessMemory.argtypes), 5)
        self.assertEqual(k32.IsWow64Process2.restype, wintypes.BOOL)

        nt = w32.bind_ntdll_proc(_FakeNt())
        self.assertEqual(nt.NtQueryInformationProcess.restype, ctypes.c_int32)
        self.assertEqual(len(nt.NtQueryInformationProcess.argtypes), 5)

    def test_utf16le_env_parser_and_double_nul_boundaries(self) -> None:
        self.assertEqual(_win32.parse_win_env_block(b""), {})
        self.assertEqual(_win32.parse_win_env_block(b"\x00\x00"), {})

        # Non-ASCII U+0100 after ASCII creates an odd-aligned 00 00 sequence in raw bytes;
        # _find_utf16le_env_end must ignore odd offsets and stop at the real even terminator.
        raw = _encode_win_env(
            [
                ("=C:", "C:\\ignored"),
                ("ASCII", "A"),
                ("UNICODE", "\u0100\u6000"),
                ("MESH_RUN_ID", "run_1=2=3"),
                ("EMPTY", ""),
            ]
        ) + b"AFTER_GARBAGE=1\x00\x00\x00\x00"
        end = _win32._find_utf16le_env_end(raw)
        self.assertIsNotNone(end)
        parsed = _win32.parse_win_env_block(raw[:end])
        self.assertEqual(
            parsed,
            {
                "ASCII": "A",
                "UNICODE": "\u0100\u6000",
                "MESH_RUN_ID": "run_1=2=3",
                "EMPTY": "",
            },
        )
        self.assertNotIn("AFTER_GARBAGE", parsed)

        # Unterminated buffer returns None from _find_utf16le_env_end
        self.assertIsNone(_win32._find_utf16le_env_end("FOO=bar\x00".encode("utf-16-le")))


class TestWin32ProcMocked(unittest.TestCase):
    """Mocked Win32 syscall tests covering identity, liveness, ancestry, PEB, and fail-closed."""

    def test_get_proc_stat_and_liveness_with_exit_code_259_and_pid_reuse(self) -> None:
        ct = 0x01DB1234_56789ABC
        procs = {
            200: _FakeProcEntry(pid=200, ppid=100, creation_time=ct, alive=True),
            # Dead process whose exit code happens to equal STILL_ACTIVE (259)
            201: _FakeProcEntry(pid=201, ppid=100, creation_time=ct, alive=False, exit_code=259),
            # Access denied protected/foreign process
            202: _FakeProcEntry(pid=202, ppid=4, creation_time=ct, alive=True, access_denied_open=True),
            # Process where NtQueryInformationProcess fails with STATUS_ACCESS_DENIED
            203: _FakeProcEntry(pid=203, ppid=100, creation_time=ct, alive=True, access_denied_pbi=True),
        }
        fake = _FakeWinProcKernelAndNt(procs)

        st = _win32.get_proc_stat_win(
            200, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertEqual(
            st,
            {
                "state": "R",
                "ppid": 100,
                "pgrp": 0,
                "session": 0,
                "num_threads": 1,
                "starttime": ct,
            },
        )
        self.assertTrue(
            _win32.is_process_alive_win(
                200, ct, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )
        # Mismatched creation time -> PID reused -> False
        self.assertFalse(
            _win32.is_process_alive_win(
                200, ct + 1, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )
        # Zero/negative expected starttime -> fail-closed False
        self.assertFalse(
            _win32.is_process_alive_win(
                200, 0, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertFalse(
            _win32.is_process_alive_win(
                200, -5, k32=fake, get_last_error_fn=fake.get_last_error
            )
        )

        # Exit code 259 (STILL_ACTIVE) on dead process MUST still report dead/None
        self.assertIsNone(
            _win32.get_proc_stat_win(
                201, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertFalse(
            _win32.is_tgid_alive_win(201, k32=fake, get_last_error_fn=fake.get_last_error)
        )
        self.assertFalse(
            _win32.is_process_alive_win(201, ct, k32=fake, get_last_error_fn=fake.get_last_error)
        )

        # Missing PID -> None / False
        self.assertIsNone(
            _win32.get_proc_stat_win(
                9999, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertFalse(
            _win32.is_process_alive_win(9999, ct, k32=fake, get_last_error_fn=fake.get_last_error)
        )

        # Access denied MUST fail closed (raise PermissionError, never return None or False)
        with self.assertRaises(PermissionError):
            _win32.get_proc_stat_win(
                202, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        with self.assertRaises(PermissionError):
            _win32.is_process_alive_win(
                202, ct, k32=fake, get_last_error_fn=fake.get_last_error
            )
        with self.assertRaises(PermissionError):
            _win32.get_proc_stat_win(
                203, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )

        # GetProcessTimes error propagates
        fake.get_process_times_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            _win32.get_proc_stat_win(
                200, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        fake.get_process_times_error = None

        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_toolhelp_ancestry_and_stale_ppid_guard(self) -> None:
        procs = {
            100: _FakeProcEntry(pid=100, ppid=50, creation_time=1000),  # meshd
            200: _FakeProcEntry(pid=200, ppid=100, creation_time=2000),  # wrapper (same tick allowed below)
            300: _FakeProcEntry(pid=300, ppid=200, creation_time=2000),  # equal CreationTime allowed
            400: _FakeProcEntry(pid=400, ppid=300, creation_time=3000),  # grandchild
            # Orphan whose PPID 500 was reused by a NEWER process (creation_time 9000 > 4000)
            500: _FakeProcEntry(pid=500, ppid=100, creation_time=9000),
            600: _FakeProcEntry(pid=600, ppid=500, creation_time=4000),
            # Child of missing/dead PPID 777
            700: _FakeProcEntry(pid=700, ppid=777, creation_time=4000),
            # Privileged boundary ancestor
            800: _FakeProcEntry(pid=800, ppid=4, creation_time=500, access_denied_open=True),
            801: _FakeProcEntry(pid=801, ppid=800, creation_time=4500),
            # Cycle guard
            900: _FakeProcEntry(pid=900, ppid=901, creation_time=5000),
            901: _FakeProcEntry(pid=901, ppid=900, creation_time=5000),
        }
        fake = _FakeWinProcKernelAndNt(procs)
        fake.snapshot_errors = [w32.ERROR_BAD_LENGTH]

        # Normal multi-hop chain stopping at meshd (100), including equal-CreationTime step (300 -> 200)
        self.assertEqual(
            _win32.get_proc_ancestry_win(
                400, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            ),
            [300, 200, 100],
        )

        # Stale PPID reused by newer process (500 created after 600): must NOT include 500 or 100!
        self.assertEqual(
            _win32.get_proc_ancestry_win(
                600, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            ),
            [],
        )

        # Dead parent PPID
        self.assertEqual(
            _win32.get_proc_ancestry_win(
                700, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            ),
            [],
        )

        # Privilege boundary on ancestor stops cleanly without trusting 800
        self.assertEqual(
            _win32.get_proc_ancestry_win(
                801, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            ),
            [],
        )

        # Privilege failure on target pid itself MUST fail closed (raise PermissionError)
        with self.assertRaises(PermissionError):
            _win32.get_proc_ancestry_win(
                800, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            )

        # Cycle terminates cleanly without looping
        self.assertEqual(
            _win32.get_proc_ancestry_win(
                900, {1, 100}, k32=fake, get_last_error_fn=fake.get_last_error
            ),
            [901],
        )

        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_peb_env_reader_64bit_32bit_wow64_and_error_cases(self) -> None:
        long_items = [(f"KEY_{i:03d}", "X" * 100) for i in range(40)] + [
            ("MESH_RUN_ID", "run_multipage_64")
        ]
        long_raw = _encode_win_env(long_items)
        next_unmapped_page = (0x50000100 + len(long_raw) + 4095) & ~4095
        procs = {
            # 64-bit process with multi-page env and trailing unmapped page boundary
            1001: _FakeProcEntry(
                pid=1001,
                ppid=100,
                creation_time=2000,
                wow64=False,
                env_items=long_items,
                env_base_addr=0x50000100,
                env_reported_size=len(long_raw) + 4096,
                unmapped_pages={next_unmapped_page},
            ),
            # 32-bit WOW64 process
            1002: _FakeProcEntry(
                pid=1002,
                ppid=100,
                creation_time=2100,
                wow64=True,
                env_items=[("MESH_RUN_ID", "run_wow64_32"), ("UNICODE_K", "\u0100ok")],
            ),
            # Genuinely empty environment
            1003: _FakeProcEntry(
                pid=1003,
                ppid=100,
                creation_time=2200,
                wow64=False,
                env_items=[],
            ),
            # Uninitialized ProcessParameters (young live process) -> raises OSError (fail-closed)
            1004: _FakeProcEntry(
                pid=1004,
                ppid=100,
                creation_time=2300,
                wow64=False,
                params_null=True,
            ),
            # Access denied on ReadProcessMemory -> raises PermissionError
            1005: _FakeProcEntry(
                pid=1005,
                ppid=100,
                creation_time=2400,
                wow64=False,
                access_denied_vm=True,
            ),
            # Process exits mid-PEB read -> returns None
            1006: _FakeProcEntry(
                pid=1006,
                ppid=100,
                creation_time=2500,
                wow64=False,
                env_items=[("MESH_RUN_ID", "run_dying")],
                die_after_vm_reads=2,
            ),
        }
        fake = _FakeWinProcKernelAndNt(procs)

        env64 = _win32.get_proc_environ_win(
            1001, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertIsNotNone(env64)
        self.assertEqual(env64["MESH_RUN_ID"], "run_multipage_64")
        self.assertEqual(env64["KEY_039"], "X" * 100)

        env32 = _win32.get_proc_environ_win(
            1002, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertEqual(env32, {"MESH_RUN_ID": "run_wow64_32", "UNICODE_K": "\u0100ok"})

        env_empty = _win32.get_proc_environ_win(
            1003, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertEqual(env_empty, {})

        with self.assertRaises(OSError):
            _win32.get_proc_environ_win(
                1004, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        with self.assertRaises(PermissionError):
            _win32.get_proc_environ_win(
                1005, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )

        self.assertIsNone(
            _win32.get_proc_environ_win(
                1006, k32=fake, ntdll=fake, get_last_error_fn=fake.get_last_error
            )
        )
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_find_run_processes_and_crash_self_now_mocked(self) -> None:
        procs = {
            100: _FakeProcEntry(pid=100, ppid=50, creation_time=1000),  # meshd (excluded)
            200: _FakeProcEntry(
                pid=200,
                ppid=100,
                creation_time=2000,
                env_items=[("MESH_RUN_ID", "run_A")],
            ),
            201: _FakeProcEntry(
                pid=201,
                ppid=200,
                creation_time=2100,
                env_items=[],  # wiped env, still caught by Toolhelp ancestry to pgid=200
            ),
            202: _FakeProcEntry(
                pid=202,
                ppid=50,
                creation_time=2200,
                env_items=[("MESH_RUN_ID", "run_A")],  # escaped, caught via PEB MESH_RUN_ID
            ),
            # Foreign bystander with access denied must be skipped cleanly without uncertain=True
            203: _FakeProcEntry(pid=203, ppid=4, creation_time=500, access_denied_open=True),
            204: _FakeProcEntry(
                pid=204,
                ppid=50,
                creation_time=2300,
                env_items=[("MESH_RUN_ID", "run_B")],
            ),
        }
        fake = _FakeWinProcKernelAndNt(procs)
        found = _win32.find_run_processes_win(
            "run_A",
            pgid=200,
            exclude={100},
            k32=fake,
            ntdll=fake,
            get_last_error_fn=fake.get_last_error,
        )
        self.assertEqual(list(found), [200, 201, 202])
        self.assertFalse(found.uncertain)

        _win32.crash_self_now_win(k32=fake)
        self.assertEqual(fake.terminated_calls, [(-1, 137)])
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_win32_dispatch_via_platform_and_core_proc(self) -> None:
        procs = {
            100: _FakeProcEntry(pid=100, ppid=50, creation_time=1000),
            200: _FakeProcEntry(
                pid=200,
                ppid=100,
                creation_time=2000,
                env_items=[("MESH_RUN_ID", "run_dispatch")],
            ),
        }
        fake = _FakeWinProcKernelAndNt(procs)
        with (
            mock.patch.object(plat_proc.sys, "platform", "win32"),
            mock.patch.object(core_proc.sys, "platform", "win32"),
            mock.patch.object(w32, "get_kernel32", return_value=fake),
            mock.patch.object(w32, "get_ntdll", return_value=fake),
            mock.patch.object(w32, "get_last_error", side_effect=fake.get_last_error),
        ):
            st = core_proc.get_proc_stat(200)
            self.assertEqual(st["starttime"], 2000)
            self.assertEqual(st["ppid"], 100)
            self.assertTrue(core_proc.is_tgid_alive(200, st))
            self.assertTrue(plat_proc.is_process_alive(200, 2000))
            self.assertFalse(plat_proc.is_process_alive(200, 2001))
            self.assertEqual(core_proc.get_proc_ancestry(200, {100}), [100])
            self.assertEqual(core_proc.get_proc_environ(200), {"MESH_RUN_ID": "run_dispatch"})
            self.assertEqual(list(core_proc.find_run_processes("run_dispatch", 200, exclude={100})), [200])
            plat_proc.crash_self_now()
        self.assertEqual(fake.terminated_calls, [(-1, 137)])
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_p2_4_find_run_processes_case_insensitive_env_mocked(self) -> None:
        procs = {
            100: _FakeProcEntry(pid=100, ppid=50, creation_time=1000),
            205: _FakeProcEntry(
                pid=205,
                ppid=50,
                creation_time=2400,
                env_items=[("mesh_run_id", "run_case")],
            ),
            206: _FakeProcEntry(
                pid=206,
                ppid=50,
                creation_time=2450,
                env_items=[("Mesh_Run_Id", "other_run")],
            ),
        }
        fake = _FakeWinProcKernelAndNt(procs)
        found = _win32.find_run_processes_win(
            "run_case",
            pgid=999,
            exclude={100},
            k32=fake,
            ntdll=fake,
            get_last_error_fn=fake.get_last_error,
        )
        self.assertEqual(list(found), [205])
        self.assertFalse(found.uncertain)


def _read_line_bounded(pipe: Any, timeout_s: float = 15.0) -> str:
    """Read one line from *pipe* on a background thread with a hard timeout."""
    q: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            q.put(pipe.readline())
        except Exception:
            q.put("")

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        return q.get(timeout=timeout_s).strip()
    except queue.Empty as exc:
        raise AssertionError(f"Child failed to emit line within {timeout_s:g}s") from exc


@unittest.skipUnless(sys.platform == "win32", "Windows real-process Win32 API tests")
class TestWin32ProcRealProcesses(unittest.TestCase):
    """Real Windows process tests exercising GetProcessTimes, Toolhelp32, PEB, and liveness."""

    def test_win_real_process_identity_liveness_ancestry_and_peb_env(self) -> None:
        run_id = f"run_win_t06_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        unicode_val = "alpha-\u0100-omega"
        env = dict(os.environ)
        env["MESH_RUN_ID"] = run_id
        env["MESH_UNICODE_VAL"] = unicode_val

        with tempfile.TemporaryDirectory() as td:
            stop_flag = os.path.join(td, "stop_gc")
            gc_code = (
                "import os, sys, time\n"
                "flag = sys.argv[1]\n"
                "for _ in range(300):\n"
                "    if os.path.exists(flag):\n"
                "        break\n"
                "    time.sleep(0.05)\n"
            )
            child_code = (
                "import subprocess, sys, time\n"
                "gc = subprocess.Popen(\n"
                "    [sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
                "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
                ")\n"
                "print(gc.pid, flush=True)\n"
                "sys.stdin.read()\n"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, gc_code, stop_flag],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                text=True,
            )
            gc_pid: int | None = None
            try:
                line = _read_line_bounded(child.stdout)
                self.assertTrue(line.isdigit(), f"Expected grandchild PID, got {line!r}")
                gc_pid = int(line)

                parent_stat = plat_proc.get_proc_stat(os.getpid())
                child_stat_1 = plat_proc.get_proc_stat(child.pid)
                child_stat_2 = plat_proc.get_proc_stat(child.pid)
                gc_stat = plat_proc.get_proc_stat(gc_pid)

                self.assertIsNotNone(parent_stat)
                self.assertIsNotNone(child_stat_1)
                self.assertIsNotNone(child_stat_2)
                self.assertIsNotNone(gc_stat)

                # CreationTime stability and monotonicity across parent -> child -> grandchild
                self.assertGreater(child_stat_1["starttime"], 0)
                self.assertEqual(child_stat_1["starttime"], child_stat_2["starttime"])
                self.assertGreaterEqual(child_stat_1["starttime"], parent_stat["starttime"])
                self.assertGreaterEqual(gc_stat["starttime"], child_stat_1["starttime"])
                self.assertEqual(child_stat_1["ppid"], os.getpid())
                self.assertEqual(gc_stat["ppid"], child.pid)

                # Liveness & PID-reuse protection
                self.assertTrue(plat_proc.is_tgid_alive(child.pid))
                self.assertTrue(plat_proc.is_process_alive(child.pid, child_stat_1["starttime"]))
                self.assertFalse(plat_proc.is_process_alive(child.pid, child_stat_1["starttime"] + 1))
                self.assertFalse(plat_proc.is_process_alive(child.pid, 0))

                # Toolhelp32 ancestry chain
                self.assertEqual(
                    plat_proc.get_proc_ancestry(gc_pid, stop_at_pids={os.getpid()}),
                    [child.pid, os.getpid()],
                )

                # Remote PEB environment reading (direct & via mesh.core.proc)
                c_env = plat_proc.get_proc_environ(child.pid)
                self.assertIsNotNone(c_env)
                self.assertEqual(c_env.get("MESH_RUN_ID"), run_id)
                self.assertEqual(c_env.get("MESH_UNICODE_VAL"), unicode_val)

                gc_env = core_proc.get_proc_environ(gc_pid)
                self.assertIsNotNone(gc_env)
                self.assertEqual(gc_env.get("MESH_RUN_ID"), run_id)

                found = plat_proc.find_run_processes(run_id, child.pid, exclude={os.getpid()})
                self.assertIn(child.pid, found)
                self.assertIn(gc_pid, found)

                # Kill child, wait for exit -> child must appear gone while grandchild stays alive
                child.kill()
                child.wait(timeout=10)
                self.assertFalse(plat_proc.is_tgid_alive(child.pid))
                self.assertFalse(plat_proc.is_process_alive(child.pid, child_stat_1["starttime"]))
                self.assertIsNone(plat_proc.get_proc_stat(child.pid))
                self.assertIsNone(plat_proc.get_proc_environ(child.pid))

                # Orphaned grandchild must NOT forge ancestry through exited parent
                self.assertEqual(
                    plat_proc.get_proc_ancestry(gc_pid, stop_at_pids={os.getpid()}),
                    [],
                )

                # Stop grandchild cleanly via flag file and verify liveness turns False
                pathlib.Path(stop_flag).write_text("1", encoding="utf-8")
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and plat_proc.is_process_alive(
                    gc_pid, gc_stat["starttime"]
                ):
                    time.sleep(0.05)
                self.assertFalse(plat_proc.is_process_alive(gc_pid, gc_stat["starttime"]))
                self.assertIsNone(plat_proc.get_proc_stat(gc_pid))
            finally:
                pathlib.Path(stop_flag).write_text("1", encoding="utf-8")
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                if gc_pid is not None and plat_proc.is_tgid_alive(gc_pid):
                    k32 = w32.get_kernel32()
                    h = k32.OpenProcess(w32.PROCESS_TERMINATE, False, gc_pid)
                    if not w32.is_null_or_invalid_handle(h):
                        try:
                            k32.TerminateProcess(h, 1)
                        finally:
                            k32.CloseHandle(h)

    def test_win_real_process_exit_code_259_not_misread_as_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])
        try:
            self.assertEqual(proc.wait(timeout=10), 259)
            self.assertFalse(plat_proc.is_tgid_alive(proc.pid))
            self.assertFalse(plat_proc.is_process_alive(proc.pid))
            self.assertIsNone(plat_proc.get_proc_stat(proc.pid))
            self.assertIsNone(plat_proc.get_proc_environ(proc.pid))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
