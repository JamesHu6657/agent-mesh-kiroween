"""Unit tests for Phase 4 overlapped Named Pipe pump, adapter resolution, and .cmd shims."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHIMS_DIR = os.path.join(REPO_ROOT, "tests", "shims")
VENDOR_SHIM_NAMES = (
    "agy",
    "claude",
    "codex",
    "devin",
    "droid",
    "hermes",
    "kiro-cli",
    "openclaw",
    "vibe",
)


class _FakePipeScriptItem:
    """One scripted Win32 ReadFile response for `_FakeWinPipeKernel`."""

    def __init__(
        self,
        *,
        sync: bool,
        data: bytes = b"",
        err: int = 0,
        complete_on_wait: Optional[int] = None,
        raced_on_cancel: bytes = b"",
    ) -> None:
        self.sync = sync
        self.data = data
        self.err = err
        self.complete_on_wait = complete_on_wait
        self.raced_on_cancel = raced_on_cancel


class _FakeWinPipeKernel:
    """Scriptable mock of kernel32 + msvcrt pipe/overlapped/wait calls for Linux & Windows."""

    def __init__(self) -> None:
        self.last_error = 0
        self._next_handle = 0x2000
        self.open_handles: Dict[int, str] = {}
        self.closed_handles: List[int] = []
        self.events: Dict[int, bool] = {}  # handle -> signaled
        self.pipes: Dict[int, Dict[str, Any]] = {}
        self.pipe_scripts: Dict[int, List[_FakePipeScriptItem]] = {}
        self.pending_ops: Dict[int, _FakePipeScriptItem] = {}  # ov_addr -> item
        self.pipe_to_event: Dict[int, int] = {}
        self.proc_handles: Dict[int, bool] = {}  # handle -> signaled
        self.cancelled_pipes: List[int] = []
        self.created_pipe_names: List[str] = []
        self.connect_pending_ovs: Dict[int, int] = {}
        self.op_log: List[Tuple[str, int]] = []
        self.get_overlapped_result_exc: Optional[BaseException] = None

        # Failure injection hooks for create_pipe_pair tests
        self.fail_create_pipe_errors: List[int] = []
        self.connect_error: Optional[int] = None
        self.connect_pending: bool = True
        self.connect_overlapped_error: Optional[int] = None
        self.create_file_error: Optional[int] = None
        self.create_event_fail_at: Optional[int] = None
        self.open_osfhandle_error: Optional[int] = None
        self._create_event_calls = 0

        # msvcrt mapping: fd -> real os pipe fd or dummy
        self._osf_real_fds: List[Tuple[int, int]] = []  # (r_fd, w_fd)

    def _alloc(self, kind: str) -> int:
        self._next_handle += 4
        h = self._next_handle
        self.open_handles[h] = kind
        return h

    def get_last_error(self) -> int:
        return self.last_error

    def set_last_error(self, val: int) -> None:
        self.last_error = int(val)

    def _event_of_ov(self, ov_ptr: Any) -> Optional[int]:
        ov_obj = getattr(ov_ptr, "_obj", ov_ptr)
        h_ev = getattr(ov_obj, "hEvent", None)
        return int(h_ev) if h_ev else None

    def CreateNamedPipeW(
        self,
        name: str,
        open_mode: int,
        pipe_mode: int,
        max_instances: int,
        out_buf: int,
        in_buf: int,
        default_timeout: int,
        sa_ptr: Any,
    ) -> int:
        self.created_pipe_names.append(name)
        if self.fail_create_pipe_errors:
            self.last_error = self.fail_create_pipe_errors.pop(0)
            return -1  # INVALID_HANDLE_VALUE
        h = self._alloc("pipe_server")
        self.pipes[h] = {
            "name": name,
            "open_mode": open_mode,
            "pipe_mode": pipe_mode,
            "max_instances": max_instances,
            "out_buf": out_buf,
            "in_buf": in_buf,
            "sa_ptr": sa_ptr,
        }
        return h

    def CreateEventW(self, sa: Any, manual_reset: bool, initial_state: bool, name: Any) -> int:
        self._create_event_calls += 1
        if self.create_event_fail_at is not None and self._create_event_calls == self.create_event_fail_at:
            self.last_error = 8  # ERROR_NOT_ENOUGH_MEMORY
            return 0
        h = self._alloc("event")
        self.events[h] = bool(initial_state)
        return h

    def ConnectNamedPipe(self, h_pipe: int, ov_ptr: Any) -> int:
        if self.connect_error is not None:
            self.last_error = self.connect_error
            return 0
        if self.connect_pending:
            self.connect_pending_ovs[self._ov_key(ov_ptr)] = h_pipe
            self.last_error = 997  # ERROR_IO_PENDING
            return 0
        h_ev = self._event_of_ov(ov_ptr)
        if h_ev in self.events:
            self.events[h_ev] = True
        return 1

    def CreateFileW(
        self,
        name: str,
        desired_access: int,
        share_mode: int,
        sa_ptr: Any,
        creation_disposition: int,
        flags_and_attrs: int,
        template: Any,
    ) -> int:
        if self.create_file_error is not None:
            self.last_error = self.create_file_error
            return -1
        return self._alloc("pipe_client")

    def SetEvent(self, h_ev: int) -> int:
        if h_ev in self.events:
            self.events[h_ev] = True
            return 1
        self.last_error = 6  # ERROR_INVALID_HANDLE
        return 0

    def ResetEvent(self, h_ev: int) -> int:
        if h_ev in self.events:
            self.events[h_ev] = False
            return 1
        self.last_error = 6
        return 0

    def CloseHandle(self, h: int) -> int:
        h_int = int(h)
        self.op_log.append(("CloseHandle", h_int))
        if h_int in self.open_handles:
            self.open_handles.pop(h_int, None)
            self.closed_handles.append(h_int)
            return 1
        return 0

    def open_osfhandle(self, h: int, flags: int) -> int:
        if self.open_osfhandle_error is not None:
            raise self.open_osfhandle_error
        r_fd, w_fd = os.pipe()
        os.close(r_fd)
        self._osf_real_fds.append((r_fd, w_fd))
        self.open_handles.pop(int(h), None)
        return w_fd

    def _ov_key(self, ov_ptr: Any) -> int:
        ov_obj = getattr(ov_ptr, "_obj", ov_ptr)
        return id(ov_obj)

    def ReadFile(self, h_pipe: int, buf: Any, n_bytes: int, nread_ptr: Any, ov_ptr: Any) -> int:
        import ctypes

        nread_obj = getattr(nread_ptr, "_obj", nread_ptr)
        ov_obj = getattr(ov_ptr, "_obj", ov_ptr)
        key = id(ov_obj)
        h_ev = self._event_of_ov(ov_ptr)
        if h_ev is not None:
            self.pipe_to_event[h_pipe] = h_ev

        script = self.pipe_scripts.get(h_pipe, [])
        if not script:
            self.last_error = 109  # ERROR_BROKEN_PIPE
            return 0
        item = script.pop(0)
        if item.sync:
            if item.err:
                self.last_error = item.err
                return 0
            chunk = item.data[:n_bytes]
            ctypes.memmove(buf, chunk, len(chunk))
            nread_obj.value = len(chunk)
            if h_ev in self.events:
                self.events[h_ev] = True
            return 1

        # Pending overlapped IRP
        item._buf = buf
        item._max = n_bytes
        item._h_pipe = h_pipe
        self.pending_ops[key] = item
        if item.complete_on_wait == 0 and h_ev in self.events:
            self.events[h_ev] = True
        elif item.complete_on_wait is not None and item.complete_on_wait > 0:
            item.complete_on_wait -= 1
        self.last_error = 997  # ERROR_IO_PENDING
        return 0

    def CancelIoEx(self, h_pipe: int, ov_ptr: Any) -> int:
        self.op_log.append(("CancelIoEx", int(h_pipe)))
        self.cancelled_pipes.append(h_pipe)
        if ov_ptr is None:
            return 1
        key = self._ov_key(ov_ptr)
        if key not in self.pending_ops and key not in self.connect_pending_ovs:
            self.last_error = 1168  # ERROR_NOT_FOUND
            return 0
        return 1

    def GetOverlappedResult(self, h_pipe: int, ov_ptr: Any, nbytes_ptr: Any, wait: bool) -> int:
        import ctypes

        self.op_log.append(("GetOverlappedResult", int(h_pipe)))
        if self.get_overlapped_result_exc is not None:
            exc = self.get_overlapped_result_exc
            self.get_overlapped_result_exc = None
            raise exc

        nbytes_obj = getattr(nbytes_ptr, "_obj", nbytes_ptr)
        # Check if this is ConnectNamedPipe's overlapped struct
        ov_obj = getattr(ov_ptr, "_obj", ov_ptr)
        key = id(ov_obj)
        if key not in self.pending_ops:
            if key in self.connect_pending_ovs and (wait or h_pipe in self.cancelled_pipes):
                self.connect_pending_ovs.pop(key, None)
                if h_pipe in self.cancelled_pipes:
                    self.last_error = 995  # ERROR_OPERATION_ABORTED
                    return 0
            if self.connect_overlapped_error is not None:
                self.last_error = self.connect_overlapped_error
                return 0
            nbytes_obj.value = 0
            return 1

        item = self.pending_ops[key]
        if item.raced_on_cancel:
            self.pending_ops.pop(key, None)
            chunk = item.raced_on_cancel[: item._max]
            ctypes.memmove(item._buf, chunk, len(chunk))
            nbytes_obj.value = len(chunk)
            return 1
        if h_pipe in self.cancelled_pipes:
            self.pending_ops.pop(key, None)
            self.last_error = 995  # ERROR_OPERATION_ABORTED
            return 0
        if not wait and item.complete_on_wait and item.complete_on_wait > 0:
            self.last_error = 996  # ERROR_IO_INCOMPLETE
            return 0
        self.pending_ops.pop(key, None)
        if item.err:
            self.last_error = item.err
            return 0
        chunk = item.data[: item._max]
        ctypes.memmove(item._buf, chunk, len(chunk))
        nbytes_obj.value = len(chunk)
        return 1

    def WaitForMultipleObjects(self, count: int, handles_arr: Any, wait_all: bool, timeout_ms: int) -> int:
        handles = [int(handles_arr[i]) for i in range(count)]
        # Advance deferred pending pipe completions on poll/wait
        for item in list(self.pending_ops.values()):
            h_pipe = getattr(item, "_h_pipe", None)
            h_ev = self.pipe_to_event.get(h_pipe) if h_pipe is not None else None
            if item.complete_on_wait is not None:
                if item.complete_on_wait == 0 and h_ev in self.events:
                    self.events[h_ev] = True
                elif item.complete_on_wait > 0:
                    item.complete_on_wait -= 1

        for idx, h in enumerate(handles):
            if self.events.get(h) or self.proc_handles.get(h):
                return idx  # WAIT_OBJECT_0 + idx
        if timeout_ms != 0xFFFFFFFF:
            return 258  # WAIT_TIMEOUT
        # If no event is immediately ready on INFINITE wait in mock, progress one pending op
        for item in list(self.pending_ops.values()):
            h_pipe = getattr(item, "_h_pipe", None)
            h_ev = self.pipe_to_event.get(h_pipe) if h_pipe is not None else None
            if h_ev in self.events:
                self.events[h_ev] = True
                if h_ev in handles:
                    return handles.index(h_ev)
        return 0xFFFFFFFF  # WAIT_FAILED

    # Stubs needed by _win32._private_security_attributes under mock
    def LocalFree(self, ptr: Any) -> int:
        return 0

    def GetCurrentProcess(self) -> int:
        return 1

    def GetCurrentProcessId(self) -> int:
        return os.getpid()


class _FakeAdvapi:
    def ConvertStringSecurityDescriptorToSecurityDescriptorW(
        self, sddl: str, rev: int, sd_out: Any, size_out: Any
    ) -> int:
        return 1


def _fake_priv_sa(*args: Any, **kwargs: Any) -> Any:
    import contextlib
    import ctypes

    @contextlib.contextmanager
    def _cm() -> Any:
        yield ctypes.c_void_p(0x1234)

    return _cm()


class TestPlatformImportHygiene(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Import-isolation check targets Linux")
    def test_importing_platform_and_phase4_modules_skips_win32_on_linux(self) -> None:
        code = (
            "import sys\n"
            "import mesh.platform\n"
            "import mesh.platform.pipe_pump\n"
            "import mesh.runwrap\n"
            "import mesh.adapters.base\n"
            "import mesh.adapters.vendor_stream\n"
            "import mesh.testing.fake_agent\n"
            "forbidden = [m for m in ('msvcrt', '_winapi', 'nt', 'mesh.platform._win32', 'mesh.platform._win32_ctypes') if m in sys.modules]\n"
            "assert not forbidden, f'Win32 modules loaded on Linux: {forbidden}'\n"
        )
        res = subprocess.run(
            [sys.executable, "-P", "-s", "-c", code],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": REPO_ROOT},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)


class TestOverlappedPipeCreation(unittest.TestCase):
    def test_create_pipe_pair_success_flags_and_cleanup(self) -> None:
        from mesh.platform import _win32, _win32_ctypes as w32, pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            reader, wfd = pipe_pump.create_pipe_pair(
                buf_size=32768,
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
            )
        try:
            self.assertGreaterEqual(wfd, 0)
            pinfo = fake_k32.pipes[reader.fileno()]
            expected_open = (
                w32.PIPE_ACCESS_INBOUND
                | w32.FILE_FLAG_OVERLAPPED
                | w32.FILE_FLAG_FIRST_PIPE_INSTANCE
            )
            expected_mode = (
                w32.PIPE_TYPE_BYTE
                | w32.PIPE_READMODE_BYTE
                | w32.PIPE_WAIT
                | w32.PIPE_REJECT_REMOTE_CLIENTS
            )
            self.assertEqual(pinfo["open_mode"], expected_open)
            self.assertEqual(pinfo["pipe_mode"], expected_mode)
            self.assertEqual(pinfo["max_instances"], 1)
            self.assertEqual(pinfo["in_buf"], 32768)
            self.assertEqual(pinfo["out_buf"], 32768)
            # connect_ev must already be closed; only pipe_server + read_event remain open
            self.assertEqual(sorted(fake_k32.open_handles.values()), ["event", "pipe_server"])
        finally:
            try:
                os.close(wfd)
            except OSError:
                pass
            reader.close()
        self.assertEqual(fake_k32.open_handles, {})

    def test_create_pipe_pair_retries_on_collision_and_fails_closed(self) -> None:
        from mesh.platform import _win32, _win32_ctypes as w32, pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        fake_k32.fail_create_pipe_errors = [w32.ERROR_ACCESS_DENIED, w32.ERROR_PIPE_BUSY]
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            reader, wfd = pipe_pump.create_pipe_pair(
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
            )
        try:
            self.assertEqual(len(fake_k32.created_pipe_names), 3)
            self.assertEqual(len(set(fake_k32.created_pipe_names)), 3)
        finally:
            os.close(wfd)
            reader.close()

        exhaust_k32 = _FakeWinPipeKernel()
        exhaust_k32.fail_create_pipe_errors = [w32.ERROR_ACCESS_DENIED] * 20
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            with self.assertRaises(OSError):
                pipe_pump.create_pipe_pair(
                    k32=exhaust_k32,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=exhaust_k32,
                    get_last_error_fn=exhaust_k32.get_last_error,
                    set_last_error_fn=exhaust_k32.set_last_error,
                )
        self.assertEqual(exhaust_k32.open_handles, {})

    def test_create_pipe_pair_closes_all_handles_on_every_error_path(self) -> None:
        from mesh.platform import _win32, pipe_pump

        scenarios = [
            {"connect_error": 5},
            {"create_file_error": 2},
            {"connect_overlapped_error": 233},
            {"create_event_fail_at": 2},
            {"open_osfhandle_error": OSError(24, "too many open files")},
        ]
        for sc in scenarios:
            with self.subTest(scenario=sc):
                fake_k32 = _FakeWinPipeKernel()
                for k, v in sc.items():
                    setattr(fake_k32, k, v)
                with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
                    with self.assertRaises(OSError):
                        pipe_pump.create_pipe_pair(
                            k32=fake_k32,
                            advapi=_FakeAdvapi(),
                            msvcrt_mod=fake_k32,
                            get_last_error_fn=fake_k32.get_last_error,
                            set_last_error_fn=fake_k32.set_last_error,
                        )
                self.assertEqual(fake_k32.open_handles, {}, f"Handle leak in {sc}: {fake_k32.open_handles}")

    def test_create_pipe_pair_cancels_and_retires_pending_connect_ov_before_closing(self) -> None:
        from mesh.platform import _win32, pipe_pump

        # CreateFileW fails after ConnectNamedPipe returned ERROR_IO_PENDING: connect IRP
        # must be cancelled and waited to completion before h_read / connect_ev are closed.
        fake_k32 = _FakeWinPipeKernel()
        fake_k32.create_file_error = 2
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            with self.assertRaises(OSError):
                pipe_pump.create_pipe_pair(
                    k32=fake_k32,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=fake_k32,
                    get_last_error_fn=fake_k32.get_last_error,
                    set_last_error_fn=fake_k32.set_last_error,
                )
        self.assertEqual(fake_k32.connect_pending_ovs, {})
        self.assertEqual(fake_k32.open_handles, {})
        h_pipe = next(iter(fake_k32.pipes))
        cancel_idx = fake_k32.op_log.index(("CancelIoEx", h_pipe))
        retire_idx = fake_k32.op_log.index(("GetOverlappedResult", h_pipe))
        close_pipe_idx = fake_k32.op_log.index(("CloseHandle", h_pipe))
        self.assertLess(cancel_idx, retire_idx)
        self.assertLess(retire_idx, close_pipe_idx)

        # Unexpected exception from GetOverlappedResult while connect_pending is still True
        fake_k32_exc = _FakeWinPipeKernel()
        fake_k32_exc.get_overlapped_result_exc = RuntimeError("wait aborted")
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            with self.assertRaises(RuntimeError):
                pipe_pump.create_pipe_pair(
                    k32=fake_k32_exc,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=fake_k32_exc,
                    get_last_error_fn=fake_k32_exc.get_last_error,
                    set_last_error_fn=fake_k32_exc.set_last_error,
                )
        self.assertEqual(fake_k32_exc.connect_pending_ovs, {})
        self.assertEqual(fake_k32_exc.open_handles, {})
        h_pipe2 = next(iter(fake_k32_exc.pipes))
        self.assertLess(
            fake_k32_exc.op_log.index(("CancelIoEx", h_pipe2)),
            fake_k32_exc.op_log.index(("CloseHandle", h_pipe2)),
        )


class TestOverlappedPumpMocked(unittest.TestCase):
    def test_pump_drains_sync_and_async_chunks_to_broken_pipe_eof(self) -> None:
        from mesh.platform import _win32, _win32_ctypes as w32, pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            reader, wfd = pipe_pump.create_pipe_pair(
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
            )
        os.close(wfd)
        fake_k32.pipe_scripts[reader.fileno()] = [
            _FakePipeScriptItem(sync=True, data=b"sync-1 "),
            _FakePipeScriptItem(sync=False, data=b"async-2 ", complete_on_wait=0),
            _FakePipeScriptItem(sync=True, data=b"sync-3"),
            _FakePipeScriptItem(sync=False, err=w32.ERROR_BROKEN_PIPE, complete_on_wait=0),
        ]
        got: List[bytes] = []
        pipe_pump.pump_overlapped_stream_win(reader, got.append, k32=fake_k32)
        self.assertEqual(b"".join(got), b"sync-1 async-2 sync-3")
        self.assertTrue(reader.closed)
        self.assertEqual(fake_k32.open_handles, {})

    def test_pump_wake_deadline_cancels_pending_irp_and_collects_raced_bytes(self) -> None:
        from mesh.platform import _win32, pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            reader, wfd = pipe_pump.create_pipe_pair(
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
            )
        os.close(wfd)
        wake_h = fake_k32.CreateEventW(None, True, False, None)

        # Stuck pending read that emits raced trailing bytes right as CancelIoEx runs.
        fake_k32.pipe_scripts[reader.fileno()] = [
            _FakePipeScriptItem(sync=True, data=b"head:"),
            _FakePipeScriptItem(sync=False, complete_on_wait=99, raced_on_cancel=b"tail!"),
        ]
        # Advance clock past deadline once wake triggers; wake is already signaled on first check.
        times = iter([0.0, 0.0, 0.0, 0.5, 2.0, 2.0, 2.0])
        deadline: List[Optional[float]] = [1.0]
        fake_k32.SetEvent(wake_h)
        h_pipe = reader.fileno()

        got: List[bytes] = []
        pipe_pump.pump_overlapped_stream_win(
            reader,
            got.append,
            drain_wake_event=wake_h,
            drain_deadline=deadline,
            k32=fake_k32,
            monotonic_fn=lambda: next(times, 5.0),
        )
        self.assertEqual(b"".join(got), b"head:tail!")
        self.assertIn(h_pipe, fake_k32.cancelled_pipes)
        self.assertEqual(fake_k32.pending_ops, {})
        fake_k32.CloseHandle(wake_h)
        self.assertEqual(fake_k32.open_handles, {})

    def test_reader_close_idempotent_with_pending_irp(self) -> None:
        from mesh.platform import pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        h_pipe = fake_k32._alloc("pipe_server")
        h_ev = fake_k32.CreateEventW(None, True, False, None)
        reader = pipe_pump.OverlappedPipeReader(
            h_pipe, h_ev, k32=fake_k32, get_last_error_fn=fake_k32.get_last_error
        )
        fake_k32.pipe_scripts[h_pipe] = [_FakePipeScriptItem(sync=False, complete_on_wait=10)]
        status, _ = reader.start_read()
        self.assertEqual(status, "pending")
        self.assertTrue(reader.pending)
        reader.close()
        reader.close()
        self.assertTrue(reader.closed)
        self.assertIn(h_pipe, fake_k32.cancelled_pipes)
        self.assertEqual(fake_k32.pending_ops, {})
        self.assertEqual(fake_k32.open_handles, {})


class _FakeJob:
    def __init__(self) -> None:
        self.terminated: List[int] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, pid: int, fake_k32: _FakeWinPipeKernel, poll_seq: List[Optional[int]], use_handle: bool = True) -> None:
        self.pid = pid
        self._k32 = fake_k32
        self._poll_seq = poll_seq
        self.returncode: Optional[int] = None
        self._mesh_job = _FakeJob()
        self._handle = fake_k32._alloc("proc") if use_handle else None
        self.killed = False

    def poll(self) -> Optional[int]:
        if self._poll_seq:
            val = self._poll_seq.pop(0)
            if val is not None:
                self.returncode = val
                if self._handle is not None:
                    self._k32.proc_handles[self._handle] = True
            return val
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


class TestRunBoundedGroupWinMocked(unittest.TestCase):
    def test_round_robin_output_capping_and_cleanup(self) -> None:
        from mesh.daemon import children
        from mesh.platform import _win32, pipe_pump, proc as plat_proc

        fake_k32 = _FakeWinPipeKernel()
        fake_proc: Optional[_FakeProc] = None

        def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
            nonlocal fake_proc
            # Verify pipe handles already created and wrapped args passed cleanly
            pipe_handles = sorted(h for h, k in fake_k32.open_handles.items() if k == "pipe_server")
            out_h, err_h = pipe_handles[0], pipe_handles[1]
            fake_k32.pipe_scripts[out_h] = [
                _FakePipeScriptItem(sync=True, data=b"OUT1-"),
                _FakePipeScriptItem(sync=False, data=b"OUT2-OVERFLOW", complete_on_wait=0),
            ]
            fake_k32.pipe_scripts[err_h] = [
                _FakePipeScriptItem(sync=False, data=b"ERR1!", complete_on_wait=0),
            ]
            fake_proc = _FakeProc(4321, fake_k32, [None, None, None, 0])
            return fake_proc

        unreg_pids: List[int] = []
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa), \
                mock.patch.object(children, "unregister", side_effect=unreg_pids.append), \
                mock.patch.object(plat_proc, "terminate_job", side_effect=lambda j, c=137: j.terminated.append(c)):
            out_b, err_b, rc = pipe_pump.run_bounded_group_win(
                [r"C:\tool\agent.exe", "--help"],
                timeout_s=5.0,
                max_output_bytes=8,
                env={},
                kill_drain_s=0.5,
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                popen_reg_fn=fake_spawn,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
            )
        self.assertEqual(out_b, b"OUT1-OUT")
        self.assertEqual(err_b, b"ERR1!")
        self.assertEqual(rc, 0)
        self.assertEqual(unreg_pids, [4321])
        self.assertIsNotNone(fake_proc)
        assert fake_proc is not None
        fake_k32.CloseHandle(fake_proc._handle)
        self.assertEqual(fake_k32.open_handles, {})

    def test_timeout_kills_job_and_cancels_pending_readers(self) -> None:
        from mesh.daemon import children
        from mesh.platform import _win32, pipe_pump, proc as plat_proc

        fake_k32 = _FakeWinPipeKernel()
        fake_proc: Optional[_FakeProc] = None

        def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
            nonlocal fake_proc
            pipe_handles = sorted(h for h, k in fake_k32.open_handles.items() if k == "pipe_server")
            out_h, err_h = pipe_handles[0], pipe_handles[1]
            fake_k32.pipe_scripts[out_h] = [_FakePipeScriptItem(sync=False, complete_on_wait=99)]
            fake_k32.pipe_scripts[err_h] = [_FakePipeScriptItem(sync=False, complete_on_wait=99)]
            fake_proc = _FakeProc(5555, fake_k32, [None] * 20)
            return fake_proc

        ticks = iter([0.0, 0.0, 0.1, 0.2, 5.0, 5.0, 5.0, 6.5, 6.5])
        unreg_pids: List[int] = []
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa), \
                mock.patch.object(children, "unregister", side_effect=unreg_pids.append), \
                mock.patch.object(plat_proc, "terminate_job", side_effect=lambda j, c=137: j.terminated.append(c)):
            with self.assertRaises(subprocess.TimeoutExpired):
                pipe_pump.run_bounded_group_win(
                    [r"C:\tool\hang.exe", "--version"],
                    timeout_s=1.0,
                    max_output_bytes=1024,
                    env={},
                    kill_drain_s=0.5,
                    k32=fake_k32,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=fake_k32,
                    popen_reg_fn=fake_spawn,
                    get_last_error_fn=fake_k32.get_last_error,
                    set_last_error_fn=fake_k32.set_last_error,
                    monotonic_fn=lambda: next(ticks, 10.0),
                )
        self.assertEqual(unreg_pids, [5555])
        assert fake_proc is not None
        self.assertEqual(fake_proc._mesh_job.terminated, [137, 137])
        self.assertTrue(fake_proc.killed)
        fake_k32.CloseHandle(fake_proc._handle)
        self.assertEqual(fake_k32.pending_ops, {})
        self.assertEqual(fake_k32.open_handles, {})

    def test_spawned_then_failed_cleans_up_everything(self) -> None:
        from mesh.daemon import children
        from mesh.platform import _win32, pipe_pump, proc as plat_proc

        fake_k32 = _FakeWinPipeKernel()
        failed_proc = _FakeProc(7777, fake_k32, [None])
        unheld: List[int] = []

        def fake_spawn(*args: Any, **kwargs: Any) -> Any:
            raise children.SpawnedThenFailed(failed_proc, OSError("assign failed"))

        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa), \
                mock.patch.object(children, "unhold", side_effect=unheld.append), \
                mock.patch.object(plat_proc, "terminate_job", side_effect=lambda j, c=137: j.terminated.append(c)):
            with self.assertRaises(children.SpawnedThenFailed):
                pipe_pump.run_bounded_group_win(
                    [r"C:\tool\x.exe"],
                    timeout_s=2.0,
                    max_output_bytes=1024,
                    env={},
                    k32=fake_k32,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=fake_k32,
                    popen_reg_fn=fake_spawn,
                    get_last_error_fn=fake_k32.get_last_error,
                    set_last_error_fn=fake_k32.set_last_error,
                )
        self.assertEqual(unheld, [7777])
        self.assertEqual(failed_proc._mesh_job.terminated, [137])
        fake_k32.CloseHandle(failed_proc._handle)
        self.assertEqual(fake_k32.open_handles, {})

    def test_escaped_holder_and_early_eof_without_handle(self) -> None:
        from mesh.daemon import children
        from mesh.platform import _win32, pipe_pump, proc as plat_proc

        fake_k32 = _FakeWinPipeKernel()

        def fake_spawn(*args: Any, **kwargs: Any) -> _FakeProc:
            pipe_handles = sorted(h for h, k in fake_k32.open_handles.items() if k == "pipe_server")
            out_h, err_h = pipe_handles[0], pipe_handles[1]
            # stdout EOFs immediately; stderr stays stuck open in escaped descendant
            fake_k32.pipe_scripts[out_h] = [_FakePipeScriptItem(sync=True, data=b"done\n")]
            fake_k32.pipe_scripts[err_h] = [_FakePipeScriptItem(sync=False, complete_on_wait=99)]
            return _FakeProc(8888, fake_k32, [None, 0], use_handle=False)

        ticks = iter([0.0, 0.0, 0.01, 0.02, 0.5, 0.5, 2.0])
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa), \
                mock.patch.object(children, "unregister"), \
                mock.patch.object(plat_proc, "terminate_job", side_effect=lambda j, c=137: j.terminated.append(c)):
            out_b, err_b, rc = pipe_pump.run_bounded_group_win(
                [r"C:\tool\escapee.exe"],
                timeout_s=5.0,
                max_output_bytes=1024,
                env={},
                kill_drain_s=0.5,
                k32=fake_k32,
                advapi=_FakeAdvapi(),
                msvcrt_mod=fake_k32,
                popen_reg_fn=fake_spawn,
                get_last_error_fn=fake_k32.get_last_error,
                set_last_error_fn=fake_k32.set_last_error,
                monotonic_fn=lambda: next(ticks, 5.0),
            )
        self.assertEqual(out_b, b"done\n")
        self.assertEqual(err_b, b"")
        self.assertEqual(rc, 0)
        self.assertEqual(fake_k32.open_handles, {})

    def test_run_bounded_group_win_rejects_unsafe_cmd_arg_without_leaking_pipes(self) -> None:
        from mesh.platform import _win32, pipe_pump

        fake_k32 = _FakeWinPipeKernel()
        with mock.patch.object(_win32, "_private_security_attributes", _fake_priv_sa):
            with self.assertRaises(ValueError):
                pipe_pump.run_bounded_group_win(
                    [r"C:\tool\agent.cmd", "--pattern=%MESH_TOKEN%"],
                    timeout_s=2.0,
                    max_output_bytes=1024,
                    env={},
                    k32=fake_k32,
                    advapi=_FakeAdvapi(),
                    msvcrt_mod=fake_k32,
                    popen_reg_fn=lambda *a, **kw: self.fail("spawn must not be called"),
                    get_last_error_fn=fake_k32.get_last_error,
                    set_last_error_fn=fake_k32.set_last_error,
                )
        self.assertEqual(fake_k32.open_handles, {})


class TestWinCmdArgsAndShims(unittest.TestCase):
    def test_prepare_win_cmd_args_quoting_and_exe_passthrough(self) -> None:
        from mesh.platform.pipe_pump import _verify_win_cmd_target_exists, prepare_win_cmd_args

        self.assertEqual(
            prepare_win_cmd_args([r"C:\Program Files\droid.exe", "--pattern=%MESH_TOKEN%", 'a"b', "x\ny"]),
            [r"C:\Program Files\droid.exe", "--pattern=%MESH_TOKEN%", 'a"b', "x\ny"],
        )
        cmd_line = prepare_win_cmd_args(
            [r"C:\My Tools\droid.cmd", "--prompt", "hello (world)\\", "plain"],
            env={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        )
        self.assertIsInstance(cmd_line, str)
        self.assertTrue(cmd_line.startswith(r'"C:\Windows\System32\cmd.exe" /d /s /c "'))
        self.assertIn(r'"C:\My Tools\droid.cmd"', cmd_line)
        self.assertIn(r'"hello (world)\\"', cmd_line)
        self.assertIn(" plain", cmd_line)

        fallback_line = prepare_win_cmd_args(["tool.bat", "x"], env={"SystemRoot": r"D:\Win"})
        self.assertTrue(fallback_line.startswith(r'"' + os.path.join(r"D:\Win", "System32", "cmd.exe") + '" /d /s /c "'))

        # Embedded quotes, '%' env expansions, CR, and LF are rejected for .cmd/.bat targets
        for bad_argv in (
            [r"C:\x\y.cmd", 'a"b'],
            [r"C:\x\y.cmd", "--pattern=%MESH_TOKEN%"],
            [r"C:\x\y.bat", "50%"],
            [r"C:\x\y.cmd", "line1\nline2"],
            [r"C:\x\y.cmd", "line1\rline2"],
            [r"C:\x\%DIR%\y.cmd", "--help"],
        ):
            with self.subTest(bad_argv=bad_argv):
                with self.assertRaises(ValueError):
                    prepare_win_cmd_args(bad_argv)

        with self.assertRaises(ValueError):
            prepare_win_cmd_args([])
        with self.assertRaises(ValueError):
            prepare_win_cmd_args(["x.exe", "bad\x00arg"])

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                _verify_win_cmd_target_exists(os.path.join(td, "missing.cmd"))
            d = os.path.join(td, "dir.cmd")
            os.makedirs(d)
            with self.assertRaises(PermissionError):
                _verify_win_cmd_target_exists(d)

    def test_all_9_cmd_tripwire_shims_present_and_crlf(self) -> None:
        cmd_files = sorted(glob.glob(os.path.join(SHIMS_DIR, "*.cmd")))
        expected = sorted(os.path.join(SHIMS_DIR, f"{n}.cmd") for n in VENDOR_SHIM_NAMES)
        self.assertEqual(cmd_files, expected)
        for path in cmd_files:
            with open(path, "rb") as f:
                raw = f.read()
            self.assertIn(b"\r\n", raw, path)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), f"bare LF in {path}")
            self.assertTrue(raw.endswith(b"\r\n"), path)
            text = raw.decode("utf-8")
            self.assertIn("_TRIPWIRE_FILE", text)
            self.assertIn("_SUITE_TRIPWIRE_FILE", text)
            self.assertIn('>>"%TRIPWIRE%" echo %~f0 called with: %*', text)
            self.assertIn("exit /b 99", text)


class TestWinAdapterResolveAndRunwrapHelpers(unittest.TestCase):
    def test_resolve_binary_and_vendor_stream_under_mocked_win32(self) -> None:
        from mesh.adapters.base import Adapter, _resolve_win_explicit_binary, is_tripwire_shim_path
        from mesh.adapters.vendor_stream import resolve_on_path

        with tempfile.TemporaryDirectory() as td:
            shim_dir = os.path.join(td, "repo", "tests", "shims")
            real_dir = os.path.join(td, "real_bin")
            os.makedirs(shim_dir)
            os.makedirs(real_dir)

            # Extensionless + .cmd tripwire shims without +x bit
            hermes_shim_cmd = os.path.join(shim_dir, "HERMES.CMD")
            real_hermes = os.path.join(real_dir, "hermes.exe")
            real_codex = os.path.join(real_dir, "codex.cmd")
            codex_shim = os.path.join(shim_dir, "codex.cmd")
            ext_droid = os.path.join(real_dir, "droid.cmd")
            for p in (hermes_shim_cmd, real_hermes, real_codex, codex_shim, ext_droid):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("stub\n")
                os.chmod(p, 0o600)

            with mock.patch.object(sys, "platform", "win32"):
                self.assertTrue(is_tripwire_shim_path(hermes_shim_cmd, "hermes"))
                self.assertIsNone(_resolve_win_explicit_binary(os.path.join(real_dir, "droid"), (".exe", ".bat")))
                self.assertEqual(
                    _resolve_win_explicit_binary(os.path.join(real_dir, "droid"), (".exe", ".cmd")),
                    os.path.abspath(ext_droid),
                )

                class _HermesLike(Adapter):
                    id = "hermes"
                    binary_names = ["hermes"]

                with mock.patch.dict(os.environ, {"PATH": f"{shim_dir}{os.pathsep}{real_dir}", "PATHEXT": ".EXE;.CMD"}):
                    # Adapter.resolve_binary: shim FIRST stops search closed -> None
                    self.assertIsNone(_HermesLike().resolve_binary())
                    # vendor_stream.resolve_on_path: shim FIRST stops search closed -> None
                    self.assertIsNone(resolve_on_path(["codex"]))

                # Without shim first, non-shim binaries resolve cleanly even with mode 0600
                with mock.patch.dict(os.environ, {"PATH": real_dir, "PATHEXT": ".EXE;.CMD"}):
                    self.assertEqual(_HermesLike().resolve_binary(), os.path.abspath(real_hermes))
                    self.assertEqual(resolve_on_path(["codex"]), os.path.abspath(real_codex))

                # P2-3 regression: win32 PATH lookup must NEVER resolve cwd or relative PATH entries
                cwd_dir = os.path.join(td, "untrusted_cwd")
                os.makedirs(cwd_dir)
                cwd_droid = os.path.join(cwd_dir, "droid.CMD")
                with open(cwd_droid, "w", encoding="utf-8") as f:
                    f.write("@echo pwned\n")
                os.chmod(cwd_droid, 0o700)

                class _DroidLike(Adapter):
                    id = "droid"
                    binary_names = ["droid"]

                old_cwd = os.getcwd()
                try:
                    os.chdir(cwd_dir)
                    empty_bin = os.path.join(td, "empty_bin")
                    os.makedirs(empty_bin)
                    with mock.patch.dict(
                        os.environ,
                        {"PATH": f".{os.pathsep}{os.pathsep}./{os.pathsep}{empty_bin}", "PATHEXT": ".EXE;.CMD"},
                    ):
                        self.assertIsNone(_DroidLike().resolve_binary())
                        self.assertIsNone(resolve_on_path(["droid"]))
                finally:
                    os.chdir(old_cwd)

    def test_runwrap_win_identity_and_job_members(self) -> None:
        from mesh import runwrap

        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            run_dir = os.path.join(home, "runs", "run_abc")
            os.makedirs(run_dir)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    runwrap._resolve_win_run_identity(run_dir, {}),
                    (os.path.realpath(home), "run_abc"),
                )
                self.assertEqual(
                    runwrap._resolve_win_run_identity(os.path.join(td, "other"), {"home": home, "run_id": "r2"}),
                    (home, "r2"),
                )
                self.assertEqual(runwrap._resolve_win_run_identity(os.path.join(td, "other"), {}), (None, None))

        stats = {
            100: {"starttime": 111},
            200: None,
            300: {"starttime": 333},
            400: {"starttime": 0},
        }
        members = runwrap._collect_job_members_at_exit(
            object(),
            self_pid=100,
            query_pids_fn=lambda _j: [300, 100, 200, 300, 400],
            get_stat_fn=lambda p: stats.get(p),
        )
        self.assertEqual(members, [{"pid": 300, "starttime": 333}])


@unittest.skipUnless(sys.platform == "win32", "Windows-only real Named Pipe and .cmd shim tests")
class TestWindowsRealOverlappedPipeAndShims(unittest.TestCase):
    def setUp(self) -> None:
        from mesh.platform.paths import temp_root

        self.tmp = tempfile.mkdtemp(prefix="mh-win-pipe-", dir=temp_root())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_real_create_pipe_pair_and_large_stream(self) -> None:
        from mesh.platform import pipe_pump

        reader, wfd = pipe_pump.create_pipe_pair()
        expected = b"".join(f"line-{i:04d}-".encode("ascii") + b"X" * 120 + b"\n" for i in range(1200))
        chunks: List[bytes] = []
        done = threading.Event()

        def _pump() -> None:
            try:
                pipe_pump.pump_overlapped_stream_win(reader, chunks.append)
            finally:
                done.set()

        t = threading.Thread(target=_pump, daemon=True)
        t.start()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, sys; data = b''.join(f'line-{i:04d}-'.encode() + b'X'*120 + b'\\n' for i in range(1200)); "
                "view = memoryview(data); "
                "\nwhile view:\n n = os.write(1, view); view = view[n:]\n",
            ],
            stdin=subprocess.DEVNULL,
            stdout=wfd,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        os.close(wfd)
        self.assertEqual(proc.wait(timeout=15), 0)
        self.assertTrue(done.wait(timeout=10))
        t.join(timeout=2)
        self.assertEqual(b"".join(chunks), expected)

    def test_real_escaped_pipe_holder_drain_deadline(self) -> None:
        from mesh.core import proc as core_proc
        from mesh.platform import pipe_pump, proc as plat_proc

        reader, wfd = pipe_pump.create_pipe_pair()
        wake = pipe_pump.create_wake_event()
        self.addCleanup(wake.close)
        pid_file = os.path.join(self.tmp, "holder.pid")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)']); "
                f"open({pid_file!r}, 'w').write(str(p.pid)); "
                "print('parent_done', flush=True)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=wfd,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        os.close(wfd)

        def _kill_holder() -> None:
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r", encoding="utf-8") as pf:
                        pid = int(pf.read().strip())
                except (OSError, ValueError):
                    return
                info = core_proc.get_proc_stat(pid)
                if info is not None and int(info.get("starttime", 0)) > 0:
                    plat_proc.kill_if_same(pid, int(info["starttime"]))

        self.addCleanup(_kill_holder)
        deadline: List[Optional[float]] = [None]
        chunks: List[bytes] = []
        done = threading.Event()
        t = threading.Thread(
            target=lambda: (
                pipe_pump.pump_overlapped_stream_win(
                    reader, chunks.append, drain_wake_event=wake, drain_deadline=deadline
                ),
                done.set(),
            ),
            daemon=True,
        )
        t.start()
        self.assertEqual(proc.wait(timeout=10), 0)
        import time

        deadline[0] = time.monotonic() + 0.3
        wake.set()
        self.assertTrue(done.wait(timeout=5))
        t.join(timeout=2)
        self.assertIn(b"parent_done", b"".join(chunks))

    def test_real_probe_bounded_and_timeout_with_spaced_cmd_stub(self) -> None:
        from mesh.adapters.base import run_readonly_cmd
        from tests.stubs import create_hanging_stub, create_test_stubs, stub_path

        spaced = os.path.join(self.tmp, "dir with spaces (x)")
        create_test_stubs(spaced, droid_version="1.3.0")
        droid_cmd = stub_path(spaced, "droid")
        res = run_readonly_cmd([droid_cmd, "--version"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("1.3.0", res.stdout)

        hang_cmd = create_hanging_stub(spaced, "hang_probe", sleep_s=15.0)
        res_to = run_readonly_cmd([hang_cmd, "--version"], timeout_s=0.3)
        self.assertEqual(res_to.returncode, -1)
        self.assertIn("timed out", res_to.stderr)

    def test_real_all_9_cmd_shims_exit_99_and_log(self) -> None:
        from mesh.platform.pipe_pump import prepare_win_cmd_args

        trip = os.path.join(self.tmp, "trip one.log")
        suite_trip = os.path.join(self.tmp, "suite two.log")
        env = {**os.environ, "_TRIPWIRE_FILE": trip, "_SUITE_TRIPWIRE_FILE": suite_trip}
        for name in VENDOR_SHIM_NAMES:
            shim = os.path.join(SHIMS_DIR, f"{name}.cmd")
            cmd = prepare_win_cmd_args([shim, "--arg", "val 2"], env=env)
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(res.returncode, 99, f"{name}.cmd rc={res.returncode} stderr={res.stderr}")
        with open(trip, "r", encoding="utf-8") as f:
            trip_txt = f.read()
        with open(suite_trip, "r", encoding="utf-8") as f:
            suite_txt = f.read()
        for name in VENDOR_SHIM_NAMES:
            self.assertIn(f'{name}.cmd called with: --arg "val 2"', trip_txt)
            self.assertIn(f'{name}.cmd called with: --arg "val 2"', suite_txt)

    def test_real_win_resolve_binary_and_vendor_stream(self) -> None:
        from mesh.adapters.droid import DroidAdapter
        from mesh.adapters.hermes import HermesAdapter
        from mesh.adapters.vendor_stream import resolve_on_path
        from tests.stubs import create_test_stubs, stub_path

        stubs = create_test_stubs(os.path.join(self.tmp, "stubs"))
        with mock.patch.dict(os.environ, {"PATH": f"{SHIMS_DIR}{os.pathsep}{stubs}"}):
            self.assertIsNone(HermesAdapter().resolve_binary())
            self.assertIsNone(resolve_on_path(["droid"]))
        with mock.patch.dict(os.environ, {"PATH": stubs}):
            resolved = DroidAdapter().resolve_binary()
            self.assertEqual(os.path.normcase(resolved or ""), os.path.normcase(stub_path(stubs, "droid")))

    def test_real_win_cmd_rejects_percent_and_preserves_safe_metachars(self) -> None:
        import json as _json
        from mesh.platform.pipe_pump import prepare_win_cmd_args
        from tests.stubs import _write_python_stub

        stub = _write_python_stub(
            self.tmp,
            "echo_args",
            "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        )
        env = {**os.environ, "MESH_TOKEN": "secret-token-value"}
        safe_args = ["--flag", "a&echo INJECT", "hello (world)\\", "a,b;c=d"]
        cmd = prepare_win_cmd_args([stub, *safe_args], env=env)
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(_json.loads(res.stdout), safe_args)
        for bad in ("--pattern=%MESH_TOKEN%", 'a"b', "line1\nline2", "line1\rline2"):
            with self.assertRaises(ValueError):
                prepare_win_cmd_args([stub, bad], env=env)


class TestFinalReviewPipePumpFixes(unittest.TestCase):
    def test_p2_3_cmd_meta_chars_quote_comma_semicolon_equals(self) -> None:
        from mesh.platform.pipe_pump import (
            _CMD_META_CHARS,
            _quote_win_cmd_arg,
            prepare_win_cmd_args,
        )

        self.assertTrue({",", ";", "="}.issubset(_CMD_META_CHARS))
        self.assertEqual(_quote_win_cmd_arg("a,b"), '"a,b"')
        self.assertEqual(_quote_win_cmd_arg("x;y"), '"x;y"')
        self.assertEqual(_quote_win_cmd_arg("k=v"), '"k=v"')
        self.assertEqual(_quote_win_cmd_arg("plain"), "plain")
        cmd = prepare_win_cmd_args(
            [r"C:\tools\shim.cmd", "a,b", "x;y", "k=v", "plain"],
            env={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        )
        self.assertIsInstance(cmd, str)
        self.assertEqual(
            cmd,
            r'"C:\Windows\System32\cmd.exe" /d /s /c "C:\tools\shim.cmd "a,b" "x;y" "k=v" plain"',
        )


if __name__ == "__main__":
    unittest.main()
