"""Unit tests for Windows Named Pipe IPC, peer SID/PID auth, and run-session classification (Phase 5 / T09)."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import errno
import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

import mesh.constants as c
from mesh.core import auth
from mesh.core.db import connect_db, init_db, transaction
from mesh.core.events import record_event
from mesh.core.init import init_mesh_home
from mesh.errors import MeshError
from mesh.platform import _win32, _win32_ctypes as w32, ipc, perms, proc as plat_proc


def _encode_sid(sid_str: str) -> bytes:
    parts = sid_str.split("-")
    rev = int(parts[1])
    auth_id = int(parts[2])
    subs = [int(x) for x in parts[3:]]
    return bytes([rev, len(subs)]) + auth_id.to_bytes(6, "big") + b"".join(
        struct.pack("<I", s) for s in subs
    )


@dataclass
class _FakePeerProc:
    pid: int
    user_sid: str
    alive: bool = True
    access_denied_open: bool = False
    access_denied_token: bool = False


class _FakeWinIpcKernel:
    """In-memory fake kernel32 + advapi32 simulating Named Pipes, overlapped I/O, and tokens."""

    def __init__(self, *, daemon_sid: str = "S-1-5-21-100-200-300-1001") -> None:
        self.daemon_sid = daemon_sid
        self.last_error = 0
        self.next_handle = 0x2000
        self.open_handles: list[int] = []
        self.closed_handles: list[int] = []
        self.handle_table: dict[int, tuple[str, Any]] = {}
        self.local_allocs: list[int] = []
        self.local_frees: list[int] = []
        self._pinned_bufs: dict[int, Any] = {}

        self.pipes_created: list[tuple[str, int, int, int, int, int, int, str]] = []
        self.pipe_peers: dict[int, int] = {}
        self.pipe_servers: dict[int, int] = {}
        self.default_server_pid: int = 100
        self.fail_get_server_pid_error: int | None = None
        self.procs: dict[int, _FakePeerProc] = {
            100: _FakePeerProc(pid=100, user_sid=daemon_sid),
        }

        # Scriptable behaviors for connect / overlapped I/O / peer errors
        self.fail_create_pipe_error: int | None = None
        self.fail_get_client_pid_error: int | None = None
        self.connect_results: list[tuple[str, Any]] = []
        self.wait_pipe_results: list[tuple[bool, int]] = []
        self.file_type_override: int = w32.FILE_TYPE_PIPE
        self.read_queue: list[tuple[str, Any]] = []
        self.write_queue: list[tuple[str, Any]] = []
        self.wait_object_results: list[int] = []
        self.overlapped_results: list[tuple[bool, int, int]] = []
        self.cancel_calls: list[int] = []
        self.flush_calls: list[int] = []
        self.flush_error: int | None = None
        self.sleep_calls: list[float] = []
        self.now_s = 100.0

    def monotonic(self) -> float:
        return self.now_s

    def sleep(self, secs: float) -> None:
        self.sleep_calls.append(secs)
        self.now_s += secs

    def get_last_error(self) -> int:
        return self.last_error

    def _alloc_handle(self, kind: str, payload: Any) -> int:
        self.next_handle += 4
        h = self.next_handle
        self.open_handles.append(h)
        self.handle_table[h] = (kind, payload)
        return h

    def _alloc_local_bytes(self, raw: bytes) -> int:
        buf = ctypes.create_string_buffer(raw, len(raw))
        addr = ctypes.addressof(buf)
        self.local_allocs.append(addr)
        self._pinned_bufs[addr] = buf
        return addr

    @staticmethod
    def _h_int(handle: Any) -> int:
        if hasattr(handle, "value"):
            return int(handle.value or 0)
        return int(handle)

    def LocalFree(self, ptr: Any) -> int:
        val = self._h_int(ptr) if ptr else 0
        if val:
            self.local_frees.append(val)
            self._pinned_bufs.pop(val, None)
        return 0

    def CloseHandle(self, handle: Any) -> int:
        h = self._h_int(handle)
        self.closed_handles.append(h)
        self.handle_table.pop(h, None)
        return 1

    def GetCurrentProcess(self) -> int:
        return -1

    # --- Token & SDDL ---
    def OpenProcess(self, access: int, inherit: bool, pid: int) -> Any:
        _ = (access, inherit)
        p = self.procs.get(pid)
        if p is None:
            self.last_error = w32.ERROR_INVALID_PARAMETER
            return None
        if p.access_denied_open:
            self.last_error = w32.ERROR_ACCESS_DENIED
            return None
        self.last_error = 0
        return self._alloc_handle("proc", p)

    def WaitForSingleObject(self, handle: Any, timeout_ms: int) -> int:
        _ = timeout_ms
        item = self.handle_table.get(self._h_int(handle))
        if item is not None and item[0] == "proc":
            p = item[1]
            return w32.WAIT_TIMEOUT if p.alive else w32.WAIT_OBJECT_0
        if self.wait_object_results:
            rc = self.wait_object_results.pop(0)
            if rc == w32.WAIT_TIMEOUT:
                self.now_s += 0.25
            return rc
        return w32.WAIT_OBJECT_0

    def GetExitCodeProcess(self, handle: Any, out_ptr: Any) -> int:
        item = self.handle_table.get(self._h_int(handle))
        if item is None or item[0] != "proc":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        p = item[1]
        ctypes.cast(out_ptr, wintypes.LPDWORD)[0] = w32.STILL_ACTIVE if p.alive else 0
        return 1

    def OpenProcessToken(self, proc_handle: Any, access: int, out_ptr: Any) -> int:
        _ = access
        if self._h_int(proc_handle) == -1:
            sid = self.daemon_sid
        else:
            item = self.handle_table.get(self._h_int(proc_handle))
            if item is None or item[0] != "proc":
                self.last_error = w32.ERROR_INVALID_HANDLE
                return 0
            p = item[1]
            if p.access_denied_token:
                self.last_error = w32.ERROR_ACCESS_DENIED
                return 0
            sid = p.user_sid
        h = self._alloc_handle("token", sid)
        ctypes.cast(out_ptr, ctypes.POINTER(wintypes.HANDLE))[0] = h
        return 1

    def GetTokenInformation(
        self, tok_handle: Any, info_class: int, buf: Any, buf_len: int, needed_ptr: Any
    ) -> int:
        item = self.handle_table.get(self._h_int(tok_handle))
        if item is None or item[0] != "token":
            self.last_error = w32.ERROR_INVALID_HANDLE
            return 0
        sid_raw = _encode_sid(item[1])
        hdr_sz = (
            ctypes.sizeof(w32.TOKEN_USER)
            if info_class == w32.TokenUser
            else ctypes.sizeof(w32.TOKEN_OWNER)
        )
        total = hdr_sz + len(sid_raw)
        ctypes.cast(needed_ptr, wintypes.LPDWORD)[0] = total
        if buf is None or buf_len < total:
            self.last_error = w32.ERROR_INSUFFICIENT_BUFFER
            return 0
        sid_addr = ctypes.addressof(buf) + hdr_sz
        ctypes.memmove(sid_addr, sid_raw, len(sid_raw))
        if info_class == w32.TokenUser:
            tu = w32.TOKEN_USER.from_buffer(buf)
            tu.User.Sid = sid_addr
        else:
            to = w32.TOKEN_OWNER.from_buffer(buf)
            to.Owner = sid_addr
        return 1

    def IsValidSid(self, psid: Any) -> int:
        return 1 if (self._h_int(psid) if psid else 0) else 0

    def ConvertSidToStringSidW(self, psid: Any, out_ptr: Any) -> int:
        addr_in = self._h_int(psid)
        hdr = ctypes.string_at(addr_in, 8)
        rev = hdr[0]
        sub_cnt = hdr[1]
        auth_val = int.from_bytes(hdr[2:8], "big")
        full = ctypes.string_at(psid, 8 + 4 * sub_cnt)
        subs = [str(struct.unpack("<I", full[8 + 4 * i : 12 + 4 * i])[0]) for i in range(sub_cnt)]
        sid_str = "-".join(["S", str(rev), str(auth_val)] + subs)
        buf = ctypes.create_unicode_buffer(sid_str)
        addr = ctypes.addressof(buf)
        self.local_allocs.append(addr)
        self._pinned_bufs[addr] = buf
        ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = addr
        return 1

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(
        self, sddl: str, rev: int, out_sd_ptr: Any, out_len_ptr: Any
    ) -> int:
        _ = (rev, out_len_ptr)
        addr = self._alloc_local_bytes((sddl + "\x00").encode("utf-8"))
        ctypes.cast(out_sd_ptr, ctypes.POINTER(ctypes.c_void_p))[0] = addr
        return 1

    # --- Pipe & Overlapped APIs ---
    def CreateNamedPipeW(
        self,
        name: str,
        open_mode: int,
        pipe_mode: int,
        max_inst: int,
        out_sz: int,
        in_sz: int,
        timeout: int,
        sa_ptr: Any,
    ) -> int:
        sddl = ""
        if sa_ptr:
            sa = ctypes.cast(sa_ptr, ctypes.POINTER(w32.SECURITY_ATTRIBUTES)).contents
            if sa.lpSecurityDescriptor:
                sddl = ctypes.string_at(sa.lpSecurityDescriptor).split(b"\x00", 1)[0].decode("utf-8")
        self.pipes_created.append(
            (name, open_mode, pipe_mode, max_inst, out_sz, in_sz, timeout, sddl)
        )
        if self.fail_create_pipe_error is not None:
            self.last_error = self.fail_create_pipe_error
            return w32.INVALID_HANDLE_VALUE
        self.last_error = 0
        return self._alloc_handle("server_pipe", name)

    def GetNamedPipeClientProcessId(self, handle: Any, out_pid_ptr: Any) -> int:
        if self.fail_get_client_pid_error is not None:
            self.last_error = self.fail_get_client_pid_error
            return 0
        pid = self.pipe_peers.get(self._h_int(handle), 0)
        ctypes.cast(out_pid_ptr, ctypes.POINTER(wintypes.ULONG))[0] = pid
        return 1

    def GetNamedPipeServerProcessId(self, handle: Any, out_pid_ptr: Any) -> int:
        if self.fail_get_server_pid_error is not None:
            self.last_error = self.fail_get_server_pid_error
            return 0
        pid = self.pipe_servers.get(self._h_int(handle), self.default_server_pid)
        ctypes.cast(out_pid_ptr, ctypes.POINTER(wintypes.ULONG))[0] = pid
        return 1

    def CreateFileW(
        self,
        name: str,
        access: int,
        share: int,
        sa: Any,
        disp: int,
        flags: int,
        tmpl: Any,
    ) -> int:
        _ = (access, share, sa, disp, flags, tmpl)
        if self.connect_results:
            kind, val = self.connect_results.pop(0)
            if kind == "err":
                self.last_error = int(val)
                return w32.INVALID_HANDLE_VALUE
            self.last_error = 0
            return self._alloc_handle("client_pipe", name)
        self.last_error = 0
        return self._alloc_handle("client_pipe", name)

    def GetFileType(self, handle: Any) -> int:
        _ = handle
        self.last_error = 0
        return self.file_type_override

    def WaitNamedPipeW(self, name: str, timeout_ms: int) -> int:
        _ = (name, timeout_ms)
        if self.wait_pipe_results:
            ok, err = self.wait_pipe_results.pop(0)
            self.last_error = err
            return 1 if ok else 0
        return 1

    def CreateEventW(self, sa: Any, manual: bool, init: bool, name: Any) -> int:
        _ = (sa, manual, init, name)
        return self._alloc_handle("event", None)

    def ReadFile(self, handle: Any, buf: Any, size: int, nread_ptr: Any, ov_ptr: Any) -> int:
        _ = (handle, ov_ptr)
        if self.read_queue:
            kind, val = self.read_queue.pop(0)
            if kind == "sync":
                chunk = val[:size]
                ctypes.memmove(buf, chunk, len(chunk))
                ctypes.cast(nread_ptr, wintypes.LPDWORD)[0] = len(chunk)
                return 1
            if kind == "pending_ok":
                chunk = val[:size]
                ctypes.memmove(buf, chunk, len(chunk))
                self.overlapped_results.append((True, len(chunk), 0))
                self.last_error = w32.ERROR_IO_PENDING
                return 0
            if kind == "pending_err":
                self.overlapped_results.append((False, 0, int(val)))
                self.last_error = w32.ERROR_IO_PENDING
                return 0
            if kind == "err":
                self.last_error = int(val)
                return 0
        ctypes.cast(nread_ptr, wintypes.LPDWORD)[0] = 0
        return 1

    def WriteFile(self, handle: Any, buf: Any, size: int, nwritten_ptr: Any, ov_ptr: Any) -> int:
        _ = (handle, buf, ov_ptr)
        if self.write_queue:
            kind, val = self.write_queue.pop(0)
            if kind == "sync":
                ctypes.cast(nwritten_ptr, wintypes.LPDWORD)[0] = int(val)
                return 1
            if kind == "pending_ok":
                self.overlapped_results.append((True, int(val), 0))
                self.last_error = w32.ERROR_IO_PENDING
                return 0
            if kind == "pending_err":
                self.overlapped_results.append((False, 0, int(val)))
                self.last_error = w32.ERROR_IO_PENDING
                return 0
            if kind == "err":
                self.last_error = int(val)
                return 0
        ctypes.cast(nwritten_ptr, wintypes.LPDWORD)[0] = size
        return 1

    def GetOverlappedResult(
        self, handle: Any, ov_ptr: Any, transferred_ptr: Any, wait: bool
    ) -> int:
        _ = (handle, ov_ptr, wait)
        if self.overlapped_results:
            ok, nbytes, err = self.overlapped_results.pop(0)
            ctypes.cast(transferred_ptr, wintypes.LPDWORD)[0] = nbytes
            self.last_error = err
            return 1 if ok else 0
        ctypes.cast(transferred_ptr, wintypes.LPDWORD)[0] = 0
        return 1

    def CancelIoEx(self, handle: Any, ov_ptr: Any) -> int:
        _ = ov_ptr
        self.cancel_calls.append(self._h_int(handle))
        return 1

    def FlushFileBuffers(self, handle: Any) -> int:
        self.flush_calls.append(self._h_int(handle))
        if self.flush_error is not None:
            self.last_error = self.flush_error
            return 0
        return 1


class TestPlatformIpcHygieneAndLayouts(unittest.TestCase):
    """Import isolation, no-eager-asyncio check, and Win32 IPC ctypes ABI verification."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX import-hygiene check")
    def test_linux_import_never_touches_windows_modules_or_eager_asyncio(self) -> None:
        code = (
            "import ctypes, sys\n"
            "assert not hasattr(ctypes, 'windll')\n"
            "from mesh.platform import ipc\n"
            "from mesh.core import auth\n"
            "from mesh import client\n"
            "from mesh.cli.commands import daemon, doctor_env\n"
            "for mod in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32_ctypes', 'mesh.platform._win32', 'asyncio'):\n"
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

    def test_win32_import_never_loads_posix_or_fcntl(self) -> None:
        code = (
            "import sys, types\n"
            "sys.platform = 'win32'\n"
            "sys.modules['fcntl'] = None\n"
            "for _m in ('_winapi', '_overlapped', 'msvcrt'):\n"
            "    _mod = types.ModuleType(_m)\n"
            "    _mod.__getattr__ = lambda _n: (_ for _ in ()).throw(AttributeError(_n)) if _n.startswith('__') else 0\n"
            "    sys.modules.setdefault(_m, _mod)\n"
            "from mesh.platform import ipc\n"
            "from mesh.core import auth\n"
            "from mesh import client\n"
            "from mesh.daemon import server\n"
            "from mesh.cli.commands import daemon, doctor_env\n"
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

    def test_ipc_ctypes_bindings_and_struct_sizes(self) -> None:
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(w32.OVERLAPPED), 32 if is_64 else 20)
        self.assertEqual(ctypes.sizeof(w32.SECURITY_ATTRIBUTES), 24 if is_64 else 12)
        self.assertEqual(w32.SE_KERNEL_OBJECT, 6)
        self.assertEqual(w32.FILE_FLAG_FIRST_PIPE_INSTANCE, 0x00080000)
        self.assertEqual(w32.PIPE_REJECT_REMOTE_CLIENTS, 0x00000008)

        class _FakeFn:
            pass

        class _FakeK32:
            def __init__(self) -> None:
                for name in (
                    "CreateNamedPipeW",
                    "ConnectNamedPipe",
                    "DisconnectNamedPipe",
                    "WaitNamedPipeW",
                    "GetNamedPipeClientProcessId",
                    "GetNamedPipeServerProcessId",
                    "WriteFile",
                    "FlushFileBuffers",
                    "GetOverlappedResult",
                    "CancelIoEx",
                ):
                    setattr(self, name, _FakeFn())

        k32 = w32.bind_kernel32_ipc(_FakeK32())
        self.assertEqual(k32.CreateNamedPipeW.restype, wintypes.HANDLE)
        self.assertEqual(len(k32.CreateNamedPipeW.argtypes), 8)
        self.assertEqual(k32.ConnectNamedPipe.restype, wintypes.BOOL)
        self.assertEqual(len(k32.ConnectNamedPipe.argtypes), 2)
        self.assertEqual(k32.DisconnectNamedPipe.restype, wintypes.BOOL)
        self.assertEqual(k32.WaitNamedPipeW.restype, wintypes.BOOL)
        self.assertEqual(k32.GetNamedPipeClientProcessId.restype, wintypes.BOOL)
        self.assertEqual(len(k32.GetNamedPipeClientProcessId.argtypes), 2)
        self.assertEqual(k32.WriteFile.restype, wintypes.BOOL)
        self.assertEqual(len(k32.WriteFile.argtypes), 5)
        self.assertEqual(k32.FlushFileBuffers.restype, wintypes.BOOL)
        self.assertEqual(k32.GetOverlappedResult.restype, wintypes.BOOL)
        self.assertEqual(len(k32.GetOverlappedResult.argtypes), 4)
        self.assertEqual(k32.CancelIoEx.restype, wintypes.BOOL)
        self.assertEqual(len(k32.CancelIoEx.argtypes), 2)


class TestWin32IpcMocked(unittest.TestCase):
    """Mocked Win32 Named Pipe server/client, SID/PID auth, and run-session classifier tests."""

    def test_pipe_endpoint_naming_and_pipe_sddl(self) -> None:
        with mock.patch("mesh.platform.ipc.os.path.realpath", side_effect=lambda p: p.replace("/", "\\")):
            ep1 = ipc.daemon_endpoint_path_win("C:/Users/Alice/.agent-mesh")
            ep2 = ipc.daemon_endpoint_path_win("c:\\users\\alice\\.agent-mesh")
            ep3 = ipc.daemon_endpoint_path_win("C:\\Users\\Bob\\.agent-mesh")

        self.assertEqual(ep1, ep2)
        self.assertNotEqual(ep1, ep3)
        self.assertRegex(ep1, r"^\\\\\.\\pipe\\LOCAL\\agent-mesh-[0-9a-f]{32}$")
        self.assertTrue(ipc.is_win_pipe_path(ep1))
        self.assertTrue(ipc.is_win_pipe_path("//./pipe/LOCAL/agent-mesh-0123"))
        self.assertFalse(ipc.is_win_pipe_path("/tmp/mh/run/mesh.sock"))
        self.assertFalse(ipc.is_win_pipe_path("\\\\.\\pipe\\"))

        user_sid = "S-1-5-21-100-200-300-1001"
        sddl = _win32.build_private_pipe_sddl(user_sid)
        self.assertEqual(sddl, f"D:P(A;;GA;;;{user_sid})(A;;GA;;;SY)")
        self.assertEqual(
            _win32.build_private_pipe_sddl(perms.LOCAL_SYSTEM_SID),
            f"D:P(A;;GA;;;{perms.LOCAL_SYSTEM_SID})",
        )
        with self.assertRaises(ValueError):
            _win32.build_private_pipe_sddl("invalid-sid")

    def test_create_named_pipe_instance_flags_sddl_and_error_cleanup(self) -> None:
        fake = _FakeWinIpcKernel()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-" + "a" * 32

        h1 = ipc._create_named_pipe_instance_win(
            pipe_name, first=True, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
        )
        h2 = ipc._create_named_pipe_instance_win(
            pipe_name, first=False, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
        )
        self.assertNotEqual(h1, h2)
        self.assertEqual(len(fake.pipes_created), 2)

        _, open1, mode1, max1, _, _, _, sddl1 = fake.pipes_created[0]
        _, open2, mode2, max2, _, _, _, sddl2 = fake.pipes_created[1]
        self.assertEqual(
            open1, w32.PIPE_ACCESS_DUPLEX | w32.FILE_FLAG_OVERLAPPED | w32.FILE_FLAG_FIRST_PIPE_INSTANCE
        )
        self.assertEqual(open2, w32.PIPE_ACCESS_DUPLEX | w32.FILE_FLAG_OVERLAPPED)
        self.assertTrue(mode1 & w32.PIPE_REJECT_REMOTE_CLIENTS)
        self.assertTrue(mode2 & w32.PIPE_REJECT_REMOTE_CLIENTS)
        self.assertEqual(max1, w32.PIPE_UNLIMITED_INSTANCES)
        self.assertEqual(max2, w32.PIPE_UNLIMITED_INSTANCES)
        self.assertEqual(sddl1, f"D:P(A;;GA;;;{fake.daemon_sid})(A;;GA;;;SY)")
        self.assertEqual(sddl2, sddl1)

        fake.fail_create_pipe_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            ipc._create_named_pipe_instance_win(
                pipe_name, first=True, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
            )
        fake.fail_create_pipe_error = None

        fake.CloseHandle(h1)
        fake.CloseHandle(h2)
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))
        self.assertEqual(sorted(fake.local_frees), sorted(fake.local_allocs))

    def test_verify_pipe_peer_sid_and_pid_fail_closed(self) -> None:
        fake = _FakeWinIpcKernel()
        fake.procs = {
            200: _FakePeerProc(pid=200, user_sid=fake.daemon_sid),
            201: _FakePeerProc(pid=201, user_sid="S-1-5-21-100-200-300-2002"),
            202: _FakePeerProc(pid=202, user_sid=fake.daemon_sid, alive=False),
            203: _FakePeerProc(pid=203, user_sid=fake.daemon_sid, access_denied_open=True),
            204: _FakePeerProc(pid=204, user_sid=fake.daemon_sid, access_denied_token=True),
        }

        h_ok = fake._alloc_handle("server_pipe", "p")
        fake.pipe_peers[h_ok] = 200
        self.assertEqual(
            ipc.verify_pipe_peer_win(
                h_ok, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
            ),
            (200, 0, 0),
        )

        # Mismatched SID -> fail closed
        fake.pipe_peers[h_ok] = 201
        with self.assertRaises(PermissionError):
            ipc.verify_pipe_peer_win(
                h_ok, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
            )

        # Exited / access-denied / missing peer -> fail closed
        for bad_pid in (202, 203, 204, 9999, 0):
            fake.pipe_peers[h_ok] = bad_pid
            with self.assertRaises(PermissionError):
                ipc.verify_pipe_peer_win(
                    h_ok, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
                )

        # GetNamedPipeClientProcessId failure -> fail closed
        fake.fail_get_client_pid_error = w32.ERROR_PIPE_NOT_CONNECTED
        with self.assertRaises(OSError):
            ipc.verify_pipe_peer_win(
                h_ok, k32=fake, advapi=fake, get_last_error_fn=fake.get_last_error
            )
        fake.fail_get_client_pid_error = None

        with self.assertRaises(PermissionError):
            ipc.verify_pipe_peer_win(None)
        with self.assertRaises(PermissionError):
            ipc.verify_pipe_peer_win(-1)

        fake.CloseHandle(h_ok)
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))
        self.assertEqual(sorted(fake.local_frees), sorted(fake.local_allocs))

    def test_connect_and_pipe_client_stream_overlapped_busy_timeout_and_eof(self) -> None:
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-" + "b" * 32
        fake = _FakeWinIpcKernel()

        # 1. Busy -> WaitNamedPipeW timeout -> WaitNamedPipeW succeeds -> connect ok
        fake.connect_results = [
            ("err", w32.ERROR_PIPE_BUSY),
            ("err", w32.ERROR_PIPE_BUSY),
            ("ok", None),
        ]
        fake.wait_pipe_results = [
            (False, w32.ERROR_SEM_TIMEOUT),
            (True, 0),
        ]
        stream = ipc.connect_pipe_win(
            pipe_name,
            timeout=2.0,
            k32=fake,
            advapi=fake,
            get_last_error_fn=fake.get_last_error,
            time_fn=fake.monotonic,
            sleep_fn=fake.sleep,
        )

        # Overlapped pending write + read + makefile + EOF on ERROR_BROKEN_PIPE
        fake.write_queue = [("pending_ok", 14)]
        stream.sendall(b'{"cmd":"ping"}\n')
        fake.read_queue = [
            ("pending_ok", b'{"ok":true}\n'),
            ("pending_err", w32.ERROR_BROKEN_PIPE),
        ]
        f = stream.makefile("r", encoding="utf-8")
        self.assertEqual(f.readline(), '{"ok":true}\n')
        self.assertEqual(f.readline(), "")
        stream.shutdown(socket.SHUT_RDWR)
        self.assertEqual(len(fake.flush_calls), 1)

        # Closing stream while makefile is open defers CloseHandle until f.close()
        stream.close()
        with self.assertRaises(OSError):
            stream.recv(1)
        self.assertNotIn(fake.open_handles[0], fake.closed_handles)
        f.close()
        self.assertIn(fake.open_handles[0], fake.closed_handles)

        # 2. Timeout cancels overlapped ReadFile and drains GetOverlappedResult once
        fake2 = _FakeWinIpcKernel()
        stream2 = ipc.connect_pipe_win(
            pipe_name,
            timeout=0.3,
            k32=fake2,
            advapi=fake2,
            get_last_error_fn=fake2.get_last_error,
            time_fn=fake2.monotonic,
            sleep_fn=fake2.sleep,
        )
        fake2.read_queue = [("pending_err", w32.ERROR_OPERATION_ABORTED)]
        fake2.wait_object_results = [w32.WAIT_TIMEOUT, w32.WAIT_TIMEOUT]
        with self.assertRaises(socket.timeout):
            stream2.recv(64)
        self.assertEqual(len(fake2.cancel_calls), 1)
        stream2.close()
        self.assertEqual(sorted(fake2.closed_handles), sorted(fake2.open_handles))

        # 3. Non-pipe file handle is rejected and closed immediately
        fake3 = _FakeWinIpcKernel()
        fake3.file_type_override = w32.FILE_TYPE_DISK
        with self.assertRaises(OSError) as ctx_np:
            ipc.connect_pipe_win(
                pipe_name,
                timeout=1.0,
                k32=fake3,
                advapi=fake3,
                get_last_error_fn=fake3.get_last_error,
                time_fn=fake3.monotonic,
                sleep_fn=fake3.sleep,
            )
        self.assertEqual(ctx_np.exception.errno, errno.EINVAL)
        self.assertEqual(sorted(fake3.closed_handles), sorted(fake3.open_handles))

        # 4. Busy past deadline raises socket.timeout
        fake4 = _FakeWinIpcKernel()
        fake4.connect_results = [("err", w32.ERROR_PIPE_BUSY)] * 5
        fake4.wait_pipe_results = [(False, w32.ERROR_SEM_TIMEOUT)] * 5
        with self.assertRaises(socket.timeout):
            ipc.connect_pipe_win(
                pipe_name,
                timeout=0.001,
                k32=fake4,
                advapi=fake4,
                get_last_error_fn=fake4.get_last_error,
                time_fn=fake4.monotonic,
                sleep_fn=fake4.sleep,
            )

        # 5. Cross-thread close while overlapped recv is active cancels I/O and defers CloseHandle until recv exits
        fake5 = _FakeWinIpcKernel()
        stream5 = ipc.connect_pipe_win(
            pipe_name,
            timeout=5.0,
            k32=fake5,
            advapi=fake5,
            get_last_error_fn=fake5.get_last_error,
            time_fn=fake5.monotonic,
            sleep_fn=fake5.sleep,
        )
        entered_wait = threading.Event()
        release_wait = threading.Event()
        orig_wait = fake5.WaitForSingleObject

        def _blocking_wait(h_event: Any, ms: int) -> int:
            entered_wait.set()
            release_wait.wait(timeout=2.0)
            return orig_wait(h_event, ms)

        cancel_lock_held: list[bool] = []
        closed_during_cancel: list[bool] = []

        def _cancel_unblocks(handle: Any, ov_ptr: Any) -> int:
            fake5.cancel_calls.append(fake5._h_int(handle))
            if ov_ptr is None:
                cancel_lock_held.append(stream5._state_lock.locked())
                release_wait.set()
                # Give reader thread a chance to reach _end_io(); CloseHandle must stay blocked
                # until close() finishes CancelIoEx and releases _state_lock.
                time.sleep(0.05)
                closed_during_cancel.append(fake5._h_int(handle) in fake5.closed_handles)
            return 1

        fake5.WaitForSingleObject = _blocking_wait  # type: ignore[method-assign]
        fake5.CancelIoEx = _cancel_unblocks  # type: ignore[method-assign]
        fake5.read_queue = [("pending_err", w32.ERROR_OPERATION_ABORTED)]
        fake5.wait_object_results = [w32.WAIT_OBJECT_0]
        recv_errors: list[BaseException] = []

        def _reader() -> None:
            try:
                stream5.recv(64)
            except BaseException as exc:
                recv_errors.append(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        self.assertTrue(entered_wait.wait(timeout=2.0))
        stream5.close()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(recv_errors), 1)
        self.assertIsInstance(recv_errors[0], OSError)
        self.assertEqual(cancel_lock_held, [True])
        self.assertEqual(closed_during_cancel, [False])
        self.assertIn(fake5.open_handles[0], fake5.closed_handles)
        self.assertEqual(len(fake5.cancel_calls), 1)

    def test_win32_classify_peer_run_session_jobs_and_fail_closed(self) -> None:
        from tests.unit.test_platform_win_jobs import _FakeProcObj, _FakeWinJobKernel

        procs = {
            200: _FakeProcObj(pid=200, ppid=100, creation_time=1000),
            300: _FakeProcObj(pid=300, ppid=200, creation_time=2000),
            400: _FakeProcObj(pid=400, ppid=1, creation_time=3000),
            500: _FakeProcObj(pid=500, ppid=1, creation_time=4000, access_denied_open=True),
        }
        fake_jobs = _FakeWinJobKernel(procs, current_pid=100)
        job = _win32.create_run_job_win(
            r"C:\mesh_home",
            "run_win_1",
            k32=fake_jobs,
            advapi=fake_jobs,
            get_last_error_fn=fake_jobs.get_last_error,
            set_last_error_fn=fake_jobs.set_last_error,
        )
        _win32.assign_process_to_job_win(
            job, 300, k32=fake_jobs, get_last_error_fn=fake_jobs.get_last_error
        )

        def fake_find_job(pid, active_runs, *, active_job_handles=None, home=None):
            return _win32.find_active_job_for_pid_win(
                pid,
                active_runs,
                active_job_handles=active_job_handles,
                home=home,
                k32=fake_jobs,
                get_last_error_fn=fake_jobs.get_last_error,
            )

        with (
            mock.patch.object(auth.sys, "platform", "win32"),
            mock.patch.object(auth._plat_proc, "find_active_job_for_pid", side_effect=fake_find_job),
            mock.patch.object(auth, "get_proc_stat", return_value={"session": 0, "ppid": 1}),
            mock.patch.object(auth, "get_proc_ancestry", return_value=[]),
            mock.patch.object(auth, "get_proc_environ", return_value={}),
        ):
            # Inside Job Object even with empty environ and no ancestry -> classified into run_win_1
            self.assertEqual(
                auth.classify_peer_run_session(
                    300,
                    {"run_win_1": 200},
                    {"run_win_1", "run_ended_2"},
                    100,
                    active_job_handles={"run_win_1": job},
                ),
                "run_win_1",
            )
            # Outside Job Object, empty environ -> None
            self.assertIsNone(
                auth.classify_peer_run_session(
                    400,
                    {"run_win_1": 200},
                    {"run_win_1", "run_ended_2"},
                    100,
                    active_job_handles={"run_win_1": job},
                )
            )
            # Unreadable peer process -> fail closed with CREDENTIAL_NOT_ALLOWED_IN_RUN
            with self.assertRaises(MeshError) as ctx:
                auth.classify_peer_run_session(
                    500,
                    {"run_win_1": 200},
                    {"run_win_1"},
                    100,
                    active_job_handles={"run_win_1": job},
                )
            self.assertEqual(ctx.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
            self.assertEqual(ctx.exception.exit_code, c.EXIT_FORBIDDEN)

        job.close()
        self.assertEqual(sorted(fake_jobs.closed_handles), sorted(fake_jobs.open_handles))

    def test_win32_client_daemon_and_doctor_env_wiring_mocked(self) -> None:
        from mesh import client as client_mod
        from mesh.cli.commands import daemon as daemon_cmd, doctor_env

        # Detached Popen kwargs never combine DETACHED_PROCESS with CREATE_NO_WINDOW
        kw = _win32.detached_popen_kwargs_win()
        self.assertEqual(kw["creationflags"], w32.DETACHED_PROCESS | w32.CREATE_NEW_PROCESS_GROUP)
        self.assertEqual(kw["creationflags"] & w32.CREATE_NO_WINDOW, 0)
        self.assertTrue(kw["close_fds"])

        # reexec_clean_env uses subprocess.call + sys.exit
        with mock.patch("subprocess.call", return_value=7) as m_call:
            with self.assertRaises(SystemExit) as ctx_exit:
                _win32.reexec_clean_env_win(["python", "-m", "mesh"], {"PATH": "C:\\bin"})
        self.assertEqual(ctx_exit.exception.code, 7)
        m_call.assert_called_once()

        # MeshClient preserves MESH_SOCKET Windows named pipe without realpath corruption
        pipe_ep = r"\\.\pipe\LOCAL\agent-mesh-" + "c" * 32
        default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
        with (
            mock.patch.object(client_mod.sys, "platform", "win32"),
            mock.patch.object(client_mod, "EXPLICIT_HOME", False),
            mock.patch.dict(os.environ, {"MESH_SOCKET": pipe_ep}, clear=False),
        ):
            os.environ.pop("MESH_HOME", None)
            mc = client_mod.MeshClient(default_home)
            self.assertEqual(mc.socket_path, pipe_ep)

        # doctor_env._daemon_running delegates to ipc.probe_daemon_endpoint
        with mock.patch("mesh.platform.ipc.probe_daemon_endpoint", return_value=True) as m_probe:
            self.assertTrue(doctor_env._daemon_running("/tmp/fake-home"))
        m_probe.assert_called_once_with("/tmp/fake-home", timeout=1.0)

        # daemon.py uses detached_popen_kwargs and mesh_cli_argv under simulated win32
        with (
            mock.patch.object(daemon_cmd.sys, "platform", "win32"),
            mock.patch.object(daemon_cmd, "_meshd_lock_held", return_value=False),
            mock.patch.object(daemon_cmd, "MeshClient") as m_mc,
            mock.patch.object(daemon_cmd.subprocess, "Popen", side_effect=RuntimeError("stop")) as m_popen,
        ):
            m_mc.return_value.home = r"C:\tmp\h"
            m_mc.return_value.execute_cmd.side_effect = RuntimeError("down")
            args = argparse.Namespace(home=r"C:\tmp\h", dry_run=False, user=False)
            with self.assertRaises(RuntimeError):
                daemon_cmd.handle_daemon_start(args)
        _, popen_kw = m_popen.call_args
        self.assertEqual(
            popen_kw["creationflags"], w32.DETACHED_PROCESS | w32.CREATE_NEW_PROCESS_GROUP
        )

    def test_named_pipe_server_transient_create_pipe_failure_and_closed_handle_guard(self) -> None:
        fake = _FakeWinIpcKernel()
        fake.procs = {200: _FakePeerProc(pid=200, user_sid=fake.daemon_sid)}
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-" + "d" * 32

        class _FakePipeHandle:
            def __init__(self, raw_h: int) -> None:
                self._handle: int | None = raw_h
                self._mesh_peer_credentials: tuple[int, int, int] | None = None

            @property
            def handle(self) -> int | None:
                return self._handle

            def fileno(self) -> int:
                if self._handle is None:
                    raise ValueError("I/O operation on closed pipe")
                return self._handle

            def close(self) -> None:
                if self._handle is not None:
                    fake.CloseHandle(self._handle)
                    self._handle = None

        class _FakeFuture:
            def __init__(self, res: Any = None, exc: BaseException | None = None) -> None:
                self._res = res
                self._exc = exc
                self._done = exc is not None or res is not None
                self._cbs: list[Any] = []

            def done(self) -> bool:
                return self._done

            def cancel(self) -> bool:
                return False

            def result(self) -> Any:
                if self._exc is not None:
                    raise self._exc
                return self._res

            def add_done_callback(self, cb: Any) -> None:
                self._cbs.append(cb)

            def resolve(self, res: Any) -> None:
                self._res = res
                self._done = True
                for cb in list(self._cbs):
                    cb(self)

        class _FakeProactor:
            def __init__(self) -> None:
                self.accepted: list[tuple[Any, _FakeFuture]] = []

            def accept_pipe(self, pipe: Any) -> _FakeFuture:
                fut = _FakeFuture()
                self.accepted.append((pipe, fut))
                return fut

        class _FakeLoop:
            def __init__(self) -> None:
                self._proactor = _FakeProactor()
                self.soon: list[tuple[Any, tuple[Any, ...]]] = []
                self.exceptions: list[dict[str, Any]] = []
                self.transports: list[tuple[Any, Any, dict[str, Any] | None]] = []

            def get_debug(self) -> bool:
                return False

            def create_future(self) -> _FakeFuture:
                return _FakeFuture()

            def call_soon(self, cb: Any, *args: Any) -> None:
                self.soon.append((cb, args))

            def call_exception_handler(self, ctx: dict[str, Any]) -> None:
                self.exceptions.append(ctx)

            def _make_duplex_pipe_transport(
                self, pipe: Any, protocol: Any, extra: dict[str, Any] | None = None
            ) -> None:
                self.transports.append((pipe, protocol, extra))

        async def _run_check() -> None:
            loop = _FakeLoop()
            srv = ipc.NamedPipeServer(
                pipe_name,
                loop=loop,
                client_connected_cb=lambda _r, _w: None,
                limit=65536,
                pipe_handle_cls=_FakePipeHandle,
                k32=fake,
                advapi=fake,
                get_last_error_fn=fake.get_last_error,
            )
            parked_pipe = srv._pipe
            self.assertIsNotNone(parked_pipe)
            self.assertTrue(srv.is_serving())

            # P2-1: transient CreateNamedPipeW failure during accept must NOT drop self._pipe or wedge server
            fake.fail_create_pipe_error = 14  # ERROR_OUTOFMEMORY
            srv.start()
            cb, args = loop.soon.pop(0)
            cb(*args)
            self.assertTrue(srv.is_serving())
            self.assertFalse(srv._closed_event.is_set())
            self.assertIs(srv._pipe, parked_pipe)
            self.assertEqual(len(loop._proactor.accepted), 0)
            self.assertEqual(len(loop.soon), 1)

            # Next retry succeeds: creates replacement parked pipe and accepts on parked_pipe
            fake.fail_create_pipe_error = None
            cb, args = loop.soon.pop(0)
            cb(*args)
            self.assertIsNot(srv._pipe, parked_pipe)
            self.assertEqual(len(loop._proactor.accepted), 1)
            acc_pipe, acc_fut = loop._proactor.accepted.pop(0)
            self.assertIs(acc_pipe, parked_pipe)

            fake.pipe_peers[acc_pipe.fileno()] = 200
            acc_fut.resolve(acc_pipe)
            self.assertEqual(len(loop.transports), 1)
            t_pipe, _, t_extra = loop.transports[0]
            self.assertIs(t_pipe, parked_pipe)
            self.assertEqual(t_pipe._mesh_peer_credentials, (200, 0, 0))
            self.assertEqual(t_extra["peer_credentials"], (200, 0, 0))
            t_pipe.close()

            # P3-1: closed PipeHandle (whose fileno() raises ValueError) in accept exception paths
            closed_pipe = _FakePipeHandle(fake._alloc_handle("server_pipe", pipe_name))
            self.assertTrue(ipc._pipe_is_open(closed_pipe))
            closed_pipe.close()
            self.assertFalse(ipc._pipe_is_open(closed_pipe))
            with self.assertRaises(ValueError):
                closed_pipe.fileno()

            for exc_cls in (BrokenPipeError, OSError, asyncio.CancelledError):
                srv._closed_event.clear()

                def _fail_accept(p: Any, _exc: type[BaseException] = exc_cls) -> _FakeFuture:
                    p.close()
                    raise _exc("boom")

                with mock.patch.object(loop._proactor, "accept_pipe", side_effect=_fail_accept):
                    srv._loop_accept_pipe()
            srv.close()

        asyncio.run(_run_check())
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

    def test_win32_run_session_denial_over_pipe_and_malformed_run_id_mocked(self) -> None:
        from mesh.core.db import connect_db
        from mesh.daemon import merge as merge_mod, server as server_mod, supervisor as sup_mod
        from tests.unit.test_platform_win_jobs import _FakeProcObj, _FakeWinJobKernel

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(home, "win-ipc-passphrase-1234")

            other_tok, other_lookup, other_hash = auth.generate_agent_token()
            conn = connect_db(os.path.join(home, "mesh.db"))
            try:
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO principals (id, kind, name, created_at) "
                        "VALUES ('agt_other', 'agent', 'other_agent', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_other', 'none', 'inbox', 0, 1, '2026-11-01T00:00:00.000Z', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                        "VALUES ('crd_other', 'agt_other', 'agent_token', ?, ?, '{}', '2026-11-01T00:00:00.000Z');",
                        (other_lookup, other_hash),
                    )
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES ('prj_1', 'P', 'G', 'active', 'usr_1', 'direct', '2026-11-01T00:00:00.000Z', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, wrapper_pid, run_dir) "
                        "VALUES ('run_ended_1', 'external', 'prj_1', 'agt_other', 1, 'none', 'exited', 'boot_1', 999998, ?);",
                        (os.path.join(home, "runs", "run_ended_1"),),
                    )
            finally:
                conn.close()

            procs = {
                300: _FakeProcObj(pid=300, ppid=1, creation_time=1000),
                301: _FakeProcObj(pid=301, ppid=1, creation_time=2000),
                400: _FakeProcObj(pid=400, ppid=1, creation_time=3000),
            }
            fake_jobs = _FakeWinJobKernel(procs, current_pid=os.getpid())
            job = _win32.create_run_job_win(
                home,
                "run_win_1",
                k32=fake_jobs,
                advapi=fake_jobs,
                get_last_error_fn=fake_jobs.get_last_error,
                set_last_error_fn=fake_jobs.set_last_error,
            )
            _win32.assign_process_to_job_win(
                job, 300, k32=fake_jobs, get_last_error_fn=fake_jobs.get_last_error
            )

            def fake_find_job(pid: int, active_runs: dict[str, int], *, active_job_handles=None, home=None):
                handles = dict(active_job_handles or {})
                if "run_win_1" in active_runs:
                    handles.setdefault("run_win_1", job)
                return _win32.find_active_job_for_pid_win(
                    pid,
                    active_runs,
                    active_job_handles=handles,
                    home=home,
                    k32=fake_jobs,
                    get_last_error_fn=fake_jobs.get_last_error,
                )

            srv = server_mod.DaemonServer(home=home, testing_mode=True)
            srv.setup_storage()
            try:
                class _FakePipeEndpoint:
                    def __init__(self, pid: int) -> None:
                        self._mesh_peer_credentials = (pid, 0, 0)

                class _FakeWriter:
                    def __init__(self, pipe_obj: Any) -> None:
                        self._pipe = pipe_obj
                        self.chunks: list[bytes] = []
                        self.closed = False

                    def get_extra_info(self, name: str, default: Any = None) -> Any:
                        if name == "pipe":
                            return self._pipe
                        return default

                    def write(self, data: bytes) -> None:
                        self.chunks.append(data)

                    async def drain(self) -> None:
                        return None

                    def close(self) -> None:
                        self.closed = True

                    async def wait_closed(self) -> None:
                        return None

                class _FakeReader:
                    def __init__(self, payload: bytes) -> None:
                        self._payload = payload

                    async def readline(self) -> bytes:
                        return self._payload

                def _call_over_pipe(pid: int, auth_obj: dict[str, Any]) -> dict[str, Any]:
                    req = (
                        json.dumps({"v": 1, "cmd": "whoami", "auth": auth_obj, "args": {}}) + "\n"
                    ).encode("utf-8")
                    writer = _FakeWriter(_FakePipeEndpoint(pid))
                    asyncio.run(srv.handle_client(_FakeReader(req), writer))
                    self.assertTrue(writer.closed)
                    return json.loads(b"".join(writer.chunks).decode("utf-8"))

                env_map = {300: {}, 301: {"MESH_RUN_ID": "run_ended_1"}, 400: {}}
                with (
                    mock.patch.object(sys, "platform", "win32"),
                    mock.patch.dict(merge_mod._ACTIVE_TEST_WRAPPERS, {"run_win_1": 999999}),
                    mock.patch.object(sup_mod, "group_still_ours", return_value=True),
                    mock.patch.object(auth._plat_proc, "find_active_job_for_pid", side_effect=fake_find_job),
                    mock.patch.object(auth, "get_proc_stat", return_value={"session": 0, "ppid": 1}),
                    mock.patch.object(auth, "get_proc_ancestry", return_value=[]),
                    mock.patch.object(auth, "get_proc_environ", side_effect=lambda p: env_map[p]),
                ):
                    for bad_pid in (300, 301):
                        for auth_obj in (
                            {"type": "user", "passphrase": "win-ipc-passphrase-1234"},
                            {"type": "token", "token": other_tok},
                        ):
                            resp = _call_over_pipe(bad_pid, auth_obj)
                            self.assertFalse(resp["ok"], (bad_pid, auth_obj, resp))
                            self.assertEqual(resp["error"]["code"], c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
                            self.assertEqual(resp["exit_code"], c.EXIT_FORBIDDEN)

                    for auth_obj in (
                        {"type": "user", "passphrase": "win-ipc-passphrase-1234"},
                        {"type": "token", "token": other_tok},
                    ):
                        ok_resp = _call_over_pipe(400, auth_obj)
                        self.assertTrue(ok_resp["ok"], (auth_obj, ok_resp))

                    # P3-2: malformed run_id causing ValueError in run_job_name_win fails closed
                    with mock.patch.object(_win32.w32, "get_kernel32", return_value=fake_jobs):
                        with self.assertRaises(MeshError) as ctx_bad_id:
                            auth.classify_peer_run_session(
                                400,
                                {"../bad": 999999},
                                {"../bad"},
                                os.getpid(),
                                home=home,
                            )
                    self.assertEqual(ctx_bad_id.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
            finally:
                if srv.db_conn is not None:
                    srv.db_conn.close()
                job.close()
            self.assertEqual(sorted(fake_jobs.closed_handles), sorted(fake_jobs.open_handles))

    def test_pipe_shutdown_non_pipe_endpoint_and_posix_parity(self) -> None:
        from mesh import client as client_mod
        from mesh.cli.commands import daemon as daemon_cmd
        from mesh.daemon import server as server_mod

        # P3-4: SHUT_RD validates handle without FlushFileBuffers; SHUT_RDWR flushes
        fake = _FakeWinIpcKernel()
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-" + "e" * 32
        stream = ipc.connect_pipe_win(
            pipe_name,
            timeout=1.0,
            k32=fake,
            advapi=fake,
            get_last_error_fn=fake.get_last_error,
            time_fn=fake.monotonic,
            sleep_fn=fake.sleep,
        )
        stream.shutdown(socket.SHUT_RD)
        self.assertEqual(len(fake.flush_calls), 0)
        stream.shutdown(socket.SHUT_WR)
        self.assertEqual(len(fake.flush_calls), 1)
        stream.close()
        with self.assertRaises(OSError):
            stream.shutdown(socket.SHUT_RD)
        self.assertEqual(sorted(fake.closed_handles), sorted(fake.open_handles))

        # P3-5: non-pipe MESH_SOCKET / invalid pipe endpoint raises MeshError(DAEMON_UNAVAILABLE) on Win32
        default_home = os.path.realpath(os.path.expanduser("~/.agent-mesh"))
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(client_mod, "EXPLICIT_HOME", False),
        ):
            for bad_ep in (
                "/tmp/not-a-pipe.sock",
                "C:\\" + "x" * 130 + "\\mesh.sock",
                r"\\.\pipe\LOCAL\agent-mesh-" + "x" * 300,
            ):
                with mock.patch.dict(os.environ, {"MESH_SOCKET": bad_ep}, clear=False):
                    os.environ.pop("MESH_HOME", None)
                    mc = client_mod.MeshClient(default_home)
                    with self.assertRaises(MeshError) as ctx_ep:
                        mc._connect_socket(1.0)
                    self.assertEqual(ctx_ep.exception.code, c.DAEMON_UNAVAILABLE)
                    self.assertEqual(ctx_ep.exception.exit_code, c.EXIT_DAEMON_UNAVAILABLE)

            with self.assertRaises(OSError) as ctx_conn:
                ipc.connect_endpoint("/tmp/not-a-pipe.sock", timeout=1.0)
            self.assertEqual(ctx_conn.exception.errno, errno.EINVAL)

        # P3-3a: daemon secret env scrubbing is exact-case on Linux, case-insensitive on Win32
        # Patch os.environ with a plain dict because Windows os._Environ uppercases keys on write.
        with mock.patch.object(sys, "platform", "linux"):
            self.assertTrue(daemon_cmd._is_daemon_secret_env("MESH_TOKEN"))
            self.assertFalse(daemon_cmd._is_daemon_secret_env("mesh_token"))
            with (
                mock.patch.object(daemon_cmd.os, "environ", {"mesh_token": "keep-on-linux"}),
                mock.patch.object(daemon_cmd.os, "execve") as m_execve,
            ):
                daemon_cmd._exec_without_secret_env()
                m_execve.assert_not_called()
        with mock.patch.object(sys, "platform", "win32"):
            self.assertTrue(daemon_cmd._is_daemon_secret_env("mesh_token"))
            self.assertTrue(daemon_cmd._is_daemon_secret_env("MESH_PASSPHRASE"))
            with (
                mock.patch.object(
                    daemon_cmd.os,
                    "environ",
                    {"mesh_token": "scrub-on-win32", "PATH": "C:\\bin"},
                ),
                mock.patch("mesh.platform.proc.reexec_clean_env") as m_reexec,
            ):
                daemon_cmd._exec_without_secret_env()
                m_reexec.assert_called_once()
                _, passed_env = m_reexec.call_args[0]
                self.assertEqual(passed_env, {"PATH": "C:\\bin"})

        # P3-3b: auth.py error strings preserve /proc/<pid>/... on Linux and use Windows wording on Win32
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(auth, "get_proc_stat", return_value=None),
        ):
            with self.assertRaises(MeshError) as ctx_stat_posix:
                auth.classify_peer_run_session(123, {}, set(), 1)
            self.assertIn("/proc/123/stat", ctx_stat_posix.exception.message)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.object(auth, "get_proc_stat", return_value=None),
        ):
            with self.assertRaises(MeshError) as ctx_stat_win:
                auth.classify_peer_run_session(123, {}, set(), 1)
            self.assertIn("process stat for peer 123", ctx_stat_win.exception.message)

        # P3-3c: Server._handle_client propagates get_peer_credentials OSError on POSIX, closes cleanly on Win32
        srv = server_mod.DaemonServer(home=os.path.join(tempfile.gettempdir(), "fake-home"))
        mock_writer = mock.MagicMock()
        mock_writer.get_extra_info.return_value = mock.MagicMock()
        mock_writer.wait_closed = mock.AsyncMock()
        mock_reader = mock.MagicMock()
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(server_mod.os, "getuid", return_value=1000, create=True),
            mock.patch("mesh.daemon.server.get_peer_credentials", side_effect=OSError("bad peer")),
        ):
            with self.assertRaises(OSError):
                asyncio.run(srv._handle_client(mock_reader, mock_writer))
        mock_writer.close.assert_not_called()

        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch("mesh.daemon.server.get_peer_credentials", side_effect=OSError("bad peer")),
        ):
            asyncio.run(srv._handle_client(mock_reader, mock_writer))
        mock_writer.close.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "Windows real Named Pipe and auth tests")
