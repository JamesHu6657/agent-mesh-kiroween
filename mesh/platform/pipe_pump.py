"""Cross-platform non-blocking pipe pumping for ``runwrap`` and bounded adapter probes.

On Linux/POSIX, this delegates directly to ``selectors.PollSelector`` (``runwrap``) and
``selectors.DefaultSelector`` + ``os.killpg`` (adapter probes), preserving byte-identical
behavior.

On Windows, Winsock ``select`` fails on pipes (``WinError 10038``) and ``PollSelector``
does not exist. This module pairs an Overlapped Named Pipe server handle
(``CreateNamedPipeW(PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED)``) with a synchronous
inheritable client write handle (``CreateFileW(..., GENERIC_WRITE, ...)``) and drives
reads via ``WaitForMultipleObjects`` + ``CancelIoEx`` + ``GetOverlappedResult``.

Nothing here touches ``_win32_ctypes``, ``_win32``, ``msvcrt``, ``_winapi``, or ``nt``
at import time so importing it on Linux remains cleanly isolated.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import itertools
import math
import os
import secrets
import selectors
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

_PIPE_SEQ = itertools.count(1)
_DEFAULT_BUF_BYTES = 65536
_SYNC_DRAIN_BURST = 64
_PIPE_CONNECT_RETRIES = 8

_WIN_EOF_ERRORS = frozenset({109, 38, 232, 233})  # BROKEN_PIPE, HANDLE_EOF, NO_DATA, PIPE_NOT_CONNECTED
_WIN_CANCEL_ERRORS = frozenset({995})  # ERROR_OPERATION_ABORTED
_CMD_META_CHARS = frozenset(" \t&|<>^()%!,;=")
_CMD_UNSAFE_CHARS = frozenset('"\r\n%')


def _quote_win_cmd_arg(arg: str) -> str:
    """Quote a single argument safely for ``cmd.exe /d /s /c``."""
    if not arg:
        return '""'
    if not any(ch in _CMD_META_CHARS for ch in arg):
        return arg
    # Double trailing backslashes before closing quote so CommandLineToArgvW
    # in the launched CRT target never treats the closing '"' as escaped.
    n_trail = len(arg) - len(arg.rstrip("\\"))
    trail = "\\" * n_trail
    return f'"{arg}{trail}"'


def prepare_win_cmd_args(
    argv: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    """Wrap ``.cmd``/``.bat`` invocations in ``cmd.exe /d /s /c`` on Windows.

    Raw ``.exe`` binaries are returned unchanged as ``list(argv)`` so they bypass
    ``cmd.exe`` completely. Arguments containing ``"``, ``%``, CR, or LF for
    ``.cmd``/``.bat`` targets are rejected with ``ValueError`` because ``cmd.exe``
    expands ``%VAR%`` inside double quotes and cannot safely quote embedded
    quotes or newlines through batch ``%*`` forwarding.
    """
    args = list(argv)
    if not args:
        raise ValueError("Command argv must not be empty")
    exe = args[0]
    if not isinstance(exe, str):
        return args
    if any("\x00" in a for a in args if isinstance(a, str)):
        raise ValueError("NUL byte in command argument")
    if not exe.lower().endswith((".cmd", ".bat")):
        return args
    for a in args:
        if not isinstance(a, str):
            raise TypeError(f"Command argument must be str, got {type(a).__name__}")
        if any(ch in _CMD_UNSAFE_CHARS for ch in a):
            raise ValueError(
                "Unsafe argument for .cmd/.bat target: embedded quotes, '%', CR, and LF are forbidden"
            )
    environ = env if env is not None else os.environ
    comspec = environ.get("COMSPEC")
    if not comspec:
        sys_root = environ.get("SystemRoot") or r"C:\Windows"
        comspec = os.path.join(sys_root, "System32", "cmd.exe")
    inner = " ".join(_quote_win_cmd_arg(a) for a in args)
    return f'"{comspec}" /d /s /c "{inner}"'


def _verify_win_cmd_target_exists(exe: str) -> None:
    """Match CreateProcess FileNotFoundError/PermissionError before cmd.exe wrapping."""
    if not isinstance(exe, str) or not exe.lower().endswith((".cmd", ".bat")):
        return
    has_sep = ("\\" in exe) or ("/" in exe) or (len(exe) >= 2 and exe[1] == ":")
    if not has_sep:
        return
    if os.path.isdir(exe):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), exe)
    if not os.path.isfile(exe):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), exe)


def _as_handle_int(handle: Any) -> int:
    val = handle.value if hasattr(handle, "value") else handle
    return int(val)


def _timeout_ms(remaining_s: float) -> int:
    if remaining_s <= 0:
        return 0
    ms = math.ceil(remaining_s * 1000.0)
    return min(0xFFFFFFFE, max(1, ms))


class OverlappedPipeReader:
    """Thread-safe RAII reader for an overlapped Win32 Named Pipe server handle.

    Keeps the ``OVERLAPPED`` struct and read buffer pinned on the instance until any
    in-flight IRP is completed or retired via ``CancelIoEx`` + ``GetOverlappedResult``.
    """

    def __init__(
        self,
        pipe_handle: Any,
        read_event: Any,
        *,
        chunk_size: int = _DEFAULT_BUF_BYTES,
        k32: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        from mesh.platform import _win32_ctypes as w32

        if w32.is_null_or_invalid_handle(pipe_handle):
            raise ValueError(f"Invalid Win32 pipe handle: {pipe_handle!r}")
        if w32.is_null_or_invalid_handle(read_event):
            raise ValueError(f"Invalid Win32 event handle: {read_event!r}")

        self._lock = threading.Lock()
        self._h_pipe: Optional[int] = _as_handle_int(pipe_handle)
        self._h_event: Optional[int] = _as_handle_int(read_event)
        self._k32 = k32 if k32 is not None else w32.get_kernel32()
        self._err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
        self._chunk_size = max(1, int(chunk_size))
        self._buf = ctypes.create_string_buffer(self._chunk_size)
        self._ov = w32.OVERLAPPED()
        self._pending = False
        self._eof = False

    @property
    def read_event_handle(self) -> int:
        """Raw Win32 manual-reset event handle signaled when ``ReadFile`` completes."""
        if self._h_event is None:
            raise OSError(errno.EBADF, "OverlappedPipeReader is closed")
        return int(self._h_event)

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def pending(self) -> bool:
        return self._pending

    @property
    def closed(self) -> bool:
        return self._h_pipe is None

    def fileno(self) -> int:
        if self._h_pipe is None:
            raise OSError(errno.EBADF, "OverlappedPipeReader is closed")
        return int(self._h_pipe)

    def _reset_overlapped(self) -> None:
        self._ov.Internal = 0
        self._ov.InternalHigh = 0
        self._ov.DUMMYUNIONNAME.DUMMYSTRUCTNAME.Offset = 0
        self._ov.DUMMYUNIONNAME.DUMMYSTRUCTNAME.OffsetHigh = 0
        self._ov.hEvent = self._h_event

    def start_read(self) -> Tuple[str, bytes]:
        """Issue one overlapped ``ReadFile``; return ``(status, data)``.

        ``status`` is one of ``"data"``, ``"pending"``, or ``"eof"``.
        """
        from mesh.platform import _win32_ctypes as w32

        if self._h_pipe is None or self._h_event is None:
            raise OSError(errno.EBADF, "OverlappedPipeReader is closed")
        if self._eof:
            return "eof", b""
        if self._pending:
            return "pending", b""

        if not self._k32.ResetEvent(self._h_event):
            raise w32.win_error(self._err_fn())
        self._reset_overlapped()
        nread = wintypes.DWORD(0)
        ok = self._k32.ReadFile(
            self._h_pipe,
            self._buf,
            self._chunk_size,
            ctypes.byref(nread),
            ctypes.byref(self._ov),
        )
        if ok:
            count = int(nread.value)
            if count > 0:
                return "data", self._buf.raw[:count]
            self._eof = True
            return "eof", b""

        err = int(self._err_fn())
        if err == w32.ERROR_IO_PENDING:
            self._pending = True
            return "pending", b""
        if err in _WIN_EOF_ERRORS:
            self._eof = True
            return "eof", b""
        self._eof = True
        raise w32.win_error(err)

    def finish_read(self, *, wait: bool = False) -> Tuple[str, bytes]:
        """Collect completion of a pending ``ReadFile`` via ``GetOverlappedResult``."""
        from mesh.platform import _win32_ctypes as w32

        if self._h_pipe is None:
            raise OSError(errno.EBADF, "OverlappedPipeReader is closed")
        if not self._pending:
            return ("eof", b"") if self._eof else ("idle", b"")

        nread = wintypes.DWORD(0)
        ok = self._k32.GetOverlappedResult(
            self._h_pipe,
            ctypes.byref(self._ov),
            ctypes.byref(nread),
            bool(wait),
        )
        if ok:
            self._pending = False
            count = int(nread.value)
            if count > 0:
                return "data", self._buf.raw[:count]
            self._eof = True
            return "eof", b""

        err = int(self._err_fn())
        if not wait and err == w32.ERROR_IO_INCOMPLETE:
            return "pending", b""
        self._pending = False
        if err in _WIN_EOF_ERRORS or err in _WIN_CANCEL_ERRORS:
            self._eof = True
            return "eof", b""
        self._eof = True
        raise w32.win_error(err)

    def cancel_and_drain(self) -> bytes:
        """Cancel any pending overlapped ``ReadFile``, retire the IRP, and return raced bytes."""
        if self._h_pipe is None or not self._pending:
            self._eof = True
            return b""
        self._k32.CancelIoEx(self._h_pipe, ctypes.byref(self._ov))
        nread = wintypes.DWORD(0)
        ok = self._k32.GetOverlappedResult(
            self._h_pipe,
            ctypes.byref(self._ov),
            ctypes.byref(nread),
            True,
        )
        self._pending = False
        self._eof = True
        if ok and int(nread.value) > 0:
            return self._buf.raw[: int(nread.value)]
        return b""

    def close(self) -> None:
        """Retire any pending I/O and close pipe and event handles (idempotent)."""
        with self._lock:
            h_pipe = self._h_pipe
            h_event = self._h_event
            if h_pipe is None and h_event is None:
                return
            if h_pipe is not None and self._pending:
                try:
                    self.cancel_and_drain()
                except OSError:
                    self._pending = False
                    self._eof = True
            self._h_pipe = None
            self._h_event = None
        if h_pipe is not None:
            try:
                self._k32.CloseHandle(h_pipe)
            except Exception:
                pass
        if h_event is not None:
            try:
                self._k32.CloseHandle(h_event)
            except Exception:
                pass

    def __enter__(self) -> "OverlappedPipeReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _close_handle_safe(k32: Any, handle: Any) -> None:
    from mesh.platform import _win32_ctypes as w32

    if not w32.is_null_or_invalid_handle(handle):
        try:
            k32.CloseHandle(_as_handle_int(handle))
        except Exception:
            pass


def create_pipe_pair(
    buf_size: int = _DEFAULT_BUF_BYTES,
    *,
    k32: Any = None,
    advapi: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
) -> Tuple[OverlappedPipeReader, int]:
    """Create a Win32 ``(OverlappedPipeReader, write_fd)`` pair with owner-only DACL."""
    from mesh.platform import _win32, _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    set_err = set_last_error_fn if set_last_error_fn is not None else w32.set_last_error
    if msvcrt_mod is None:
        import msvcrt as msvcrt_mod  # lazy Windows-only import

    h_read: Any = None
    h_write: Any = None
    connect_ev: Any = None
    read_ev: Any = None
    write_fd: Optional[int] = None
    pipe_name = ""
    connect_ov: Any = None
    connect_pending = False

    try:
        with _win32._private_security_attributes(
            advapi=dll_adv, k32=dll_k32, err_fn=err_fn, inherit=False
        ) as sa_ptr:
            open_mode = (
                w32.PIPE_ACCESS_INBOUND
                | w32.FILE_FLAG_OVERLAPPED
                | w32.FILE_FLAG_FIRST_PIPE_INSTANCE
            )
            pipe_mode = (
                w32.PIPE_TYPE_BYTE
                | w32.PIPE_READMODE_BYTE
                | w32.PIPE_WAIT
                | w32.PIPE_REJECT_REMOTE_CLIENTS
            )
            last_err = 0
            for _attempt in range(_PIPE_CONNECT_RETRIES):
                seq = next(_PIPE_SEQ)
                pipe_name = (
                    f"\\\\.\\pipe\\mesh-pump-{os.getpid()}-"
                    f"{threading.get_ident()}-{seq}-{secrets.token_hex(4)}"
                )
                set_err(0)
                cand = dll_k32.CreateNamedPipeW(
                    pipe_name,
                    open_mode,
                    pipe_mode,
                    1,
                    buf_size,
                    buf_size,
                    0,
                    sa_ptr,
                )
                if not w32.is_null_or_invalid_handle(cand):
                    h_read = cand
                    break
                last_err = int(err_fn())
                if last_err not in (w32.ERROR_ACCESS_DENIED, w32.ERROR_PIPE_BUSY):
                    raise w32.win_error(last_err, pipe_name)
            if h_read is None:
                raise w32.win_error(last_err or w32.ERROR_ACCESS_DENIED, pipe_name)

            connect_ev = dll_k32.CreateEventW(None, True, False, None)
            if w32.is_null_or_invalid_handle(connect_ev):
                raise w32.win_error(err_fn(), pipe_name)

            connect_ov = w32.OVERLAPPED()
            connect_ov.hEvent = _as_handle_int(connect_ev)
            set_err(0)
            connected = dll_k32.ConnectNamedPipe(h_read, ctypes.byref(connect_ov))
            if not connected:
                c_err = int(err_fn())
                if c_err == w32.ERROR_IO_PENDING:
                    connect_pending = True
                elif c_err != w32.ERROR_PIPE_CONNECTED:
                    raise w32.win_error(c_err, pipe_name)

            h_write = dll_k32.CreateFileW(
                pipe_name,
                w32.GENERIC_WRITE,
                0,
                sa_ptr,
                w32.OPEN_EXISTING,
                w32.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if w32.is_null_or_invalid_handle(h_write):
                raise w32.win_error(err_fn(), pipe_name)

            if connect_pending:
                nbytes = wintypes.DWORD(0)
                ok = dll_k32.GetOverlappedResult(
                    h_read, ctypes.byref(connect_ov), ctypes.byref(nbytes), True
                )
                connect_pending = False
                if not ok:
                    raise w32.win_error(err_fn(), pipe_name)

        _close_handle_safe(dll_k32, connect_ev)
        connect_ev = None

        read_ev = dll_k32.CreateEventW(None, True, False, None)
        if w32.is_null_or_invalid_handle(read_ev):
            raise w32.win_error(err_fn(), pipe_name)

        osf_flags = (
            os.O_WRONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0)
        )
        write_fd = int(msvcrt_mod.open_osfhandle(_as_handle_int(h_write), osf_flags))
        h_write = None  # owned by write_fd now

        reader = OverlappedPipeReader(
            h_read,
            read_ev,
            chunk_size=buf_size,
            k32=dll_k32,
            get_last_error_fn=err_fn,
        )
        h_read = None
        read_ev = None
        return reader, write_fd
    except BaseException:
        if connect_pending and connect_ov is not None and not w32.is_null_or_invalid_handle(h_read):
            # Retire any pending ConnectNamedPipe IRP before closing h_read / freeing connect_ov.
            try:
                dll_k32.CancelIoEx(_as_handle_int(h_read), ctypes.byref(connect_ov))
            except Exception:
                pass
            try:
                nbytes = wintypes.DWORD(0)
                dll_k32.GetOverlappedResult(
                    _as_handle_int(h_read), ctypes.byref(connect_ov), ctypes.byref(nbytes), True
                )
            except Exception:
                pass
        if write_fd is not None:
            try:
                os.close(write_fd)
            except OSError:
                pass
        _close_handle_safe(dll_k32, h_write)
        _close_handle_safe(dll_k32, read_ev)
        _close_handle_safe(dll_k32, connect_ev)
        _close_handle_safe(dll_k32, h_read)
        raise


def create_wake_event(
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Create an anonymous manual-reset Win32 ``EventHandle`` for pump wakeups."""
    from mesh.platform import _win32, _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    h_ev = dll_k32.CreateEventW(None, True, False, None)
    if w32.is_null_or_invalid_handle(h_ev):
        raise w32.win_error(err_fn())
    return _win32.EventHandle(h_ev, name=None, k32=dll_k32, get_last_error_fn=err_fn)


