"""Native Windows process identity, liveness, Toolhelp ancestry, and PEB env readers.

Nothing in this module touches ``ctypes.windll``, ``msvcrt``, ``_winapi``, or
``nt`` at import time so importing it on Linux for pure-logic/mock testing is safe.
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
import errno
import hashlib
import signal
import sys
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set

from mesh.platform import _win32_ctypes as w32

# Safety bound for remote PEB environment reads (4 MiB) and UNICODE_STRING cmdline (32,767 UTF-16 chars).
_MAX_ENV_BYTES = 4 * 1024 * 1024
_MAX_CMDLINE_BYTES = 65534
_ENV_PAGE_SIZE = 4096
_TOOLHELP_RETRIES = 3

_WOW64_32BIT_MACHINES = frozenset(
    {
        w32.IMAGE_FILE_MACHINE_I386,
        w32.IMAGE_FILE_MACHINE_ARM,
        w32.IMAGE_FILE_MACHINE_THUMB,
        w32.IMAGE_FILE_MACHINE_ARMNT,
    }
)


class _NullPebError(OSError):
    """Internal marker for kernel/minimal processes with ``PebBaseAddress == 0``."""

    _mesh_null_peb = True


def _win_proc_error(code: int) -> OSError:
    """Build an ``OSError`` (or ``PermissionError`` / ``FileExistsError``) from Win32 *code*."""
    format_error = getattr(ctypes, "FormatError", None)
    descr = format_error(code).strip() if format_error is not None else f"WinError {code}"
    if code == w32.ERROR_ACCESS_DENIED:
        if sys.platform == "win32":
            return PermissionError(errno.EACCES, descr, None, code)
        exc = PermissionError(errno.EACCES, descr)
        exc.winerror = code  # type: ignore[attr-defined]
        return exc
    if code in (w32.ERROR_ALREADY_EXISTS, w32.ERROR_FILE_EXISTS):
        if sys.platform == "win32":
            return FileExistsError(errno.EEXIST, descr, None, code)
        exc_e = FileExistsError(errno.EEXIST, descr)
        exc_e.winerror = code  # type: ignore[attr-defined]
        return exc_e
    return w32.win_error(code)


def _ntstatus_to_win_error(status: int, *, dll_nt: Any) -> OSError:
    """Convert a failing ``NTSTATUS`` into a Win32 ``OSError`` / ``PermissionError``."""
    dos_err = int(dll_nt.RtlNtStatusToDosError(int(status)))
    if dos_err == 0:
        if int(status) == w32.STATUS_ACCESS_DENIED:
            dos_err = w32.ERROR_ACCESS_DENIED
        else:
            dos_err = w32.ERROR_INVALID_PARAMETER
    return _win_proc_error(dos_err)


def _is_handle_alive_win(
    handle: Any,
    *,
    k32: Any,
    err_fn: Callable[[], int],
) -> bool:
    """Return True iff open process *handle* is still alive.

    Combines ``WaitForSingleObject(h, 0)`` with ``GetExitCodeProcess`` so a dead
    process that exited with code 259 (``STILL_ACTIVE``) is never misread as alive.
    """
    wait_rc = int(k32.WaitForSingleObject(handle, 0))
    if wait_rc == w32.WAIT_OBJECT_0:
        return False
    if wait_rc == w32.WAIT_FAILED:
        raise _win_proc_error(err_fn())
    if wait_rc != w32.WAIT_TIMEOUT:
        raise OSError(errno.EIO, f"WaitForSingleObject returned unexpected status {wait_rc:#x}")

    exit_code = wintypes.DWORD(0)
    if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
        raise _win_proc_error(err_fn())
    if int(exit_code.value) != w32.STILL_ACTIVE:
        return False
    return True


def is_popen_exited_unreaped_win(
    proc: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True once ``proc`` has exited on Win32, without closing ``proc._handle``.

    Matches POSIX ``os.waitid(..., WNOHANG | WNOWAIT)`` semantics: keeping ``proc._handle``
    open preserves kernel PID reservation until the caller explicitly reaps it.
    """
    if getattr(proc, "returncode", None) is not None:
        return True
    handle = getattr(proc, "_handle", None)
    if handle is None or getattr(handle, "closed", False):
        return True
    try:
        h_val = int(handle)
    except (TypeError, ValueError):
        h_val = handle
    if not h_val:
        return True

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    wait_rc = int(dll_k32.WaitForSingleObject(h_val, 0))
    if wait_rc == w32.WAIT_OBJECT_0:
        return True
    if wait_rc == w32.WAIT_TIMEOUT:
        return False
    if wait_rc == w32.WAIT_FAILED:
        code = int(err_fn())
        if code == 6:  # ERROR_INVALID_HANDLE: already closed/reaped
            return True
        raise _win_proc_error(code)
    raise OSError(errno.EIO, f"WaitForSingleObject returned unexpected status {wait_rc:#x}")


def _get_handle_creation_time_win(
    handle: Any,
    *,
    k32: Any,
    err_fn: Callable[[], int],
) -> int:
    """Read 64-bit ``CreationTime`` (100-ns intervals since 1601-01-01) from *handle*."""
    creation_ft = w32.FILETIME()
    exit_ft = w32.FILETIME()
    kernel_ft = w32.FILETIME()
    user_ft = w32.FILETIME()
    ok = k32.GetProcessTimes(
        handle,
        ctypes.byref(creation_ft),
        ctypes.byref(exit_ft),
        ctypes.byref(kernel_ft),
        ctypes.byref(user_ft),
    )
    if not ok:
        raise _win_proc_error(err_fn())
    creation_time = (int(creation_ft.dwHighDateTime) << 32) | int(creation_ft.dwLowDateTime)
    if creation_time <= 0:
        raise OSError(errno.EIO, f"Invalid GetProcessTimes CreationTime {creation_time}")
    return creation_time


def _open_process_or_none_win(
    pid: int,
    access: int,
    *,
    k32: Any,
    err_fn: Callable[[], int],
) -> Any:
    """Open *pid* with *access*, returning ``None`` strictly when *pid* does not exist."""
    if int(pid) <= 0:
        return None
    handle = k32.OpenProcess(access, False, int(pid))
    if w32.is_null_or_invalid_handle(handle):
        err = err_fn()
        if err == w32.ERROR_INVALID_PARAMETER:
            return None
        raise _win_proc_error(err)
    return handle


def _get_pid_creation_time_or_none_win(
    pid: int,
    *,
    k32: Any,
    err_fn: Callable[[], int],
) -> Optional[int]:
    """Return live process ``CreationTime`` for *pid*, ``None`` if gone, or raise on error."""
    handle = _open_process_or_none_win(
        pid,
        w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE,
        k32=k32,
        err_fn=err_fn,
    )
    if handle is None:
        return None
    try:
        if not _is_handle_alive_win(handle, k32=k32, err_fn=err_fn):
            return None
        creation_time = _get_handle_creation_time_win(handle, k32=k32, err_fn=err_fn)
        if not _is_handle_alive_win(handle, k32=k32, err_fn=err_fn):
            return None
        return creation_time
    finally:
        k32.CloseHandle(handle)


