"""Cross-platform advisory file locking (fcntl.flock on POSIX, LockFileEx on Windows).

Windows-specific imports (``msvcrt`` and Win32 ctypes loaders) and POSIX-only
``fcntl`` imports are guarded so importing this module on either OS never loads
the other OS's primitives.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Any, Callable, Optional

if sys.platform != "win32":
    from mesh.platform import _posix


def open_lock_file(path: str, *, mode: int = 0o600) -> int:
    """Open or create *path* for locking and mark the descriptor non-inheritable."""
    if sys.platform != "win32":
        return _posix.open_lock_file(path, mode=mode)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        os.set_inheritable(fd, False)
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire_lock_win(
    fd: int,
    *,
    exclusive: bool = True,
    blocking: bool = True,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    set_inheritable_fn: Callable[[int, bool], None] = os.set_inheritable,
) -> None:
    """Acquire a byte-0 range lock on *fd* via Win32 ``LockFileEx``."""
    from mesh.platform import _win32_ctypes as w32

    set_inheritable_fn(fd, False)
    dll = k32 if k32 is not None else w32.get_kernel32()
    if msvcrt_mod is None:
        import msvcrt as msvcrt_mod  # lazy Windows-only import

    handle = msvcrt_mod.get_osfhandle(fd)
    if w32.is_invalid_handle(handle):
        raise OSError(errno.EBADF, "Invalid file descriptor handle")

    flags = w32.LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
    if not blocking:
        flags |= w32.LOCKFILE_FAIL_IMMEDIATELY

    ov = w32.OVERLAPPED()
    ok = dll.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov))
    if not ok:
        err = get_last_error_fn() if get_last_error_fn is not None else w32.get_last_error()
        if err == w32.ERROR_LOCK_VIOLATION:
            exc = BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            exc.winerror = err  # type: ignore[attr-defined]
            raise exc
        raise w32.win_error(err)


def release_lock_win(
    fd: int,
    *,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Release the byte-0 range lock on *fd* via Win32 ``UnlockFileEx``."""
    from mesh.platform import _win32_ctypes as w32

    dll = k32 if k32 is not None else w32.get_kernel32()
    if msvcrt_mod is None:
        import msvcrt as msvcrt_mod  # lazy Windows-only import

    handle = msvcrt_mod.get_osfhandle(fd)
    if w32.is_invalid_handle(handle):
        raise OSError(errno.EBADF, "Invalid file descriptor handle")

    ov = w32.OVERLAPPED()
    ok = dll.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))
    if not ok:
        err = get_last_error_fn() if get_last_error_fn is not None else w32.get_last_error()
        raise w32.win_error(err)


def is_exclusive_lock_held_win(
    path: str,
    *,
    open_fn: Callable[..., int] = os.open,
    close_fn: Callable[[int], None] = os.close,
    acquire_fn: Optional[Callable[..., None]] = None,
    release_fn: Optional[Callable[[int], None]] = None,
) -> bool:
    """Probe whether another handle holds an exclusive lock on *path* on Windows."""
    acq = acquire_fn if acquire_fn is not None else acquire_lock_win
    rel = release_fn if release_fn is not None else release_lock_win
    try:
        fd = open_fn(path, os.O_RDONLY)
    except OSError:
        return False

    acquired = False
    try:
        acq(fd, exclusive=False, blocking=False)
        acquired = True
        return False
    except (BlockingIOError, OSError):
        return True
    finally:
        if acquired:
            try:
                rel(fd)
            except OSError:
                pass
        close_fn(fd)


def acquire_lock(fd: int, *, exclusive: bool = True, blocking: bool = True) -> None:
    """Acquire an exclusive or shared advisory lock on open descriptor *fd*."""
    if sys.platform == "win32":
        acquire_lock_win(fd, exclusive=exclusive, blocking=blocking)
        return
    _posix.acquire_lock(fd, exclusive=exclusive, blocking=blocking)


def release_lock(fd: int) -> None:
    """Release an advisory lock held on open descriptor *fd*."""
    if sys.platform == "win32":
        release_lock_win(fd)
        return
    _posix.release_lock(fd)


def is_exclusive_lock_held(path: str) -> bool:
    """Return True if an exclusive lock is currently held on *path*."""
    if sys.platform == "win32":
        return is_exclusive_lock_held_win(path)
    return _posix.is_exclusive_lock_held(path)


class FileLock:
    """Context manager managing a non-inheritable advisory lock on *path*."""

    def __init__(self, path: str, *, exclusive: bool = True, blocking: bool = True) -> None:
        self.path = path
        self.exclusive = exclusive
        self.blocking = blocking
        self.fd: Optional[int] = None

    def acquire(self) -> int:
        if self.fd is not None:
            return self.fd
        fd = open_lock_file(self.path)
        try:
            acquire_lock(fd, exclusive=self.exclusive, blocking=self.blocking)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return fd

    def release(self) -> None:
        if self.fd is None:
            return
        fd = self.fd
        self.fd = None
        try:
            release_lock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