def pump_fd_stream_posix(
    pipe_fd: int,
    on_chunk: Callable[[bytes], None],
    *,
    drain_wake_fd: Optional[int] = None,
    drain_deadline: Optional[List[Optional[float]]] = None,
    chunk_size: int = _DEFAULT_BUF_BYTES,
) -> None:
    """POSIX ``selectors.PollSelector`` pump for ``runwrap._pump_stream``."""

    def past_deadline() -> bool:
        dl = drain_deadline[0] if drain_deadline else None
        return dl is not None and time.monotonic() >= dl

    sel = None
    try:
        os.set_blocking(pipe_fd, False)
        sel = selectors.PollSelector()
        sel.register(pipe_fd, selectors.EVENT_READ, data="pipe")
        wake_watched = False
        if drain_wake_fd is not None:
            sel.register(drain_wake_fd, selectors.EVENT_READ, data="wake")
            wake_watched = True

        eof = False
        while not eof:
            dl = drain_deadline[0] if drain_deadline else None
            if dl is not None:
                if wake_watched:
                    try:
                        sel.unregister(drain_wake_fd)
                    except (KeyError, ValueError):
                        pass
                    wake_watched = False
                remaining = dl - time.monotonic()
                if remaining <= 0:
                    break
                timeout: Optional[float] = remaining
            else:
                timeout = None

            events = sel.select(timeout)
            if not events:
                break
            pipe_ready = False
            for key, _ in events:
                if key.data == "wake":
                    if wake_watched:
                        try:
                            sel.unregister(drain_wake_fd)
                        except (KeyError, ValueError):
                            pass
                        wake_watched = False
                elif key.data == "pipe":
                    pipe_ready = True
            if not pipe_ready:
                continue
            while True:
                try:
                    chunk = os.read(pipe_fd, chunk_size)
                except BlockingIOError:
                    break
                except InterruptedError:
                    continue
                except OSError:
                    eof = True
                    break
                if not chunk:
                    eof = True
                    break
                on_chunk(chunk)
                if past_deadline():
                    eof = True
                    break
    finally:
        if sel is not None:
            try:
                sel.close()
            except OSError:
                pass


