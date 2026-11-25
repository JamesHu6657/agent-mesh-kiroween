"""Cross-platform path predicates, normalization, and reparse-point checks.

Linux paths behave byte-identically to the pre-port implementation.
"""

from __future__ import annotations

import ntpath
import os
import stat
import sys
import tempfile
from typing import Any, Callable, List

if sys.platform != "win32":
    from mesh.platform import _posix
else:
    _posix: Any = None


def _posix_mod() -> Any:
    """Lazily import ``mesh.platform._posix`` (supports mocked Linux tests on Windows)."""
    global _posix
    if _posix is None:
        from mesh.platform import _posix as posix_mod

        _posix = posix_mod
    return _posix

_REPARSE_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_fs_root_win(path: str, *, pathmod: Any = ntpath) -> bool:
    """Return True if *path* is a Windows drive root, UNC share root, or ``\\`` / ``/``."""
    if not path:
        return False
    drive, tail = pathmod.splitdrive(path)
    if tail in ("\\", "/"):
        return True
    if not tail and (drive.startswith("\\\\") or drive.startswith("//")):
        return True
    return False


def _strip_win_extended_prefix(path: str) -> str:
    """Normalize ``\\\\?\\UNC\\...`` -> ``\\\\...`` and ``\\\\?\\<drive>:...`` -> ``<drive>:...``."""
    if len(path) >= 8 and path[:4] == "\\\\?\\" and path[4:8].upper() == "UNC\\":
        return "\\\\" + path[8:]
    if (
        len(path) >= 6
        and path[:4] == "\\\\?\\"
        and path[4].isascii()
        and path[4].isalpha()
        and path[5] == ":"
    ):
        return path[4:]
    return path


def norm_cmp_win(path: str, *, pathmod: Any = ntpath) -> str:
    """Case- and separator-normalize a Windows path for comparison."""
    return _strip_win_extended_prefix(pathmod.normcase(path))


def same_path_win(
    a: str,
    b: str,
    *,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    pathmod: Any = ntpath,
) -> bool:
    """Return True if *a* and *b* refer to the same canonical path on Windows."""
    return norm_cmp_win(realpath_fn(a), pathmod=pathmod) == norm_cmp_win(
        realpath_fn(b), pathmod=pathmod
    )


def _ensure_unc_root_sep(path: str, pathmod: Any) -> str:
    """Append a trailing backslash for bare ``\\\\server\\share`` UNC roots for commonpath."""
    drive, tail = pathmod.splitdrive(path)
    if not tail and (drive.startswith("\\\\") or drive.startswith("//")):
        return drive + "\\"
    return path


def is_path_equal_or_parent_win(
    parent: str,
    child: str,
    *,
    realpath_fn: Callable[[str], str] = os.path.realpath,
    pathmod: Any = ntpath,
) -> bool:
    """Return True if *child* is equal to or inside *parent* on Windows."""
    parent_cmp = _ensure_unc_root_sep(norm_cmp_win(realpath_fn(parent), pathmod=pathmod), pathmod)
    child_cmp = _ensure_unc_root_sep(norm_cmp_win(realpath_fn(child), pathmod=pathmod), pathmod)
    if parent_cmp == child_cmp:
        return True
    try:
        return pathmod.commonpath([parent_cmp, child_cmp]) == parent_cmp
    except ValueError:
        return False