class TestWin32IpcRealWindows(unittest.TestCase):
    """Real Windows Named Pipe IPC, DACL, peer SID/PID, and run-session tests."""

    def _make_home(self, tmp: str) -> str:
        home = os.path.join(tmp, "home")
        with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
            init_mesh_home(home, "win-ipc-passphrase-1234")
        return home

    def test_real_pipe_dacl_first_instance_burst_oversize_and_stream(self) -> None:
        from mesh.cli.commands import doctor_env
        from mesh.daemon.server import DaemonServer, MAX_REQUEST_BYTES

        with tempfile.TemporaryDirectory() as td:
            home = self._make_home(td)
            srv = DaemonServer(home=home, testing_mode=True)
            ready = threading.Event()
            errors: list[BaseException] = []

            def _run_srv() -> None:
                async def _runner() -> None:
                    task = asyncio.create_task(srv.run_server())
                    for _ in range(400):
                        if srv.server is not None and srv.server.is_serving():
                            ready.set()
                            break
                        await asyncio.sleep(0.01)
                    await task

                try:
                    asyncio.run(_runner())
                except BaseException as exc:
                    errors.append(exc)
                    ready.set()

            t = threading.Thread(target=_run_srv, daemon=True)
            t.start()
            try:
                self.assertTrue(ready.wait(timeout=15.0))
                self.assertEqual(errors, [])

                pipe_path = ipc.daemon_endpoint_path(home)
                self.assertTrue(ipc.is_win_pipe_path(pipe_path))
                self.assertTrue(doctor_env._daemon_running(home))

                # 1. Duplicate FILE_FLAG_FIRST_PIPE_INSTANCE must fail with PermissionError (ERROR_ACCESS_DENIED)
                with self.assertRaises(PermissionError):
                    ipc._create_named_pipe_instance_win(pipe_path, first=True)

                # 2. Verify pipe handle DACL has owner-only access (no Everyone / Users / AuthUsers)
                stream_dacl = ipc.connect_endpoint(pipe_path, timeout=5.0)
                try:
                    k32 = w32.get_kernel32()
                    adv = w32.get_advapi32()
                    user_sid, owner_sid = perms.get_current_user_and_owner_sids_win(
                        advapi=adv, k32=k32
                    )
                    trusted_owners = set(perms.WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, owner_sid}
                    trusted_aces = set(perms.WELL_KNOWN_TRUSTED_ACE_SIDS) | {user_sid, owner_sid}

                    # Inspect the server pipe instance security descriptor via GetSecurityInfo on the handle
                    raw_h = stream_dacl.fileno()
                    owner_p = ctypes.c_void_p()
                    dacl_p = ctypes.c_void_p()
                    sd_p = ctypes.c_void_p()
                    rc = adv.GetSecurityInfo(
                        raw_h,
                        w32.SE_KERNEL_OBJECT,
                        w32.OWNER_SECURITY_INFORMATION | w32.DACL_SECURITY_INFORMATION,
                        ctypes.byref(owner_p),
                        None,
                        ctypes.byref(dacl_p),
                        None,
                        ctypes.byref(sd_p),
                    )
                    self.assertEqual(rc, 0)
                    try:
                        p_owner = perms._sid_ptr_to_str(owner_p.value, advapi=adv, k32=k32)
                        self.assertIn(p_owner, trusted_owners)
                        self.assertTrue(bool(dacl_p.value))
                        acl = w32.ACL.from_address(dacl_p.value)
                        seen_sids: set[str] = set()
                        for idx in range(acl.AceCount):
                            ace_p = ctypes.c_void_p()
                            self.assertTrue(adv.GetAce(dacl_p.value, idx, ctypes.byref(ace_p)))
                            ace = w32.ACCESS_ALLOWED_ACE.from_address(ace_p.value)
                            sid_str = perms._sid_ptr_to_str(
                                ace_p.value + w32.ACCESS_ALLOWED_ACE.SidStart.offset,
                                advapi=adv,
                                k32=k32,
                            )
                            seen_sids.add(sid_str)
                            self.assertIn(sid_str, trusted_aces)
                        self.assertIn(user_sid, seen_sids)
                        self.assertNotIn(perms.EVERYONE_SID, seen_sids)
                        self.assertNotIn(perms.BUILTIN_USERS_SID, seen_sids)
                        self.assertNotIn(perms.AUTHENTICATED_USERS_SID, seen_sids)
                    finally:
                        if sd_p.value:
                            k32.LocalFree(sd_p)
                finally:
                    stream_dacl.close()

                # 3. 32-client concurrent burst over MeshClient -> Named Pipe
                from mesh.client import MeshClient

                def _burst_call(_idx: int) -> int:
                    cli = MeshClient(home=home, timeout=15.0)
                    resp = cli.send_request(
                        {"v": 1, "cmd": "daemon.status", "auth": {"type": "none"}}
                    )
                    self.assertTrue(resp.get("ok"), resp)
                    return int(resp["data"]["pid"])

                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                    pids = list(pool.map(_burst_call, range(32)))
                self.assertEqual(len(pids), 32)
                self.assertEqual(set(pids), {os.getpid()})

                # 4. Oversize request line drain (> MAX_REQUEST_BYTES) returns INVALID_ARGUMENT cleanly
                cli_over = MeshClient(home=home, timeout=15.0)
                over_resp = cli_over.send_request(
                    {
                        "v": 1,
                        "cmd": "daemon.status",
                        "auth": {"type": "none"},
                        "args": {"pad": "x" * (MAX_REQUEST_BYTES + 16384)},
                    }
                )
                self.assertFalse(over_resp.get("ok"))
                self.assertEqual(over_resp["error"]["code"], c.INVALID_ARGUMENT)

                # 5. NDJSON streaming (event.tail) over Named Pipe
                # Use a thread-local SQLite connection; srv.db_conn belongs to thread t.
                evt_conn = connect_db(os.path.join(home, "mesh.db"))
                try:
                    with transaction(evt_conn):
                        record_event(
                            evt_conn,
                            type_="project.created",
                            actor_id="usr_1",
                            actor_kind="user",
                            via="direct",
                            project_id="prj_stream",
                            to_state="active",
                        )
                finally:
                    evt_conn.close()
                stream_cli = MeshClient(home=home, timeout=10.0)
                stream_sock = stream_cli._connect_socket(10.0)
                try:
                    req = {
                        "v": 1,
                        "cmd": "event.tail",
                        "auth": {"type": "user", "passphrase": "win-ipc-passphrase-1234"},
                        "args": {"since": 0, "timeout": "1s"},
                    }
                    stream_sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
                    f = stream_sock.makefile("r", encoding="utf-8")
                    lines = [ln.strip() for ln in f if ln.strip()]
                    f.close()
                finally:
                    stream_sock.close()
                events_out = [json.loads(ln) for ln in lines]
                self.assertGreaterEqual(len(events_out), 2)
                created_evts = [ev for ev in events_out if ev.get("type") == "project.created"]
                self.assertEqual(len(created_evts), 1)
                self.assertEqual(created_evts[0]["project_id"], "prj_stream")

                # 6. Stop daemon cleanly via daemon.stop RPC
                stop_cli = MeshClient(home=home, timeout=10.0)
                stop_resp = stop_cli.send_request(
                    {
                        "v": 1,
                        "cmd": "daemon.stop",
                        "auth": {"type": "user", "passphrase": "win-ipc-passphrase-1234"},
                        "args": {},
                    }
                )
                self.assertTrue(stop_resp.get("ok"), stop_resp)
            finally:
                if srv.stop_event is not None:
                    srv.stop_event.set()
                t.join(timeout=15.0)
            self.assertFalse(t.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(doctor_env._daemon_running(home))

    def test_real_classify_peer_run_session_job_and_peb(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            os.makedirs(home, exist_ok=True)
            active_run_id = f"run_active_{uuid.uuid4().hex[:8]}"
            ended_run_id = f"run_ended_{uuid.uuid4().hex[:8]}"

            # Case 1: Process inside Named Run Job with wiped env={} and broken ancestry (still caught via IsProcessInJob!)
            job = plat_proc.create_run_job(home, active_run_id)
            child_job = None
            child_peb = None
            child_outside = None
            try:
                child_job = plat_proc.spawn_in_job(
                    [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                    job,
                    env={},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                self.assertEqual(child_job.stdout.readline().strip(), "ready")
                # Even with wrapper_pid=999999 (so ancestry does not match) and env={}, Job Object classifies it!
                self.assertEqual(
                    auth.classify_peer_run_session(
                        child_job.pid,
                        {active_run_id: 999999},
                        {active_run_id, ended_run_id},
                        os.getpid(),
                        home=home,
                    ),
                    active_run_id,
                )

                # Case 2: Child outside Job Object with ended run's MESH_RUN_ID in PEB
                peb_env = dict(os.environ)
                peb_env["MESH_RUN_ID"] = ended_run_id
                child_peb = subprocess.Popen(
                    [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                    env=peb_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                self.assertEqual(child_peb.stdout.readline().strip(), "ready")
                self.assertEqual(
                    auth.classify_peer_run_session(
                        child_peb.pid,
                        {active_run_id: 999999},
                        {active_run_id, ended_run_id},
                        os.getpid(),
                        home=home,
                    ),
                    ended_run_id,
                )

                # Case 3: Unrelated process -> None
                out_env = {k: v for k, v in os.environ.items() if k.upper() != "MESH_RUN_ID"}
                child_outside = subprocess.Popen(
                    [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
                    env=out_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                self.assertEqual(child_outside.stdout.readline().strip(), "ready")
                self.assertIsNone(
                    auth.classify_peer_run_session(
                        child_outside.pid,
                        {active_run_id: 999999},
                        {active_run_id, ended_run_id},
                        os.getpid(),
                        home=home,
                    )
                )

                # Case 4: Nonexistent PID -> fail closed with CREDENTIAL_NOT_ALLOWED_IN_RUN
                with self.assertRaises(MeshError) as ctx_dead:
                    auth.classify_peer_run_session(
                        99999999,
                        {active_run_id: 999999},
                        {active_run_id, ended_run_id},
                        os.getpid(),
                        home=home,
                    )
                self.assertEqual(ctx_dead.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
            finally:
                for p in (child_job, child_peb, child_outside):
                    if p is not None:
                        if p.poll() is None:
                            p.kill()
                            p.wait(timeout=5)
                        if p.stdout is not None:
                            p.stdout.close()
                job.close()

    def test_real_run_session_denial_over_named_pipe(self) -> None:
        from mesh.core.db import connect_db
        from mesh.daemon import merge as merge_mod
        from mesh.daemon.server import DaemonServer

        repo_root = str(pathlib.Path(__file__).resolve().parents[2])
        passphrase = "win-ipc-passphrase-1234"
        active_run_id = f"run_active_{uuid.uuid4().hex[:8]}"
        ended_run_id = f"run_ended_{uuid.uuid4().hex[:8]}"

        with tempfile.TemporaryDirectory() as td:
            home = self._make_home(td)
            other_tok, other_lookup, other_hash = auth.generate_agent_token()
            conn = connect_db(os.path.join(home, "mesh.db"))
            try:
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO principals (id, kind, name, created_at) "
                        "VALUES ('agt_other', 'agent', 'other_agent', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO agents (id, adapter, delivery, can_lead, max_concurrency, created_at, updated_at) "
                        "VALUES ('agt_other', 'none', 'inbox', 0, 1, '2026-11-01T00:00:00.000Z', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO credentials (id, principal_id, kind, lookup, secret_hash, scope_json, created_at) "
                        "VALUES ('crd_other', 'agt_other', 'agent_token', ?, ?, '{}', '2026-11-01T00:00:00.000Z');",
                        (other_lookup, other_hash),
                    )
                    conn.execute(
                        "INSERT INTO projects (id, name, goal, status, created_by, created_via, created_at, updated_at) "
                        "VALUES ('prj_denial', 'Denial', 'Test', 'active', 'usr_1', 'direct', '2026-11-01T00:00:00.000Z', '2026-11-01T00:00:00.000Z');"
                    )
                    conn.execute(
                        "INSERT INTO runs (id, kind, project_id, agent_id, fence, adapter, status, boot_id, wrapper_pid, run_dir, end_reason) "
                        "VALUES (?, 'external', 'prj_denial', 'agt_other', 1, 'none', 'exited', 'boot_1', 999998, ?, 'completed');",
                        (ended_run_id, os.path.join(home, "runs", ended_run_id)),
                    )
            finally:
                conn.close()

            job = plat_proc.create_run_job(home, active_run_id)
            srv = DaemonServer(home=home, testing_mode=True)
            ready = threading.Event()
            errors: list[BaseException] = []

            def _run_srv() -> None:
                async def _runner() -> None:
                    task = asyncio.create_task(srv.run_server())
                    for _ in range(400):
                        if srv.server is not None and srv.server.is_serving():
                            ready.set()
                            break
                        await asyncio.sleep(0.01)
                    await task

                try:
                    asyncio.run(_runner())
                except BaseException as exc:
                    errors.append(exc)
                    ready.set()

            probe_code = (
                "import argparse, json, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from mesh.client import MeshClient\n"
                "from mesh.errors import MeshError\n"
                "home, passphrase, other_tok = sys.argv[2], sys.argv[3], sys.argv[4]\n"
                "out = {}\n"
                "for label, auth_obj in (\n"
                "    ('user', {'type': 'user', 'passphrase': passphrase}),\n"
                "    ('token', {'type': 'token', 'token': other_tok}),\n"
                "):\n"
                "    cli = MeshClient(home=home, timeout=10.0)\n"
                "    try:\n"
                "        resp = cli.execute_cmd(argparse.Namespace(dry_run=False), 'whoami', auth_override=auth_obj)\n"
                "        out[f'exec_{label}'] = {'ok': True, 'resp': resp}\n"
                "    except MeshError as exc:\n"
                "        out[f'exec_{label}'] = {'ok': False, 'code': exc.code, 'exit_code': exc.exit_code}\n"
                "    out[f'raw_{label}'] = cli.send_request({'v': 1, 'cmd': 'whoami', 'auth': auth_obj, 'args': {}})\n"
                "print(json.dumps(out), flush=True)\n"
            )

            child_procs: list[subprocess.Popen[str]] = []
            try:
                with mock.patch.dict(merge_mod._ACTIVE_TEST_WRAPPERS, {active_run_id: 999999}):
                    t = threading.Thread(target=_run_srv, daemon=True)
                    t.start()
                    try:
                        self.assertTrue(ready.wait(timeout=15.0))
                        self.assertEqual(errors, [])

                        # 1. Child inside Job Object with wiped env={} -> denied via Named Pipe + IsProcessInJob
                        child_job = plat_proc.spawn_in_job(
                            [sys.executable, "-c", probe_code, repo_root, home, passphrase, other_tok],
                            job,
                            env={},
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        child_procs.append(child_job)
                        out_job, err_job = child_job.communicate(timeout=20)
                        self.assertEqual(child_job.returncode, 0, f"stderr={err_job}")
                        res_job = json.loads(out_job)

                        # 2. Child outside Job Object with ended run's MESH_RUN_ID -> denied via Named Pipe + PEB
                        peb_env = dict(os.environ)
                        peb_env["MESH_RUN_ID"] = ended_run_id
                        child_peb = subprocess.Popen(
                            [sys.executable, "-c", probe_code, repo_root, home, passphrase, other_tok],
                            env=peb_env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        child_procs.append(child_peb)
                        out_peb, err_peb = child_peb.communicate(timeout=20)
                        self.assertEqual(child_peb.returncode, 0, f"stderr={err_peb}")
                        res_peb = json.loads(out_peb)

                        # 3. Control child outside any run -> allowed
                        out_env = {k: v for k, v in os.environ.items() if k.upper() != "MESH_RUN_ID"}
                        child_out = subprocess.Popen(
                            [sys.executable, "-c", probe_code, repo_root, home, passphrase, other_tok],
                            env=out_env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        child_procs.append(child_out)
                        out_ctrl, err_ctrl = child_out.communicate(timeout=20)
                        self.assertEqual(child_out.returncode, 0, f"stderr={err_ctrl}")
                        res_ctrl = json.loads(out_ctrl)

                        for label, res in (("job", res_job), ("peb", res_peb)):
                            for auth_kind in ("user", "token"):
                                ex = res[f"exec_{auth_kind}"]
                                raw = res[f"raw_{auth_kind}"]
                                self.assertFalse(ex["ok"], (label, auth_kind, ex))
                                self.assertEqual(ex["code"], c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
                                self.assertEqual(ex["exit_code"], c.EXIT_FORBIDDEN)
                                self.assertFalse(raw["ok"], (label, auth_kind, raw))
                                self.assertEqual(raw["error"]["code"], c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
                                self.assertEqual(raw["exit_code"], c.EXIT_FORBIDDEN)

                        for auth_kind in ("user", "token"):
                            self.assertTrue(res_ctrl[f"exec_{auth_kind}"]["ok"], res_ctrl)
                            self.assertTrue(res_ctrl[f"raw_{auth_kind}"]["ok"], res_ctrl)
                    finally:
                        if srv.stop_event is not None:
                            srv.stop_event.set()
                        t.join(timeout=15.0)
                self.assertFalse(t.is_alive())
                self.assertEqual(errors, [])

                conn_chk = connect_db(os.path.join(home, "mesh.db"), read_only=True)
                try:
                    rows = conn_chk.execute(
                        "SELECT run_id, COUNT(*) AS cnt FROM events "
                        "WHERE type = 'auth.credential_in_run' GROUP BY run_id;"
                    ).fetchall()
                    counts = {r["run_id"]: r["cnt"] for r in rows}
                    self.assertGreaterEqual(counts.get(active_run_id, 0), 4)
                    self.assertGreaterEqual(counts.get(ended_run_id, 0), 4)
                finally:
                    conn_chk.close()
            finally:
                for p in child_procs:
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=5)
                job.close()


class TestFinalReviewIpcWinFixes(unittest.TestCase):
    """Regression tests for R13 P0-1, P2-4, and P3-4."""

    def test_p0_1_verify_pipe_server_win_and_connect_fail_closed_mocked(self) -> None:
        pipe_name = r"\\.\pipe\LOCAL\agent-mesh-test-p0-verify"

        # 1. Matching daemon SID succeeds and returns server PID 100
        fake_ok = _FakeWinIpcKernel()
        stream = ipc.connect_pipe_win(
            pipe_name,
            timeout=1.0,
            k32=fake_ok,
            advapi=fake_ok,
            get_last_error_fn=fake_ok.get_last_error,
            time_fn=fake_ok.monotonic,
            sleep_fn=fake_ok.sleep,
        )
        stream.close()
        self.assertEqual(sorted(fake_ok.closed_handles), sorted(fake_ok.open_handles))
        self.assertEqual(sorted(fake_ok.local_frees), sorted(fake_ok.local_allocs))

        # 2. Mismatched server SID fails closed with PermissionError and closes handle
        fake_bad_sid = _FakeWinIpcKernel()
        fake_bad_sid.procs[300] = _FakePeerProc(pid=300, user_sid="S-1-5-21-100-200-300-2999")
        fake_bad_sid.default_server_pid = 300
        with self.assertRaises(PermissionError):
            ipc.connect_pipe_win(
                pipe_name,
                timeout=1.0,
                k32=fake_bad_sid,
                advapi=fake_bad_sid,
                get_last_error_fn=fake_bad_sid.get_last_error,
                time_fn=fake_bad_sid.monotonic,
                sleep_fn=fake_bad_sid.sleep,
            )
        self.assertEqual(sorted(fake_bad_sid.closed_handles), sorted(fake_bad_sid.open_handles))

        # 3. GetNamedPipeServerProcessId error fails closed and closes handle
        fake_err = _FakeWinIpcKernel()
        fake_err.fail_get_server_pid_error = w32.ERROR_ACCESS_DENIED
        with self.assertRaises(PermissionError):
            ipc.connect_pipe_win(
                pipe_name,
                timeout=1.0,
                k32=fake_err,
                advapi=fake_err,
                get_last_error_fn=fake_err.get_last_error,
                time_fn=fake_err.monotonic,
                sleep_fn=fake_err.sleep,
            )
        self.assertEqual(sorted(fake_err.closed_handles), sorted(fake_err.open_handles))

        # 4. Dead server process fails closed and closes handle
        fake_dead = _FakeWinIpcKernel()
        fake_dead.procs[100].alive = False
        with self.assertRaises(PermissionError):
            ipc.connect_pipe_win(
                pipe_name,
                timeout=1.0,
                k32=fake_dead,
                advapi=fake_dead,
                get_last_error_fn=fake_dead.get_last_error,
                time_fn=fake_dead.monotonic,
                sleep_fn=fake_dead.sleep,
            )
        self.assertEqual(sorted(fake_dead.closed_handles), sorted(fake_dead.open_handles))

    def test_p0_1_unpredictable_pipe_pointer_and_client_refresh_mocked(self) -> None:
        from mesh import client as client_mod
        from mesh.platform import fs as plat_fs, paths as plat_paths, perms as plat_perms

        with tempfile.TemporaryDirectory() as td:
            home = os.path.realpath(td)
            p1 = ipc.generate_pipe_endpoint_win(home)
            p2 = ipc.generate_pipe_endpoint_win(home)
            self.assertNotEqual(p1, p2)
            self.assertTrue(ipc.is_win_pipe_path(p1))
            self.assertTrue(ipc.is_win_pipe_path(p2))
            self.assertRegex(p1, r"^\\\\\.\\pipe\\LOCAL\\agent-mesh-[0-9a-f]{16}-[0-9a-f]{32}$")

            # Fallback deterministic endpoint is used only when mesh.pipe is absent
            det = ipc._deterministic_pipe_endpoint_win(home)
            self.assertEqual(ipc.daemon_endpoint_path_win(home), det)

            # Write and verify mesh.pipe atomically with nofollow + private perms
            chmods: list[tuple[int, int]] = []
            with (
                mock.patch.object(plat_fs, "fsync_dir", return_value=None),
                mock.patch.object(plat_perms, "verify_private_owner_and_perms", return_value=None) as m_verify,
                mock.patch.object(plat_paths, "is_reparse_link", return_value=False),
            ):
                pointer = ipc.write_pipe_endpoint_win(
                    home,
                    p1,
                    open_fn=lambda p, flags, mode=0o600, **_k: os.open(p, flags, mode),
                    fchmod_fn=lambda fd, m: chmods.append((fd, m)),
                    replace_fn=os.replace,
                    dir_acl_fn=lambda _d: None,
                )
                self.assertEqual(pointer, ipc.pipe_pointer_path_win(home))
                self.assertTrue(any(m == 0o600 for _, m in chmods))

                with (
                    mock.patch.object(plat_fs, "open_regular_nofollow", side_effect=lambda p, flags, mode=0o600, **_k: os.open(p, flags, mode)),
                ):
                    self.assertEqual(ipc.read_pipe_endpoint_win(home), p1)
                    self.assertEqual(ipc.daemon_endpoint_path_win(home), p1)
                    self.assertGreaterEqual(m_verify.call_count, 2)

                    # Reparse run/ directory must be rejected on Windows
                    with (
                        mock.patch.object(sys, "platform", "win32"),
                        mock.patch.object(plat_paths, "is_reparse_link", return_value=True),
                    ):
                        with self.assertRaises(OSError) as ctx_rep:
                            ipc.read_pipe_endpoint_win(home)
                        self.assertEqual(ctx_rep.exception.errno, errno.ELOOP)

                # Invalid pipe name in mesh.pipe must fail closed with EINVAL
                with open(pointer, "w", encoding="utf-8") as pf:
                    pf.write("C:\\evil\\path\n")
                with mock.patch.object(plat_fs, "open_regular_nofollow", side_effect=lambda p, flags, mode=0o600, **_k: os.open(p, flags, mode)):
                    with self.assertRaises(OSError) as ctx_inv:
                        ipc.read_pipe_endpoint_win(home)
                    self.assertEqual(ctx_inv.exception.errno, errno.EINVAL)

                # Tampered permissions fail closed via verify_private_owner_and_perms
                with mock.patch.object(plat_fs, "open_regular_nofollow", side_effect=lambda p, flags, mode=0o600, **_k: os.open(p, flags, mode)):
                    with mock.patch.object(
                        plat_perms,
                        "verify_private_owner_and_perms",
                        side_effect=PermissionError(errno.EACCES, "bad dacls"),
                    ):
                        with self.assertRaises(PermissionError):
                            ipc.read_pipe_endpoint_win(home)

            # MeshClient constructed before daemon start refreshes socket_path inside _connect_socket
            current_ep = [det]
            with mock.patch.object(ipc, "daemon_endpoint_path", side_effect=lambda _h: current_ep[0]):
                mc = client_mod.MeshClient(home)
                self.assertEqual(mc.socket_path, det)
                current_ep[0] = p1
                with (
                    mock.patch.object(client_mod.sys, "platform", "win32"),
                    mock.patch.object(ipc, "connect_endpoint", return_value="STREAM") as m_conn,
                ):
                    self.assertEqual(mc._connect_socket(1.0), "STREAM")
                self.assertEqual(mc.socket_path, p1)
                m_conn.assert_called_once_with(p1, timeout=1.0)

            ipc.remove_pipe_endpoint_win(home)
            self.assertFalse(os.path.exists(pointer))

    def test_p2_4_classify_peer_run_session_case_insensitive_mesh_run_id_mocked(self) -> None:
        with (
            mock.patch.object(auth.sys, "platform", "win32"),
            mock.patch.object(auth._plat_proc, "find_active_job_for_pid", return_value=None),
            mock.patch.object(auth, "get_proc_stat", return_value={"session": 0, "ppid": 1}),
            mock.patch.object(auth, "get_proc_ancestry", return_value=[]),
            mock.patch.object(auth, "get_proc_environ", return_value={"mesh_run_id": "run_ci"}),
        ):
            self.assertEqual(
                auth.classify_peer_run_session(400, {"run_ci": 200}, {"run_ci"}, 100),
                "run_ci",
            )

    def test_p3_4_daemon_start_preserves_mixed_case_pythonpath_win32_mocked(self) -> None:
        from mesh.cli.commands import daemon as daemon_cmd

        captured_env: dict[str, str] = {}

        def _fake_popen(*_a: Any, **kw: Any) -> Any:
            captured_env.update(kw["env"])
            raise RuntimeError("stop-before-spawn")

        fake_env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        fake_env["PythonPath"] = r"C:\custom_py"
        with (
            mock.patch.object(daemon_cmd.sys, "platform", "win32"),
            mock.patch.object(daemon_cmd, "_meshd_lock_held", return_value=False),
            mock.patch.object(daemon_cmd, "MeshClient") as m_mc,
            mock.patch.object(daemon_cmd.subprocess, "Popen", side_effect=_fake_popen),
            mock.patch.object(daemon_cmd.os, "environ", fake_env),
        ):
            m_mc.return_value.home = r"C:\tmp\h"
            m_mc.return_value.execute_cmd.side_effect = RuntimeError("down")
            args = argparse.Namespace(home=r"C:\tmp\h", dry_run=False, user=False)
            with self.assertRaises(RuntimeError):
                daemon_cmd.handle_daemon_start(args)
        self.assertEqual(captured_env.get("PythonPath"), r"C:\custom_py")
        self.assertNotIn("PYTHONPATH", captured_env)

    def test_win32_named_pipe_server_wrapper_and_daemon_status_run_session_mocked(self) -> None:
        from mesh.daemon.server import DaemonServer

        fake_k32 = _FakeWinIpcKernel()
        with mock.patch.object(ipc, "_create_named_pipe_instance_win", return_value=0x3456) as m_inst:
            srv_h = ipc.create_named_pipe_server_win(
                r"\\.\pipe\LOCAL\agent-mesh-test-1",
                first_instance=False,
                k32=fake_k32,
            )
            self.assertEqual(srv_h.handle, 0x3456)
            self.assertFalse(srv_h.closed)
            srv_h.close()
            srv_h.close()
            self.assertTrue(srv_h.closed)
            self.assertEqual(fake_k32.closed_handles, [0x3456])
            m_inst.assert_called_once()

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "h")
            os.makedirs(os.path.join(home, "run"), mode=0o700)
            conn = connect_db(os.path.join(home, "mesh.db"))
            init_db(os.path.join(home, "mesh.db")).close()
            try:
                srv = DaemonServer(home, testing_mode=True)
                srv.db_conn = conn
                srv.start_time = time.time()
                with (
                    mock.patch("mesh.daemon.server.sys.platform", "win32"),
                    mock.patch("mesh.daemon.server.get_peer_credentials", return_value=(1234, 0, 0)),
                    mock.patch("mesh.daemon.server.classify_peer_run_session", return_value="run_win_1"),
                ):
                    with self.assertRaises(MeshError) as cm:
                        asyncio.run(
                            srv.authenticate_and_authorize(
                                peer_pid=1234,
                                auth_info={"type": "none"},
                                cmd="daemon.status",
                                request_id="req_ds",
                            )
                        )
                    self.assertEqual(cm.exception.code, c.CREDENTIAL_NOT_ALLOWED_IN_RUN)
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE type = 'auth.credential_in_run';"
                    ).fetchone()[0]
                    self.assertEqual(cnt, 0)
            finally:
                conn.close()


@unittest.skipUnless(sys.platform == "win32", "Windows-only P0-1 pipe server verification and mesh.pipe")
class TestFinalReviewIpcWinRealWindows(unittest.TestCase):
    def test_p0_1_real_unpredictable_pipe_pointer_and_server_sid_verification(self) -> None:
        with tempfile.TemporaryDirectory(dir=ipc._plat_paths.temp_root() if hasattr(ipc, "_plat_paths") else None) as td:
            home = os.path.realpath(td)
            p1 = ipc.generate_pipe_endpoint_win(home)
            p2 = ipc.generate_pipe_endpoint_win(home)
            self.assertNotEqual(p1, p2)

            srv_h = ipc.create_named_pipe_server_win(p1, first_instance=True)
            try:
                pointer = ipc.write_pipe_endpoint_win(home, p1)
                self.assertEqual(ipc.read_pipe_endpoint_win(home), p1)
                self.assertEqual(ipc.daemon_endpoint_path_win(home), p1)
                perms.verify_private_owner_and_perms(pointer, is_dir=False)

                k32 = w32.get_kernel32()
                ov_r = w32.OVERLAPPED()
                ev_r = k32.CreateEventW(None, True, False, None)
                ov_r.hEvent = ev_r
                try:
                    k32.ConnectNamedPipe(srv_h.handle, ctypes.byref(ov_r))
                    stream = ipc.connect_pipe_win(p1, timeout=2.0)
                    stream.close()
                finally:
                    k32.CloseHandle(ev_r)

                # Second pipe instance: reject mismatched expected_user_sid fail-closed
                srv_h2 = ipc.create_named_pipe_server_win(p1, first_instance=False)
                try:
                    ov_r2 = w32.OVERLAPPED()
                    ev_r2 = k32.CreateEventW(None, True, False, None)
                    ov_r2.hEvent = ev_r2
                    try:
                        k32.ConnectNamedPipe(srv_h2.handle, ctypes.byref(ov_r2))
                        with self.assertRaises(PermissionError):
                            ipc.connect_pipe_win(p1, timeout=2.0, expected_user_sid="S-1-5-18")
                    finally:
                        k32.CloseHandle(ev_r2)
                finally:
                    srv_h2.close()
            finally:
                srv_h.close()
                ipc.remove_pipe_endpoint_win(home)
                self.assertIsNone(ipc.read_pipe_endpoint_win(home))


if __name__ == "__main__":
    unittest.main()