def pump_overlapped_stream_win(
    reader: OverlappedPipeReader,
    on_chunk: Callable[[bytes], None],
    *,
    drain_wake_event: Any = None,
    drain_deadline: Optional[List[Optional[float]]] = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Drive *reader* using overlapped ``ReadFile`` + ``WaitForMultipleObjects``."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else reader._k32
    err_fn = get_last_error_fn if get_last_error_fn is not None else reader._err_fn
    wake_h: Optional[int] = None
    if drain_wake_event is not None:
        wake_h = (
            int(drain_wake_event.fileno())
            if hasattr(drain_wake_event, "fileno")
            else int(drain_wake_event)
        )

    def past_deadline() -> bool:
        dl = drain_deadline[0] if drain_deadline else None
        return dl is not None and monotonic_fn() >= dl

    wake_seen = False
    try:
        while not reader.eof:
            if past_deadline():
                break

            if not reader.pending:
                burst = 0
                eof_or_pending = False
                while burst < _SYNC_DRAIN_BURST:
                    try:
                        status, data = reader.start_read()
                    except OSError:
                        eof_or_pending = True
                        break
                    if status == "data":
                        on_chunk(data)
                        burst += 1
                        if past_deadline():
                            eof_or_pending = True
                            break
                        continue
                    if status == "eof":
                        eof_or_pending = True
                    break
                if eof_or_pending or reader.eof or past_deadline():
                    break
                if not reader.pending:
                    continue

            dl = drain_deadline[0] if drain_deadline else None
            if dl is not None:
                remaining = dl - monotonic_fn()
                if remaining <= 0:
                    break
                wait_handles = [reader.read_event_handle]
                wait_ms = _timeout_ms(remaining)
                wake_seen = True
            else:
                wait_handles = [reader.read_event_handle]
                if wake_h is not None and not wake_seen:
                    wait_handles.append(wake_h)
                wait_ms = w32.INFINITE

            arr = (wintypes.HANDLE * len(wait_handles))(*wait_handles)
            rc = int(dll_k32.WaitForMultipleObjects(len(wait_handles), arr, False, wait_ms))
            if rc == w32.WAIT_TIMEOUT:
                break
            if rc == w32.WAIT_FAILED:
                _ = err_fn()
                break
            if rc == w32.WAIT_OBJECT_0:
                try:
                    status, data = reader.finish_read(wait=False)
                except OSError:
                    break
                if status == "data":
                    on_chunk(data)
                elif status == "eof":
                    break
            elif len(wait_handles) == 2 and rc == w32.WAIT_OBJECT_0 + 1:
                wake_seen = True
            else:
                break
    finally:
        if reader.pending:
            try:
                trailing = reader.cancel_and_drain()
                if trailing:
                    on_chunk(trailing)
            except OSError:
                pass
        reader.close()


def _wait_and_close_failed_probe_posix(proc: Any, kill_drain_s: float) -> None:
    from mesh.daemon import children

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=kill_drain_s)
    except Exception:
        pass
    children.unhold(proc.pid)