def is_reparse_stat_win(st: Any) -> bool:
    """Return True if lstat result *st* is a symlink or NTFS reparse point."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool((getattr(st, "st_file_attributes", 0) or 0) & _REPARSE_ATTR)


def strip_trailing_seps_win(path: str, *, pathmod: Any = ntpath) -> str:
    """Strip trailing ``\\`` and ``/`` separators on Windows, preserving drive/UNC roots."""
    if is_fs_root_win(path, pathmod=pathmod):
        return path
    return path.rstrip("\\/") or path


def is_reparse_link_win(path: str) -> bool:
    """Return True if *path* is a Windows symlink, NTFS junction, or reparse point."""
    if not path:
        return False
    # Strip trailing separators so "junction\\" cannot bypass the reparse check.
    probe = strip_trailing_seps_win(path)
    if len(probe) >= 240 and not probe.startswith(("\\\\?\\", "\\\\.\\")):
        absp = os.path.abspath(probe)
        if len(absp) >= 240 and not absp.startswith("\\\\"):
            probe = "\\\\?\\" + absp
    if os.path.islink(probe):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None and isjunction(probe):
        return True
    try:
        st = os.lstat(probe)
    except (OSError, ValueError):
        return False
    return is_reparse_stat_win(st)


def long_path_win(path: str) -> str:
    """Expand Windows 8.3 short components (e.g. ``RUNNER~1``) without resolving symlinks."""
    if not path or sys.platform != "win32" or os.name != "nt":
        return path
    import ctypes
    from ctypes import wintypes

    probe = path
    tail_parts: List[str] = []
    while probe and not os.path.lexists(probe):
        if not is_fs_root_win(probe) and probe.endswith(("\\", "/")):
            probe = probe.rstrip("\\/")
            continue
        parent, leaf = ntpath.split(probe)
        if not leaf or parent == probe:
            break
        tail_parts.append(leaf)
        probe = parent
    if not probe or not os.path.lexists(probe):
        return path

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_long = k32.GetLongPathNameW
    get_long.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_long.restype = wintypes.DWORD

    buf_len = max(260, len(probe) + 64)
    buf = ctypes.create_unicode_buffer(buf_len)
    rc = int(get_long(probe, buf, buf_len))
    if rc > buf_len:
        buf = ctypes.create_unicode_buffer(rc + 1)
        rc = int(get_long(probe, buf, rc + 1))
    if rc <= 0:
        return path
    expanded = buf.value
    for part in reversed(tail_parts):
        expanded = ntpath.join(expanded, part)
    return expanded


def temp_root_win() -> str:
    """Return the canonicalized Windows temp root (expanding 8.3 short names)."""
    return long_path_win(os.path.realpath(tempfile.gettempdir()))


def split_path_segments_win(path: str) -> List[str]:
    """Split *path* on both ``\\`` and ``/``, dropping empty segments."""
    return [seg for seg in path.replace("/", "\\").split("\\") if seg]


def has_traversal_segment_win(path: str) -> bool:
    """Return True if *path* contains ``..`` under either ``\\`` or ``/`` separators."""
    if len(path) >= 2 and path[1] == ":" and path[0].isascii() and path[0].isalpha():
        path = path[2:]
    return any(seg == ".." for seg in split_path_segments_win(path))


def is_fs_root(path: str) -> bool:
    """Return True if *path* is a filesystem root (``/`` on POSIX; drive/UNC root on Windows)."""
    if sys.platform == "win32":
        return is_fs_root_win(path)
    return _posix_mod().is_fs_root(path)


def norm_cmp(path: str) -> str:
    """Normalize *path* for equality/containment comparison (identity on POSIX)."""
    if sys.platform == "win32":
        return norm_cmp_win(path)
    return _posix_mod().norm_cmp(path)


def same_path(a: str, b: str) -> bool:
    """Return True if *a* and *b* resolve to the same canonical path."""
    if sys.platform == "win32":
        return same_path_win(a, b)
    return _posix_mod().same_path(a, b)


def is_path_equal_or_parent(parent: str, child: str) -> bool:
    """Return True if *child* is equal to *parent* or located inside *parent*."""
    if sys.platform == "win32":
        return is_path_equal_or_parent_win(parent, child)
    return _posix_mod().is_path_equal_or_parent(parent, child)


def is_reparse_link(path: str) -> bool:
    """Return True if *path* is a symlink (or NTFS junction/reparse point on Windows)."""
    if sys.platform == "win32":
        return is_reparse_link_win(path)
    return _posix_mod().is_reparse_link(path)


def is_reparse_stat(st: os.stat_result) -> bool:
    """Return True if *st* represents a symlink or reparse point."""
    if sys.platform == "win32":
        return is_reparse_stat_win(st)
    return _posix_mod().is_reparse_stat(st)


def temp_root() -> str:
    """Return the platform temp root (``/tmp`` on POSIX; canonical ``gettempdir`` on Windows)."""
    if sys.platform == "win32":
        return temp_root_win()
    return _posix_mod().temp_root()


def split_path_segments(path: str) -> List[str]:
    """Split *path* into non-empty components using platform separators."""
    if sys.platform == "win32":
        return split_path_segments_win(path)
    return _posix_mod().split_path_segments(path)


def has_traversal_segment(path: str) -> bool:
    """Return True if *path* contains any ``..`` traversal component."""
    if sys.platform == "win32":
        return has_traversal_segment_win(path)
    return _posix_mod().has_traversal_segment(path)


def strip_trailing_seps(path: str) -> str:
    """Strip trailing directory separators (``/`` on POSIX; ``\\`` and ``/`` on Windows)."""
    if sys.platform == "win32":
        return strip_trailing_seps_win(path)
    return _posix_mod().strip_trailing_seps(path)