def get_proc_stat_win(
    pid: int,
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Dict[str, Any]]:
    """Return ``{state, ppid, pgrp, session, num_threads, starttime}`` or ``None`` if gone.

    ``starttime`` is the 64-bit Win32 ``FILETIME`` ``CreationTime`` (100-ns units > 0).
    Raises ``PermissionError`` / ``OSError`` if the process cannot be queried.
    """
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    handle = _open_process_or_none_win(
        pid,
        w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if handle is None:
        return None
    try:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        starttime = _get_handle_creation_time_win(handle, k32=dll_k32, err_fn=err_fn)

        pbi = w32.PROCESS_BASIC_INFORMATION()
        ret_len = ctypes.c_uint32(0)
        status = int(
            dll_nt.NtQueryInformationProcess(
                handle,
                w32.ProcessBasicInformation,
                ctypes.byref(pbi),
                ctypes.sizeof(pbi),
                ctypes.byref(ret_len),
            )
        )
        if status < 0:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise _ntstatus_to_win_error(status, dll_nt=dll_nt)

        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None

        return {
            "state": "R",
            "ppid": int(pbi.InheritedFromUniqueProcessId),
            "pgrp": 0,
            "session": 0,
            "num_threads": 1,
            "starttime": starttime,
        }
    finally:
        dll_k32.CloseHandle(handle)


def is_process_alive_win(
    pid: int,
    expected_starttime: Optional[int] = None,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True if *pid* is live and (when given) matches *expected_starttime*.

    Pins the kernel ``EPROCESS`` via ``OpenProcess`` before checking liveness and
    ``CreationTime``. Fails closed: invalid/zero ``expected_starttime`` returns False;
    ``ERROR_ACCESS_DENIED`` or query errors raise ``OSError``.
    """
    exp_st: Optional[int] = None
    if expected_starttime is not None:
        try:
            exp_st = int(expected_starttime)
        except (TypeError, ValueError):
            return False
        if exp_st <= 0:
            return False

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    handle = _open_process_or_none_win(
        pid,
        w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if handle is None:
        return False
    try:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return False
        if exp_st is not None:
            actual_st = _get_handle_creation_time_win(handle, k32=dll_k32, err_fn=err_fn)
            if actual_st != exp_st:
                return False
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return False
        return True
    finally:
        dll_k32.CloseHandle(handle)


def is_tgid_alive_win(
    pid: int,
    info: Optional[Dict[str, Any]] = None,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True while *pid* is alive on Windows (no zombie state in NT)."""
    if int(pid) <= 0:
        return False
    if info is not None:
        if info.get("state") in ("Z", "X"):
            return False
        if "starttime" in info:
            try:
                if int(info["starttime"]) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True
    return is_process_alive_win(pid, k32=k32, get_last_error_fn=get_last_error_fn)


def list_toolhelp_processes_win(
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Dict[int, int]:
    """Snapshot ``{pid: ppid}`` across the host via ``CreateToolhelp32Snapshot``."""
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    last_err = 0
    for _attempt in range(_TOOLHELP_RETRIES):
        snap = dll_k32.CreateToolhelp32Snapshot(w32.TH32CS_SNAPPROCESS, 0)
        if w32.is_null_or_invalid_handle(snap):
            last_err = err_fn()
            if last_err == w32.ERROR_BAD_LENGTH:
                continue
            raise _win_proc_error(last_err)
        try:
            parent_map: Dict[int, int] = {}
            entry = w32.PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(w32.PROCESSENTRY32W)
            ok = dll_k32.Process32FirstW(snap, ctypes.byref(entry))
            if not ok:
                last_err = err_fn()
                if last_err == w32.ERROR_NO_MORE_FILES:
                    return parent_map
                if last_err == w32.ERROR_BAD_LENGTH:
                    continue
                raise _win_proc_error(last_err)

            retry_snapshot = False
            while True:
                parent_map[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = dll_k32.Process32NextW(snap, ctypes.byref(entry))
                if not ok:
                    last_err = err_fn()
                    if last_err == w32.ERROR_NO_MORE_FILES:
                        break
                    if last_err == w32.ERROR_BAD_LENGTH:
                        retry_snapshot = True
                        break
                    raise _win_proc_error(last_err)
            if not retry_snapshot:
                return parent_map
        finally:
            dll_k32.CloseHandle(snap)
    raise _win_proc_error(last_err or w32.ERROR_BAD_LENGTH)


def _ancestry_from_map_win(
    pid: int,
    parent_map: Dict[int, int],
    stop_at_pids: Set[int],
    *,
    k32: Any,
    err_fn: Callable[[], int],
    ct_cache: Optional[Dict[int, Optional[int]]] = None,
) -> List[int]:
    target = int(pid)
    if target <= 0 or target in stop_at_pids:
        return []

    def _cached_ct(proc_id: int) -> Optional[int]:
        if ct_cache is not None and proc_id in ct_cache:
            return ct_cache[proc_id]
        ct_val = _get_pid_creation_time_or_none_win(proc_id, k32=k32, err_fn=err_fn)
        if ct_cache is not None:
            ct_cache[proc_id] = ct_val
        return ct_val

    curr_ct = _cached_ct(target)
    if curr_ct is None or target not in parent_map:
        return []

    chain: List[int] = []
    visited: Set[int] = {target}
    curr = target

    while curr > 0 and curr not in stop_at_pids:
        ppid = parent_map.get(curr)
        if ppid is None or ppid <= 0 or ppid in visited or ppid not in parent_map:
            break
        try:
            parent_ct = _cached_ct(ppid)
        except PermissionError:
            # Privilege boundary (e.g. SYSTEM/service ancestor): stop without trusting unverified parent.
            break
        if parent_ct is None or parent_ct > curr_ct:
            # Parent exited or PID was reused after child creation: reject stale/forged link.
            break
        chain.append(ppid)
        visited.add(ppid)
        curr = ppid
        curr_ct = parent_ct
    return chain


def get_proc_ancestry_win(
    pid: int,
    stop_at_pids: Optional[Set[int]] = None,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> List[int]:
    """Walk Windows PPID chain via Toolhelp32, enforcing ``parent.starttime <= child.starttime``."""
    stops = set(stop_at_pids or ())
    target = int(pid)
    if target <= 0 or target in stops:
        return []

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    # Verify the target PID itself before snapshot so ACCESS_DENIED fails closed immediately.
    curr_ct = _get_pid_creation_time_or_none_win(target, k32=dll_k32, err_fn=err_fn)
    if curr_ct is None:
        return []

    parent_map = list_toolhelp_processes_win(k32=dll_k32, get_last_error_fn=err_fn)
    return _ancestry_from_map_win(
        target,
        parent_map,
        stops,
        k32=dll_k32,
        err_fn=err_fn,
        ct_cache={target: curr_ct},
    )


def _find_utf16le_env_end(raw: bytes) -> Optional[int]:
    """Return byte end offset of a UTF-16LE NUL-NUL terminated env block, or ``None``."""
    if len(raw) >= 2 and raw[:2] == b"\x00\x00":
        return 2
    pos = 0
    while True:
        idx = raw.find(b"\x00\x00\x00\x00", pos)
        if idx == -1:
            return None
        if (idx & 1) == 0:
            return idx + 4
        pos = idx + 1


def parse_win_env_block(raw: bytes) -> Dict[str, str]:
    """Parse a Windows UTF-16LE environment block (double-NUL terminated) into a dict."""
    if not raw or (len(raw) >= 2 and raw[:2] == b"\x00\x00"):
        return {}
    even_len = len(raw) & ~1
    if even_len == 0:
        return {}
    text = raw[:even_len].decode("utf-16-le", errors="replace")
    env: Dict[str, str] = {}
    for entry in text.split("\x00"):
        if not entry:
            break
        if entry.startswith("="):
            # Windows cmd/CRT stores hidden per-drive CWD markers like "=C:=C:\\dir".
            continue
        if "=" in entry:
            k, v = entry.split("=", 1)
            if k:
                env[k] = v
    return env


def _read_remote_bytes(
    dll_k32: Any,
    handle: Any,
    addr: int,
    size: int,
    *,
    err_fn: Callable[[], int],
) -> tuple[Optional[bytes], int]:
    buf = ctypes.create_string_buffer(size)
    nread = ctypes.c_size_t(0)
    ok = dll_k32.ReadProcessMemory(
        handle,
        int(addr),
        buf,
        size,
        ctypes.byref(nread),
    )
    if not ok:
        return None, err_fn()
    actual = int(nread.value) if int(nread.value) > 0 else size
    if actual < size:
        return None, w32.ERROR_PARTIAL_COPY
    return buf.raw[:size], 0


def _read_remote_struct(
    dll_k32: Any,
    handle: Any,
    addr: int,
    struct_cls: Any,
    *,
    err_fn: Callable[[], int],
) -> tuple[Any, int]:
    obj = struct_cls()
    nread = ctypes.c_size_t(0)
    size = ctypes.sizeof(obj)
    ok = dll_k32.ReadProcessMemory(
        handle,
        int(addr),
        ctypes.byref(obj),
        size,
        ctypes.byref(nread),
    )
    if not ok:
        return None, err_fn()
    actual = int(nread.value) if int(nread.value) > 0 else size
    if actual < size:
        return None, w32.ERROR_PARTIAL_COPY
    return obj, 0


def _read_env_block_win(
    dll_k32: Any,
    handle: Any,
    env_ptr: int,
    env_size: int,
    *,
    err_fn: Callable[[], int],
) -> Optional[bytes]:
    if 0 < env_size <= _MAX_ENV_BYTES:
        data, err = _read_remote_bytes(dll_k32, handle, env_ptr, env_size, err_fn=err_fn)
        if data is not None:
            end = _find_utf16le_env_end(data)
            if end is not None:
                return data[:end]
        else:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            if err == w32.ERROR_ACCESS_DENIED:
                raise _win_proc_error(err)
            if err != w32.ERROR_PARTIAL_COPY:
                raise _win_proc_error(err)

    chunks: List[bytes] = []
    total = 0
    cur_addr = int(env_ptr)
    while total < _MAX_ENV_BYTES:
        page_rem = _ENV_PAGE_SIZE - (cur_addr & (_ENV_PAGE_SIZE - 1))
        chunk_len = min(page_rem, _MAX_ENV_BYTES - total)
        chunk, err = _read_remote_bytes(dll_k32, handle, cur_addr, chunk_len, err_fn=err_fn)
        if chunk is None:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise _win_proc_error(err)
        chunks.append(chunk)
        total += len(chunk)
        cur_addr += len(chunk)
        combined = b"".join(chunks)
        end = _find_utf16le_env_end(combined)
        if end is not None:
            return combined[:end]

    raise OSError(errno.EIO, "Target process PEB environment block exceeded size bound without terminator")


def _read_peb_process_parameters_win(
    dll_k32: Any,
    dll_nt: Any,
    handle: Any,
    *,
    err_fn: Callable[[], int],
) -> Optional[tuple[bool, int, Any, int]]:
    """Read target process ``RTL_USER_PROCESS_PARAMETERS`` (64-bit or WOW64 32-bit)."""
    is_64_host = ctypes.sizeof(ctypes.c_void_p) == 8
    is_wow64 = not is_64_host
    peb32_addr = 0

    if is_64_host:
        wow64_machine = w32.IMAGE_FILE_MACHINE_UNKNOWN
        is_wow64_fn = getattr(dll_k32, "IsWow64Process2", None)
        if is_wow64_fn is not None:
            proc_machine = ctypes.c_uint16(0)
            native_machine = ctypes.c_uint16(0)
            if not is_wow64_fn(handle, ctypes.byref(proc_machine), ctypes.byref(native_machine)):
                if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                    return None
                raise _win_proc_error(err_fn())
            wow64_machine = int(proc_machine.value)

        peb32_val = ctypes.c_size_t(0)
        ret_len = ctypes.c_uint32(0)
        status = int(
            dll_nt.NtQueryInformationProcess(
                handle,
                w32.ProcessWow64Information,
                ctypes.byref(peb32_val),
                ctypes.sizeof(peb32_val),
                ctypes.byref(ret_len),
            )
        )
        if status < 0:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise _ntstatus_to_win_error(status, dll_nt=dll_nt)

        peb32_addr = int(peb32_val.value)
        if peb32_addr != 0:
            is_wow64 = True
        elif wow64_machine in _WOW64_32BIT_MACHINES:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise OSError(errno.EIO, "Target WOW64 PEB32 is not initialized")

    if is_wow64:
        if not peb32_addr:
            pbi = w32.PROCESS_BASIC_INFORMATION()
            ret_len = ctypes.c_uint32(0)
            status = int(
                dll_nt.NtQueryInformationProcess(
                    handle,
                    w32.ProcessBasicInformation,
                    ctypes.byref(pbi),
                    ctypes.sizeof(pbi),
                    ctypes.byref(ret_len),
                )
            )
            if status < 0:
                if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                    return None
                raise _ntstatus_to_win_error(status, dll_nt=dll_nt)
            peb32_addr = int(pbi.PebBaseAddress or 0)
        if not peb32_addr:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise OSError(errno.EIO, "Target PEB32 address is NULL")

        peb32, err = _read_remote_struct(dll_k32, handle, peb32_addr, w32.PEB32, err_fn=err_fn)
        if peb32 is None:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise _win_proc_error(err)
        params_addr = int(peb32.ProcessParameters)
        if not params_addr:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise OSError(errno.EIO, "Target PEB32 ProcessParameters is NULL")

        params32, err = _read_remote_struct(
            dll_k32, handle, params_addr, w32.RTL_USER_PROCESS_PARAMETERS32, err_fn=err_fn
        )
        return True, params_addr, params32, err

    pbi = w32.PROCESS_BASIC_INFORMATION()
    ret_len = ctypes.c_uint32(0)
    status = int(
        dll_nt.NtQueryInformationProcess(
            handle,
            w32.ProcessBasicInformation,
            ctypes.byref(pbi),
            ctypes.sizeof(pbi),
            ctypes.byref(ret_len),
        )
    )
    if status < 0:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        raise _ntstatus_to_win_error(status, dll_nt=dll_nt)

    peb_addr = int(pbi.PebBaseAddress or 0)
    if not peb_addr:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        raise _NullPebError(errno.EIO, "Target PEB address is NULL")

    peb64, err = _read_remote_struct(dll_k32, handle, peb_addr, w32.PEB64, err_fn=err_fn)
    if peb64 is None:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        raise _win_proc_error(err)

    params_addr = int(peb64.ProcessParameters)
    if not params_addr:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        raise OSError(errno.EIO, "Target PEB ProcessParameters is NULL")

    params64, err = _read_remote_struct(
        dll_k32, handle, params_addr, w32.RTL_USER_PROCESS_PARAMETERS64, err_fn=err_fn
    )
    return False, params_addr, params64, err


def _extract_env_ptr_and_size_win(
    dll_k32: Any,
    dll_nt: Any,
    handle: Any,
    *,
    err_fn: Callable[[], int],
) -> Optional[tuple[int, int]]:
    res = _read_peb_process_parameters_win(dll_k32, dll_nt, handle, err_fn=err_fn)
    if res is None:
        return None
    is_wow64, params_addr, params, err = res
    if params is not None:
        return int(params.Environment), int(params.EnvironmentSize)
    if err == w32.ERROR_PARTIAL_COPY:
        struct_cls = w32.RTL_USER_PROCESS_PARAMETERS32 if is_wow64 else w32.RTL_USER_PROCESS_PARAMETERS64
        width = 4 if is_wow64 else 8
        env_bytes, err2 = _read_remote_bytes(
            dll_k32,
            handle,
            params_addr + struct_cls.Environment.offset,
            width,
            err_fn=err_fn,
        )
        if env_bytes is not None:
            return int.from_bytes(env_bytes, "little"), 0
        err = err2
    if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
        return None
    raise _win_proc_error(err)


def parse_win_command_line(cmdline: str) -> List[str]:
    """Parse a Windows ``CommandLine`` string into argv tokens using MS CRT rules."""
    if not cmdline:
        return []
    args: List[str] = []
    n = len(cmdline)
    i = 0
    while i < n:
        while i < n and cmdline[i] in (" ", "\t"):
            i += 1
        if i >= n:
            break
        buf: List[str] = []
        in_quotes = False
        while i < n:
            ch = cmdline[i]
            if not in_quotes and ch in (" ", "\t"):
                break
            if ch == "\\":
                bs_count = 0
                while i < n and cmdline[i] == "\\":
                    bs_count += 1
                    i += 1
                if i < n and cmdline[i] == '"':
                    buf.append("\\" * (bs_count // 2))
                    if bs_count % 2 == 1:
                        buf.append('"')
                        i += 1
                else:
                    buf.append("\\" * bs_count)
                continue
            if ch == '"':
                if in_quotes and i + 1 < n and cmdline[i + 1] == '"':
                    buf.append('"')
                    i += 2
                    continue
                in_quotes = not in_quotes
                i += 1
                continue
            buf.append(ch)
            i += 1
        args.append("".join(buf))
    return args


def _extract_cmdline_ptr_and_len_win(
    dll_k32: Any,
    dll_nt: Any,
    handle: Any,
    *,
    err_fn: Callable[[], int],
) -> Optional[tuple[int, int]]:
    res = _read_peb_process_parameters_win(dll_k32, dll_nt, handle, err_fn=err_fn)
    if res is None:
        return None
    is_wow64, params_addr, params, err = res
    if params is not None:
        return int(params.CommandLine.Buffer), int(params.CommandLine.Length)
    if err == w32.ERROR_PARTIAL_COPY:
        struct_cls = w32.RTL_USER_PROCESS_PARAMETERS32 if is_wow64 else w32.RTL_USER_PROCESS_PARAMETERS64
        ustr_cls = w32.UNICODE_STRING32 if is_wow64 else w32.UNICODE_STRING64
        ustr, err2 = _read_remote_struct(
            dll_k32,
            handle,
            params_addr + struct_cls.CommandLine.offset,
            ustr_cls,
            err_fn=err_fn,
        )
        if ustr is not None:
            return int(ustr.Buffer), int(ustr.Length)
        err = err2
    if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
        return None
    raise _win_proc_error(err)


def get_proc_cmdline_win(
    pid: int,
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[List[str]]:
    """Read and parse the target process PEB ``CommandLine`` into argv on Windows.

    Returns ``None`` when the process is gone, ``[]`` when empty, and raises
    ``PermissionError`` / ``OSError`` when a live process cannot be inspected.
    """
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    handle = _open_process_or_none_win(
        pid,
        w32.PROCESS_QUERY_INFORMATION | w32.PROCESS_VM_READ | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if handle is None:
        return None
    try:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None
        extracted = _extract_cmdline_ptr_and_len_win(dll_k32, dll_nt, handle, err_fn=err_fn)
        if extracted is None:
            return None
        cmd_ptr, cmd_len = extracted
        if cmd_len == 0:
            return []
        if cmd_len < 0 or cmd_len > _MAX_CMDLINE_BYTES:
            raise OSError(errno.EIO, f"Target process PEB CommandLine length out of range: {cmd_len}")
        if not cmd_ptr:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise OSError(errno.EIO, "Target process PEB CommandLine pointer is NULL")
        raw, err = _read_remote_bytes(dll_k32, handle, cmd_ptr, cmd_len, err_fn=err_fn)
        if raw is None:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise _win_proc_error(err)
        even_len = len(raw) & ~1
        if even_len == 0:
            return []
        text = raw[:even_len].decode("utf-16-le", errors="replace").rstrip("\x00")
        return parse_win_command_line(text)
    finally:
        dll_k32.CloseHandle(handle)


def get_proc_environ_win(
    pid: int,
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Dict[str, str]]:
    """Read and parse the target process PEB environment block on Windows.

    Returns ``None`` when the process is gone, ``{}`` when its environment is
    genuinely empty, and raises ``PermissionError`` / ``OSError`` when the live
    process's PEB or environment cannot be read.
    """
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    handle = _open_process_or_none_win(
        pid,
        w32.PROCESS_QUERY_INFORMATION | w32.PROCESS_VM_READ | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if handle is None:
        return None
    try:
        if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
            return None

        extracted = _extract_env_ptr_and_size_win(dll_k32, dll_nt, handle, err_fn=err_fn)
        if extracted is None:
            return None
        env_ptr, env_size = extracted
        if not env_ptr:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return None
            raise OSError(errno.EIO, "Target process PEB Environment pointer is NULL")

        raw = _read_env_block_win(dll_k32, handle, env_ptr, env_size, err_fn=err_fn)
        if raw is None:
            return None
        return parse_win_env_block(raw)
    finally:
        dll_k32.CloseHandle(handle)


def _should_mark_uncertain_env_error_win(
    pid: int,
    exc: OSError,
    *,
    k32: Any,
    advapi: Any,
    err_fn: Callable[[], int],
    my_sid: Optional[str],
) -> bool:
    """Return True only if *exc* occurred on a live, same-user, non-minimal process."""
    if getattr(exc, "_mesh_null_peb", False):
        return False
    if exc.errno in (errno.ENOENT, errno.ESRCH) or getattr(exc, "winerror", None) == w32.ERROR_INVALID_PARAMETER:
        return False
    try:
        ct = _get_pid_creation_time_or_none_win(pid, k32=k32, err_fn=err_fn)
    except PermissionError:
        return False
    except OSError:
        return True
    if ct is None:
        return False
    if my_sid:
        try:
            target_sid = get_process_user_sid_win(
                pid, k32=k32, advapi=advapi, get_last_error_fn=err_fn
            )
        except PermissionError:
            return False
        except OSError:
            return True
        if target_sid != my_sid:
            return False
    return True


def _win_env_get(env: Optional[Mapping[str, str]], key: str) -> Optional[str]:
    if not env:
        return None
    if key in env:
        return env[key]
    target = key.upper()
    for k, v in env.items():
        if k.upper() == target:
            return v
    return None


def find_run_processes_win(
    run_id: str,
    pgid: Optional[int],
    exclude: Optional[Set[int]] = None,
    *,
    k32: Any = None,
    ntdll: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Find live processes belonging to *run_id* via Toolhelp ancestry and PEB ``MESH_RUN_ID``."""
    from mesh.core.proc import ScanPids

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    if advapi is not None:
        dll_adv = advapi
    elif sys.platform == "win32":
        try:
            dll_adv = w32.get_advapi32()
        except NotImplementedError:
            dll_adv = None
    else:
        dll_adv = None
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    my_sid: Optional[str] = None
    if dll_adv is not None:
        try:
            my_sid = get_process_user_sid_win(
                int(dll_k32.GetCurrentProcessId()),
                k32=dll_k32,
                advapi=dll_adv,
                get_last_error_fn=err_fn,
            )
        except OSError:
            my_sid = None

    skip = set(exclude or ())
    try:
        parent_map = list_toolhelp_processes_win(k32=dll_k32, get_last_error_fn=err_fn)
    except OSError:
        return ScanPids(uncertain=True)

    found: List[int] = []
    uncertain = False
    ct_cache: Dict[int, Optional[int]] = {}
    anchor_stops = {int(pgid)} if pgid and int(pgid) > 0 else set()

    for pid in sorted(parent_map):
        if pid <= 0 or pid in skip:
            continue

        if anchor_stops:
            anchor_pid = int(pgid)  # type: ignore[arg-type]
            if pid == anchor_pid:
                try:
                    ct_val = _get_pid_creation_time_or_none_win(pid, k32=dll_k32, err_fn=err_fn)
                except OSError:
                    uncertain = True
                    continue
                if ct_val is not None:
                    ct_cache[pid] = ct_val
                    found.append(pid)
                continue

            try:
                chain = _ancestry_from_map_win(
                    pid,
                    parent_map,
                    anchor_stops,
                    k32=dll_k32,
                    err_fn=err_fn,
                    ct_cache=ct_cache,
                )
            except PermissionError:
                chain = []
            except OSError:
                uncertain = True
                chain = []
            if anchor_pid in chain:
                found.append(pid)
                continue

        try:
            env = get_proc_environ_win(
                pid,
                k32=dll_k32,
                ntdll=dll_nt,
                get_last_error_fn=err_fn,
            )
        except PermissionError:
            # Foreign/elevated bystander processes return ERROR_ACCESS_DENIED on Windows.
            continue
        except OSError as exc:
            if _should_mark_uncertain_env_error_win(
                pid,
                exc,
                k32=dll_k32,
                advapi=dll_adv,
                err_fn=err_fn,
                my_sid=my_sid,
            ):
                uncertain = True
            continue
        if _win_env_get(env, "MESH_RUN_ID") == run_id:
            found.append(pid)

    return ScanPids(sorted(found), uncertain=uncertain)


def crash_self_now_win(*, k32: Any = None) -> None:
    """Immediately terminate the current process via ``TerminateProcess`` (SIGKILL eq.)."""
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    h_proc = dll_k32.GetCurrentProcess()
    dll_k32.TerminateProcess(h_proc, 137)


_SIGKILL_NUM = getattr(signal, "SIGKILL", 9)
_SIGTERM_NUM = getattr(signal, "SIGTERM", 15)
_SIGBREAK_NUM = getattr(signal, "SIGBREAK", 21)
_CTRL_BREAK_NUM = getattr(signal, "CTRL_BREAK_EVENT", 1)
_STATUS_NOT_IMPLEMENTED = -1073741822  # 0xC0000002
_JOB_PID_LIST_MAX_GROWTH_RETRIES = 16


class _WinHandle:
    """Thread-safe RAII wrapper around an owned Win32 kernel ``HANDLE``."""

    def __init__(
        self,
        handle: Any,
        *,
        k32: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        if w32.is_null_or_invalid_handle(handle):
            raise ValueError(f"Invalid Win32 handle: {handle!r}")
        self._lock = threading.Lock()
        self._handle: Any = int(handle)
        self._k32 = k32 if k32 is not None else w32.get_kernel32()
        self._err_fn = get_last_error_fn

    @property
    def handle(self) -> Optional[int]:
        """Raw Win32 ``HANDLE`` integer, or ``None`` once closed."""
        return self._handle

    @property
    def closed(self) -> bool:
        """Return True once the underlying handle has been closed."""
        return self._handle is None

    def fileno(self) -> int:
        """Return the raw Win32 ``HANDLE`` integer or raise if closed."""
        return self._require_handle()

    def _require_handle(self) -> int:
        with self._lock:
            if self._handle is None:
                raise OSError(errno.EBADF, "Win32 handle is closed")
            return int(self._handle)

    def close(self) -> None:
        """Close the owned Win32 handle (idempotent)."""
        with self._lock:
            h = self._handle
            if h is None:
                return
            self._handle = None
        self._k32.CloseHandle(h)

    def __enter__(self) -> "_WinHandle":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class JobHandle(_WinHandle):
    """Owned Win32 Job Object handle supporting query, assignment, and termination."""

    def __init__(
        self,
        handle: Any,
        *,
        name: Optional[str] = None,
        ephemeral: bool = True,
        k32: Any = None,
        ntdll: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        super().__init__(handle, k32=k32, get_last_error_fn=get_last_error_fn)
        self.name = name
        self.ephemeral = bool(ephemeral)
        self._ntdll = ntdll

    def assign(self, proc_or_pid: Any) -> None:
        """Assign *proc_or_pid* (``Popen`` or PID) to this Job Object."""
        assign_process_to_job_win(
            self,
            proc_or_pid,
            k32=self._k32,
            get_last_error_fn=self._err_fn,
        )

    def query_pids(self) -> List[int]:
        """Return sorted live PIDs assigned to this Job Object."""
        return query_job_pids_win(
            self,
            k32=self._k32,
            get_last_error_fn=self._err_fn,
        )

    def contains(self, pid_or_proc: Any) -> bool:
        """Return True iff *pid_or_proc* is currently inside this Job Object."""
        return is_process_in_job_win(
            pid_or_proc,
            self,
            k32=self._k32,
            get_last_error_fn=self._err_fn,
        )

    def terminate(self, exit_code: int = 137) -> None:
        """Terminate every process associated with this Job Object."""
        terminate_job_win(
            self,
            exit_code=exit_code,
            k32=self._k32,
            get_last_error_fn=self._err_fn,
        )


class EventHandle(_WinHandle):
    """Owned Win32 Event handle for graceful cross-process run-stop signalling."""

    def __init__(
        self,
        handle: Any,
        *,
        name: Optional[str] = None,
        k32: Any = None,
        get_last_error_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        super().__init__(handle, k32=k32, get_last_error_fn=get_last_error_fn)
        self.name = name

    def set(self) -> None:
        """Signal the event via ``SetEvent``."""
        h = self._require_handle()
        err_fn = self._err_fn if self._err_fn is not None else w32.get_last_error
        if not self._k32.SetEvent(h):
            raise _win_proc_error(err_fn())


def _validate_run_id_for_win_obj(run_id: str) -> str:
    if (
        not isinstance(run_id, str)
        or not run_id
        or "\\" in run_id
        or "/" in run_id
        or "\x00" in run_id
        or ".." in run_id
    ):
        raise ValueError(f"Invalid run_id for Win32 named object: {run_id!r}")
    return run_id


def home_hash_win(home: str) -> str:
    """Return the 16-hex-char canonical SHA-256 digest identifying *home*."""
    from mesh.platform import paths as plat_paths

    norm = plat_paths.norm_cmp(plat_paths.os.path.realpath(home))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def run_job_name_win(home: str, run_id: str) -> str:
    """Return ``Local\\mesh-job-<sha256(norm_home)[:16]>-<run_id>``."""
    return f"Local\\mesh-job-{home_hash_win(home)}-{_validate_run_id_for_win_obj(run_id)}"


def run_stop_event_name_win(home: str, run_id: str) -> str:
    """Return ``Local\\mesh-stop-<sha256(norm_home)[:16]>-<run_id>``."""
    return f"Local\\mesh-stop-{home_hash_win(home)}-{_validate_run_id_for_win_obj(run_id)}"


def build_private_kernel_sddl(user_sid: str) -> str:
    """Build protected owner-only SDDL for Win32 kernel objects (Job / Event)."""
    from mesh.platform import perms as _perms

    sid = _perms._validate_sid_str(user_sid)
    trustees = [sid]
    if sid != _perms.LOCAL_SYSTEM_SID:
        trustees.append("SY")
    if sid != _perms.BUILTIN_ADMINISTRATORS_SID:
        trustees.append("BA")
    aces = "".join(f"(A;;GA;;;{t})" for t in trustees)
    return f"D:P{aces}"


@contextlib.contextmanager
def _private_security_attributes(
    *,
    advapi: Any,
    k32: Any,
    err_fn: Callable[[], int],
    inherit: bool = False,
) -> Iterator[Any]:
    """Allocate a fail-closed owner-only ``SECURITY_ATTRIBUTES`` and free its SD on exit."""
    from mesh.platform import perms as _perms

    user_sid, _owner_sid = _perms.get_current_user_and_owner_sids_win(
        advapi=advapi, k32=k32, get_last_error_fn=err_fn
    )
    sddl = build_private_kernel_sddl(user_sid)
    sd_ptr = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, w32.SDDL_REVISION_1, ctypes.byref(sd_ptr), None
    ):
        raise _win_proc_error(err_fn())
    if not sd_ptr.value:
        raise OSError(errno.EINVAL, "ConvertStringSecurityDescriptorToSecurityDescriptorW returned NULL")
    try:
        sa = w32.SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(w32.SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=sd_ptr.value,
            bInheritHandle=bool(inherit),
        )
        yield ctypes.byref(sa)
    finally:
        if sd_ptr.value:
            k32.LocalFree(sd_ptr)


def create_job_win(
    *,
    kill_on_close: bool = True,
    name: Optional[str] = None,
    k32: Any = None,
    advapi: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
) -> JobHandle:
    """Create a Win32 Job Object (ephemeral or named) with breakaway disabled."""
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    set_err = set_last_error_fn if set_last_error_fn is not None else w32.set_last_error

    if name is not None:
        dll_adv = advapi if advapi is not None else w32.get_advapi32()
        with _private_security_attributes(
            advapi=dll_adv, k32=dll_k32, err_fn=err_fn, inherit=False
        ) as sa_ptr:
            set_err(0)
            h_job = dll_k32.CreateJobObjectW(sa_ptr, name)
            create_err = err_fn()
    else:
        set_err(0)
        h_job = dll_k32.CreateJobObjectW(None, None)
        create_err = err_fn()

    if w32.is_null_or_invalid_handle(h_job):
        raise _win_proc_error(create_err)

    try:
        if name is not None and create_err == w32.ERROR_ALREADY_EXISTS:
            raise _win_proc_error(w32.ERROR_ALREADY_EXISTS)
        info = w32.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # Never set BREAKAWAY_OK or SILENT_BREAKAWAY_OK so descendants cannot escape.
        info.BasicLimitInformation.LimitFlags = (
            w32.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if kill_on_close else 0
        )
        ok = dll_k32.SetInformationJobObject(
            h_job,
            w32.JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            raise _win_proc_error(err_fn())
        return JobHandle(
            h_job,
            name=name,
            ephemeral=(name is None),
            k32=dll_k32,
            ntdll=ntdll,
            get_last_error_fn=err_fn,
        )
    except BaseException:
        dll_k32.CloseHandle(h_job)
        raise


def create_ephemeral_job_win(
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
) -> JobHandle:
    """Create an anonymous Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``."""
    return create_job_win(
        kill_on_close=True,
        name=None,
        k32=k32,
        ntdll=ntdll,
        get_last_error_fn=get_last_error_fn,
        set_last_error_fn=set_last_error_fn,
    )


def create_run_job_win(
    home: str,
    run_id: str,
    *,
    kill_on_close: bool = False,
    k32: Any = None,
    advapi: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
) -> JobHandle:
    """Create an owner-only Named Run Job Object (``LimitFlags=0`` by default for I8)."""
    job_name = run_job_name_win(home, run_id)
    return create_job_win(
        kill_on_close=kill_on_close,
        name=job_name,
        k32=k32,
        advapi=advapi,
        ntdll=ntdll,
        get_last_error_fn=get_last_error_fn,
        set_last_error_fn=set_last_error_fn,
    )


def open_run_job_win(
    home: str,
    run_id: str,
    *,
    terminate: bool = True,
    k32: Any = None,
    advapi: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[JobHandle]:
    """Open an existing Named Run Job Object, verifying owner SID; ``None`` strictly on not-found."""
    from mesh.platform import perms as _perms

    job_name = run_job_name_win(home, run_id)
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    access = (
        w32.READ_CONTROL
        | w32.JOB_OBJECT_QUERY
        | (w32.JOB_OBJECT_TERMINATE if terminate else 0)
    )
    h_job = dll_k32.OpenJobObjectW(access, False, job_name)
    if w32.is_null_or_invalid_handle(h_job):
        err = err_fn()
        if err == w32.ERROR_FILE_NOT_FOUND:
            return None
        raise _win_proc_error(err)
    try:
        try:
            user_sid, default_owner_sid = _perms.get_current_user_and_owner_sids_win(
                advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
            )
        except PermissionError:
            raise
        except OSError as exc:
            raise PermissionError(
                errno.EACCES, f"Unable to resolve current user SID for Named Run Job {job_name!r}: {exc}"
            ) from exc
        trusted_owners = set(_perms.WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
        owner_ptr = ctypes.c_void_p()
        sd_ptr = ctypes.c_void_p()
        rc = dll_adv.GetSecurityInfo(
            h_job,
            w32.SE_KERNEL_OBJECT,
            w32.OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner_ptr),
            None,
            None,
            None,
            ctypes.byref(sd_ptr),
        )
        if rc != 0:
            raise PermissionError(
                errno.EACCES, f"GetSecurityInfo failed on Named Run Job {job_name!r} (WinError {rc})"
            )
        try:
            owner_sid = _perms._sid_ptr_to_str(
                owner_ptr.value, advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
            )
        except PermissionError:
            raise
        except OSError as exc:
            raise PermissionError(
                errno.EACCES, f"Invalid owner SID on Named Run Job {job_name!r}: {exc}"
            ) from exc
        finally:
            if sd_ptr.value:
                dll_k32.LocalFree(sd_ptr)
        if owner_sid not in trusted_owners:
            raise PermissionError(
                errno.EACCES,
                f"Named Run Job {job_name!r} owner SID {owner_sid} is not trusted",
            )
        return JobHandle(
            h_job,
            name=job_name,
            ephemeral=False,
            k32=dll_k32,
            ntdll=ntdll,
            get_last_error_fn=err_fn,
        )
    except BaseException:
        dll_k32.CloseHandle(h_job)
        raise


def create_stop_event_win(
    home: str,
    run_id: str,
    *,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_last_error_fn: Optional[Callable[[int], None]] = None,
) -> EventHandle:
    """Create an owner-only manual-reset Named Stop Event for *home* and *run_id*."""
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    set_err = set_last_error_fn if set_last_error_fn is not None else w32.set_last_error

    ev_name = run_stop_event_name_win(home, run_id)
    with _private_security_attributes(
        advapi=dll_adv, k32=dll_k32, err_fn=err_fn, inherit=False
    ) as sa_ptr:
        set_err(0)
        h_ev = dll_k32.CreateEventW(sa_ptr, True, False, ev_name)
        create_err = err_fn()

    if w32.is_null_or_invalid_handle(h_ev):
        raise _win_proc_error(create_err)
    if create_err == w32.ERROR_ALREADY_EXISTS:
        dll_k32.CloseHandle(h_ev)
        raise _win_proc_error(w32.ERROR_ALREADY_EXISTS)
    return EventHandle(h_ev, name=ev_name, k32=dll_k32, get_last_error_fn=err_fn)


def _extract_job_raw_handle(job: Any) -> int:
    if isinstance(job, JobHandle):
        return job._require_handle()
    if hasattr(job, "fileno"):
        return int(job.fileno())
    if isinstance(job, int) and not isinstance(job, bool):
        if w32.is_null_or_invalid_handle(job):
            raise OSError(errno.EBADF, "Invalid Win32 Job handle")
        return int(job)
    raise TypeError(f"Expected JobHandle or Win32 handle int, got {type(job).__name__}")


def _resolve_job_deps(
    job: Any,
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]],
    *,
    ntdll: Any = None,
) -> tuple[int, Any, Any, Callable[[], int]]:
    h_job = _extract_job_raw_handle(job)
    dll_k32 = k32 if k32 is not None else getattr(job, "_k32", None)
    if dll_k32 is None:
        dll_k32 = w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else getattr(job, "_ntdll", None)
    err_fn = get_last_error_fn if get_last_error_fn is not None else getattr(job, "_err_fn", None)
    if err_fn is None:
        err_fn = w32.get_last_error
    return h_job, dll_k32, dll_nt, err_fn


def assign_process_handle_to_job_win(
    job: Any,
    proc_handle: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Assign an explicit Win32 process *proc_handle* to *job*."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    h_proc = int(proc_handle)
    if w32.is_null_or_invalid_handle(h_proc):
        raise OSError(errno.EBADF, "Invalid Win32 process handle")
    if not dll_k32.AssignProcessToJobObject(h_job, h_proc):
        raise _win_proc_error(err_fn())


def assign_process_to_job_win(
    job: Any,
    proc_or_pid: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Assign a ``Popen``/asyncio process or numeric PID to *job*."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    proc_handle = getattr(proc_or_pid, "_handle", None)
    if proc_handle is None:
        transport = getattr(proc_or_pid, "_transport", None)
        raw_proc = getattr(transport, "_proc", None) if transport is not None else None
        proc_handle = getattr(raw_proc, "_handle", None)
    if proc_handle is not None:
        if not dll_k32.AssignProcessToJobObject(h_job, int(proc_handle)):
            raise _win_proc_error(err_fn())
        return
    if not isinstance(proc_or_pid, int) or isinstance(proc_or_pid, bool):
        raise TypeError(f"Expected Popen or integer PID, got {type(proc_or_pid).__name__}")
    h_proc = _open_process_or_none_win(
        int(proc_or_pid),
        w32.PROCESS_SET_QUOTA | w32.PROCESS_TERMINATE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if h_proc is None:
        raise OSError(errno.ESRCH, f"Process {proc_or_pid} does not exist")
    try:
        if not dll_k32.AssignProcessToJobObject(h_job, h_proc):
            raise _win_proc_error(err_fn())
    finally:
        dll_k32.CloseHandle(h_proc)


def query_job_pids_win(
    job: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    initial_capacity: int = 64,
) -> List[int]:
    """Return sorted live PIDs assigned to *job* via ``JobObjectBasicProcessIdList``."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    header_off = w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.ProcessIdList.offset
    elem_sz = ctypes.sizeof(ctypes.c_size_t)
    cap = max(1, int(initial_capacity))

    for _attempt in range(_JOB_PID_LIST_MAX_GROWTH_RETRIES):
        buf_sz = header_off + cap * elem_sz
        buf = ctypes.create_string_buffer(buf_sz)
        ret_len = wintypes.DWORD(0)
        ok = dll_k32.QueryInformationJobObject(
            h_job,
            w32.JobObjectBasicProcessIdList,
            buf,
            buf_sz,
            ctypes.byref(ret_len),
        )
        hdr = w32.JOBOBJECT_BASIC_PROCESS_ID_LIST.from_buffer(buf)
        assigned = int(hdr.NumberOfAssignedProcesses)
        in_list = int(hdr.NumberOfProcessIdsInList)
        if not ok:
            err = err_fn()
            if err in (w32.ERROR_MORE_DATA, w32.ERROR_INSUFFICIENT_BUFFER):
                cap = max(cap * 2, assigned + 16)
                continue
            raise _win_proc_error(err)
        if in_list < assigned:
            cap = max(cap * 2, assigned + 16)
            continue
        if in_list == 0:
            return []
        if header_off + in_list * elem_sz > len(buf):
            cap = max(cap * 2, in_list + 16)
            continue
        arr = (ctypes.c_size_t * in_list).from_buffer(buf, header_off)
        return sorted({int(p) for p in arr if int(p) > 0})

    raise OSError(errno.EIO, "QueryInformationJobObject(JobObjectBasicProcessIdList) exceeded resize limit")


def is_process_handle_in_job_win(
    proc_handle: Any,
    job: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True iff open process *proc_handle* is inside *job*."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    h_proc = int(proc_handle)
    if w32.is_null_or_invalid_handle(h_proc):
        raise OSError(errno.EBADF, "Invalid Win32 process handle")
    res = wintypes.BOOL(0)
    if not dll_k32.IsProcessInJob(h_proc, h_job, ctypes.byref(res)):
        raise _win_proc_error(err_fn())
    return bool(res.value)


def is_process_in_job_win(
    pid_or_proc: Any,
    job: Any,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True iff *pid_or_proc* (``Popen`` or integer PID) is inside *job*."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    proc_handle = getattr(pid_or_proc, "_handle", None)
    if proc_handle is None:
        transport = getattr(pid_or_proc, "_transport", None)
        raw_proc = getattr(transport, "_proc", None) if transport is not None else None
        proc_handle = getattr(raw_proc, "_handle", None)
    if proc_handle is not None:
        return is_process_handle_in_job_win(
            int(proc_handle), h_job, k32=dll_k32, get_last_error_fn=err_fn
        )

    if not isinstance(pid_or_proc, int) or isinstance(pid_or_proc, bool):
        raise TypeError(f"Expected Popen or integer PID, got {type(pid_or_proc).__name__}")
    h_proc = _open_process_or_none_win(
        int(pid_or_proc),
        w32.PROCESS_QUERY_LIMITED_INFORMATION,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if h_proc is None:
        return False
    try:
        return is_process_handle_in_job_win(
            h_proc, h_job, k32=dll_k32, get_last_error_fn=err_fn
        )
    finally:
        dll_k32.CloseHandle(h_proc)


def terminate_job_win(
    job: Any,
    exit_code: int = 137,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Terminate all processes in *job* with *exit_code* via ``TerminateJobObject``."""
    h_job, dll_k32, _dll_nt, err_fn = _resolve_job_deps(job, k32, get_last_error_fn)
    if not dll_k32.TerminateJobObject(h_job, int(exit_code)):
        raise _win_proc_error(err_fn())


def resume_process_main_thread_win(
    proc_handle: Any,
    pid: int,
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Resume the initial suspended thread of a newly created process."""
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    target_pid = int(pid)
    nt_get_next = getattr(dll_nt, "NtGetNextThread", None) if dll_nt is not None else None
    nt_err: Optional[OSError] = None
    if nt_get_next is not None:
        owned_handle: Any = None
        query_handle: Optional[int] = None
        if proc_handle is not None:
            raw_h = int(proc_handle)
            if w32.is_null_or_invalid_handle(raw_h):
                raise OSError(errno.EBADF, "Invalid Win32 process handle")
            query_handle = raw_h
        else:
            try:
                opened = _open_process_or_none_win(
                    target_pid,
                    w32.PROCESS_QUERY_INFORMATION,
                    k32=dll_k32,
                    err_fn=err_fn,
                )
            except OSError as exc:
                nt_err = exc
                opened = None
            if opened is not None:
                owned_handle = opened
                query_handle = int(opened)

        if query_handle is not None:
            try:
                h_thread = wintypes.HANDLE()
                status = int(
                    nt_get_next(
                        query_handle,
                        None,
                        w32.THREAD_SUSPEND_RESUME,
                        0,
                        0,
                        ctypes.byref(h_thread),
                    )
                )
                if status >= 0 and not w32.is_null_or_invalid_handle(h_thread.value):
                    try:
                        rc = int(dll_k32.ResumeThread(h_thread.value))
                        if rc == w32.THREAD_RESUME_FAILED:
                            raise _win_proc_error(err_fn())
                        return
                    finally:
                        dll_k32.CloseHandle(h_thread.value)
                if status < 0 and status not in (_STATUS_NOT_IMPLEMENTED, w32.STATUS_NO_MORE_ENTRIES):
                    nt_err = _ntstatus_to_win_error(status, dll_nt=dll_nt)
            finally:
                if owned_handle is not None:
                    dll_k32.CloseHandle(owned_handle)

    # Fallback: Toolhelp32 thread snapshot filtered to th32OwnerProcessID == pid.
    last_err = 0
    for _attempt in range(_TOOLHELP_RETRIES):
        snap = dll_k32.CreateToolhelp32Snapshot(w32.TH32CS_SNAPTHREAD, 0)
        if w32.is_null_or_invalid_handle(snap):
            last_err = err_fn()
            if last_err == w32.ERROR_BAD_LENGTH:
                continue
            raise _win_proc_error(last_err)
        resumed = 0
        retry_snapshot = False
        try:
            entry = w32.THREADENTRY32()
            entry.dwSize = ctypes.sizeof(w32.THREADENTRY32)
            ok = dll_k32.Thread32First(snap, ctypes.byref(entry))
            if not ok:
                last_err = err_fn()
                if last_err == w32.ERROR_BAD_LENGTH:
                    retry_snapshot = True
                elif last_err != w32.ERROR_NO_MORE_FILES:
                    raise _win_proc_error(last_err)
            while ok and not retry_snapshot:
                if int(entry.th32OwnerProcessID) == target_pid:
                    h_thr = dll_k32.OpenThread(
                        w32.THREAD_SUSPEND_RESUME, False, int(entry.th32ThreadID)
                    )
                    if w32.is_null_or_invalid_handle(h_thr):
                        raise _win_proc_error(err_fn())
                    try:
                        rc = int(dll_k32.ResumeThread(h_thr))
                        if rc == w32.THREAD_RESUME_FAILED:
                            raise _win_proc_error(err_fn())
                        resumed += 1
                    finally:
                        dll_k32.CloseHandle(h_thr)
                ok = dll_k32.Thread32Next(snap, ctypes.byref(entry))
                if not ok:
                    last_err = err_fn()
                    if last_err == w32.ERROR_BAD_LENGTH:
                        retry_snapshot = True
                        break
                    if last_err != w32.ERROR_NO_MORE_FILES:
                        raise _win_proc_error(last_err)
        finally:
            dll_k32.CloseHandle(snap)
        if retry_snapshot:
            continue
        if resumed > 0:
            return
        if nt_err is not None:
            raise nt_err
        raise OSError(errno.ESRCH, f"No suspended initial thread found for pid {target_pid}")
    raise _win_proc_error(last_err or w32.ERROR_BAD_LENGTH)


def _cleanup_failed_proc_win(
    proc: Any,
    proc_handle: Optional[int],
    k32: Any,
    *,
    err_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Terminate and close stdio/handles of a suspended child that failed job setup."""
    terminated = False
    if proc_handle is not None and not w32.is_null_or_invalid_handle(proc_handle):
        try:
            terminated = bool(k32.TerminateProcess(proc_handle, 1))
        except Exception:
            pass
    if not terminated:
        kill_fn = getattr(proc, "kill", None)
        if kill_fn is not None:
            try:
                kill_fn()
                terminated = True
            except Exception:
                pass
    if not terminated:
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            try:
                h_tmp = _open_process_or_none_win(
                    pid,
                    w32.PROCESS_TERMINATE,
                    k32=k32,
                    err_fn=err_fn if err_fn is not None else w32.get_last_error,
                )
            except Exception:
                h_tmp = None
            if h_tmp is not None:
                try:
                    k32.TerminateProcess(h_tmp, 1)
                except Exception:
                    pass
                finally:
                    try:
                        k32.CloseHandle(h_tmp)
                    except Exception:
                        pass
    for stream in (
        getattr(proc, "stdin", None),
        getattr(proc, "stdout", None),
        getattr(proc, "stderr", None),
    ):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    wait_fn = getattr(proc, "wait", None)
    if wait_fn is not None:
        try:
            wait_fn(timeout=5.0)
        except Exception:
            pass
    raw_h = getattr(proc, "_handle", None)
    close_fn = getattr(raw_h, "Close", None)
    if close_fn is not None:
        try:
            close_fn()
        except Exception:
            pass


def spawn_in_job_win(
    argv: Any,
    job: Any,
    *,
    popen_fn: Optional[Callable[..., Any]] = None,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    **popen_kwargs: Any,
) -> Any:
    """Spawn *argv* suspended, assign it to *job*, resume its main thread, and return ``proc``."""
    if popen_fn is None:
        import subprocess

        popen_fn = subprocess.Popen
    h_job, dll_k32, dll_nt, err_fn = _resolve_job_deps(
        job, k32, get_last_error_fn, ntdll=ntdll
    )
    kw = dict(popen_kwargs)
    kw.pop("start_new_session", None)
    flags = int(kw.pop("creationflags", 0) or 0)
    flags |= w32.CREATE_SUSPENDED | w32.CREATE_NEW_PROCESS_GROUP
    if not (flags & (w32.DETACHED_PROCESS | w32.CREATE_NEW_CONSOLE)):
        flags |= w32.CREATE_NO_WINDOW
    kw["creationflags"] = flags

    proc = popen_fn(argv, **kw)
    proc_handle: Optional[int] = None
    try:
        proc_handle = int(proc._handle)
        if w32.is_null_or_invalid_handle(proc_handle):
            raise OSError(errno.EBADF, "Invalid Win32 process handle")
        if not dll_k32.AssignProcessToJobObject(h_job, proc_handle):
            raise _win_proc_error(err_fn())
        resume_process_main_thread_win(
            proc_handle,
            int(proc.pid),
            k32=dll_k32,
            ntdll=dll_nt,
            get_last_error_fn=err_fn,
        )
    except BaseException:
        _cleanup_failed_proc_win(proc, proc_handle, dll_k32, err_fn=err_fn)
        raise

    proc._mesh_job = job
    proc._mesh_job_handle = h_job
    return proc


def signal_run_stop_event_win(
    home: str,
    run_id: str,
    *,
    k32: Any = None,
) -> bool:
    """Open and signal the Named Stop Event for (*home*, *run_id*), returning True if set."""
    if not home or not run_id:
        return False
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    try:
        stop_name = run_stop_event_name_win(home, run_id)
    except ValueError:
        return False
    ev = dll_k32.OpenEventW(w32.EVENT_MODIFY_STATE, False, stop_name)
    if w32.is_null_or_invalid_handle(ev):
        return False
    try:
        return bool(dll_k32.SetEvent(ev))
    finally:
        dll_k32.CloseHandle(ev)


def signal_if_same_win(
    pid: int,
    starttime: int,
    sig: int,
    *,
    run_id: Optional[str] = None,
    home: Optional[str] = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Pin *pid* via ``OpenProcess``, verify ``CreationTime == starttime``, and signal/kill.

    Eliminates PID-reuse TOCTOU completely because ``TerminateProcess`` operates on the
    verified ``HANDLE`` rather than the numeric PID. Fails closed (returns ``False``).
    """
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    try:
        target_pid = int(pid)
        exp_st = int(starttime)
        sig_num = int(sig)
    except (TypeError, ValueError):
        return False

    cur_pid = int(dll_k32.GetCurrentProcessId())
    if target_pid <= 1 or target_pid == cur_pid or exp_st <= 0:
        return False

    is_probe = sig_num == 0
    is_term = sig_num in (_SIGTERM_NUM, _SIGBREAK_NUM, _CTRL_BREAK_NUM)
    is_kill = sig_num == _SIGKILL_NUM
    if not (is_probe or is_term or is_kill):
        return False

    access = w32.PROCESS_TERMINATE | w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE
    try:
        handle = _open_process_or_none_win(target_pid, access, k32=dll_k32, err_fn=err_fn)
    except OSError:
        return False
    if handle is None:
        return False

    try:
        try:
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return False
            actual_st = _get_handle_creation_time_win(handle, k32=dll_k32, err_fn=err_fn)
            if actual_st != exp_st:
                return False
            if not _is_handle_alive_win(handle, k32=dll_k32, err_fn=err_fn):
                return False
        except OSError:
            return False

        if is_probe:
            return True

        if is_kill:
            return bool(dll_k32.TerminateProcess(handle, 137))

        return bool(run_id and home and signal_run_stop_event_win(home, run_id, k32=dll_k32))
    finally:
        dll_k32.CloseHandle(handle)


def kill_if_same_win(
    pid: int,
    starttime: int,
    *,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Handle-pinned hard kill verifying ``(pid, CreationTime)`` before ``TerminateProcess``."""
    return signal_if_same_win(
        pid,
        starttime,
        _SIGKILL_NUM,
        k32=k32,
        get_last_error_fn=get_last_error_fn,
    )


def detached_popen_kwargs_win() -> Dict[str, Any]:
    """Return ``subprocess.Popen`` kwargs for a detached background daemon on Windows.

    Note: ``DETACHED_PROCESS`` and ``CREATE_NO_WINDOW`` are mutually exclusive in Win32
    ``CreateProcessW`` (combining them raises ``WinError 87``); ``DETACHED_PROCESS``
    detaches from any parent console without creating a visible window.
    """
    return {
        "creationflags": w32.DETACHED_PROCESS | w32.CREATE_NEW_PROCESS_GROUP,
        "close_fds": True,
    }


def reexec_clean_env_win(argv: List[str], env: Dict[str, str]) -> None:
    """Replace the current process cleanly on Windows without ``os.execve`` detachment."""
    import subprocess

    rc = subprocess.call(argv, env=env)
    sys.exit(rc)


def build_private_pipe_sddl(user_sid: str) -> str:
    """Build protected owner-only SDDL for Named Pipes (``CurrentUserSID`` + ``SYSTEM``)."""
    from mesh.platform import perms as _perms

    sid = _perms._validate_sid_str(user_sid)
    trustees = [sid]
    if sid != _perms.LOCAL_SYSTEM_SID:
        trustees.append("SY")
    aces = "".join(f"(A;;GA;;;{t})" for t in trustees)
    return f"D:P{aces}"


def get_process_user_sid_win(
    pid: int,
    *,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> str:
    """Open *pid* and return its ``TokenUser`` SID string, failing closed on error."""
    from mesh.platform import perms as _perms

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    h_proc = _open_process_or_none_win(
        int(pid),
        w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if h_proc is None:
        raise PermissionError(errno.EACCES, f"Peer process {pid} does not exist or cannot be opened")
    try:
        if not _is_handle_alive_win(h_proc, k32=dll_k32, err_fn=err_fn):
            raise PermissionError(errno.EACCES, f"Peer process {pid} is no longer running")
        h_tok = wintypes.HANDLE()
        if not dll_adv.OpenProcessToken(h_proc, w32.TOKEN_QUERY, ctypes.byref(h_tok)):
            raise _win_proc_error(err_fn())
        try:
            return _perms._query_token_sid(
                h_tok.value if hasattr(h_tok, "value") else h_tok,
                w32.TokenUser,
                advapi=dll_adv,
                k32=dll_k32,
                get_last_error_fn=err_fn,
            )
        finally:
            tok_val = h_tok.value if hasattr(h_tok, "value") else h_tok
            if tok_val:
                dll_k32.CloseHandle(tok_val)
    finally:
        dll_k32.CloseHandle(h_proc)


def find_active_job_for_pid_win(
    pid: int,
    active_runs: Dict[str, int],
    *,
    active_job_handles: Optional[Dict[str, Any]] = None,
    home: Optional[str] = None,
    k32: Any = None,
    advapi: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[str]:
    """Return active ``run_id`` if *pid* is inside any active run's Job Object.

    Fails closed by raising ``OSError`` / ``PermissionError`` if the live process or
    Job Objects cannot be inspected.
    """
    job_map = dict(active_job_handles) if active_job_handles else {}
    candidate_ids: List[str] = list(job_map.keys())
    if home:
        for rid in active_runs:
            if rid not in job_map:
                candidate_ids.append(rid)
    if not candidate_ids:
        return None

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    h_proc = _open_process_or_none_win(
        int(pid),
        w32.PROCESS_QUERY_LIMITED_INFORMATION | w32.SYNCHRONIZE,
        k32=dll_k32,
        err_fn=err_fn,
    )
    if h_proc is None:
        raise OSError(errno.ESRCH, f"Peer process {pid} does not exist")

    try:
        if not _is_handle_alive_win(h_proc, k32=dll_k32, err_fn=err_fn):
            raise OSError(errno.ESRCH, f"Peer process {pid} is no longer running")
        for rid in candidate_ids:
            if rid in job_map:
                if is_process_handle_in_job_win(
                    h_proc, job_map[rid], k32=dll_k32, get_last_error_fn=err_fn
                ):
                    return rid
            elif home:
                opened = open_run_job_win(
                    home, rid, terminate=False, k32=dll_k32, advapi=advapi, get_last_error_fn=err_fn
                )
                if opened is not None:
                    try:
                        if is_process_handle_in_job_win(
                            h_proc, opened, k32=dll_k32, get_last_error_fn=err_fn
                        ):
                            return rid
                    finally:
                        opened.close()
        return None
    finally:
        dll_k32.CloseHandle(h_proc)
