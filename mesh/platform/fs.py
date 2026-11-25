"""Cross-platform filesystem primitives (atomic replace, shared read, fsync, nofollow open).

Windows-specific imports (``msvcrt`` and Win32 ctypes loaders) are guarded so
importing this module on Linux never loads them.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os
import stat
import sys
import time
from typing import Any, Callable

if sys.platform != "win32":
    from mesh.platform import _posix

# Bounded 5-step micro-backoff (~77 ms total) for transient AV/reader sharing locks.
_REPLACE_BACKOFF_S = (0.002, 0.005, 0.010, 0.020, 0.040)
_TRANSIENT_REPLACE_WINERRORS = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION
_READ_CHUNK_BYTES = 65536


def atomic_replace_win(
    src: str,
    dst: str,
    *,
    replace_fn: Callable[[str, str], None] = os.replace,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Replace *dst* with *src*, retrying transiently on WinError 5/32."""
    for delay in _REPLACE_BACKOFF_S:
        try:
            replace_fn(src, dst)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in _TRANSIENT_REPLACE_WINERRORS:
                raise
            sleep_fn(delay)
    replace_fn(src, dst)


def read_bytes_shared_win(path: str, *, k32: Any = None) -> bytes:
    """Read all bytes from *path* on Windows with FILE_SHARE_READ|WRITE|DELETE."""
    from mesh.platform import _win32_ctypes as w32

    dll = k32 if k32 is not None else w32.get_kernel32()
    fspath = os.fspath(path)
    handle = dll.CreateFileW(
        fspath,
        w32.GENERIC_READ,
        w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
        None,
        w32.OPEN_EXISTING,
        w32.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if w32.is_invalid_handle(handle):
        raise w32.win_error(w32.get_last_error(), fspath)

    try:
        ftype = dll.GetFileType(handle)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err = w32.get_last_error()
            if err != 0:
                raise w32.win_error(err, fspath)
        if ftype != w32.FILE_TYPE_DISK:
            raise OSError(errno.EINVAL, "Refusing non-disk file", fspath)

        buf = ctypes.create_string_buffer(_READ_CHUNK_BYTES)
        nread = wintypes.DWORD(0)
        chunks = []
        while True:
            ok = dll.ReadFile(handle, buf, len(buf), ctypes.byref(nread), None)
            if not ok:
                err = w32.get_last_error()
                if err == w32.ERROR_HANDLE_EOF:
                    break
                raise w32.win_error(err, fspath)
            if nread.value == 0:
                break
            chunks.append(buf.raw[: nread.value])
        return b"".join(chunks)
    finally:
        dll.CloseHandle(handle)


def fsync_dir_win(dir_path: str) -> None:
    """Safe no-op on Windows (NTFS forbids ``os.open(dir, O_RDONLY)``)."""
    _ = dir_path
    return None


def _win_open_access_and_disposition(
    flags: int, fspath: str, w32: Any
) -> tuple[int, int, int, int, bool]:
    """Translate POSIX ``os.open`` flags to Win32 access, disposition, and CRT osf flags."""
    accmode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
    if accmode == os.O_RDONLY:
        access = w32.GENERIC_READ
    elif accmode == os.O_WRONLY:
        access = w32.GENERIC_WRITE | w32.FILE_READ_ATTRIBUTES
    elif accmode == os.O_RDWR:
        access = w32.GENERIC_READ | w32.GENERIC_WRITE
    else:
        raise OSError(errno.EINVAL, "Invalid open access mode", fspath)

    creat = bool(flags & os.O_CREAT)
    excl = bool(flags & os.O_EXCL)
    trunc = bool(flags & os.O_TRUNC)
    if trunc and accmode == os.O_RDONLY:
        raise OSError(errno.EINVAL, "O_TRUNC requires write access", fspath)

    # Never use CREATE_ALWAYS or TRUNCATE_EXISTING before our reparse check,
    # otherwise CreateFileW would truncate/clobber before inspection.
    if creat and excl:
        disposition = w32.CREATE_NEW
    elif creat:
        disposition = w32.OPEN_ALWAYS
    else:
        disposition = w32.OPEN_EXISTING

    osf_flags = accmode | getattr(os, "O_NOINHERIT", 0)
    o_text = getattr(os, "O_TEXT", 0)
    if o_text and (flags & o_text):
        osf_flags |= o_text
    else:
        osf_flags |= getattr(os, "O_BINARY", 0)
    if flags & os.O_APPEND:
        osf_flags |= os.O_APPEND

    return access, disposition, osf_flags, accmode, trunc


def _win_long_path(fspath: str) -> str:
    """Add ``\\\\?\\`` extended-path prefix on Windows when ``len(fspath) >= 240``."""
    if (
        not fspath
        or fspath.startswith(("\\\\?\\", "\\\\.\\"))
        or fspath.upper() in ("NUL", "CON", "PRN", "AUX")
    ):
        return fspath
    if len(fspath) >= 240:
        absp = os.path.abspath(fspath)
        if len(absp) >= 240 and not absp.startswith("\\\\"):
            return "\\\\?\\" + absp
    return fspath


def open_regular_nofollow_win(
    path: str,
    flags: int,
    mode: int = 0o600,
    *,
    label: str = "file",
    k32: Any = None,
    msvcrt_mod: Any = None,
) -> int:
    """Open a regular non-reparse disk file on Windows and return a CRT fd."""
    _ = mode  # NTFS DACLs are managed by platform.perms; mode 0o600 is default rw.
    from mesh.platform import _win32_ctypes as w32

    fspath = os.fspath(path)
    open_path = _win_long_path(fspath) if k32 is None else fspath
    access, disposition, osf_flags, _accmode, need_trunc = _win_open_access_and_disposition(
        flags, fspath, w32
    )
    dll = k32 if k32 is not None else w32.get_kernel32()
    if msvcrt_mod is None:
        import msvcrt as msvcrt_mod  # lazy Windows-only import

    handle = dll.CreateFileW(
        open_path,
        access,
        w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
        None,
        disposition,
        w32.FILE_ATTRIBUTE_NORMAL | w32.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if w32.is_invalid_handle(handle):
        err = w32.get_last_error()
        if err == w32.ERROR_ACCESS_DENIED and k32 is None:
            import stat as _stat

            try:
                st = os.lstat(open_path)
            except OSError:
                st = None
            if st is not None:
                if (getattr(st, "st_file_attributes", 0) or 0) & w32.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
                if _stat.S_ISDIR(st.st_mode):
                    raise OSError(errno.EINVAL, f"Refusing non-regular {label}", fspath)
        raise w32.win_error(err, fspath)

    fd: int | None = None
    try:
        ftype = dll.GetFileType(handle)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err = w32.get_last_error()
            if err != 0:
                raise w32.win_error(err, fspath)
        if ftype != w32.FILE_TYPE_DISK:
            raise OSError(errno.EINVAL, f"Refusing non-regular {label}", fspath)

        info = w32.BY_HANDLE_FILE_INFORMATION()
        if not dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise w32.win_error(w32.get_last_error(), fspath)
        if info.dwFileAttributes & w32.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
        if info.dwFileAttributes & w32.FILE_ATTRIBUTE_DIRECTORY:
            raise OSError(errno.EINVAL, f"Refusing non-regular {label}", fspath)

        if need_trunc:
            if not dll.SetFilePointerEx(handle, 0, None, w32.FILE_BEGIN):
                raise w32.win_error(w32.get_last_error(), fspath)
            if not dll.SetEndOfFile(handle):
                raise w32.win_error(w32.get_last_error(), fspath)

        h_val = handle.value if hasattr(handle, "value") else handle
        fd = int(msvcrt_mod.open_osfhandle(h_val, osf_flags))
        return fd
    except BaseException:
        if fd is None:
            dll.CloseHandle(handle)
        else:
            os.close(fd)
        raise


def atomic_replace(src: str, dst: str) -> None:
    """Atomically replace *dst* with *src* (bounded retry on WinError 5/32 on Windows)."""
    if sys.platform == "win32":
        atomic_replace_win(src, dst)
        return
    _posix.atomic_replace(src, dst)


def read_bytes_shared(path: str) -> bytes:
    """Read *path* in binary mode, permitting concurrent delete/rename on Windows."""
    if sys.platform == "win32":
        return read_bytes_shared_win(path)
    return _posix.read_bytes_shared(path)


def fsync_dir(dir_path: str) -> None:
    """Flush directory entry metadata on POSIX; safe no-op on Windows."""
    if sys.platform == "win32":
        fsync_dir_win(dir_path)
        return
    _posix.fsync_dir(dir_path)


def open_regular_nofollow(
    path: str, flags: int, mode: int = 0o600, label: str = "file"
) -> int:
    """Open a regular file without following symlinks, junctions, or non-disk files."""
    if sys.platform == "win32":
        return open_regular_nofollow_win(path, flags, mode, label=label)
    return _posix.open_regular_nofollow(path, flags, mode, label=label)


def unlink_nonregular_win(path: str) -> None:
    """Unlink a file (clearing ``FILE_ATTRIBUTE_READONLY``), symlink, or NTFS junction."""
    from mesh.platform import paths as _plat_paths

    try:
        os.unlink(path)
    except OSError as exc:
        is_reparse = _plat_paths.is_reparse_link(path)
        if isinstance(exc, PermissionError) and not is_reparse:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                os.unlink(path)
                return
            except OSError:
                pass
        if is_reparse:
            try:
                os.rmdir(path)
                return
            except OSError:
                pass
        raise


def unlink_nonregular(path: str) -> None:
    """Unlink a file, POSIX symlink, or Windows symlink/NTFS directory junction."""
    if sys.platform == "win32":
        unlink_nonregular_win(path)
        return
    _posix.unlink_nonregular(path)