def run_bounded_group_posix(
    argv: List[str],
    timeout_s: float,
    max_output_bytes: int,
    env: Dict[str, str],
    *,
    kill_drain_s: float = 1.0,
) -> Tuple[bytes, bytes, int]:
    """POSIX ``selectors.DefaultSelector`` + ``os.killpg`` bounded probe runner."""
    import subprocess
    from mesh.daemon import children

    try:
        proc = children.popen_registered(
            "probe",
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
            start_new_session=True,
        )
    except children.SpawnedThenFailed as exc:
        _wait_and_close_failed_probe_posix(exc.proc, kill_drain_s)
        raise

    bufs = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    sel = selectors.DefaultSelector()
    for fd in bufs:
        os.set_blocking(fd, False)
        sel.register(fd, selectors.EVENT_READ)
    timed_out = False
    deadline = time.monotonic() + timeout_s
    drain_until: Optional[float] = None
    try:
        while True:
            now = time.monotonic()
            exited = proc.poll() is not None
            if exited and drain_until is None:
                drain_until = now + kill_drain_s
            if exited and not sel.get_map():
                break
            if drain_until is not None and now >= drain_until:
                break
            if drain_until is None and now >= deadline:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                drain_until = now + kill_drain_s
                continue
            limit = drain_until if drain_until is not None else deadline
            wait = max(0.0, min(0.05, limit - now))
            if not sel.get_map():
                time.sleep(wait)
                continue
            for key, _ in sel.select(timeout=wait):
                fd = key.fd
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(fd)
                    continue
                room = max_output_bytes - len(bufs[fd])
                if room > 0:
                    bufs[fd].extend(chunk[:room])
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=kill_drain_s)
        except subprocess.TimeoutExpired:
            pass
    finally:
        sel.close()
        out_b = bytes(bufs[proc.stdout.fileno()])
        err_b = bytes(bufs[proc.stderr.fileno()])
        for f in (proc.stdout, proc.stderr):
            try:
                f.close()
            except OSError:
                pass
        children.unregister(proc.pid)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout_s)
    return out_b, err_b, proc.returncode if proc.returncode is not None else -1


