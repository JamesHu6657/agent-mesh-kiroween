"""Cross-platform daemon IPC (AF_UNIX on POSIX, Windows Named Pipes on Win32).

Windows-specific ctypes loaders and ``asyncio`` imports are deferred so importing
this module on Linux never loads Windows primitives, and importing it during CLI
verbs never triggers eager ``asyncio`` startup overhead.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import hashlib
import hmac
import io
import math
import os
import secrets
import socket
import sys
import threading
import time
from typing import Any, Callable, Optional, Set, Tuple

from mesh.platform import paths

if sys.platform != "win32":
    from mesh.platform import _posix

WIN_PIPE_PREFIX = r"\\.\pipe\LOCAL\agent-mesh-"
PIPE_FILE_NAME = "mesh.pipe"
_PIPE_BUFFER_BYTES = 65536
_WRITE_CHUNK_BYTES = 65536
_WAIT_SLICE_MS = 250
_MAX_PIPE_NAME_CHARS = 256


def is_win_pipe_path(path: str) -> bool:
    """Return True if *path* names a Windows ``\\\\.\\pipe\\...`` endpoint."""
    if not isinstance(path, str) or not path:
        return False
    norm = path.replace("/", "\\").lower()
    return norm.startswith("\\\\.\\pipe\\") and len(norm) > len("\\\\.\\pipe\\")


def pipe_pointer_path_win(home: str) -> str:
    """Return the path ``$MESH_HOME/run/mesh.pipe`` storing the active Windows pipe name."""
    return os.path.join(home, "run", PIPE_FILE_NAME)


def _deterministic_pipe_endpoint_win(home: str) -> str:
    norm_home = paths.norm_cmp_win(os.path.realpath(home))
    digest = hashlib.sha256(norm_home.encode("utf-8")).hexdigest()[:32]
    return f"{WIN_PIPE_PREFIX}{digest}"


def generate_pipe_endpoint_win(home: str) -> str:
    """Generate an unpredictable ``\\\\.\\pipe\\LOCAL\\agent-mesh-<hash16>-<128-bit random>``."""
    norm_home = paths.norm_cmp_win(os.path.realpath(home))
    home_tag = hashlib.sha256(norm_home.encode("utf-8")).hexdigest()[:16]
    nonce = secrets.token_hex(16)
    return f"{WIN_PIPE_PREFIX}{home_tag}-{nonce}"


def write_pipe_endpoint_win(
    home: str,
    pipe_name: str,
    *,
    open_fn: Optional[Callable[..., int]] = None,
    fchmod_fn: Optional[Callable[[int, int], None]] = None,
    replace_fn: Optional[Callable[[str, str], None]] = None,
    dir_acl_fn: Optional[Callable[[str], None]] = None,
) -> str:
    """Atomically write *pipe_name* into ``$MESH_HOME/run/mesh.pipe`` with owner-only DACL."""
    if not is_win_pipe_path(pipe_name) or len(pipe_name) > _MAX_PIPE_NAME_CHARS:
        raise ValueError(f"Invalid Windows named pipe path: {pipe_name!r}")
    if any(ch in pipe_name for ch in "\r\n\x00"):
        raise ValueError(f"Invalid characters in Windows named pipe path: {pipe_name!r}")

    from mesh.platform import fs, perms

    o_fn = open_fn if open_fn is not None else fs.open_regular_nofollow
    fc_fn = fchmod_fn if fchmod_fn is not None else perms.fchmod_private
    rep_fn = replace_fn if replace_fn is not None else fs.atomic_replace
    da_fn = dir_acl_fn if dir_acl_fn is not None else perms.set_private_dir_acl

    run_dir = os.path.join(home, "run")
    run_existed = os.path.isdir(run_dir)
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    if sys.platform == "win32" and not run_existed:
        da_fn(run_dir)

    pipe_path = pipe_pointer_path_win(home)
    tmp_path = os.path.join(run_dir, f"{PIPE_FILE_NAME}.tmp.{os.getpid()}.{secrets.token_hex(6)}")
    fd = o_fn(
        tmp_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        label="pipe endpoint file",
    )
    try:
        fc_fn(fd, 0o600)
        os.write(fd, (pipe_name + "\n").encode("utf-8"))
        os.fsync(fd)
    except BaseException:
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise
    os.close(fd)
    try:
        rep_fn(tmp_path, pipe_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return pipe_path


def read_pipe_endpoint_win(
    home: str,
    *,
    open_fn: Optional[Callable[..., int]] = None,
    verify_fn: Optional[Callable[..., None]] = None,
) -> Optional[str]:
    """Read and verify ``$MESH_HOME/run/mesh.pipe``; return ``None`` strictly when absent."""
    import mesh.constants as c
    from mesh.platform import fs, perms

    run_dir = os.path.join(home, "run")
    if sys.platform == "win32" and paths.is_reparse_link(run_dir):
        raise OSError(errno.ELOOP, "Refusing symlink or reparse point run directory", run_dir)
    pipe_path = pipe_pointer_path_win(home)
    if open_fn is None and sys.platform != "win32" and not os.path.lexists(pipe_path):
        return None
    o_fn = open_fn if open_fn is not None else fs.open_regular_nofollow
    v_fn = verify_fn if verify_fn is not None else perms.verify_private_owner_and_perms
    try:
        fd = o_fn(pipe_path, os.O_RDONLY, 0o600, label="pipe endpoint file")
    except FileNotFoundError:
        return None
    except NotImplementedError:
        if not hasattr(ctypes, "windll") and not os.path.lexists(pipe_path):
            return None
        raise
    try:
        v_fn(
            fd,
            is_dir=False,
            label="pipe endpoint file",
            error_code=c.INSECURE_PERMISSIONS,
            exit_code=c.EXIT_USAGE,
        )
        chunks = []
        total = 0
        while total < 1024:
            chunk = os.read(fd, 1024 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks).decode("utf-8", errors="strict").rstrip("\r\n")
    if (
        not raw
        or any(ch in raw for ch in "\r\n\x00")
        or not is_win_pipe_path(raw)
        or len(raw) > _MAX_PIPE_NAME_CHARS
    ):
        raise OSError(errno.EINVAL, f"Invalid Windows named pipe endpoint in {pipe_path!r}", pipe_path)
    return raw


def remove_pipe_endpoint_win(home: str) -> None:
    """Remove ``$MESH_HOME/run/mesh.pipe`` if present (best-effort cleanup)."""
    pipe_path = pipe_pointer_path_win(home)
    try:
        os.remove(pipe_path)
    except OSError:
        pass


def daemon_endpoint_path_win(home: str) -> str:
    """Return live endpoint from ``$MESH_HOME/run/mesh.pipe``, or deterministic fallback if absent."""
    live = read_pipe_endpoint_win(home)
    if live is not None:
        return live
    return _deterministic_pipe_endpoint_win(home)


def daemon_endpoint_path(home: str) -> str:
    """Return the daemon IPC endpoint path (``mesh.sock`` on POSIX, Named Pipe on Windows)."""
    if sys.platform == "win32":
        return daemon_endpoint_path_win(home)
    return _posix.daemon_endpoint_path(home)


def _create_named_pipe_instance_win(
    pipe_name: str,
    *,
    first: bool,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> int:
    from mesh.platform import _win32, _win32_ctypes as w32, perms

    if not is_win_pipe_path(pipe_name) or len(pipe_name) > _MAX_PIPE_NAME_CHARS:
        raise ValueError(f"Invalid Windows named pipe path: {pipe_name!r}")

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    user_sid, _ = perms.get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
    )
    sddl = _win32.build_private_pipe_sddl(user_sid)

    sd_ptr = ctypes.c_void_p()
    if not dll_adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, w32.SDDL_REVISION_1, ctypes.byref(sd_ptr), None
    ):
        raise _win32._win_proc_error(err_fn())
    if not sd_ptr.value:
        raise OSError(
            errno.EINVAL, "ConvertStringSecurityDescriptorToSecurityDescriptorW returned NULL"
        )

    try:
        sa = w32.SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(w32.SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=sd_ptr.value,
            bInheritHandle=False,
        )
        open_mode = w32.PIPE_ACCESS_DUPLEX | w32.FILE_FLAG_OVERLAPPED
        if first:
            open_mode |= w32.FILE_FLAG_FIRST_PIPE_INSTANCE
        pipe_mode = (
            w32.PIPE_TYPE_BYTE
            | w32.PIPE_READMODE_BYTE
            | w32.PIPE_WAIT
            | w32.PIPE_REJECT_REMOTE_CLIENTS
        )
        h_pipe = dll_k32.CreateNamedPipeW(
            pipe_name,
            open_mode,
            pipe_mode,
            w32.PIPE_UNLIMITED_INSTANCES,
            _PIPE_BUFFER_BYTES,
            _PIPE_BUFFER_BYTES,
            w32.NMPWAIT_USE_DEFAULT_WAIT,
            ctypes.byref(sa),
        )
        if w32.is_null_or_invalid_handle(h_pipe):
            raise _win32._win_proc_error(err_fn())
        return int(h_pipe)
    finally:
        if sd_ptr.value:
            dll_k32.LocalFree(sd_ptr)


def create_named_pipe_server_win(
    pipe_name: str,
    *,
    first_instance: bool = True,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Create a Win32 Named Pipe server instance wrapped in an RAII ``_WinHandle``."""
    from mesh.platform import _win32, _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    h_pipe = _create_named_pipe_instance_win(
        pipe_name,
        first=first_instance,
        k32=dll_k32,
        advapi=advapi,
        get_last_error_fn=err_fn,
    )
    return _win32._WinHandle(h_pipe, k32=dll_k32, get_last_error_fn=err_fn)


