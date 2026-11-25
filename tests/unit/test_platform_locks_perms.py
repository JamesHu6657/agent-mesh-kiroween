"""Unit tests for mesh.platform.locks and mesh.platform.perms (Phase 2a / T04)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import mesh.constants as c
from mesh.errors import MeshError
from mesh.platform import locks, paths, perms
from tests import win_sim


def _encode_win32_sid(sid_str: str) -> bytes:
    """Encode a standard Windows SID string like 'S-1-5-32-544' into raw NT SID bytes."""
    parts = sid_str.split("-")
    if len(parts) < 4 or parts[0] != "S":
        raise ValueError(f"Unsupported SID: {sid_str!r}")
    rev = int(parts[1])
    auth = int(parts[2])
    subs = [int(x) for x in parts[3:]]
    header = bytes([rev, len(subs)]) + auth.to_bytes(6, "big")
    return header + b"".join(struct.pack("<I", s) for s in subs)


def _decode_win32_sid(raw: bytes) -> str:
    """Decode raw NT SID bytes into a canonical 'S-...' string."""
    rev = raw[0]
    sub_count = raw[1]
    auth = int.from_bytes(raw[2:8], "big")
    subs = [struct.unpack("<I", raw[8 + 4 * i : 12 + 4 * i])[0] for i in range(sub_count)]
    return "-".join(["S", str(rev), str(auth)] + [str(s) for s in subs])


def _build_win32_acl_buffer(aces: list[tuple[int, int, int, str]]) -> ctypes.Array:
    """Pack a binary Win32 ACL + ACEs + binary SIDs into a ctypes string buffer."""
    ace_blobs: list[bytes] = []
    for ace_type, ace_flags, mask, sid_str in aces:
        sid_bytes = _encode_win32_sid(sid_str)
        ace_size = 8 + len(sid_bytes)
        ace_blobs.append(struct.pack("<BBHI", ace_type, ace_flags, ace_size, mask) + sid_bytes)
    total_size = 8 + sum(len(b) for b in ace_blobs)
    acl_hdr = struct.pack("<BBHHH", 2, 0, total_size, len(ace_blobs), 0)
    raw = acl_hdr + b"".join(ace_blobs)
    return ctypes.create_string_buffer(raw, len(raw))


class TestPlatformLocksPermsImportHygiene(unittest.TestCase):
    """Ensure importing locks/perms stays OS-isolated."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX import-hygiene check")
    def test_linux_import_never_touches_windows_modules(self) -> None:
        code = (
            "import ctypes, sys\n"
            "assert not hasattr(ctypes, 'windll')\n"
            "import mesh.platform\n"
            "from mesh.platform import locks, perms\n"
            "for mod in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32_ctypes'):\n"
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

    def test_locks_import_under_win32_never_requires_fcntl(self) -> None:
        # tests/win_sim.py: fcntl stays poisoned while Windows-only stdlib
        # modules are stubbed, so this still proves fcntl is never needed.
        code = (
            win_sim.SIMULATED_WIN32_PRELUDE
            + "from mesh.platform import locks, perms\n"
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


class TestPosixLocksAndPerms(unittest.TestCase):
    """POSIX lock contention, crash cleanup, and permission parity checks."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only behavior")
    def test_posix_cross_process_lock_contention_and_kill_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            lock_path = os.path.join(td, "test.lock")
            self.assertFalse(locks.is_exclusive_lock_held(lock_path))

            fd = locks.open_lock_file(lock_path)
            try:
                self.assertFalse(os.get_inheritable(fd))
                locks.acquire_lock(fd, exclusive=True, blocking=False)
                self.assertTrue(locks.is_exclusive_lock_held(lock_path))

                # Child process cannot take non-blocking exclusive or shared lock
                probe_code = (
                    "import sys\n"
                    "from mesh.platform import locks\n"
                    "fd = locks.open_lock_file(sys.argv[1])\n"
                    "try:\n"
                    "    locks.acquire_lock(fd, exclusive=(sys.argv[2] == 'ex'), blocking=False)\n"
                    "    sys.exit(0)\n"
                    "except BlockingIOError:\n"
                    "    sys.exit(42)\n"
                )
                repo_root = str(pathlib.Path(__file__).resolve().parents[2])
                env = dict(os.environ, PYTHONPATH=repo_root)
                for mode in ("ex", "sh"):
                    res = subprocess.run(
                        [sys.executable, "-c", probe_code, lock_path, mode],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(res.returncode, 42, f"mode={mode} stderr={res.stderr}")

                locks.release_lock(fd)
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
            finally:
                os.close(fd)

            # Shared locks allow concurrent readers, block exclusive writer
            with locks.FileLock(lock_path, exclusive=False, blocking=False):
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
                res_sh = subprocess.run(
                    [sys.executable, "-c", probe_code, lock_path, "sh"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(res_sh.returncode, 0, res_sh.stderr)
                res_ex = subprocess.run(
                    [sys.executable, "-c", probe_code, lock_path, "ex"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(res_ex.returncode, 42, res_ex.stderr)

            # Abrupt SIGKILL of child holder releases lock immediately
            ready_file = os.path.join(td, "child.ready")
            holder_code = (
                "import pathlib, sys, time\n"
                "from mesh.platform import locks\n"
                "fd = locks.open_lock_file(sys.argv[1])\n"
                "locks.acquire_lock(fd, exclusive=True, blocking=False)\n"
                "pathlib.Path(sys.argv[2]).write_text('1', encoding='utf-8')\n"
                "time.sleep(30)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", holder_code, lock_path, ready_file],
                env=env,
            )
            try:
                deadline = time.monotonic() + 5.0
                while not os.path.exists(ready_file) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(ready_file))
                self.assertTrue(locks.is_exclusive_lock_held(lock_path))
                proc.kill()
                proc.wait(timeout=5)
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only behavior")
    def test_posix_perms_and_daemon_helper_wiring(self) -> None:
        from mesh.cli.commands.daemon import _meshd_lock_held

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            run_dir = os.path.join(home, "run")
            os.makedirs(run_dir, mode=0o700)
            perms.set_private_dir_acl(home)
            self.assertEqual(os.stat(home).st_mode & 0o777, 0o700)
            perms.verify_private_owner_and_perms(home, is_dir=True)

            f_path = os.path.join(home, "secret.tok")
            fd = os.open(f_path, os.O_WRONLY | os.O_CREAT, 0o644)
            try:
                perms.fchmod_private(fd, 0o600)
                perms.verify_private_owner_and_perms(fd, is_dir=False)
            finally:
                os.close(fd)
            self.assertEqual(os.stat(f_path).st_mode & 0o777, 0o600)
            perms.verify_private_owner_and_perms(f_path, is_dir=False)

            os.chmod(f_path, 0o644)
            with self.assertRaises(MeshError) as ctx:
                perms.verify_private_owner_and_perms(f_path, is_dir=False)
            self.assertEqual(ctx.exception.code, c.INSECURE_PERMISSIONS)

            os.chmod(home, 0o755)
            perms.verify_dir_not_other_writable(home)
            os.chmod(home, 0o777)
            with self.assertRaises(MeshError) as ctx2:
                perms.verify_dir_not_other_writable(home)
            self.assertEqual(ctx2.exception.code, c.INSECURE_PERMISSIONS)
            os.chmod(home, 0o1777)
            perms.verify_dir_not_other_writable(home)

            # Foreign UID check
            fake_st = os.stat_result((0o100600, 1, 1, 1, os.getuid() + 1, 0, 0, 0, 0, 0))
            with mock.patch("os.stat", return_value=fake_st):
                with self.assertRaises(MeshError) as ctx_uid:
                    perms.verify_private_owner_and_perms(f_path)
                self.assertEqual(ctx_uid.exception.code, c.INSECURE_PERMISSIONS)

            with self.assertRaises(NotImplementedError):
                perms.grant_everyone_read_for_test(f_path)

            # _meshd_lock_held via platform.locks
            self.assertFalse(_meshd_lock_held(home))
            lock_file = os.path.join(run_dir, "meshd.lock")
            with locks.FileLock(lock_file, exclusive=True, blocking=False):
                self.assertTrue(_meshd_lock_held(home))
            self.assertFalse(_meshd_lock_held(home))


class TestWin32LocksPermsPureLogic(unittest.TestCase):
    """Cross-platform pure-logic and mock-ctypes tests exercised on Linux & Windows."""

    def test_win32_ctypes_bindings_and_struct_layouts(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        class _FakeFn:
            pass

        class _FakeKernel:
            def __init__(self) -> None:
                for name in ("LockFileEx", "UnlockFileEx", "LocalFree", "GetCurrentProcess", "ReOpenFile"):
                    setattr(self, name, _FakeFn())

        class _FakeAdvapi:
            def __init__(self) -> None:
                for name in (
                    "OpenProcessToken",
                    "GetTokenInformation",
                    "ConvertSidToStringSidW",
                    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                    "GetSecurityDescriptorDacl",
                    "GetSecurityInfo",
                    "SetSecurityInfo",
                    "GetAce",
                    "IsValidSid",
                ):
                    setattr(self, name, _FakeFn())

        k32 = w32.bind_kernel32_locks(_FakeKernel())
        self.assertEqual(k32.LockFileEx.restype, wintypes.BOOL)
        self.assertEqual(len(k32.LockFileEx.argtypes), 6)
        self.assertEqual(k32.UnlockFileEx.restype, wintypes.BOOL)
        self.assertEqual(len(k32.UnlockFileEx.argtypes), 5)
        self.assertEqual(k32.LocalFree.argtypes, [wintypes.HLOCAL])
        self.assertEqual(k32.LocalFree.restype, wintypes.HLOCAL)
        self.assertEqual(k32.GetCurrentProcess.argtypes, [])
        self.assertEqual(k32.GetCurrentProcess.restype, wintypes.HANDLE)
        self.assertEqual(k32.ReOpenFile.restype, wintypes.HANDLE)
        self.assertEqual(len(k32.ReOpenFile.argtypes), 4)

        adv = w32.bind_advapi32(_FakeAdvapi())
        self.assertEqual(adv.OpenProcessToken.restype, wintypes.BOOL)
        self.assertEqual(len(adv.OpenProcessToken.argtypes), 3)
        self.assertEqual(adv.GetTokenInformation.restype, wintypes.BOOL)
        self.assertEqual(len(adv.GetTokenInformation.argtypes), 5)
        self.assertEqual(adv.ConvertSidToStringSidW.restype, wintypes.BOOL)
        self.assertEqual(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype, wintypes.BOOL)
        self.assertEqual(adv.GetSecurityDescriptorDacl.restype, wintypes.BOOL)
        self.assertEqual(adv.GetSecurityInfo.restype, wintypes.DWORD)
        self.assertEqual(len(adv.GetSecurityInfo.argtypes), 8)
        self.assertEqual(adv.SetSecurityInfo.restype, wintypes.DWORD)
        self.assertEqual(len(adv.SetSecurityInfo.argtypes), 7)
        self.assertEqual(adv.GetAce.restype, wintypes.BOOL)
        self.assertEqual(adv.IsValidSid.restype, wintypes.BOOL)

        # Verify exact Win32 ABI struct sizes/offsets regardless of host LP64 vs LLP64
        expected_ov_size = 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20
        self.assertEqual(ctypes.sizeof(w32.OVERLAPPED), expected_ov_size)
        ov = w32.OVERLAPPED()
        self.assertEqual(ov.Offset, 0)
        self.assertEqual(ov.OffsetHigh, 0)
        self.assertIsNone(ov.hEvent)
        self.assertEqual(ctypes.sizeof(w32.ACL), 8)
        self.assertEqual(ctypes.sizeof(w32.ACE_HEADER), 4)
        self.assertEqual(w32.ACCESS_ALLOWED_ACE.SidStart.offset, 8)

    def test_win32_locks_mock_flags_overlapped_and_errors(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        lock_calls = []
        unlock_calls = []
        inheritable_calls = []

        class _MockKernel:
            def __init__(self, *, lock_ok: int = 1, unlock_ok: int = 1) -> None:
                self.lock_ok = lock_ok
                self.unlock_ok = unlock_ok

            def LockFileEx(self, handle, flags, reserved, low, high, ov_ptr):
                ov = ctypes.cast(ov_ptr, ctypes.POINTER(w32.OVERLAPPED)).contents
                lock_calls.append((handle, flags, reserved, low, high, ov.Offset, ov.OffsetHigh, ov.hEvent))
                return self.lock_ok

            def UnlockFileEx(self, handle, reserved, low, high, ov_ptr):
                ov = ctypes.cast(ov_ptr, ctypes.POINTER(w32.OVERLAPPED)).contents
                unlock_calls.append((handle, reserved, low, high, ov.Offset, ov.OffsetHigh, ov.hEvent))
                return self.unlock_ok

        fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: fd + 1000)

        locks.acquire_lock_win(
            7,
            exclusive=True,
            blocking=False,
            k32=_MockKernel(),
            msvcrt_mod=fake_msvcrt,
            set_inheritable_fn=lambda fd, inh: inheritable_calls.append((fd, inh)),
        )
        locks.acquire_lock_win(
            8,
            exclusive=False,
            blocking=True,
            k32=_MockKernel(),
            msvcrt_mod=fake_msvcrt,
            set_inheritable_fn=lambda fd, inh: inheritable_calls.append((fd, inh)),
        )
        self.assertEqual(inheritable_calls, [(7, False), (8, False)])
        self.assertEqual(
            lock_calls,
            [
                (1007, w32.LOCKFILE_EXCLUSIVE_LOCK | w32.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, 0, 0, None),
                (1008, 0, 0, 1, 0, 0, 0, None),
            ],
        )

        locks.release_lock_win(7, k32=_MockKernel(), msvcrt_mod=fake_msvcrt)
        self.assertEqual(unlock_calls, [(1007, 0, 1, 0, 0, 0, None)])

        # ERROR_LOCK_VIOLATION raises BlockingIOError; other errors raise OSError
        with self.assertRaises(BlockingIOError) as ctx:
            locks.acquire_lock_win(
                7,
                exclusive=True,
                blocking=False,
                k32=_MockKernel(lock_ok=0),
                msvcrt_mod=fake_msvcrt,
                get_last_error_fn=lambda: w32.ERROR_LOCK_VIOLATION,
                set_inheritable_fn=lambda fd, inh: None,
            )
        self.assertEqual(getattr(ctx.exception, "winerror", None), w32.ERROR_LOCK_VIOLATION)

        with self.assertRaises(OSError) as ctx2:
            locks.acquire_lock_win(
                7,
                k32=_MockKernel(lock_ok=0),
                msvcrt_mod=fake_msvcrt,
                get_last_error_fn=lambda: w32.ERROR_ACCESS_DENIED,
                set_inheritable_fn=lambda fd, inh: None,
            )
        self.assertNotIsInstance(ctx2.exception, BlockingIOError)

        # is_exclusive_lock_held_win probe behavior and cleanup
        closed = []
        released = []
        self.assertFalse(
            locks.is_exclusive_lock_held_win(
                "missing.lock",
                open_fn=mock.Mock(side_effect=FileNotFoundError("no")),
                close_fn=closed.append,
            )
        )
        self.assertEqual(closed, [])

        self.assertFalse(
            locks.is_exclusive_lock_held_win(
                "free.lock",
                open_fn=lambda p, f: 55,
                close_fn=closed.append,
                acquire_fn=lambda fd, **kw: None,
                release_fn=released.append,
            )
        )
        self.assertEqual(released, [55])
        self.assertEqual(closed, [55])

        closed.clear()
        released.clear()
        self.assertTrue(
            locks.is_exclusive_lock_held_win(
                "busy.lock",
                open_fn=lambda p, f: 56,
                close_fn=closed.append,
                acquire_fn=mock.Mock(side_effect=BlockingIOError(errno.EAGAIN, "busy")),
                release_fn=released.append,
            )
        )
        self.assertEqual(released, [])
        self.assertEqual(closed, [56])

    def test_win32_sddl_and_dacl_evaluator(self) -> None:
        user_sid = "S-1-5-21-100-200-300-1001"
        self.assertEqual(
            perms.build_private_sddl(user_sid, is_dir=True),
            f"D:PAI(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
        )
        self.assertEqual(
            perms.build_private_sddl(user_sid, is_dir=False),
            f"D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)",
        )
        self.assertEqual(
            perms.build_private_sddl(perms.LOCAL_SYSTEM_SID, is_dir=False),
            f"D:P(A;;FA;;;{perms.LOCAL_SYSTEM_SID})(A;;FA;;;BA)",
        )
        self.assertEqual(
            perms.build_everyone_sddl(user_sid, is_dir=True, access="FR"),
            f"D:PAI(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FR;;;WD)",
        )
        with self.assertRaises(ValueError):
            perms.build_private_sddl("S-1-5;evil", is_dir=True)

        trusted_owners = {user_sid, perms.LOCAL_SYSTEM_SID, perms.BUILTIN_ADMINISTRATORS_SID}
        trusted_aces = trusted_owners | {perms.OWNER_RIGHTS_SID}

        # Valid private DACL (including OwnerRights and elevated BA owner)
        ok_aces = [
            (0, 3, 0x001F01FF, user_sid),
            (0, 3, 0x001F01FF, perms.LOCAL_SYSTEM_SID),
            (0, 3, 0x001F01FF, perms.BUILTIN_ADMINISTRATORS_SID),
            (0, 0, 0x00120089, perms.OWNER_RIGHTS_SID),
            (0, 0, 0, perms.EVERYONE_SID),  # zero-mask allow ACE grants nothing
            (1, 0, 0x10000000, perms.EVERYONE_SID),  # deny ACE restricts, never grants
        ]
        self.assertIsNone(
            perms.evaluate_win_dacl(
                perms.BUILTIN_ADMINISTRATORS_SID,
                ok_aces,
                trusted_owner_sids=trusted_owners,
                trusted_ace_sids=trusted_aces,
                write_only=False,
            )
        )

        # Foreign owner rejected
        v_owner = perms.evaluate_win_dacl(
            "S-1-5-21-100-200-300-9999",
            ok_aces,
            trusted_owner_sids=trusted_owners,
            trusted_ace_sids=trusted_aces,
        )
        self.assertEqual(v_owner["reason"], "owner")

        # NULL DACL rejected
        v_null = perms.evaluate_win_dacl(
            user_sid,
            None,
            trusted_owner_sids=trusted_owners,
            trusted_ace_sids=trusted_aces,
        )
        self.assertEqual(v_null["reason"], "null_dacl")
        self.assertTrue(v_null["null_dacl"])

        # Everyone / Users / Authenticated Users read ACE rejected under strict 0600/0700
        for bad_sid in (
            perms.EVERYONE_SID,
            perms.BUILTIN_USERS_SID,
            perms.AUTHENTICATED_USERS_SID,
            "S-1-5-21-100-200-300-1002",
        ):
            v_bad = perms.evaluate_win_dacl(
                user_sid,
                ok_aces + [(0, 0, 0x00120089, bad_sid)],
                trusted_owner_sids=trusted_owners,
                trusted_ace_sids=trusted_aces,
                write_only=False,
            )
            self.assertEqual(v_bad["reason"], "perms")
            self.assertEqual(v_bad["bad_sids"], [bad_sid])

        # Under write_only=True (0o022 equiv), Everyone read-only is allowed, write mask is rejected
        read_only_aces = ok_aces + [(0, 0, 0x00120089, perms.EVERYONE_SID)]
        self.assertIsNone(
            perms.evaluate_win_dacl(
                user_sid,
                read_only_aces,
                trusted_owner_sids=trusted_owners,
                trusted_ace_sids=trusted_aces,
                write_only=True,
            )
        )
        for write_bit in (0x0002, 0x0004, 0x0040, 0x00010000, 0x00040000, 0x01000000, 0x40000000, 0x10000000):
            v_w = perms.evaluate_win_dacl(
                user_sid,
                ok_aces + [(0, 0, write_bit, perms.BUILTIN_USERS_SID)],
                trusted_owner_sids=trusted_owners,
                trusted_ace_sids=trusted_aces,
                write_only=True,
            )
            self.assertEqual(v_w["reason"], "perms", f"Expected failure for write_bit={write_bit:#x}")

        # Unknown ACE type fails closed
        v_unk = perms.evaluate_win_dacl(
            user_sid,
            [(9, 0, 0x001F01FF, perms.EVERYONE_SID)],
            trusted_owner_sids=trusted_owners,
            trusted_ace_sids=trusted_aces,
        )
        self.assertEqual(v_unk["reason"], "unknown_ace")

    def test_win32_acl_ctypes_parsing_localfree_closehandle_and_reopenfile(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        user_sid = "S-1-5-21-10-20-30-1001"
        owner_sid_bytes = ctypes.create_string_buffer(_encode_win32_sid(user_sid))

        class _MockHarness:
            def __init__(self, aces: list[tuple[int, int, int, str]], *, is_dir: bool = False) -> None:
                self.acl_buf = _build_win32_acl_buffer(aces)
                self.is_dir = is_dir
                self.attrs = w32.FILE_ATTRIBUTE_DIRECTORY if is_dir else w32.FILE_ATTRIBUTE_NORMAL
                self.ftype = w32.FILE_TYPE_DISK
                self.allocs: dict[int, Any] = {}
                self.freed: list[int] = []
                self.closed_handles: list[int] = []
                self.open_handles: list[int] = []
                self.next_handle = 500
                self.set_calls: list[tuple[int, int, Any]] = []
                self.deny_first_set = False
                self.fail_second_set = False
                self.null_dacl = False
                self.sddl_seen: list[str] = []
                self.fail_get_ace = False
                self.fail_create = False

            def _alloc_str(self, text: str) -> int:
                buf = ctypes.create_unicode_buffer(text)
                addr = ctypes.addressof(buf)
                self.allocs[addr] = buf
                return addr

            def _alloc_sd_marker(self) -> int:
                buf = ctypes.create_string_buffer(64)
                addr = ctypes.addressof(buf)
                self.allocs[addr] = buf
                return addr

            # kernel32 methods
            def GetCurrentProcess(self) -> int:
                return -1

            def CreateFileW(self, *args) -> int:
                if self.fail_create:
                    return w32.INVALID_HANDLE_VALUE
                self.next_handle += 1
                h = self.next_handle
                self.open_handles.append(h)
                return h

            def ReOpenFile(self, orig, access, share, flags) -> int:
                self.next_handle += 1
                h = self.next_handle
                self.open_handles.append(h)
                return h

            def CloseHandle(self, h) -> int:
                addr = h.value if hasattr(h, "value") else int(h)
                self.closed_handles.append(addr)
                return 1

            def LocalFree(self, ptr) -> int:
                addr = ptr.value if hasattr(ptr, "value") else int(ptr)
                self.freed.append(addr)
                self.allocs.pop(addr, None)
                return 0

            def GetFileType(self, h) -> int:
                return self.ftype

            def GetFileInformationByHandle(self, h, info_ptr) -> int:
                info = ctypes.cast(info_ptr, ctypes.POINTER(w32.BY_HANDLE_FILE_INFORMATION)).contents
                info.dwFileAttributes = self.attrs
                return 1

            # advapi32 methods
            def OpenProcessToken(self, proc_h, access, tok_ptr) -> int:
                self.next_handle += 1
                h = self.next_handle
                self.open_handles.append(h)
                ctypes.cast(tok_ptr, ctypes.POINTER(wintypes.HANDLE)).contents.value = h
                return 1

            def GetTokenInformation(self, tok_h, info_cls, buf, buf_len, needed_ptr) -> int:
                needed = ctypes.cast(needed_ptr, wintypes.LPDWORD).contents
                if buf is None:
                    needed.value = ctypes.sizeof(w32.TOKEN_USER)
                    return 0
                if info_cls == w32.TokenUser:
                    tu = w32.TOKEN_USER.from_buffer(buf)
                    tu.User.Sid = ctypes.addressof(owner_sid_bytes)
                else:
                    to = w32.TOKEN_OWNER.from_buffer(buf)
                    to.Owner = ctypes.addressof(owner_sid_bytes)
                return 1

            def IsValidSid(self, psid) -> int:
                return 1 if psid else 0

            def ConvertSidToStringSidW(self, psid, out_ptr) -> int:
                raw_hdr = ctypes.string_at(psid, 8)
                total_len = 8 + 4 * raw_hdr[1]
                sid_str = _decode_win32_sid(ctypes.string_at(psid, total_len))
                addr = self._alloc_str(sid_str)
                ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = addr
                return 1

            def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, sddl, rev, sd_ptr, sz_ptr) -> int:
                self.sddl_seen.append(sddl)
                addr = self._alloc_sd_marker()
                ctypes.cast(sd_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = addr
                return 1

            def GetSecurityDescriptorDacl(self, sd, present_ptr, dacl_ptr, defaulted_ptr) -> int:
                ctypes.cast(present_ptr, ctypes.POINTER(wintypes.BOOL)).contents.value = 1
                ctypes.cast(dacl_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(self.acl_buf)
                return 1

            def GetSecurityInfo(self, h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p, sd_p) -> int:
                ctypes.cast(owner_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(owner_sid_bytes)
                dacl_addr = 0 if self.null_dacl else ctypes.addressof(self.acl_buf)
                ctypes.cast(dacl_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = dacl_addr
                ctypes.cast(sd_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = self._alloc_sd_marker()
                return 0

            def SetSecurityInfo(self, h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p) -> int:
                self.set_calls.append((h, sec_info, dacl_p))
                if self.deny_first_set and len(self.set_calls) == 1:
                    return w32.ERROR_ACCESS_DENIED
                if self.fail_second_set:
                    return w32.ERROR_ACCESS_DENIED
                return 0

            def GetAce(self, pacl, idx, ace_out) -> int:
                if self.fail_get_ace:
                    return 0
                acl_hdr = w32.ACL.from_address(pacl)
                off = 8
                for cur in range(acl_hdr.AceCount):
                    if cur == idx:
                        ctypes.cast(ace_out, ctypes.POINTER(ctypes.c_void_p)).contents.value = pacl + off
                        return 1
                    hdr = w32.ACE_HEADER.from_address(pacl + off)
                    off += hdr.AceSize
                return 0

        # 1. Valid DACL parsed via real ctypes ACL/ACE/SID structs; zero leaks
        h1 = _MockHarness(
            [
                (0, 3, 0x001F01FF, user_sid),
                (0, 3, 0x001F01FF, perms.LOCAL_SYSTEM_SID),
                (0, 3, 0x001F01FF, perms.BUILTIN_ADMINISTRATORS_SID),
            ],
            is_dir=True,
        )
        perms.verify_private_owner_and_perms_win(
            "C:\\mesh_home",
            is_dir=True,
            advapi=h1,
            k32=h1,
            get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
        )
        self.assertEqual(h1.allocs, {}, "Leaked LocalAlloc buffers")
        self.assertEqual(sorted(h1.closed_handles), sorted(h1.open_handles))

        # 2. Insecure Everyone ACE triggers MeshError(INSECURE_PERMISSIONS) and still frees all memory/handles
        h2 = _MockHarness(
            [
                (0, 3, 0x001F01FF, user_sid),
                (0, 3, 0x00120089, perms.EVERYONE_SID),
            ],
            is_dir=False,
        )
        with self.assertRaises(MeshError) as ctx_perm:
            perms.verify_private_owner_and_perms_win(
                "C:\\secret.tok",
                is_dir=False,
                advapi=h2,
                k32=h2,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(ctx_perm.exception.code, c.INSECURE_PERMISSIONS)
        self.assertEqual(ctx_perm.exception.details["bad_sids"], [perms.EVERYONE_SID])
        self.assertEqual(h2.allocs, {})
        self.assertEqual(sorted(h2.closed_handles), sorted(h2.open_handles))

        # 2b. NULL DACL triggers MeshError(INSECURE_PERMISSIONS) and frees all memory/handles
        h2_null = _MockHarness([], is_dir=False)
        h2_null.null_dacl = True
        with self.assertRaises(MeshError) as ctx_null:
            perms.verify_private_owner_and_perms_win(
                "C:\\null_dacl.tok",
                is_dir=False,
                advapi=h2_null,
                k32=h2_null,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(ctx_null.exception.code, c.INSECURE_PERMISSIONS)
        self.assertTrue(ctx_null.exception.details["null_dacl"])
        self.assertEqual(h2_null.allocs, {})
        self.assertEqual(sorted(h2_null.closed_handles), sorted(h2_null.open_handles))

        # 3. GetAce error frees SD and closes all handles
        h3 = _MockHarness([(0, 0, 0x001F01FF, user_sid)])
        h3.fail_get_ace = True
        with self.assertRaises(OSError):
            perms.verify_private_owner_and_perms_win(
                "C:\\err.tok",
                advapi=h3,
                k32=h3,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(h3.allocs, {})
        self.assertEqual(sorted(h3.closed_handles), sorted(h3.open_handles))

        # 4. fchmod_private_win on fd lacking WRITE_DAC triggers ReOpenFile, frees SD, closes token & reopened handles (NOT caller fd handle)
        h4 = _MockHarness([(0, 0, 0x001F01FF, user_sid)], is_dir=False)
        h4.deny_first_set = True
        fake_msvcrt = SimpleNamespace(get_osfhandle=lambda fd: 9000 + fd)
        perms.fchmod_private_win(
            12,
            0o600,
            advapi=h4,
            k32=h4,
            msvcrt_mod=fake_msvcrt,
            get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
        )
        self.assertEqual(len(h4.set_calls), 2)
        self.assertEqual(h4.set_calls[0][0], 9012)
        self.assertNotEqual(h4.set_calls[1][0], 9012)
        self.assertNotIn(9012, h4.closed_handles)
        self.assertEqual(h4.allocs, {})
        self.assertEqual(sorted(h4.closed_handles), sorted(h4.open_handles))
        self.assertEqual(h4.sddl_seen, [perms.build_private_sddl(user_sid, is_dir=False)])

        # 4b. If ReOpenFile SetSecurityInfo also fails, SD & reopened handle are still freed/closed
        h4_fail = _MockHarness([(0, 0, 0x001F01FF, user_sid)], is_dir=False)
        h4_fail.deny_first_set = True
        h4_fail.fail_second_set = True
        with self.assertRaises(OSError):
            perms.fchmod_private_win(
                12,
                0o600,
                advapi=h4_fail,
                k32=h4_fail,
                msvcrt_mod=fake_msvcrt,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(h4_fail.allocs, {})
        self.assertEqual(sorted(h4_fail.closed_handles), sorted(h4_fail.open_handles))

        # 5. Reparse point rejected with ELOOP and handle closed
        h5 = _MockHarness([], is_dir=True)
        h5.attrs = w32.FILE_ATTRIBUTE_DIRECTORY | w32.FILE_ATTRIBUTE_REPARSE_POINT
        with self.assertRaises(OSError) as ctx_rep:
            perms.set_private_dir_acl_win(
                "C:\\junc",
                advapi=h5,
                k32=h5,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(ctx_rep.exception.errno, errno.ELOOP)
        self.assertEqual(h5.allocs, {})
        self.assertEqual(sorted(h5.closed_handles), sorted(h5.open_handles))

        # 6. verify_dir_not_other_writable_win rejects Everyone write, accepts Everyone read
        h6_ro = _MockHarness(
            [(0, 3, 0x001F01FF, user_sid), (0, 3, 0x00120089, perms.EVERYONE_SID)],
            is_dir=True,
        )
        perms.verify_dir_not_other_writable_win(
            "C:\\tokdir",
            advapi=h6_ro,
            k32=h6_ro,
            get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
        )
        self.assertEqual(h6_ro.allocs, {})
        self.assertEqual(sorted(h6_ro.closed_handles), sorted(h6_ro.open_handles))

        h6_rw = _MockHarness(
            [(0, 3, 0x001F01FF, user_sid), (0, 3, 0x00120116, perms.BUILTIN_USERS_SID)],
            is_dir=True,
        )
        with self.assertRaises(MeshError) as ctx_rw:
            perms.verify_dir_not_other_writable_win(
                "C:\\tokdir_bad",
                advapi=h6_rw,
                k32=h6_rw,
                get_last_error_fn=lambda: w32.ERROR_INSUFFICIENT_BUFFER,
            )
        self.assertEqual(ctx_rw.exception.code, c.INSECURE_PERMISSIONS)
        self.assertEqual(h6_rw.allocs, {})
        self.assertEqual(sorted(h6_rw.closed_handles), sorted(h6_rw.open_handles))

    def test_win32_ntdll_bindings_and_struct_layouts(self) -> None:
        from mesh.platform import _win32_ctypes as w32

        class _FakeFn:
            pass

        class _FakeNtdll:
            def __init__(self) -> None:
                for name in ("NtOpenFile", "NtQueryDirectoryFile", "RtlNtStatusToDosError"):
                    setattr(self, name, _FakeFn())

        ntdll = w32.bind_ntdll(_FakeNtdll())
        self.assertEqual(ntdll.NtOpenFile.restype, ctypes.c_long)
        self.assertEqual(len(ntdll.NtOpenFile.argtypes), 6)
        self.assertEqual(ntdll.NtQueryDirectoryFile.restype, ctypes.c_long)
        self.assertEqual(len(ntdll.NtQueryDirectoryFile.argtypes), 11)
        self.assertEqual(ntdll.RtlNtStatusToDosError.restype, wintypes.ULONG)
        self.assertEqual(len(ntdll.RtlNtStatusToDosError.argtypes), 1)
        self.assertEqual(ctypes.sizeof(w32.FILE_NAMES_INFORMATION), 12)
        ptr_sz = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(w32.IO_STATUS_BLOCK), 2 * ptr_sz)
        self.assertGreaterEqual(ctypes.sizeof(w32.OBJECT_ATTRIBUTES), 24 if ptr_sz == 4 else 48)

    def test_win32_doctor_perms_mock_walk_reparse_hardlink_hooks_and_leaks(self) -> None:
        from mesh.platform import _win32_ctypes as w32
        from mesh.platform import doctor_perms_win as dpw

        user_sid = "S-1-5-21-10-20-30-1001"
        other_sid = "S-1-5-21-10-20-30-9999"
        user_sid_bytes = ctypes.create_string_buffer(_encode_win32_sid(user_sid))
        other_sid_bytes = ctypes.create_string_buffer(_encode_win32_sid(other_sid))

        class _Node:
            def __init__(
                self,
                ino: int,
                *,
                is_dir: bool = False,
                reparse: bool = False,
                nlink: int = 1,
                owner_sid: str = user_sid,
                aces: Optional[list[tuple[int, int, int, str]]] = None,
            ) -> None:
                self.ino = ino
                self.is_dir = is_dir
                self.reparse = reparse
                self.nlink = nlink
                self.owner_sid = owner_sid
                self.aces = list(
                    aces
                    if aces is not None
                    else [
                        (0, 3, 0x001F01FF, user_sid),
                        (0, 3, 0x00120089, perms.EVERYONE_SID),
                    ]
                )
                self.children: dict[str, _Node] = {}
                self.set_count = 0

        class _MockNtVfs:
            def __init__(self) -> None:
                self.root = _Node(1, is_dir=True)
                self.allocs: dict[int, Any] = {}
                self.open_handles: list[int] = []
                self.closed_handles: list[int] = []
                self.handles: dict[int, tuple[_Node, int]] = {}
                self.scan_done: set[int] = set()
                self.next_handle = 700
                self.last_error = w32.ERROR_INSUFFICIENT_BUFFER

            def _alloc_str(self, text: str) -> int:
                buf = ctypes.create_unicode_buffer(text)
                addr = ctypes.addressof(buf)
                self.allocs[addr] = buf
                return addr

            def _alloc_sd_marker(self) -> int:
                buf = ctypes.create_string_buffer(64)
                addr = ctypes.addressof(buf)
                self.allocs[addr] = buf
                return addr

            def _new_handle(self, node: _Node, access: int) -> int:
                self.next_handle += 1
                h = self.next_handle
                self.open_handles.append(h)
                self.handles[h] = (node, access)
                return h

            def GetCurrentProcess(self) -> int:
                return -1

            def CreateFileW(self, path, access, share, sa, disp, flags, tmpl) -> int:
                clean = path.rstrip("\\")
                if clean == "C:":
                    return self._new_handle(self.root, access)
                self.last_error = w32.ERROR_FILE_NOT_FOUND
                return w32.INVALID_HANDLE_VALUE

            def ReOpenFile(self, orig, access, share, flags) -> int:
                node, _ = self.handles[orig]
                return self._new_handle(node, access)

            def CloseHandle(self, h) -> int:
                addr = h.value if hasattr(h, "value") else int(h)
                self.closed_handles.append(addr)
                self.handles.pop(addr, None)
                self.scan_done.discard(addr)
                return 1

            def LocalFree(self, ptr) -> int:
                addr = ptr.value if hasattr(ptr, "value") else int(ptr)
                self.allocs.pop(addr, None)
                return 0

            def GetFileType(self, h) -> int:
                return w32.FILE_TYPE_DISK

            def GetFileInformationByHandle(self, h, info_ptr) -> int:
                node, _ = self.handles[h]
                info = ctypes.cast(info_ptr, ctypes.POINTER(w32.BY_HANDLE_FILE_INFORMATION)).contents
                attrs = w32.FILE_ATTRIBUTE_DIRECTORY if node.is_dir else w32.FILE_ATTRIBUTE_NORMAL
                if node.reparse:
                    attrs |= w32.FILE_ATTRIBUTE_REPARSE_POINT
                info.dwFileAttributes = attrs
                info.dwVolumeSerialNumber = 0x1234
                info.nFileIndexHigh = 0
                info.nFileIndexLow = node.ino
                info.nNumberOfLinks = node.nlink
                return 1

            def NtOpenFile(self, h_out_ptr, access, oa_ptr, io_sb_ptr, share, opts) -> int:
                oa = ctypes.cast(oa_ptr, ctypes.POINTER(w32.OBJECT_ATTRIBUTES)).contents
                us = oa.ObjectName.contents
                name = ctypes.string_at(us.Buffer, us.Length).decode("utf-16-le")
                parent_node, _ = self.handles[oa.RootDirectory]
                child = parent_node.children.get(name)
                if child is None:
                    return -1073741772  # STATUS_OBJECT_NAME_NOT_FOUND
                if (opts & w32.FILE_DIRECTORY_FILE) and not child.is_dir and not child.reparse:
                    return -1073741565  # STATUS_NOT_A_DIRECTORY
                h = self._new_handle(child, access)
                ctypes.cast(h_out_ptr, ctypes.POINTER(wintypes.HANDLE)).contents.value = h
                return 0

            def NtQueryDirectoryFile(self, dir_h, ev, apc, ctx, io_sb_ptr, buf, buf_len, cls, single, fn, restart) -> int:
                node, _ = self.handles[dir_h]
                if not restart and dir_h in self.scan_done:
                    return w32.STATUS_NO_MORE_FILES
                self.scan_done.add(dir_h)
                names = [".", ".."] + sorted(node.children.keys())
                base_addr = ctypes.addressof(buf)
                off = 0
                for idx, entry_name in enumerate(names):
                    wbytes = entry_name.encode("utf-16-le")
                    rec_size = ((12 + len(wbytes)) + 7) & ~7
                    next_off = 0 if idx == len(names) - 1 else rec_size
                    struct.pack_into("<III", buf, off, next_off, idx + 1, len(wbytes))
                    ctypes.memmove(base_addr + off + 12, wbytes, len(wbytes))
                    off += rec_size
                io_sb = ctypes.cast(io_sb_ptr, ctypes.POINTER(w32.IO_STATUS_BLOCK)).contents
                io_sb.Information = off
                return 0

            def RtlNtStatusToDosError(self, status: int) -> int:
                if status == -1073741772:
                    return w32.ERROR_FILE_NOT_FOUND
                if status == -1073741565:
                    return 267
                return w32.ERROR_ACCESS_DENIED

            def OpenProcessToken(self, proc_h, access, tok_ptr) -> int:
                self.next_handle += 1
                h = self.next_handle
                self.open_handles.append(h)
                ctypes.cast(tok_ptr, ctypes.POINTER(wintypes.HANDLE)).contents.value = h
                return 1

            def GetTokenInformation(self, tok_h, info_cls, buf, buf_len, needed_ptr) -> int:
                needed = ctypes.cast(needed_ptr, wintypes.LPDWORD).contents
                if buf is None:
                    needed.value = ctypes.sizeof(w32.TOKEN_USER)
                    return 0
                if info_cls == w32.TokenUser:
                    tu = w32.TOKEN_USER.from_buffer(buf)
                    tu.User.Sid = ctypes.addressof(user_sid_bytes)
                else:
                    to = w32.TOKEN_OWNER.from_buffer(buf)
                    to.Owner = ctypes.addressof(user_sid_bytes)
                return 1

            def IsValidSid(self, psid) -> int:
                return 1 if psid else 0

            def ConvertSidToStringSidW(self, psid, out_ptr) -> int:
                raw_hdr = ctypes.string_at(psid, 8)
                total_len = 8 + 4 * raw_hdr[1]
                sid_str = _decode_win32_sid(ctypes.string_at(psid, total_len))
                addr = self._alloc_str(sid_str)
                ctypes.cast(out_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = addr
                return 1

            def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, sddl, rev, sd_ptr, sz_ptr) -> int:
                addr = self._alloc_sd_marker()
                ctypes.cast(sd_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = addr
                return 1

            def GetSecurityDescriptorDacl(self, sd, present_ptr, dacl_ptr, defaulted_ptr) -> int:
                ctypes.cast(present_ptr, ctypes.POINTER(wintypes.BOOL)).contents.value = 1
                ctypes.cast(dacl_ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value = 1
                return 1

            def GetSecurityInfo(self, h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p, sd_p) -> int:
                node, _ = self.handles[h]
                owner_buf = other_sid_bytes if node.owner_sid == other_sid else user_sid_bytes
                acl_buf = _build_win32_acl_buffer(node.aces)
                self._keep_acl = acl_buf
                ctypes.cast(owner_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(owner_buf)
                ctypes.cast(dacl_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(acl_buf)
                ctypes.cast(sd_p, ctypes.POINTER(ctypes.c_void_p)).contents.value = self._alloc_sd_marker()
                return 0

            def SetSecurityInfo(self, h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p) -> int:
                node, _ = self.handles[h]
                node.set_count += 1
                node.aces = [
                    (0, 3, 0x001F01FF, user_sid),
                    (0, 3, 0x001F01FF, perms.LOCAL_SYSTEM_SID),
                    (0, 3, 0x001F01FF, perms.BUILTIN_ADMINISTRATORS_SID),
                ]
                return 0

            def GetAce(self, pacl, idx, ace_out) -> int:
                acl_hdr = w32.ACL.from_address(pacl)
                off = 8
                for cur in range(acl_hdr.AceCount):
                    if cur == idx:
                        ctypes.cast(ace_out, ctypes.POINTER(ctypes.c_void_p)).contents.value = pacl + off
                        return 1
                    hdr = w32.ACE_HEADER.from_address(pacl + off)
                    off += hdr.AceSize
                return 0

        def _build_tree(vfs: _MockNtVfs):
            home = _Node(10, is_dir=True)
            db = _Node(11, is_dir=False)
            runs = _Node(12, is_dir=True)
            r1 = _Node(13, is_dir=True)
            f = _Node(14, is_dir=False)
            junc = _Node(15, is_dir=True, reparse=True)
            hl = _Node(16, is_dir=False, nlink=2)
            foreign = _Node(17, is_dir=False, owner_sid=other_sid)
            outside = _Node(99, is_dir=True)
            outside_f = _Node(100, is_dir=False)
            outside.children["secret"] = outside_f

            r1.children["f"] = f
            runs.children["r1"] = r1
            runs.children["junc"] = junc
            runs.children["hl"] = hl
            runs.children["foreign"] = foreign
            home.children["mesh.db"] = db
            home.children["runs"] = runs
            vfs.root.children["home"] = home
            vfs.root.children["outside"] = outside
            return home, db, runs, r1, f, junc, hl, foreign, outside, outside_f

        # 1. Clean walk skips reparse/hardlink/foreign and repairs the rest with zero leaks
        vfs1 = _MockNtVfs()
        home, db, runs, r1, f, junc, hl, foreign, outside, outside_f = _build_tree(vfs1)
        ident = dpw.home_identity_win(
            "C:\\home", k32=vfs1, ntdll=vfs1, get_last_error_fn=lambda: vfs1.last_error
        )
        self.assertEqual(ident, (0x1234, 10))
        rep1 = dpw.fix_home_permissions(
            "C:\\home",
            expected=ident,
            k32=vfs1,
            advapi=vfs1,
            ntdll=vfs1,
            get_last_error_fn=lambda: vfs1.last_error,
        )
        self.assertEqual(rep1.unsafe, [])
        self.assertIn({"path": "runs/junc", "reason": "symlink, not followed"}, rep1.skipped)
        self.assertIn({"path": "runs/hl", "reason": "hardlinked"}, rep1.skipped)
        self.assertIn({"path": "runs/foreign", "reason": f"owned by SID {other_sid}"}, rep1.skipped)
        self.assertEqual(set(rep1.repaired), {".", "mesh.db", "runs", "runs/r1", "runs/r1/f"})
        self.assertEqual((junc.set_count, hl.set_count, foreign.set_count, outside.set_count), (0, 0, 0, 0))
        self.assertEqual(vfs1.allocs, {})
        self.assertEqual(sorted(vfs1.closed_handles), sorted(vfs1.open_handles))

        # 2. before-open-dir hook swapping 'runs' to a reparse junction never descends or touches outside
        vfs2 = _MockNtVfs()
        home2, _, runs2, _, _, _, _, _, outside2, outside2_f = _build_tree(vfs2)
        del runs2.children["junc"], runs2.children["hl"], runs2.children["foreign"]

        def _swap_before_open_dir(stage: str, rel: str) -> None:
            if stage == "before-open-dir" and rel == "runs":
                home2.children["runs"] = _Node(155, is_dir=True, reparse=True)

        rep2 = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            hook=_swap_before_open_dir,
            k32=vfs2,
            advapi=vfs2,
            ntdll=vfs2,
            get_last_error_fn=lambda: vfs2.last_error,
        )
        self.assertIn({"path": "runs", "reason": "directory changed during the walk, not descended"}, rep2.skipped)
        self.assertIn("runs", [u["path"] for u in rep2.unsafe])
        self.assertEqual(outside2.set_count, 0)
        self.assertEqual(outside2_f.set_count, 0)
        self.assertEqual(vfs2.allocs, {})
        self.assertEqual(sorted(vfs2.closed_handles), sorted(vfs2.open_handles))

        # 3. before-verify loosening triggers report.unsafe
        vfs3 = _MockNtVfs()
        home3, _, runs3, r1_3, f3, _, _, _, _, _ = _build_tree(vfs3)
        del runs3.children["junc"], runs3.children["hl"], runs3.children["foreign"]

        def _loosen_before_verify(stage: str, rel: str) -> None:
            if stage == "before-verify" and rel == ".":
                f3.aces.append((0, 3, 0x00120089, perms.EVERYONE_SID))

        rep3 = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            hook=_loosen_before_verify,
            k32=vfs3,
            advapi=vfs3,
            ntdll=vfs3,
            get_last_error_fn=lambda: vfs3.last_error,
        )
        self.assertEqual([u["path"] for u in rep3.unsafe], ["runs/r1/f"])
        self.assertEqual(vfs3.allocs, {})
        self.assertEqual(sorted(vfs3.closed_handles), sorted(vfs3.open_handles))

        # 4. before-open-home swap & expected=None & absent home
        vfs4 = _MockNtVfs()
        _build_tree(vfs4)

        def _swap_home(stage: str, rel: str) -> None:
            if stage == "before-open-home" and rel == ".":
                vfs4.root.children["home"] = _Node(999, is_dir=True)

        rep4 = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            hook=_swap_home,
            k32=vfs4,
            advapi=vfs4,
            ntdll=vfs4,
            get_last_error_fn=lambda: vfs4.last_error,
        )
        self.assertEqual(rep4.repaired, {})
        self.assertIn("precheck", rep4.skipped[0]["reason"])
        self.assertEqual(vfs4.allocs, {})
        self.assertEqual(sorted(vfs4.closed_handles), sorted(vfs4.open_handles))

        rep_none = dpw.fix_home_permissions(
            "C:\\home",
            expected=None,
            k32=vfs4,
            advapi=vfs4,
            ntdll=vfs4,
            get_last_error_fn=lambda: vfs4.last_error,
        )
        self.assertEqual(rep_none.repaired, {})
        self.assertIn("precheck", rep_none.skipped[0]["reason"])

        rep_abs = dpw.fix_home_permissions(
            "C:\\missing_home",
            expected=None,
            k32=vfs4,
            advapi=vfs4,
            ntdll=vfs4,
            get_last_error_fn=lambda: vfs4.last_error,
        )
        self.assertEqual((rep_abs.repaired, rep_abs.skipped, rep_abs.unsafe), ({}, [], []))

        # 5. ReOpenFile returning INVALID_HANDLE_VALUE falls back to _open_home_nofollow_win cleanly;
        #    and if fallback fails on verify pass, report.unsafe marks '.' with zero handle leaks
        class _ReopenFailVfs(_MockNtVfs):
            def __init__(self) -> None:
                super().__init__()
                self.fail_reopen = True
                self.fail_get_sec_for: set[int] = set()
                self.reopen_calls = 0

            def ReOpenFile(self, orig, access, share, flags) -> int:
                self.reopen_calls += 1
                if self.fail_reopen:
                    return w32.INVALID_HANDLE_VALUE
                return super().ReOpenFile(orig, access, share, flags)

            def GetSecurityInfo(self, h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p, sd_p) -> int:
                node, _ = self.handles[h]
                if node.ino in self.fail_get_sec_for:
                    return w32.ERROR_ACCESS_DENIED
                return super().GetSecurityInfo(h, obj_type, sec_info, owner_p, grp_p, dacl_p, sacl_p, sd_p)

        vfs5a = _ReopenFailVfs()
        _build_tree(vfs5a)
        del vfs5a.root.children["home"].children["runs"].children["junc"]
        del vfs5a.root.children["home"].children["runs"].children["hl"]
        del vfs5a.root.children["home"].children["runs"].children["foreign"]
        rep5a = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            k32=vfs5a,
            advapi=vfs5a,
            ntdll=vfs5a,
            get_last_error_fn=lambda: vfs5a.last_error,
        )
        self.assertGreaterEqual(vfs5a.reopen_calls, 2)
        self.assertEqual(rep5a.unsafe, [])
        self.assertEqual(vfs5a.allocs, {})
        self.assertEqual(sorted(vfs5a.closed_handles), sorted(vfs5a.open_handles))

        vfs5b = _ReopenFailVfs()
        _build_tree(vfs5b)
        del vfs5b.root.children["home"].children["runs"].children["junc"]
        del vfs5b.root.children["home"].children["runs"].children["hl"]
        del vfs5b.root.children["home"].children["runs"].children["foreign"]
        orig_open_rel = vfs5b.NtOpenFile
        verify_fallback_count = 0

        def _drop_home_on_verify_reopen_fallback(h_out_ptr, access, oa_ptr, io_sb_ptr, share, opts):
            nonlocal verify_fallback_count
            rc_nt = orig_open_rel(h_out_ptr, access, oa_ptr, io_sb_ptr, share, opts)
            # Once repair walk succeeded and verify opened h_home (with READ_CONTROL, no FILE_LIST_DIRECTORY),
            # drop 'home' right before _open_dir_for_listing_win's ReOpenFile fallback runs.
            if (access & w32.READ_CONTROL) and not (access & w32.WRITE_DAC) and not (access & w32.FILE_LIST_DIRECTORY):
                verify_fallback_count += 1
                if verify_fallback_count == 1:
                    vfs5b.root.children.pop("home", None)
            return rc_nt

        vfs5b.NtOpenFile = _drop_home_on_verify_reopen_fallback  # type: ignore[method-assign]
        rep5b = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            k32=vfs5b,
            advapi=vfs5b,
            ntdll=vfs5b,
            get_last_error_fn=lambda: vfs5b.last_error,
        )
        self.assertIn(".", [u["path"] for u in rep5b.unsafe])
        self.assertEqual(vfs5b.allocs, {})
        self.assertEqual(sorted(vfs5b.closed_handles), sorted(vfs5b.open_handles))

        # 6. GetSecurityInfo failure during verify pass marks entry unsafe with zero handle/alloc leaks
        for target_ino, expected_unsafe in ((10, "."), (14, "runs/r1/f")):
            vfs6 = _ReopenFailVfs()
            vfs6.fail_reopen = False
            _build_tree(vfs6)
            del vfs6.root.children["home"].children["runs"].children["junc"]
            del vfs6.root.children["home"].children["runs"].children["hl"]
            del vfs6.root.children["home"].children["runs"].children["foreign"]

            def _fail_sec_before_verify(stage: str, rel: str, _v=vfs6, _ino=target_ino) -> None:
                if stage == "before-verify" and rel == ".":
                    _v.fail_get_sec_for.add(_ino)

            rep6 = dpw.fix_home_permissions(
                "C:\\home",
                expected=(0x1234, 10),
                hook=_fail_sec_before_verify,
                k32=vfs6,
                advapi=vfs6,
                ntdll=vfs6,
                get_last_error_fn=lambda: vfs6.last_error,
            )
            self.assertIn(expected_unsafe, [u["path"] for u in rep6.unsafe])
            self.assertTrue(
                any(u["path"] == expected_unsafe and "cannot re-verify" in u["reason"] for u in rep6.unsafe),
                rep6.unsafe,
            )
            self.assertEqual(vfs6.allocs, {})
            self.assertEqual(sorted(vfs6.closed_handles), sorted(vfs6.open_handles))

        # 7. Non-absolute, drive-relative, drive-less rooted, and '..' home_dir inputs fail with EINVAL
        #    without ever opening C:\foo or touching any tree
        vfs7 = _MockNtVfs()
        foo_node = _Node(201, is_dir=True)
        vfs7.root.children["foo"] = foo_node
        create_calls: list[str] = []
        orig_create = vfs7.CreateFileW

        def _track_create(path, *args):
            create_calls.append(path)
            return orig_create(path, *args)

        vfs7.CreateFileW = _track_create  # type: ignore[method-assign]
        for bad_home in (
            "C:foo",
            "C:",
            "foo\\bar",
            "\\foo",
            "/foo",
            "",
            "   ",
            "C:\\home\\..\\foo",
            "\\\\?\\C:foo",
            "\\\\?\\foo",
        ):
            with self.assertRaises(OSError, msg=bad_home) as ctx_s:
                dpw._split_win_root_and_parts(bad_home)
            self.assertEqual(ctx_s.exception.errno, errno.EINVAL)
            with self.assertRaises(OSError, msg=bad_home) as ctx_i:
                dpw.home_identity_win(bad_home, k32=vfs7, ntdll=vfs7)
            self.assertEqual(ctx_i.exception.errno, errno.EINVAL)
            with self.assertRaises(OSError, msg=bad_home) as ctx_f:
                dpw.fix_home_permissions(bad_home, expected=(0x1234, 201), k32=vfs7, advapi=vfs7, ntdll=vfs7)
            self.assertEqual(ctx_f.exception.errno, errno.EINVAL)
        self.assertEqual(create_calls, [])
        self.assertEqual(foo_node.set_count, 0)

        self.assertEqual(dpw._split_win_root_and_parts("\\\\server\\share\\home"), ("\\\\server\\share\\", ["home"]))
        self.assertEqual(dpw._split_win_root_and_parts("//server/share/home"), ("\\\\server\\share\\", ["home"]))
        self.assertEqual(dpw._split_win_root_and_parts("\\\\?\\C:\\home"), ("C:\\", ["home"]))
        self.assertEqual(
            dpw._split_win_root_and_parts("\\\\?\\UNC\\server\\share\\home"), ("\\\\server\\share\\", ["home"])
        )

        # 8. before-chmod leaf junction swap only touches moved file and reports leaf unsafe;
        #    before-verify directory junction swap reports 'runs/r1' without duplicating 'runs/r1/f'
        vfs8a = _MockNtVfs()
        _home8a, _db8a, runs8a, r1_8a, f8a, _, _, _, outside8a, outside8a_f = _build_tree(vfs8a)
        del runs8a.children["junc"], runs8a.children["hl"], runs8a.children["foreign"]

        def _swap_leaf_before_chmod(stage: str, rel: str) -> None:
            if stage == "before-chmod" and rel == "runs/r1/f":
                r1_8a.children["f"] = _Node(156, is_dir=True, reparse=True)

        rep8a = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            hook=_swap_leaf_before_chmod,
            k32=vfs8a,
            advapi=vfs8a,
            ntdll=vfs8a,
            get_last_error_fn=lambda: vfs8a.last_error,
        )
        self.assertEqual(f8a.set_count, 1)
        self.assertEqual((outside8a.set_count, outside8a_f.set_count), (0, 0))
        self.assertEqual([u["path"] for u in rep8a.unsafe], ["runs/r1/f"])
        self.assertEqual(vfs8a.allocs, {})
        self.assertEqual(sorted(vfs8a.closed_handles), sorted(vfs8a.open_handles))

        vfs8b = _MockNtVfs()
        _home8b, _db8b, runs8b, _r1_8b, _f8b, _, _, _, outside8b, outside8b_f = _build_tree(vfs8b)
        del runs8b.children["junc"], runs8b.children["hl"], runs8b.children["foreign"]

        def _swap_r1_before_verify(stage: str, rel: str) -> None:
            if stage == "before-verify" and rel == ".":
                runs8b.children["r1"] = _Node(157, is_dir=True, reparse=True)

        rep8b = dpw.fix_home_permissions(
            "C:\\home",
            expected=(0x1234, 10),
            hook=_swap_r1_before_verify,
            k32=vfs8b,
            advapi=vfs8b,
            ntdll=vfs8b,
            get_last_error_fn=lambda: vfs8b.last_error,
        )
        self.assertEqual((outside8b.set_count, outside8b_f.set_count), (0, 0))
        self.assertEqual([u["path"] for u in rep8b.unsafe], ["runs/r1"])
        self.assertEqual(vfs8b.allocs, {})
        self.assertEqual(sorted(vfs8b.closed_handles), sorted(vfs8b.open_handles))

        # 9. OSError raised by hook (e.g. WinError 5 when NTFS refuses renaming a dir with an open child handle)
        #    propagates cleanly instead of being swallowed into report.skipped, with zero handle/alloc leaks
        for stage_target, rel_target in (
            ("before-chmod", "."),
            ("before-open-dir", "runs"),
            ("before-chmod", "runs/r1/f"),
            ("before-verify", "."),
        ):
            vfs9 = _MockNtVfs()
            _home9, _db9, runs9, _r1_9, _f9, _, _, _, _, _ = _build_tree(vfs9)
            del runs9.children["junc"], runs9.children["hl"], runs9.children["foreign"]

            def _raise_perm_err(stage: str, rel: str, _st=stage_target, _rt=rel_target) -> None:
                if stage == _st and rel == _rt:
                    raise PermissionError(errno.EACCES, "simulated NTFS rename denial")

            with self.assertRaises(PermissionError):
                dpw.fix_home_permissions(
                    "C:\\home",
                    expected=(0x1234, 10),
                    hook=_raise_perm_err,
                    k32=vfs9,
                    advapi=vfs9,
                    ntdll=vfs9,
                    get_last_error_fn=lambda: vfs9.last_error,
                )
            self.assertEqual(vfs9.allocs, {})
            self.assertEqual(sorted(vfs9.closed_handles), sorted(vfs9.open_handles))

    def test_t05_cross_platform_wiring_pure_logic(self) -> None:
        from mesh.cli.commands import doctor_env, setup as setup_cmd
        from mesh.core import passphrase as pp_mod

        # 1. doctor_env accepts Windows iff Python >= 3.12
        from typing import NamedTuple

        class _FakeVersion(NamedTuple):
            major: int
            minor: int
            micro: int
            releaselevel: str = "final"
            serial: int = 0

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(doctor_env.platform, "system", return_value="Windows"), \
                    mock.patch.object(doctor_env.platform, "release", return_value="11"), \
                    mock.patch.object(doctor_env.shutil, "which", return_value=None):
                with mock.patch.object(doctor_env.sys, "version_info", _FakeVersion(3, 12, 1)):
                    checks_ok = doctor_env.environment_checks(td, False)
                self.assertTrue([x for x in checks_ok if x["name"] == "platform"][0]["ok"])
                self.assertTrue([x for x in checks_ok if x["name"] == "python"][0]["ok"])

                with mock.patch.object(doctor_env.sys, "version_info", _FakeVersion(3, 11, 9)):
                    checks_old = doctor_env.environment_checks(td, False)
                self.assertFalse([x for x in checks_old if x["name"] == "platform"][0]["ok"])
                self.assertFalse([x for x in checks_old if x["name"] == "python"][0]["ok"])

        # 2. passphrase._prompt checks CONIN$ on Windows and maps errors to NO_TTY
        opened = []

        class _DummyTty:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_open(path, mode="r"):
            opened.append(path)
            return _DummyTty()

        with mock.patch.object(pp_mod.sys, "platform", "win32"), \
                mock.patch.object(pp_mod.os, "isatty", return_value=False), \
                mock.patch("builtins.open", side_effect=_fake_open), \
                mock.patch.object(pp_mod.getpass, "getpass", return_value="secret-passphrase-123"):
            self.assertEqual(pp_mod._prompt("Prompt: "), "secret-passphrase-123")
        self.assertEqual(opened, ["CONIN$"])

        with mock.patch.object(pp_mod.sys, "platform", "win32"), \
                mock.patch.object(pp_mod.os, "isatty", return_value=False), \
                mock.patch("builtins.open", side_effect=OSError("no console")):
            with self.assertRaises(MeshError) as ctx_tty:
                pp_mod._prompt("Prompt: ")
            self.assertEqual(ctx_tty.exception.code, c.NO_TTY)

        # 3. setup._run on simulated win32 uses private --passphrase-file (no pass_fds) and cleans up
        with tempfile.TemporaryDirectory() as td:
            seen_calls = []

            def _fake_run(cmd, **kw):
                seen_calls.append((list(cmd), dict(kw)))
                idx = cmd.index("--passphrase-file")
                pp_path = cmd[idx + 1]
                self.assertTrue(os.path.isfile(pp_path))
                with open(pp_path, "r", encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "win-setup-passphrase-123")
                return SimpleNamespace(returncode=0, stdout='{"ok": true, "data": {}}', stderr="")

            def _fake_open_nofollow(path, flags, mode=0o600, label="file"):
                return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)

            with mock.patch.object(setup_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd, "temp_root", return_value=td), \
                    mock.patch.object(setup_cmd.perms, "set_private_dir_acl") as mock_dir_acl, \
                    mock.patch.object(setup_cmd.perms, "fchmod_private") as mock_fchmod, \
                    mock.patch.object(setup_cmd, "open_regular_nofollow", side_effect=_fake_open_nofollow), \
                    mock.patch.object(setup_cmd.subprocess, "run", side_effect=_fake_run):
                rc, body = setup_cmd._run(td, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(rc, 0)
            self.assertEqual(body, {"ok": True, "data": {}})
            self.assertEqual(len(seen_calls), 1)
            self.assertNotIn("pass_fds", seen_calls[0][1])
            self.assertIn("--passphrase-file", seen_calls[0][0])
            self.assertNotIn("--passphrase-fd", seen_calls[0][0])
            mock_dir_acl.assert_called_once()
            mock_fchmod.assert_called_once()
            self.assertEqual(os.listdir(td), [])

        # 4. doctor._fix_perms and _home_identity dispatch on win32 & ensure_owned_dir_writable_win
        from mesh.cli.commands import doctor as doctor_cmd
        from mesh.platform import doctor_perms_win as dpw_mod

        with mock.patch.object(doctor_cmd.sys, "platform", "win32"), \
                mock.patch.object(dpw_mod, "home_identity_win", return_value=(1, 2)) as id_mock, \
                mock.patch.object(dpw_mod, "fix_home_permissions", return_value="win_rep") as fix_mock:
            self.assertEqual(doctor_cmd._home_identity("C:\\h"), (1, 2))
            self.assertEqual(doctor_cmd._fix_perms("C:\\h", (1, 2)), "win_rep")
            id_mock.assert_called_once_with("C:\\h")
            fix_mock.assert_called_once()

        chmod_calls = []
        with mock.patch.object(perms, "is_owned_by_current_user_win", return_value=False):
            self.assertFalse(
                perms.ensure_owned_dir_writable_win("C:\\foreign", chmod_fn=lambda p, m: chmod_calls.append((p, m)))
            )
        self.assertEqual(chmod_calls, [])
        ro_st = os.stat_result((0o040555, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(perms, "is_owned_by_current_user_win", return_value=True), \
                mock.patch.object(perms, "_win_dir_has_owner_rw_acl", return_value=True):
            self.assertTrue(
                perms.ensure_owned_dir_writable_win(
                    "C:\\owned_ro",
                    st=ro_st,
                    chmod_fn=lambda p, m: chmod_calls.append((p, m)),
                )
            )
        self.assertEqual(len(chmod_calls), 1)

        rw_st = os.stat_result((0o040700, 1, 1, 1, 0, 0, 0, 0, 0, 0))
        acl_calls = []

        class _CloseOnlyK32:
            def CloseHandle(self, _h):
                return 1

        fake_k32 = _CloseOnlyK32()
        with mock.patch.object(perms, "is_owned_by_current_user_win", return_value=True), \
                mock.patch.object(perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001")), \
                mock.patch.object(perms, "_open_path_for_security_win", return_value=(0x111, True)), \
                mock.patch.object(perms, "_inspect_handle_security_win", return_value=("S-1-5-21-1-1001", [])), \
                mock.patch.object(perms, "set_private_dir_acl_win", side_effect=lambda p, m, **kw: acl_calls.append((p, m))):
            self.assertTrue(
                perms.ensure_owned_dir_writable_win(
                    "C:\\owned_dp",
                    st=rw_st,
                    advapi=object(),
                    k32=fake_k32,
                    chmod_fn=lambda p, m: chmod_calls.append((p, m)),
                )
            )
        self.assertEqual(acl_calls, [("C:\\owned_dp", 0o700)])
        acl_calls.clear()
        with mock.patch.object(perms, "is_owned_by_current_user_win", return_value=True), \
                mock.patch.object(perms, "get_current_user_and_owner_sids_win", return_value=("S-1-5-21-1-1001", "S-1-5-21-1-1001")), \
                mock.patch.object(perms, "_open_path_for_security_win", return_value=(0x111, True)), \
                mock.patch.object(perms, "_inspect_handle_security_win", return_value=("S-1-5-21-1-1001", [(0, 3, 0x001F01FF, "S-1-5-21-1-1001")])), \
                mock.patch.object(perms, "set_private_dir_acl_win", side_effect=lambda p, m, **kw: acl_calls.append((p, m))):
            self.assertFalse(
                perms.ensure_owned_dir_writable_win(
                    "C:\\owned_ok",
                    st=rw_st,
                    advapi=object(),
                    k32=fake_k32,
                    chmod_fn=lambda p, m: chmod_calls.append((p, m)),
                )
            )
        self.assertEqual(acl_calls, [])

        # 5. setup._run pre-try failure cleans up pp_dir/pp_file & transient sharing error retries
        with tempfile.TemporaryDirectory() as td:
            # 5a. Failure inside open_regular_nofollow (e.g. ValueError) removes pp_dir
            with mock.patch.object(setup_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd, "temp_root", return_value=td), \
                    mock.patch.object(setup_cmd.perms, "set_private_dir_acl"), \
                    mock.patch.object(setup_cmd, "open_regular_nofollow", side_effect=ValueError("bad handle")), \
                    mock.patch.object(setup_cmd.subprocess, "run", side_effect=AssertionError("must not spawn")):
                with self.assertRaises(ValueError):
                    setup_cmd._run(td, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(os.listdir(td), [])

            # 5b. Failure inside fchmod_private after file creation removes both pp_file and pp_dir
            def _create_then_fail(path, flags, mode=0o600, label="file"):
                return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)

            with mock.patch.object(setup_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd, "temp_root", return_value=td), \
                    mock.patch.object(setup_cmd.perms, "set_private_dir_acl"), \
                    mock.patch.object(setup_cmd, "open_regular_nofollow", side_effect=_create_then_fail), \
                    mock.patch.object(setup_cmd.perms, "fchmod_private", side_effect=OSError(errno.EACCES, "acl fail")), \
                    mock.patch.object(setup_cmd.subprocess, "run", side_effect=AssertionError("must not spawn")):
                with self.assertRaises(OSError):
                    setup_cmd._run(td, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(os.listdir(td), [])

            # 5c. Transient WinError 32 on os.unlink during cleanup retries and deletes file & dir
            unlink_attempts = 0
            real_unlink = os.unlink

            def _flaky_unlink(p: str) -> None:
                nonlocal unlink_attempts
                unlink_attempts += 1
                if unlink_attempts <= 2:
                    exc = PermissionError("sharing violation")
                    exc.winerror = 32  # type: ignore[attr-defined]
                    raise exc
                real_unlink(p)

            with mock.patch.object(setup_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd, "temp_root", return_value=td), \
                    mock.patch.object(setup_cmd.perms, "set_private_dir_acl"), \
                    mock.patch.object(setup_cmd.perms, "fchmod_private"), \
                    mock.patch.object(setup_cmd, "open_regular_nofollow", side_effect=_create_then_fail), \
                    mock.patch.object(setup_cmd.os, "unlink", side_effect=_flaky_unlink), \
                    mock.patch.object(setup_cmd.time, "sleep"), \
                    mock.patch.object(
                        setup_cmd.subprocess,
                        "run",
                        return_value=SimpleNamespace(returncode=0, stdout='{"ok": true, "data": {}}', stderr=""),
                    ):
                rc, _body = setup_cmd._run(td, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(rc, 0)
            self.assertEqual(unlink_attempts, 3)
            self.assertEqual(os.listdir(td), [])

        # 6. settings.validate_setting wraps OSError from win32 verify_private_owner_and_perms into MeshError
        from mesh.cli.commands import agent as agent_cmd
        from mesh.core import settings as settings_mod

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            ws = os.path.join(td, "ws")
            tok_dir = os.path.join(td, "tok_dir")
            tok_file = os.path.join(tok_dir, "lead.token")
            os.makedirs(home, mode=0o700)
            os.makedirs(ws, mode=0o700)
            os.makedirs(tok_dir, mode=0o700)
            pathlib.Path(tok_file).write_text("tok\n", encoding="utf-8")

            with mock.patch.object(settings_mod.sys, "platform", "win32"), \
                    mock.patch.object(
                        settings_mod.perms,
                        "verify_private_owner_and_perms",
                        side_effect=OSError(errno.ELOOP, "Refusing symlink or reparse point"),
                    ):
                with self.assertRaises(MeshError) as ctx_set:
                    settings_mod.validate_setting("workspaces.root", ws, home)
                self.assertEqual(ctx_set.exception.code, c.INVALID_SETTING)
                self.assertEqual(ctx_set.exception.exit_code, c.EXIT_USAGE)

            # 7. agent._check_out_path distinguishes OSError from foreign ownership on win32
            with mock.patch.object(agent_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd.perms, "verify_dir_not_other_writable"), \
                    mock.patch.object(perms, "is_owned_by_current_user", side_effect=OSError(errno.ENOENT, "gone")):
                with self.assertRaises(MeshError) as ctx_out_err:
                    agent_cmd._check_out_path(home, tok_file, force=False)
                self.assertEqual(ctx_out_err.exception.code, c.INVALID_ARGUMENT)
                self.assertIn("cannot be verified safely", ctx_out_err.exception.message)

            with mock.patch.object(agent_cmd.sys, "platform", "win32"), \
                    mock.patch.object(setup_cmd.perms, "verify_dir_not_other_writable"), \
                    mock.patch.object(perms, "is_owned_by_current_user", return_value=False):
                with self.assertRaises(MeshError) as ctx_out_owner:
                    agent_cmd._check_out_path(home, tok_file, force=False)
                self.assertEqual(ctx_out_owner.exception.code, c.INVALID_ARGUMENT)
                self.assertIn("owned by another user", ctx_out_owner.exception.message)


class TestWindowsNativeLocksAndPerms(unittest.TestCase):
    """Real Win32 LockFileEx and NTFS DACL tests executed on Windows CI."""

    @unittest.skipUnless(sys.platform == "win32", "Windows-only LockFileEx syscall tests")
    def test_win32_cross_process_lockfileex_and_terminate_release(self) -> None:
        from mesh.cli.commands.daemon import _meshd_lock_held

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            home = os.path.join(td, "home")
            run_dir = os.path.join(home, "run")
            os.makedirs(run_dir)
            lock_path = os.path.join(run_dir, "meshd.lock")
            repo_root = str(pathlib.Path(__file__).resolve().parents[2])
            env = dict(os.environ, PYTHONPATH=repo_root)

            self.assertFalse(locks.is_exclusive_lock_held(lock_path))
            self.assertFalse(_meshd_lock_held(home))

            fd = locks.open_lock_file(lock_path)
            try:
                self.assertFalse(os.get_inheritable(fd))
                locks.acquire_lock(fd, exclusive=True, blocking=False)
                self.assertTrue(locks.is_exclusive_lock_held(lock_path))
                self.assertTrue(_meshd_lock_held(home))

                probe_code = (
                    "import sys\n"
                    "from mesh.platform import locks\n"
                    "fd = locks.open_lock_file(sys.argv[1])\n"
                    "try:\n"
                    "    locks.acquire_lock(fd, exclusive=(sys.argv[2] == 'ex'), blocking=False)\n"
                    "    sys.exit(0)\n"
                    "except BlockingIOError:\n"
                    "    sys.exit(42)\n"
                    "finally:\n"
                    "    try:\n"
                    "        locks.release_lock(fd)\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "    import os; os.close(fd)\n"
                )
                for mode in ("ex", "sh"):
                    res = subprocess.run(
                        [sys.executable, "-c", probe_code, lock_path, mode],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(res.returncode, 42, f"mode={mode} stderr={res.stderr}")

                locks.release_lock(fd)
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
                self.assertFalse(_meshd_lock_held(home))
            finally:
                os.close(fd)

            # Shared locks permit concurrent shared holder and block exclusive writer
            with locks.FileLock(lock_path, exclusive=False, blocking=False):
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
                res_sh = subprocess.run(
                    [sys.executable, "-c", probe_code, lock_path, "sh"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(res_sh.returncode, 0, res_sh.stderr)
                res_ex = subprocess.run(
                    [sys.executable, "-c", probe_code, lock_path, "ex"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(res_ex.returncode, 42, res_ex.stderr)

            # Abrupt TerminateProcess (proc.kill()) releases LockFileEx immediately
            ready_file = os.path.join(td, "win_holder.ready")
            holder_code = (
                "import pathlib, sys, time\n"
                "from mesh.platform import locks\n"
                "fd = locks.open_lock_file(sys.argv[1])\n"
                "locks.acquire_lock(fd, exclusive=True, blocking=False)\n"
                "pathlib.Path(sys.argv[2]).write_text('1', encoding='utf-8')\n"
                "time.sleep(30)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", holder_code, lock_path, ready_file],
                env=env,
            )
            try:
                deadline = time.monotonic() + 10.0
                while not os.path.exists(ready_file) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(os.path.exists(ready_file))
                self.assertTrue(locks.is_exclusive_lock_held(lock_path))
                proc.kill()
                proc.wait(timeout=10)
                self.assertFalse(locks.is_exclusive_lock_held(lock_path))
                with locks.FileLock(lock_path, exclusive=True, blocking=False):
                    self.assertTrue(locks.is_exclusive_lock_held(lock_path))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only NTFS DACL syscall tests")
    def test_win32_ntfs_dacls_inheritance_everyone_and_junction_rejection(self) -> None:
        import _winapi

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            home = os.path.join(td, "home")
            os.makedirs(home)
            perms.set_private_dir_acl(home)
            perms.verify_private_owner_and_perms(home, is_dir=True)
            perms.verify_dir_not_other_writable(home)

            # Newly created subdirectory & file inside protected home inherit private DACL
            sub = os.path.join(home, "runs")
            os.makedirs(sub)
            perms.verify_private_owner_and_perms(sub, is_dir=True)
            child_file = os.path.join(home, "mesh.db")
            pathlib.Path(child_file).write_bytes(b"sqlite")
            perms.verify_private_owner_and_perms(child_file, is_dir=False)

            # fchmod_private works directly on os.open(O_WRONLY | O_CREAT) fd via ReOpenFile fallback
            tok_file = os.path.join(td, "agent.tok")
            fd = os.open(tok_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, b"secret-token\n")
                perms.fchmod_private(fd, 0o600)
                perms.verify_private_owner_and_perms(fd, is_dir=False)
            finally:
                os.close(fd)
            perms.verify_private_owner_and_perms(tok_file, is_dir=False)

            # grant_everyone_read_for_test triggers INSECURE_PERMISSIONS on private check,
            # while verify_dir_not_other_writable still accepts read-only Everyone.
            perms.grant_everyone_read_for_test(home)
            with self.assertRaises(MeshError) as ctx_dir:
                perms.verify_private_owner_and_perms(home, is_dir=True)
            self.assertEqual(ctx_dir.exception.code, c.INSECURE_PERMISSIONS)
            perms.verify_dir_not_other_writable(home)

            # grant_everyone_write_for_test fails verify_dir_not_other_writable too
            perms.grant_everyone_write_for_test(home)
            with self.assertRaises(MeshError) as ctx_wr:
                perms.verify_dir_not_other_writable(home)
            self.assertEqual(ctx_wr.exception.code, c.INSECURE_PERMISSIONS)

            # Re-hardening strips Everyone ACE cleanly
            perms.set_private_dir_acl(home)
            perms.verify_private_owner_and_perms(home, is_dir=True)
            perms.verify_dir_not_other_writable(home)

            perms.grant_everyone_read_for_test(tok_file)
            with self.assertRaises(MeshError) as ctx_file:
                perms.verify_private_owner_and_perms(tok_file, is_dir=False)
            self.assertEqual(ctx_file.exception.code, c.INSECURE_PERMISSIONS)
            perms.set_private_file_acl(tok_file)
            perms.verify_private_owner_and_perms(tok_file, is_dir=False)

            # NTFS junction rejected with ELOOP on ACL set/verify
            target = os.path.join(td, "junc_target")
            os.makedirs(target)
            junc = os.path.join(td, "junc_link")
            _winapi.CreateJunction(target, junc)
            for fn in (perms.set_private_dir_acl, perms.chmod_private, perms.verify_private_owner_and_perms):
                with self.assertRaises(OSError) as ctx_j:
                    fn(junc)
                self.assertEqual(ctx_j.exception.errno, errno.ELOOP)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only doctor --fix-perms junction swap tests")
    def test_win32_doctor_fix_perms_junction_swap_hook_and_hardlinks(self) -> None:
        import _winapi
        import argparse
        from mesh.cli.commands import doctor
        from mesh.core.init import init_mesh_home

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            home = os.path.join(td, "home")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(home, "win-doctor-passphrase-1234")

            runs = os.path.join(home, "runs")
            r1 = os.path.join(runs, "r1")
            os.makedirs(r1)
            leaf_f = os.path.join(r1, "f")
            pathlib.Path(leaf_f).write_text("x", encoding="utf-8")

            outside_dir = os.path.join(td, "outside_dir")
            os.makedirs(outside_dir)
            outside_file = os.path.join(outside_dir, "victim.txt")
            pathlib.Path(outside_file).write_text("victim", encoding="utf-8")
            perms.grant_everyone_read_for_test(outside_dir)
            perms.grant_everyone_read_for_test(outside_file)

            self.addCleanup(setattr, doctor, "_FIX_PERMS_HOOK", None)
            self.addCleanup(setattr, doctor, "_RACE_HOOK", None)

            # 1. _FIX_PERMS_HOOK swaps 'runs' for an NTFS junction at before-open-dir
            fired: list[int] = []
            runs_moved = os.path.join(td, "runs_moved")

            def _swap_runs(stage: str, rel: str) -> None:
                if stage == "before-open-dir" and rel == "runs" and not fired:
                    fired.append(1)
                    os.rename(runs, runs_moved)
                    _winapi.CreateJunction(outside_dir, runs)

            doctor._FIX_PERMS_HOOK = _swap_runs
            args = argparse.Namespace(home=home, fix_perms=True, check_invariants=False)
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                with self.assertRaises(MeshError) as ctx_swap:
                    doctor.handle_doctor(args)
            doctor._FIX_PERMS_HOOK = None
            self.assertEqual(fired, [1])
            self.assertEqual(ctx_swap.exception.code, c.INSECURE_PERMISSIONS)
            self.assertIn("runs", [u["path"] for u in ctx_swap.exception.details["unsafe"]])
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_dir, is_dir=True)
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_file, is_dir=False)

            os.rmdir(runs)
            os.rename(runs_moved, runs)

            # 2a. _FIX_PERMS_HOOK swaps leaf 'runs/r1/f' for an NTFS junction during before-chmod;
            #     handle-bound DACL repair touches only the moved file, never outside_dir/outside_file.
            fired_leaf: list[int] = []
            leaf_moved = os.path.join(td, "leaf_f_moved")

            def _swap_leaf(stage: str, rel: str) -> None:
                if stage == "before-chmod" and rel == "runs/r1/f" and not fired_leaf:
                    fired_leaf.append(1)
                    os.rename(leaf_f, leaf_moved)
                    _winapi.CreateJunction(outside_dir, leaf_f)

            doctor._FIX_PERMS_HOOK = _swap_leaf
            rep_leaf = doctor._fix_perms(home, doctor._home_identity(home))
            doctor._FIX_PERMS_HOOK = None
            self.assertEqual(fired_leaf, [1])
            self.assertIn("runs/r1/f", [u["path"] for u in rep_leaf.unsafe])
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_dir, is_dir=True)
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_file, is_dir=False)

            os.rmdir(leaf_f)
            os.rename(leaf_moved, leaf_f)

            # 2b. _FIX_PERMS_HOOK swaps 'runs/r1' for an NTFS junction before verify (NTFS forbids
            #     renaming an ancestor directory while a handle on child 'runs/r1/f' is open).
            fired2: list[int] = []
            r1_moved = os.path.join(td, "r1_moved")

            def _swap_r1(stage: str, rel: str) -> None:
                if stage == "before-verify" and rel == "." and not fired2:
                    fired2.append(1)
                    os.rename(r1, r1_moved)
                    _winapi.CreateJunction(outside_dir, r1)

            doctor._FIX_PERMS_HOOK = _swap_r1
            rep_r1 = doctor._fix_perms(home, doctor._home_identity(home))
            doctor._FIX_PERMS_HOOK = None
            self.assertEqual(fired2, [1])
            unsafe_paths = [u["path"] for u in rep_r1.unsafe]
            self.assertIn("runs/r1", unsafe_paths)
            self.assertNotIn("runs/r1/f", unsafe_paths)
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_dir, is_dir=True)
            with self.assertRaises(MeshError):
                perms.verify_private_owner_and_perms(outside_file, is_dir=False)

            os.rmdir(r1)
            os.rename(r1_moved, r1)

            # 3. Pre-existing junction and hardlink are skipped without touching targets
            junc = os.path.join(runs, "junc")
            _winapi.CreateJunction(outside_dir, junc)
            hl = os.path.join(runs, "hl.txt")
            os.link(outside_file, hl)
            try:
                rep_skip = doctor._fix_perms(home, doctor._home_identity(home))
                self.assertIn({"path": "runs/junc", "reason": "symlink, not followed"}, rep_skip.skipped)
                self.assertIn({"path": "runs/hl.txt", "reason": "hardlinked"}, rep_skip.skipped)
                with self.assertRaises(MeshError):
                    perms.verify_private_owner_and_perms(outside_dir, is_dir=True)
                with self.assertRaises(MeshError):
                    perms.verify_private_owner_and_perms(outside_file, is_dir=False)
            finally:
                os.rmdir(junc)
                os.unlink(hl)

            # 4. Clean repair restores private DACL on loosened home & mesh.db
            perms.grant_everyone_read_for_test(home)
            perms.grant_everyone_read_for_test(os.path.join(home, "mesh.db"))
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                res = doctor.handle_doctor(args)
            self.assertEqual(res, {"fixed": True, "home": home})
            perms.verify_private_owner_and_perms(home, is_dir=True)
            perms.verify_private_owner_and_perms(os.path.join(home, "mesh.db"), is_dir=False)
            perms.verify_private_owner_and_perms(leaf_f, is_dir=False)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only T05 wired permissions & passphrase tests")
    def test_win32_wired_perms_passphrase_setup_logfile_and_doctor_env(self) -> None:
        import _winapi
        from mesh.cli.commands import agent as agent_cmd, doctor_env, setup as setup_cmd
        from mesh.core import logfile, passphrase as pp_mod, paths as core_paths, settings
        from mesh.core.init import init_mesh_home

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            home = os.path.join(td, "home")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                init_mesh_home(home, "win-wired-passphrase-1234")

            core_paths.check_home_permissions(home)
            perms.verify_private_owner_and_perms(os.path.join(home, "mesh.db"), is_dir=False)
            perms.grant_everyone_read_for_test(home)
            with self.assertRaises(MeshError) as ctx_h:
                core_paths.check_home_permissions(home)
            self.assertEqual(ctx_h.exception.code, c.INSECURE_PERMISSIONS)
            perms.set_private_dir_acl(home, 0o700)

            # logfile.append_line works without os.fchmod and sets private ACL
            logfile.append_line(home, "win-log-entry")
            log_path = os.path.join(home, "logs", "meshd.log")
            self.assertTrue(os.path.isfile(log_path))
            perms.verify_private_owner_and_perms(log_path, is_dir=False)

            # settings.validate_setting workspaces.root uses NTFS DACLs
            ws = os.path.join(td, "ws")
            os.makedirs(ws)
            perms.set_private_dir_acl(ws, 0o700)
            self.assertEqual(settings.validate_setting("workspaces.root", ws, home), ws)
            perms.grant_everyone_read_for_test(ws)
            with self.assertRaises(MeshError) as ctx_ws:
                settings.validate_setting("workspaces.root", ws, home)
            self.assertEqual(ctx_ws.exception.code, c.INVALID_SETTING)

            # passphrase file check & junction rejection
            pp_file = os.path.join(td, "pp.txt")
            pathlib.Path(pp_file).write_text("win-secret-passphrase\n", encoding="utf-8")
            perms.set_private_file_acl(pp_file, 0o600)
            self.assertEqual(
                pp_mod.read_passphrase(SimpleNamespace(passphrase_fd=None, passphrase_file=pp_file), env={}),
                "win-secret-passphrase",
            )
            perms.grant_everyone_read_for_test(pp_file)
            with self.assertRaises(MeshError) as ctx_pp:
                pp_mod.read_passphrase(SimpleNamespace(passphrase_fd=None, passphrase_file=pp_file), env={})
            self.assertEqual(ctx_pp.exception.code, c.INSECURE_PERMISSIONS)

            # setup._check_token_destination & agent._write_out
            tok_dir = os.path.join(td, "tok_dir")
            os.makedirs(tok_dir)
            perms.set_private_dir_acl(tok_dir, 0o700)
            tok_file = os.path.join(tok_dir, "lead.token")
            setup_cmd._check_token_destination(home, tok_file)
            perms.grant_everyone_write_for_test(tok_dir)
            with self.assertRaises(MeshError) as ctx_td:
                setup_cmd._check_token_destination(home, tok_file)
            self.assertEqual(ctx_td.exception.code, c.INVALID_ARGUMENT)
            perms.set_private_dir_acl(tok_dir, 0o700)

            agent_cmd._write_out(tok_file, "mesh_at_win_test_token", force=False)
            core_paths.check_token_file_secure(tok_file, home=home)

            # Junction token file refused
            junc_tok = os.path.join(tok_dir, "junc.token")
            _winapi.CreateJunction(tok_dir, junc_tok)
            try:
                with self.assertRaises(MeshError) as ctx_jt:
                    core_paths.check_token_file_secure(junc_tok, home=home)
                self.assertEqual(ctx_jt.exception.code, c.TOKEN_FILE_INSECURE)
            finally:
                os.rmdir(junc_tok)

            # setup._run with passphrase works on Windows without pass_fds and initializes home end-to-end
            home2 = os.path.join(td, "home_setup_init")
            with mock.patch.dict(os.environ, {"MESH_TESTING": "1"}):
                rc, body = setup_cmd._run(home2, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(rc, 0, body)
            self.assertTrue(os.path.isfile(os.path.join(home2, "mesh.db")))
            core_paths.check_home_permissions(home2)

            # doctor_env accepts Windows on Python 3.12+
            checks = doctor_env.environment_checks(home, True, testing_mode=True)
            plat_chk = [x for x in checks if x["name"] == "platform"][0]
            self.assertTrue(plat_chk["ok"], plat_chk)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only R3 review fix checks")
    def test_win32_r3_review_fixes_native(self) -> None:
        import _winapi
        from mesh.cli.commands import agent as agent_cmd, setup as setup_cmd
        from mesh.core import settings
        from mesh.platform import doctor_perms_win as dpw

        with tempfile.TemporaryDirectory(dir=paths.temp_root()) as td:
            home = os.path.join(td, "home")
            os.makedirs(home)
            perms.set_private_dir_acl(home, 0o700)

            # P2: non-absolute / drive-relative paths rejected with EINVAL on real Windows
            for bad in ("C:rel_mesh", "rel\\mesh", "\\rooted_mesh"):
                with self.assertRaises(OSError, msg=bad) as ctx_i:
                    dpw.home_identity_win(bad)
                self.assertEqual(ctx_i.exception.errno, errno.EINVAL)
                with self.assertRaises(OSError, msg=bad) as ctx_f:
                    dpw.fix_home_permissions(bad, expected=(1, 1))
                self.assertEqual(ctx_f.exception.errno, errno.EINVAL)

            # P3 #1: settings.validate_setting wraps OSError (e.g. junction swap) as MeshError(INVALID_SETTING)
            ws_target = os.path.join(td, "ws_target")
            os.makedirs(ws_target)
            perms.set_private_dir_acl(ws_target, 0o700)
            ws_junc = os.path.join(td, "ws_junc")
            _winapi.CreateJunction(ws_target, ws_junc)
            try:
                with mock.patch.object(settings, "is_reparse_link", return_value=False):
                    with self.assertRaises(MeshError) as ctx_ws:
                        settings.validate_setting("workspaces.root", ws_junc, home)
                self.assertEqual(ctx_ws.exception.code, c.INVALID_SETTING)
                self.assertEqual(ctx_ws.exception.exit_code, c.EXIT_USAGE)
            finally:
                os.rmdir(ws_junc)

            # P3 #2: setup._run cleans up mesh-pp-* directory on pre-subprocess failure
            pp_dir_parent = os.path.join(td, "pp_tmp")
            os.makedirs(pp_dir_parent)
            with mock.patch.object(setup_cmd, "temp_root", return_value=pp_dir_parent), \
                    mock.patch.object(setup_cmd.perms, "fchmod_private", side_effect=OSError(errno.EACCES, "fail")):
                with self.assertRaises(OSError):
                    setup_cmd._run(home, ["init"], passphrase="win-setup-passphrase-123")
            self.assertEqual(os.listdir(pp_dir_parent), [])

            # P3 #11: agent._check_out_path distinguishes OSError from foreign owner
            tok_dir = os.path.join(td, "tok_dir")
            os.makedirs(tok_dir)
            perms.set_private_dir_acl(tok_dir, 0o700)
            tok_file = os.path.join(tok_dir, "a.token")
            pathlib.Path(tok_file).write_text("tok\n", encoding="utf-8")
            with mock.patch.object(perms, "is_owned_by_current_user", side_effect=OSError(errno.ENOENT, "gone")):
                with self.assertRaises(MeshError) as ctx_out:
                    agent_cmd._check_out_path(home, tok_file, force=False)
            self.assertEqual(ctx_out.exception.code, c.INVALID_ARGUMENT)
            self.assertIn("cannot be verified safely", ctx_out.exception.message)


if __name__ == "__main__":
    unittest.main()