def _append_capped(buf: bytearray, chunk: bytes, max_output_bytes: int) -> None:
    if not chunk:
        return
    room = max_output_bytes - len(buf)
    if room > 0:
        buf.extend(chunk[:room])


def _kill_probe_win(proc: Any) -> None:
    from mesh.platform import proc as _plat_proc

    job = getattr(proc, "_mesh_job", None)
    if job is not None:
        try:
            _plat_proc.terminate_job(job, 137)
        except OSError:
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def _drain_ready_win(
    reader: OverlappedPipeReader,
    buf: bytearray,
    max_output_bytes: int,
    limit: float,
    monotonic_fn: Callable[[], float],
) -> None:
    burst = 0
    while not reader.eof and not reader.pending and burst < _SYNC_DRAIN_BURST:
        if monotonic_fn() >= limit:
            break
        try:
            status, data = reader.start_read()
        except OSError:
            break
        if status == "data":
            _append_capped(buf, data, max_output_bytes)
            burst += 1
            continue
        break


def run_bounded_group_win(
    argv: List[str],
    timeout_s: float,
    max_output_bytes: int,
    env: Dict[str, str],
    *,
    kill_drain_s: float = 1.0,
    k32: Any = None,
    advapi: Any = None,
    msvcrt_mod: Any = None,
    popen_reg_fn: Optional[Callable[..., Any]] = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> Tuple[bytes, bytes, int]:
    """Windows Job Object + Overlapped Named Pipe bounded probe runner."""
    import subprocess
    from mesh.daemon import children
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    if popen_reg_fn is None:
        _verify_win_cmd_target_exists(argv[0])
    spawn_args = prepare_win_cmd_args(argv, env=env)
    spawn_fn = popen_reg_fn if popen_reg_fn is not None else children.popen_registered

    out_reader, out_w_fd = create_pipe_pair(
        k32=dll_k32,
        advapi=advapi,
        msvcrt_mod=msvcrt_mod,
        get_last_error_fn=err_fn,
        set_last_error_fn=set_last_error_fn,
    )
    try:
        err_reader, err_w_fd = create_pipe_pair(
            k32=dll_k32,
            advapi=advapi,
            msvcrt_mod=msvcrt_mod,
            get_last_error_fn=err_fn,
            set_last_error_fn=set_last_error_fn,
        )
    except BaseException:
        try:
            os.close(out_w_fd)
        except OSError:
            pass
        out_reader.close()
        raise

    proc: Any = None
    try:
        try:
            proc = spawn_fn(
                "probe",
                spawn_args,
                stdin=subprocess.DEVNULL,
                stdout=out_w_fd,
                stderr=err_w_fd,
                env=env,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except children.SpawnedThenFailed as exc:
            for wfd in (out_w_fd, err_w_fd):
                try:
                    os.close(wfd)
                except OSError:
                    pass
            _kill_probe_win(exc.proc)
            try:
                exc.proc.wait(timeout=kill_drain_s)
            except Exception:
                pass
            children._close_owned_proc_job(exc.proc)
            children.unhold(exc.proc.pid)
            raise
        finally:
            for wfd in (out_w_fd, err_w_fd):
                try:
                    os.close(wfd)
                except OSError:
                    pass
    except BaseException:
        out_reader.close()
        err_reader.close()
        raise

    out_buf = bytearray()
    err_buf = bytearray()
    timed_out = False
    exited = False
    deadline = monotonic_fn() + timeout_s
    drain_until: Optional[float] = None
    proc_handle_raw = getattr(proc, "_handle", None)
    proc_handle = _as_handle_int(proc_handle_raw) if proc_handle_raw is not None else None

    try:
        while True:
            if not exited and proc.poll() is not None:
                exited = True
                if drain_until is None:
                    drain_until = monotonic_fn() + kill_drain_s

            if exited and out_reader.eof and err_reader.eof:
                break

            now = monotonic_fn()
            if drain_until is not None and now >= drain_until:
                break
            if drain_until is None and now >= deadline:
                timed_out = True
                _kill_probe_win(proc)
                drain_until = now + kill_drain_s
                continue

            limit = drain_until if drain_until is not None else deadline
            _drain_ready_win(out_reader, out_buf, max_output_bytes, limit, monotonic_fn)
            _drain_ready_win(err_reader, err_buf, max_output_bytes, limit, monotonic_fn)

            if not exited and proc.poll() is not None:
                exited = True
                if drain_until is None:
                    drain_until = monotonic_fn() + kill_drain_s
            if exited and out_reader.eof and err_reader.eof:
                break

            now = monotonic_fn()
            limit = drain_until if drain_until is not None else deadline
            if now >= limit:
                continue
            if (not out_reader.eof and not out_reader.pending) or (not err_reader.eof and not err_reader.pending):
                continue

            wait_handles: List[int] = []
            tags: List[str] = []
            if not out_reader.eof and out_reader.pending:
                wait_handles.append(out_reader.read_event_handle)
                tags.append("out")
            if not err_reader.eof and err_reader.pending:
                wait_handles.append(err_reader.read_event_handle)
                tags.append("err")
            if not exited and proc_handle is not None:
                wait_handles.append(proc_handle)
                tags.append("proc")

            if not wait_handles:
                time.sleep(min(0.02, max(0.0, limit - now)))
                continue

            wait_ms = _timeout_ms(limit - now)
            arr = (wintypes.HANDLE * len(wait_handles))(*wait_handles)
            rc = int(dll_k32.WaitForMultipleObjects(len(wait_handles), arr, False, wait_ms))
            if rc == w32.WAIT_TIMEOUT:
                continue
            if rc == w32.WAIT_FAILED:
                _ = err_fn()
                break
            idx = rc - w32.WAIT_OBJECT_0
            if 0 <= idx < len(tags):
                tag = tags[idx]
                if tag == "out":
                    try:
                        status, data = out_reader.finish_read(wait=False)
                        if status == "data":
                            _append_capped(out_buf, data, max_output_bytes)
                    except OSError:
                        pass
                elif tag == "err":
                    try:
                        status, data = err_reader.finish_read(wait=False)
                        if status == "data":
                            _append_capped(err_buf, data, max_output_bytes)
                    except OSError:
                        pass
                # Also poll the other pending stream non-blockingly for round-robin fairness.
                for other_tag, other_reader, other_buf in (
                    ("out", out_reader, out_buf),
                    ("err", err_reader, err_buf),
                ):
                    if other_tag != tag and other_reader.pending and not other_reader.eof:
                        try:
                            st2, d2 = other_reader.finish_read(wait=False)
                            if st2 == "data":
                                _append_capped(other_buf, d2, max_output_bytes)
                        except OSError:
                            pass
            else:
                break

        _kill_probe_win(proc)
        for reader, buf in ((out_reader, out_buf), (err_reader, err_buf)):
            if reader.pending:
                try:
                    _append_capped(buf, reader.cancel_and_drain(), max_output_bytes)
                except OSError:
                    pass
        try:
            proc.wait(timeout=kill_drain_s)
        except subprocess.TimeoutExpired:
            pass
    finally:
        out_reader.close()
        err_reader.close()
        children._close_owned_proc_job(proc)
        children.unregister(proc.pid)

    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout_s)
    return bytes(out_buf), bytes(err_buf), proc.returncode if proc.returncode is not None else -1


def run_bounded_group(
    argv: List[str],
    timeout_s: float,
    max_output_bytes: int,
    env: Dict[str, str],
    *,
    kill_drain_s: float = 1.0,
) -> Tuple[bytes, bytes, int]:
    """Run a bounded probe subprocess in its own group/job and drain stdout/stderr."""
    if sys.platform == "win32":
        return run_bounded_group_win(
            argv, timeout_s, max_output_bytes, env, kill_drain_s=kill_drain_s
        )
    return run_bounded_group_posix(
        argv, timeout_s, max_output_bytes, env, kill_drain_s=kill_drain_s
    )