def verify_pipe_peer_win(
    pipe_handle: Any,
    *,
    expected_user_sid: Optional[str] = None,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Tuple[int, int, int]:
    """Authenticate a connected Named Pipe client via PID + ``TokenUser`` SID.

    Fails closed by raising ``PermissionError`` / ``OSError`` whenever the peer PID,
    process token, or ``TokenUser`` SID cannot be verified or mismatches the daemon user.
    """
    from mesh.platform import _win32, _win32_ctypes as w32, perms

    if pipe_handle is None:
        raise PermissionError(errno.EACCES, "Missing peer pipe handle")
    raw_h = pipe_handle.fileno() if hasattr(pipe_handle, "fileno") else int(pipe_handle)
    if w32.is_null_or_invalid_handle(raw_h):
        raise PermissionError(errno.EACCES, "Invalid peer pipe handle")

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    client_pid = wintypes.ULONG(0)
    if not dll_k32.GetNamedPipeClientProcessId(raw_h, ctypes.byref(client_pid)):
        raise _win32._win_proc_error(err_fn())
    pid = int(client_pid.value)
    if pid <= 0:
        raise PermissionError(errno.EACCES, f"Invalid pipe peer pid {pid}")

    if expected_user_sid is None:
        expected_user_sid, _ = perms.get_current_user_and_owner_sids_win(
            advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
        )

    peer_sid = _win32.get_process_user_sid_win(
        pid,
        k32=dll_k32,
        advapi=dll_adv,
        get_last_error_fn=err_fn,
    )
    if not hmac.compare_digest(peer_sid, expected_user_sid):
        raise PermissionError(
            errno.EACCES,
            f"Pipe peer pid {pid} SID {peer_sid} does not match daemon user SID",
        )
    return pid, 0, 0


def get_peer_credentials(endpoint: Any) -> Tuple[int, int, int]:
    """Return ``(pid, uid, gid)`` for a connected client socket or Windows Named Pipe."""
    if sys.platform != "win32":
        return _posix.get_peer_credentials(endpoint)
    if endpoint is None:
        raise PermissionError(errno.EACCES, "Missing peer pipe handle")
    cached = getattr(endpoint, "_mesh_peer_credentials", None)
    if cached is not None:
        pid, uid, gid = cached
        return int(pid), int(uid), int(gid)
    return verify_pipe_peer_win(endpoint)


def _pipe_is_open(pipe: Any) -> bool:
    """Return True if *pipe* is an open ``PipeHandle`` without raising on closed pipes."""
    if pipe is None:
        return False
    if hasattr(pipe, "handle"):
        return pipe.handle is not None
    try:
        return pipe.fileno() != -1
    except (ValueError, OSError):
        return False


class NamedPipeServer:
    """Asyncio server wrapping a Windows ``ProactorEventLoop`` Named Pipe accept-ahead loop."""

    def __init__(
        self,
        address: str,
        *,
        loop: Any,
        client_connected_cb: Any,
        limit: int,
        pipe_handle_cls: Any,
        k32: Any = None,
        advapi: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        import asyncio
        from mesh.platform import _win32_ctypes as w32, perms

        self._address = address
        self._loop = loop
        self._client_connected_cb = client_connected_cb
        self._limit = int(limit)
        self._pipe_handle_cls = pipe_handle_cls
        self._k32 = k32 if k32 is not None else w32.get_kernel32()
        self._advapi = advapi if advapi is not None else w32.get_advapi32()
        self._err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
        self._user_sid, _ = perms.get_current_user_and_owner_sids_win(
            advapi=self._advapi, k32=self._k32, get_last_error_fn=self._err_fn
        )

        self._closed = False
        self._free_instances: Set[Any] = set()
        self._accept_pipe_future: Any = None
        self._closed_event = asyncio.Event()
        self._pipe: Any = None
        try:
            self._pipe = self._new_pipe(first=True)
        except BaseException:
            self.close()
            raise

    @property
    def sockets(self) -> Tuple[Any, ...]:
        return ()

    def is_serving(self) -> bool:
        return not self._closed

    def _new_pipe(self, *, first: bool) -> Any:
        if self._closed:
            return None
        raw_h = _create_named_pipe_instance_win(
            self._address,
            first=first,
            k32=self._k32,
            advapi=self._advapi,
            get_last_error_fn=self._err_fn,
        )
        pipe = self._pipe_handle_cls(raw_h)
        self._free_instances.add(pipe)
        return pipe

    def _get_unconnected_pipe(self) -> Any:
        if self._closed:
            return None
        cur = self._pipe
        self._pipe = self._new_pipe(first=False)
        return cur

    def start(self) -> None:
        if not self._closed:
            self._loop.call_soon(self._loop_accept_pipe)

    def _loop_accept_pipe(self, f: Any = None) -> None:
        import asyncio

        pipe = None
        try:
            if f is not None:
                pipe = f.result()
                self._free_instances.discard(pipe)

                if self._closed:
                    pipe.close()
                    self._closed_event.set()
                    return

                try:
                    creds = verify_pipe_peer_win(
                        pipe,
                        expected_user_sid=self._user_sid,
                        k32=self._k32,
                        advapi=self._advapi,
                        get_last_error_fn=self._err_fn,
                    )
                except OSError:
                    pipe.close()
                    pipe = None
                else:
                    pipe._mesh_peer_credentials = creds
                    reader = asyncio.StreamReader(limit=self._limit, loop=self._loop)
                    protocol = asyncio.StreamReaderProtocol(
                        reader, self._client_connected_cb, loop=self._loop
                    )
                    self._loop._make_duplex_pipe_transport(
                        pipe,
                        protocol,
                        extra={"addr": self._address, "peer_credentials": creds},
                    )
                    pipe = None

            if self._closed:
                self._closed_event.set()
                return

            pipe = self._get_unconnected_pipe()
            if pipe is None:
                self._closed_event.set()
                return

            next_fut = self._loop._proactor.accept_pipe(pipe)
        except BrokenPipeError:
            if _pipe_is_open(pipe):
                self._free_instances.discard(pipe)
                pipe.close()
            if not self._closed:
                self._loop.call_soon(self._loop_accept_pipe)
            else:
                self._closed_event.set()
        except OSError as exc:
            if _pipe_is_open(pipe):
                self._free_instances.discard(pipe)
                self._loop.call_exception_handler(
                    {
                        "message": "Pipe accept failed",
                        "exception": exc,
                        "pipe": pipe,
                    }
                )
                pipe.close()
            if not self._closed:
                self._loop.call_soon(self._loop_accept_pipe)
            else:
                self._closed_event.set()
        except asyncio.CancelledError:
            if _pipe_is_open(pipe):
                self._free_instances.discard(pipe)
                pipe.close()
            self._closed_event.set()
        else:
            self._accept_pipe_future = next_fut
            next_fut.add_done_callback(self._loop_accept_pipe)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fut = self._accept_pipe_future
        self._accept_pipe_future = None
        if fut is not None and not fut.done():
            fut.cancel()
        else:
            self._closed_event.set()
        for pipe in list(self._free_instances):
            try:
                pipe.close()
            except Exception:
                pass
        self._free_instances.clear()
        self._pipe = None

    async def wait_closed(self) -> None:
        import asyncio

        await self._closed_event.wait()
        await asyncio.sleep(0)

    async def __aenter__(self) -> "NamedPipeServer":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.close()
        await self.wait_closed()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


async def start_daemon_server(
    client_connected_cb: Any,
    path: str,
    *,
    backlog: int = 64,
    limit: int = 65536,
) -> Any:
    """Start the daemon IPC server (``AF_UNIX`` on POSIX, Named Pipe on Windows)."""
    if sys.platform != "win32":
        return await _posix.start_daemon_server(
            client_connected_cb, path, backlog=backlog, limit=limit
        )

    import asyncio
    from asyncio import windows_utils

    _ = backlog  # Named Pipe concurrency uses accept-ahead + Server MAX_CONNECTIONS
    loop = asyncio.get_running_loop()
    if not (hasattr(loop, "_proactor") and hasattr(loop, "_make_duplex_pipe_transport")):
        raise RuntimeError("Windows Named Pipe IPC requires asyncio ProactorEventLoop")
    srv = NamedPipeServer(
        path,
        loop=loop,
        client_connected_cb=client_connected_cb,
        limit=limit,
        pipe_handle_cls=windows_utils.PipeHandle,
    )
    srv.start()
    return srv


class _PipeRawIO(io.RawIOBase):
    """RawIOBase adapter over ``PipeClientStream`` backing ``makefile()``."""

    def __init__(self, stream: "PipeClientStream", mode: str) -> None:
        super().__init__()
        if mode not in ("r", "w", "rw", "rb", "wb", "rwb"):
            raise ValueError(f"invalid mode: {mode!r}")
        self._stream: Optional[PipeClientStream] = stream
        if "b" not in mode:
            mode += "b"
        self._mode = mode
        self._reading = "r" in mode
        self._writing = "w" in mode
        self._timeout_occurred = False

    def readinto(self, b: Any) -> Optional[int]:
        self._checkClosed()
        self._checkReadable()
        if self._timeout_occurred:
            raise OSError("cannot read from timed out object")
        assert self._stream is not None
        try:
            return self._stream._recv_into_raw(b)
        except TimeoutError:
            self._timeout_occurred = True
            raise
        except BlockingIOError:
            return None

    def write(self, b: Any) -> Optional[int]:
        self._checkClosed()
        self._checkWritable()
        assert self._stream is not None
        try:
            return self._stream._send_raw(b)
        except BlockingIOError:
            return None

    def readable(self) -> bool:
        if self.closed:
            raise ValueError("I/O operation on closed pipe stream.")
        return self._reading

    def writable(self) -> bool:
        if self.closed:
            raise ValueError("I/O operation on closed pipe stream.")
        return self._writing

    def seekable(self) -> bool:
        if self.closed:
            raise ValueError("I/O operation on closed pipe stream.")
        return False

    def fileno(self) -> int:
        self._checkClosed()
        assert self._stream is not None
        return self._stream.fileno()

    @property
    def name(self) -> int:
        if not self.closed and self._stream is not None:
            try:
                return self._stream.fileno()
            except OSError:
                return -1
        return -1

    @property
    def mode(self) -> str:
        return self._mode

    def close(self) -> None:
        if self.closed:
            return
        super().close()
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream._decref_socketios()


class PipeClientStream:
    """Socket-compatible overlapped Windows Named Pipe client stream."""

    def __init__(
        self,
        handle: int,
        pipe_name: str,
        *,
        timeout: Optional[float] = 30.0,
        k32: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        from mesh.platform import _win32_ctypes as w32

        self._handle: Optional[int] = None
        self._state_lock = threading.Lock()
        self._active_io = 0
        self._io_refs = 0
        self._closed_requested = False
        if w32.is_null_or_invalid_handle(handle):
            raise ValueError(f"Invalid pipe handle: {handle!r}")
        self._handle = int(handle)
        self.pipe_name = pipe_name
        self._k32 = k32 if k32 is not None else w32.get_kernel32()
        self._err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
        self._time_fn = time_fn
        self._timeout: Optional[float] = None
        self.settimeout(timeout)

    def settimeout(self, value: Optional[float]) -> None:
        if value is not None:
            val = float(value)
            if val < 0:
                raise ValueError("Timeout value out of range")
            self._timeout = val
        else:
            self._timeout = None

    def gettimeout(self) -> Optional[float]:
        return self._timeout

    def fileno(self) -> int:
        if self._handle is None:
            raise OSError(errno.EBADF, "Pipe stream is closed")
        return int(self._handle)

    def _require_handle(self, *, allow_pending_io_refs: bool = False) -> int:
        if self._handle is None:
            raise OSError(errno.EBADF, "Pipe stream is closed")
        if self._closed_requested and not allow_pending_io_refs:
            raise OSError(errno.EBADF, "Pipe stream is closed")
        return int(self._handle)

    def _begin_io(self, *, allow_pending_io_refs: bool = False) -> int:
        with self._state_lock:
            handle = self._require_handle(allow_pending_io_refs=allow_pending_io_refs)
            self._active_io += 1
            return handle

    def _end_io(self) -> None:
        close_h: Optional[int] = None
        with self._state_lock:
            if self._active_io > 0:
                self._active_io -= 1
            if (
                self._closed_requested
                and self._io_refs <= 0
                and self._active_io <= 0
                and self._handle is not None
            ):
                close_h = self._handle
                self._handle = None
        if close_h is not None:
            self._k32.CloseHandle(close_h)

    def _win_err(self, code: int) -> OSError:
        from mesh.platform import _win32_ctypes as w32

        if code in (w32.ERROR_BROKEN_PIPE, w32.ERROR_NO_DATA, w32.ERROR_PIPE_NOT_CONNECTED):
            exc = BrokenPipeError(errno.EPIPE, f"Named pipe disconnected (WinError {code})")
            exc.winerror = code  # type: ignore[attr-defined]
            return exc
        return w32.win_error(code, self.pipe_name)

    def _cancel_and_drain(self, handle: int, ov: Any) -> Tuple[bool, int, int]:
        from mesh.platform import _win32_ctypes as w32

        self._k32.CancelIoEx(handle, ctypes.byref(ov))
        transferred = wintypes.DWORD(0)
        ok = self._k32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(transferred), True)
        err = 0 if ok else int(self._err_fn())
        return bool(ok), int(transferred.value), err

    def _wait_overlapped(
        self, handle: int, h_event: Any, ov: Any, deadline: Optional[float]
    ) -> Tuple[bool, int, int]:
        from mesh.platform import _win32_ctypes as w32

        drained = False
        try:
            while True:
                if self._timeout == 0.0:
                    slice_ms = 0
                elif deadline is not None:
                    rem = deadline - self._time_fn()
                    if rem <= 0:
                        ok, nbytes, err = self._cancel_and_drain(handle, ov)
                        drained = True
                        if ok:
                            return True, nbytes, 0
                        if err == w32.ERROR_OPERATION_ABORTED:
                            raise socket.timeout("timed out")
                        return False, nbytes, err
                    slice_ms = max(1, min(_WAIT_SLICE_MS, int(math.ceil(rem * 1000.0))))
                else:
                    slice_ms = _WAIT_SLICE_MS

                rc = int(self._k32.WaitForSingleObject(h_event, slice_ms))
                if rc == w32.WAIT_OBJECT_0:
                    transferred = wintypes.DWORD(0)
                    ok = self._k32.GetOverlappedResult(
                        handle, ctypes.byref(ov), ctypes.byref(transferred), False
                    )
                    drained = True
                    if ok:
                        return True, int(transferred.value), 0
                    return False, int(transferred.value), int(self._err_fn())
                if rc == w32.WAIT_FAILED:
                    wait_err = int(self._err_fn())
                    self._cancel_and_drain(handle, ov)
                    drained = True
                    raise self._win_err(wait_err)
                if rc != w32.WAIT_TIMEOUT:
                    self._cancel_and_drain(handle, ov)
                    drained = True
                    raise OSError(
                        errno.EIO, f"WaitForSingleObject returned unexpected status {rc:#x}"
                    )

                if self._timeout == 0.0:
                    ok, nbytes, err = self._cancel_and_drain(handle, ov)
                    drained = True
                    if ok:
                        return True, nbytes, 0
                    if err == w32.ERROR_OPERATION_ABORTED:
                        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
                    return False, nbytes, err
        except BaseException:
            if not drained:
                self._cancel_and_drain(handle, ov)
            raise

    def _create_io_event(self) -> int:
        from mesh.platform import _win32_ctypes as w32

        h_event = self._k32.CreateEventW(None, True, False, None)
        if w32.is_null_or_invalid_handle(h_event):
            raise self._win_err(int(self._err_fn()))
        return int(h_event)

    def _recv_impl(self, bufsize: int, *, allow_pending_io_refs: bool = False) -> bytes:
        from mesh.platform import _win32_ctypes as w32

        size = int(bufsize)
        if size < 0:
            raise ValueError("negative buffersize in recv")
        if size == 0:
            return b""
        handle = self._begin_io(allow_pending_io_refs=allow_pending_io_refs)
        h_event: Optional[int] = None
        try:
            deadline = None if self._timeout is None else self._time_fn() + self._timeout
            buf = ctypes.create_string_buffer(size)
            nread = wintypes.DWORD(0)
            h_event = self._create_io_event()
            ov = w32.OVERLAPPED()
            ov.hEvent = h_event
            ok = self._k32.ReadFile(handle, buf, size, ctypes.byref(nread), ctypes.byref(ov))
            if ok:
                return buf.raw[: int(nread.value)]
            err = int(self._err_fn())
            if err in (w32.ERROR_BROKEN_PIPE, w32.ERROR_HANDLE_EOF, w32.ERROR_PIPE_NOT_CONNECTED):
                return b""
            if err != w32.ERROR_IO_PENDING:
                raise self._win_err(err)
            done_ok, done_bytes, done_err = self._wait_overlapped(handle, h_event, ov, deadline)
            if done_ok:
                return buf.raw[:done_bytes]
            if done_err in (
                w32.ERROR_BROKEN_PIPE,
                w32.ERROR_HANDLE_EOF,
                w32.ERROR_PIPE_NOT_CONNECTED,
            ):
                return b""
            if done_err == w32.ERROR_OPERATION_ABORTED and self._closed_requested:
                raise OSError(errno.EBADF, "Pipe stream is closed")
            raise self._win_err(done_err)
        finally:
            if h_event is not None:
                self._k32.CloseHandle(h_event)
            self._end_io()

    def recv(self, bufsize: int) -> bytes:
        return self._recv_impl(bufsize, allow_pending_io_refs=False)

    def _recv_into_raw(self, buffer: Any) -> int:
        view = memoryview(buffer)
        if view.ndim != 1 or view.format != "B":
            view = view.cast("B")
        if len(view) == 0:
            return 0
        data = self._recv_impl(len(view), allow_pending_io_refs=True)
        n = len(data)
        view[:n] = data
        return n

    def recv_into(self, buffer: Any) -> int:
        self._require_handle(allow_pending_io_refs=False)
        return self._recv_into_raw(buffer)

    def _send_chunk(
        self, chunk: bytes, deadline: Optional[float], *, allow_pending_io_refs: bool = False
    ) -> int:
        from mesh.platform import _win32_ctypes as w32

        if not chunk:
            return 0
        handle = self._begin_io(allow_pending_io_refs=allow_pending_io_refs)
        h_event: Optional[int] = None
        try:
            cbuf = ctypes.create_string_buffer(chunk, len(chunk))
            nwritten = wintypes.DWORD(0)
            h_event = self._create_io_event()
            ov = w32.OVERLAPPED()
            ov.hEvent = h_event
            ok = self._k32.WriteFile(
                handle, cbuf, len(chunk), ctypes.byref(nwritten), ctypes.byref(ov)
            )
            if ok:
                return int(nwritten.value)
            err = int(self._err_fn())
            if err != w32.ERROR_IO_PENDING:
                raise self._win_err(err)
            done_ok, done_bytes, done_err = self._wait_overlapped(handle, h_event, ov, deadline)
            if done_ok:
                return done_bytes
            if done_err == w32.ERROR_OPERATION_ABORTED and self._closed_requested:
                raise OSError(errno.EBADF, "Pipe stream is closed")
            raise self._win_err(done_err)
        finally:
            if h_event is not None:
                self._k32.CloseHandle(h_event)
            self._end_io()

    def _send_raw(self, data: Any) -> int:
        raw = bytes(memoryview(data))
        if not raw:
            self._require_handle(allow_pending_io_refs=True)
            return 0
        deadline = None if self._timeout is None else self._time_fn() + self._timeout
        return self._send_chunk(raw[:_WRITE_CHUNK_BYTES], deadline, allow_pending_io_refs=True)

    def send(self, data: Any) -> int:
        raw = bytes(memoryview(data))
        if not raw:
            self._require_handle(allow_pending_io_refs=False)
            return 0
        deadline = None if self._timeout is None else self._time_fn() + self._timeout
        return self._send_chunk(raw[:_WRITE_CHUNK_BYTES], deadline, allow_pending_io_refs=False)

    def sendall(self, data: Any) -> None:
        raw = bytes(memoryview(data))
        if not raw:
            self._require_handle()
            return
        deadline = None if self._timeout is None else self._time_fn() + self._timeout
        offset = 0
        while offset < len(raw):
            chunk = raw[offset : offset + _WRITE_CHUNK_BYTES]
            sent = self._send_chunk(chunk, deadline)
            if sent <= 0:
                raise BrokenPipeError(errno.EPIPE, "Named pipe send wrote 0 bytes")
            offset += sent

    def makefile(
        self,
        mode: str = "r",
        buffering: Optional[int] = None,
        *,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
        newline: Optional[str] = None,
    ) -> Any:
        if not set(mode) <= {"r", "w", "b"}:
            raise ValueError(f"invalid mode {mode!r} (only r, w, b allowed)")
        writing = "w" in mode
        reading = "r" in mode or not writing
        binary = "b" in mode
        rawmode = ("r" if reading else "") + ("w" if writing else "")
        raw = _PipeRawIO(self, rawmode)
        self._io_refs += 1
        if buffering is None:
            buffering = -1
        if buffering < 0:
            buffering = io.DEFAULT_BUFFER_SIZE
        if buffering == 0:
            if not binary:
                raise ValueError("unbuffered streams must be binary")
            return raw
        if reading and writing:
            buf: Any = io.BufferedRWPair(raw, raw, buffering)
        elif reading:
            buf = io.BufferedReader(raw, buffering)
        else:
            buf = io.BufferedWriter(raw, buffering)
        if binary:
            return buf
        text_enc = io.text_encoding(encoding)
        text = io.TextIOWrapper(buf, text_enc, errors, newline)
        text.mode = mode  # type: ignore[attr-defined]
        return text

    def shutdown(self, how: int) -> None:
        """Shut down one or both halves of the stream.

        Note: Win32 ``FlushFileBuffers`` is synchronous (no overlapped mode) and
        blocks until the server drains all buffered writes for ``SHUT_WR`` /
        ``SHUT_RDWR``, unlike POSIX ``socket.shutdown`` which returns immediately.
        ``SHUT_RD`` validates handle liveness and returns without flushing.
        """
        from mesh.platform import _win32_ctypes as w32

        handle = self._require_handle()
        if how == socket.SHUT_RD:
            return
        if not self._k32.FlushFileBuffers(handle):
            err = int(self._err_fn())
            if err not in (w32.ERROR_BROKEN_PIPE, w32.ERROR_PIPE_NOT_CONNECTED, w32.ERROR_NO_DATA):
                raise self._win_err(err)

    def _decref_socketios(self) -> None:
        close_h: Optional[int] = None
        with self._state_lock:
            if self._io_refs > 0:
                self._io_refs -= 1
            if (
                self._closed_requested
                and self._io_refs <= 0
                and self._active_io <= 0
                and self._handle is not None
            ):
                close_h = self._handle
                self._handle = None
        if close_h is not None:
            self._k32.CloseHandle(close_h)

    def _real_close(self) -> None:
        close_h: Optional[int] = None
        with self._state_lock:
            h = self._handle
            if h is None or self._active_io > 0:
                return
            self._handle = None
            close_h = h
        if close_h is not None:
            self._k32.CloseHandle(close_h)

    def close(self) -> None:
        close_h: Optional[int] = None
        with self._state_lock:
            self._closed_requested = True
            h = self._handle
            if h is not None:
                if self._active_io > 0:
                    try:
                        self._k32.CancelIoEx(h, None)
                    except Exception:
                        pass
                elif self._io_refs <= 0:
                    self._handle = None
                    close_h = h
        if close_h is not None:
            self._k32.CloseHandle(close_h)

    def __enter__(self) -> "PipeClientStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._real_close()
        except Exception:
            pass


def verify_pipe_server_win(
    pipe_handle: Any,
    *,
    expected_user_sid: Optional[str] = None,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> int:
    """Authenticate a connected Named Pipe server via PID + ``TokenUser`` SID.

    Fails closed by raising ``PermissionError`` whenever the server PID, process
    token, or ``TokenUser`` SID cannot be verified or mismatches the current user.
    """
    from mesh.platform import _win32, _win32_ctypes as w32, perms

    if pipe_handle is None:
        raise PermissionError(errno.EACCES, "Missing server pipe handle")
    raw_h = pipe_handle.fileno() if hasattr(pipe_handle, "fileno") else int(pipe_handle)
    if w32.is_null_or_invalid_handle(raw_h):
        raise PermissionError(errno.EACCES, "Invalid server pipe handle")

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    server_pid = wintypes.ULONG(0)
    if not dll_k32.GetNamedPipeServerProcessId(raw_h, ctypes.byref(server_pid)):
        err = int(err_fn())
        raise PermissionError(
            errno.EACCES, f"GetNamedPipeServerProcessId failed (WinError {err})"
        )
    pid = int(server_pid.value)
    if pid <= 0:
        raise PermissionError(errno.EACCES, f"Invalid pipe server pid {pid}")

    if expected_user_sid is None:
        try:
            expected_user_sid, _ = perms.get_current_user_and_owner_sids_win(
                advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
            )
        except PermissionError:
            raise
        except OSError as exc:
            raise PermissionError(
                errno.EACCES, f"Unable to resolve current user SID: {exc}"
            ) from exc

    try:
        server_sid = _win32.get_process_user_sid_win(
            pid,
            k32=dll_k32,
            advapi=dll_adv,
            get_last_error_fn=err_fn,
        )
    except PermissionError:
        raise
    except OSError as exc:
        raise PermissionError(
            errno.EACCES, f"Unable to resolve pipe server pid {pid} SID: {exc}"
        ) from exc

    if not hmac.compare_digest(server_sid, expected_user_sid):
        raise PermissionError(
            errno.EACCES,
            f"Pipe server pid {pid} SID {server_sid} does not match current user SID",
        )
    return pid


def connect_pipe_win(
    pipe_name: str,
    timeout: Optional[float] = 30.0,
    *,
    expected_user_sid: Optional[str] = None,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> PipeClientStream:
    """Connect to Windows Named Pipe *pipe_name* with ``ERROR_PIPE_BUSY`` retry."""
    from mesh.platform import _win32, _win32_ctypes as w32

    if not is_win_pipe_path(pipe_name) or len(pipe_name) > _MAX_PIPE_NAME_CHARS:
        raise ValueError(f"Invalid Windows named pipe path: {pipe_name!r}")

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    deadline: Optional[float] = None
    if timeout is not None:
        tval = float(timeout)
        if tval < 0:
            raise ValueError("Timeout value out of range")
        deadline = time_fn() + tval

    flags = w32.FILE_FLAG_OVERLAPPED | w32.SECURITY_SQOS_PRESENT | w32.SECURITY_IDENTIFICATION
    while True:
        h = dll_k32.CreateFileW(
            pipe_name,
            w32.GENERIC_READ | w32.GENERIC_WRITE,
            0,
            None,
            w32.OPEN_EXISTING,
            flags,
            None,
        )
        if not w32.is_null_or_invalid_handle(h):
            try:
                ftype = int(dll_k32.GetFileType(h))
                if ftype == w32.FILE_TYPE_UNKNOWN:
                    ferr = int(err_fn())
                    if ferr != 0:
                        raise _win32._win_proc_error(ferr)
                if ftype != w32.FILE_TYPE_PIPE:
                    raise OSError(errno.EINVAL, "Endpoint is not a Windows named pipe", pipe_name)
                verify_pipe_server_win(
                    int(h),
                    expected_user_sid=expected_user_sid,
                    k32=dll_k32,
                    advapi=advapi,
                    get_last_error_fn=err_fn,
                )
                return PipeClientStream(
                    int(h),
                    pipe_name,
                    timeout=timeout,
                    k32=dll_k32,
                    get_last_error_fn=err_fn,
                    time_fn=time_fn,
                )
            except BaseException:
                dll_k32.CloseHandle(h)
                raise

        err = int(err_fn())
        if err != w32.ERROR_PIPE_BUSY:
            raise w32.win_error(err, pipe_name)

        if deadline is not None:
            rem = deadline - time_fn()
            if rem <= 0:
                raise socket.timeout(f"Timed out waiting for pipe {pipe_name}")
            wait_ms = max(1, min(_WAIT_SLICE_MS, int(math.ceil(rem * 1000.0))))
        else:
            wait_ms = _WAIT_SLICE_MS

        if not dll_k32.WaitNamedPipeW(pipe_name, wait_ms):
            werr = int(err_fn())
            if werr in (w32.ERROR_SEM_TIMEOUT, w32.ERROR_PIPE_BUSY):
                if deadline is not None and time_fn() >= deadline:
                    raise socket.timeout(f"Timed out waiting for pipe {pipe_name}")
                sleep_fn(0.002)
                continue
            raise w32.win_error(werr, pipe_name)


def connect_endpoint(endpoint: str, timeout: Optional[float] = 30.0) -> Any:
    """Connect to the daemon IPC endpoint (``AF_UNIX`` on POSIX, Named Pipe on Windows)."""
    if sys.platform == "win32":
        if not is_win_pipe_path(endpoint) or len(endpoint) > _MAX_PIPE_NAME_CHARS:
            raise OSError(
                errno.EINVAL, f"Invalid Windows named pipe endpoint: {endpoint!r}", endpoint
            )
        return connect_pipe_win(endpoint, timeout=timeout)
    return _posix.connect_endpoint(endpoint, timeout=timeout)


def probe_daemon_endpoint(home_dir: str, timeout: float = 1.0) -> bool:
    """Return True if a daemon is accepting connections for *home_dir*."""
    if sys.platform != "win32":
        return _posix.probe_daemon_endpoint(home_dir, timeout=timeout)
    from mesh.errors import MeshError

    try:
        endpoint = daemon_endpoint_path(home_dir)
        stream = connect_endpoint(endpoint, timeout=timeout)
    except (OSError, MeshError, ValueError):
        return False
    try:
        stream.close()
    except OSError:
        pass
    return True
