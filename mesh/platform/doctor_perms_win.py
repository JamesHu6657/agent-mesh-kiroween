"""Windows handle-bound `mesh doctor --fix-perms` implementation (Phase 2b / T05).

Linux ``mesh/cli/commands/doctor.py:_fix_perms`` remains completely untouched.
On Windows, this module walks ``$MESH_HOME`` without ever following symlinks or
NTFS directory junctions:

1. Opens ``$MESH_HOME`` component-by-component from the drive/UNC root using
   ``CreateFileW`` + ``ntdll.NtOpenFile`` with ``FILE_OPEN_REPARSE_POINT``.
2. Verifies ``GetFileInformationByHandle`` matches the precheck ``(vol, file_index)``
   and owner SID is trusted.
3. Walks known files and known directories via handle-relative ``NtOpenFile``
   (``RootDirectory = parent_handle``) and ``NtQueryDirectoryFile`` so swapped
   ancestors cannot redirect opens outside ``$MESH_HOME``.
4. Skips reparse points (symlinks and junctions) with ``"symlink, not followed"``,
   hardlinked regular files (``nNumberOfLinks > 1``) with ``"hardlinked"``, and
   foreign-owned entries.
5. Applies protected private NTFS DACLs directly on open handles via
   ``SetSecurityInfo``.
6. Re-verifies every repaired entry's ``(vol, file_index)``, non-reparse status,
   and DACL after the walk, populating ``report.unsafe`` if anything changed.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import ntpath
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from mesh.platform import perms

_KNOWN_DIRS = ("run", "runs", "workspaces", "logs", "merges")
_KNOWN_FILES = ("mesh.db", "mesh.db-wal", "mesh.db-shm", "mesh.db-journal")

WinIdent = Tuple[int, int]


class WinFixReport:
    """Report matching ``doctor._FixReport`` attributes (skipped, repaired, unsafe)."""

    def __init__(self) -> None:
        self.skipped: List[Dict[str, str]] = []
        self.repaired: Dict[str, Tuple[WinIdent, int]] = {}
        self.unsafe: List[Dict[str, str]] = []

    def skip(self, rel: str, reason: str) -> None:
        self.skipped.append({"path": rel, "reason": reason})

    def done(self, rel: str, ident: WinIdent, mode: int) -> None:
        self.repaired[rel] = (ident, mode)


def _handle_ident(
    handle: Any,
    *,
    k32: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Tuple[WinIdent, int, int]:
    """Return ``((dwVolumeSerialNumber, file_index_64), dwFileAttributes, nNumberOfLinks)``."""
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    ftype = k32.GetFileType(handle)
    if ftype == w32.FILE_TYPE_UNKNOWN:
        err = err_fn()
        if err != 0:
            raise w32.win_error(err)
    if ftype != w32.FILE_TYPE_DISK:
        raise OSError(errno.EINVAL, "Refusing non-disk file handle")

    info = w32.BY_HANDLE_FILE_INFORMATION()
    if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise w32.win_error(err_fn())
    file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    return (
        (int(info.dwVolumeSerialNumber), file_index),
        int(info.dwFileAttributes),
        int(info.nNumberOfLinks),
    )


def _nt_error(status: int, ntdll: Any, w32: Any, path: Optional[str] = None) -> OSError:
    dos_err = int(ntdll.RtlNtStatusToDosError(status))
    if dos_err == 0:
        dos_err = w32.ERROR_ACCESS_DENIED
    return w32.win_error(dos_err, path)


def _open_rel_nt(
    parent_handle: Any,
    name: str,
    *,
    desired_access: int,
    directory_only: bool = False,
    ntdll: Any,
    k32: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Any]:
    """Open single child component *name* relative to *parent_handle* without following reparse points.

    Returns ``None`` if the child does not exist.
    """
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "\\" in name
        or "/" in name
        or ":" in name
        or "\x00" in name
    ):
        raise OSError(errno.EINVAL, "Invalid child entry name", str(name))

    name_raw = name.encode("utf-16-le")
    name_buf = ctypes.create_string_buffer(name_raw + b"\x00\x00")
    byte_len = len(name_raw)
    us = w32.UNICODE_STRING(
        Length=byte_len,
        MaximumLength=byte_len + 2,
        Buffer=ctypes.addressof(name_buf),
    )
    oa = w32.OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(w32.OBJECT_ATTRIBUTES),
        RootDirectory=parent_handle,
        ObjectName=ctypes.pointer(us),
        Attributes=w32.OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_sb = w32.IO_STATUS_BLOCK()
    handle = wintypes.HANDLE()
    open_opts = (
        w32.FILE_OPEN_REPARSE_POINT
        | w32.FILE_OPEN_FOR_BACKUP_INTENT
        | w32.FILE_SYNCHRONOUS_IO_NONALERT
    )
    if directory_only:
        open_opts |= w32.FILE_DIRECTORY_FILE
    share = w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE

    status = int(
        ntdll.NtOpenFile(
            ctypes.byref(handle),
            desired_access,
            ctypes.byref(oa),
            ctypes.byref(io_sb),
            share,
            open_opts,
        )
    )
    if status < 0:
        err = _nt_error(status, ntdll, w32, name)
        if isinstance(err, FileNotFoundError) or getattr(err, "winerror", None) in (
            w32.ERROR_FILE_NOT_FOUND,
            w32.ERROR_PATH_NOT_FOUND,
        ):
            return None
        raise err

    # Verify disk type cleanly; close if invalid.
    h_val = handle.value if hasattr(handle, "value") else handle
    try:
        ftype = k32.GetFileType(h_val)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err_code = get_last_error_fn() if get_last_error_fn is not None else w32.get_last_error()
            if err_code != 0:
                raise w32.win_error(err_code, name)
        return h_val
    except BaseException:
        k32.CloseHandle(h_val)
        raise


def _split_win_root_and_parts(home_dir: str) -> Tuple[str, List[str]]:
    raw = os.fspath(home_dir)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise OSError(errno.EINVAL, "Expected non-empty absolute Windows path", home_dir)
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[len("\\\\?\\UNC\\") :]
    elif raw.startswith("\\\\?\\"):
        raw = raw[len("\\\\?\\") :]
    drive, tail = ntpath.splitdrive(raw)
    if not drive or not ntpath.isabs(raw):
        raise OSError(errno.EINVAL, "Expected absolute Windows path with drive or UNC root", home_dir)
    if drive.startswith("\\\\") or drive.startswith("//"):
        norm_drive = drive.replace("/", "\\")
        root = norm_drive if norm_drive.endswith("\\") else (norm_drive + "\\")
    else:
        root = drive + "\\"
    parts = [p for p in tail.replace("/", "\\").split("\\") if p and p != "."]
    if any(p == ".." for p in parts):
        raise OSError(errno.EINVAL, "Refusing '..' component in home path", home_dir)
    return root, parts


def _open_home_nofollow_win(
    home_dir: str,
    *,
    desired_access: int,
    k32: Any,
    ntdll: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Open *home_dir* component-by-component from its drive/UNC root without following reparse points."""
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    root, parts = _split_win_root_and_parts(home_dir)
    cur = k32.CreateFileW(
        root,
        desired_access if not parts else (w32.FILE_READ_ATTRIBUTES | w32.SYNCHRONIZE),
        w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
        None,
        w32.OPEN_EXISTING,
        w32.FILE_FLAG_BACKUP_SEMANTICS | w32.FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if w32.is_invalid_handle(cur):
        raise w32.win_error(err_fn(), root)

    try:
        _ident, attrs, _nlink = _handle_ident(cur, k32=k32, w32=w32, get_last_error_fn=err_fn)
        if not (attrs & w32.FILE_ATTRIBUTE_DIRECTORY):
            raise OSError(errno.ENOTDIR, "Expected root directory", root)
        if attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "Refusing reparse point root", root)

        for idx, part in enumerate(parts):
            is_last = idx == len(parts) - 1
            access = desired_access if is_last else (w32.FILE_READ_ATTRIBUTES | w32.SYNCHRONIZE)
            nxt = _open_rel_nt(
                cur,
                part,
                desired_access=access,
                directory_only=True,
                ntdll=ntdll,
                k32=k32,
                w32=w32,
                get_last_error_fn=err_fn,
            )
            if nxt is None:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", home_dir)
            try:
                _c_ident, c_attrs, _c_nlink = _handle_ident(
                    nxt, k32=k32, w32=w32, get_last_error_fn=err_fn
                )
                if c_attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise OSError(errno.ELOOP, "Refusing symlink or reparse point", part)
                if not (c_attrs & w32.FILE_ATTRIBUTE_DIRECTORY):
                    raise OSError(errno.ENOTDIR, "Expected directory", part)
            except BaseException:
                k32.CloseHandle(nxt)
                raise
            k32.CloseHandle(cur)
            cur = nxt
        return cur
    except BaseException:
        k32.CloseHandle(cur)
        raise


def _listdir_handle_win(
    dir_handle: Any,
    *,
    ntdll: Any,
    w32: Any,
) -> List[str]:
    """Enumerate child names directly from an open directory *dir_handle* via ``NtQueryDirectoryFile``."""
    buf = ctypes.create_string_buffer(65536)
    base_addr = ctypes.addressof(buf)
    names: List[str] = []
    restart = 1
    while True:
        io_sb = w32.IO_STATUS_BLOCK()
        status = int(
            ntdll.NtQueryDirectoryFile(
                dir_handle,
                None,
                None,
                None,
                ctypes.byref(io_sb),
                buf,
                len(buf),
                w32.FILE_NAMES_INFORMATION_CLASS,
                0,
                None,
                restart,
            )
        )
        restart = 0
        if status == w32.STATUS_NO_MORE_FILES:
            break
        if status < 0 and status != w32.STATUS_BUFFER_OVERFLOW:
            raise _nt_error(status, ntdll, w32)
        if status == w32.STATUS_BUFFER_OVERFLOW and io_sb.Information == 0:
            raise OSError(errno.EIO, "Directory entry exceeds query buffer")

        off = 0
        while True:
            info = w32.FILE_NAMES_INFORMATION.from_address(base_addr + off)
            name = ctypes.string_at(base_addr + off + 12, info.FileNameLength).decode("utf-16-le")
            if name not in (".", ".."):
                names.append(name)
            if info.NextEntryOffset == 0:
                break
            off += info.NextEntryOffset
    return sorted(names)


def _open_dir_for_listing_win(
    parent_handle: Optional[Any],
    name: str,
    want_ident: WinIdent,
    orig_handle: Optional[Any] = None,
    *,
    home_dir: Optional[str] = None,
    k32: Any,
    ntdll: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Any]:
    """Open a listing handle on the same directory object as *want_ident*; None if changed/reparse."""
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    access = w32.FILE_LIST_DIRECTORY | w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.SYNCHRONIZE
    if parent_handle is None:
        h_list = w32.INVALID_HANDLE_VALUE
        if orig_handle is not None and hasattr(k32, "ReOpenFile"):
            h_list = k32.ReOpenFile(
                orig_handle,
                access,
                w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
                w32.FILE_FLAG_BACKUP_SEMANTICS | w32.FILE_FLAG_OPEN_REPARSE_POINT,
            )
        if w32.is_invalid_handle(h_list):
            if home_dir is None:
                return None
            h_list = _open_home_nofollow_win(
                home_dir,
                desired_access=access,
                k32=k32,
                ntdll=ntdll,
                w32=w32,
                get_last_error_fn=err_fn,
            )
    else:
        try:
            h_list = _open_rel_nt(
                parent_handle,
                name,
                desired_access=access,
                directory_only=True,
                ntdll=ntdll,
                k32=k32,
                w32=w32,
                get_last_error_fn=err_fn,
            )
        except OSError:
            return None
        if h_list is None:
            return None

    try:
        got_ident, attrs, _nlink = _handle_ident(h_list, k32=k32, w32=w32, get_last_error_fn=err_fn)
        if (attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT) or not (attrs & w32.FILE_ATTRIBUTE_DIRECTORY):
            k32.CloseHandle(h_list)
            return None
        if got_ident != want_ident:
            k32.CloseHandle(h_list)
            return None
        return h_list
    except BaseException:
        k32.CloseHandle(h_list)
        raise


def _open_entry_with_fallback(
    parent_handle: Any,
    name: str,
    *,
    ntdll: Any,
    k32: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Any]:
    access_rw = w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.WRITE_DAC | w32.SYNCHRONIZE
    try:
        return _open_rel_nt(
            parent_handle,
            name,
            desired_access=access_rw,
            directory_only=False,
            ntdll=ntdll,
            k32=k32,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
    except PermissionError:
        access_ro = w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.SYNCHRONIZE
        return _open_rel_nt(
            parent_handle,
            name,
            desired_access=access_ro,
            directory_only=False,
            ntdll=ntdll,
            k32=k32,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )


def _fix_entry_win(
    parent_handle: Any,
    name: str,
    rel: str,
    user_sid: str,
    trusted_owners: set[str],
    report: WinFixReport,
    hook: Callable[[str, str], None],
    *,
    k32: Any,
    advapi: Any,
    ntdll: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[Tuple[Any, WinIdent]]:
    try:
        handle = _open_entry_with_fallback(
            parent_handle,
            name,
            ntdll=ntdll,
            k32=k32,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
    except OSError as exc:
        report.skip(rel, f"cannot open ({type(exc).__name__})")
        return None
    if handle is None:
        return None

    try:
        ftype = k32.GetFileType(handle)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err = get_last_error_fn() if get_last_error_fn is not None else w32.get_last_error()
            if err != 0:
                raise w32.win_error(err, rel)
        if ftype != w32.FILE_TYPE_DISK:
            return None

        ident, attrs, nlink = _handle_ident(handle, k32=k32, w32=w32, get_last_error_fn=get_last_error_fn)
        if attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT:
            report.skip(rel, "symlink, not followed")
            return None

        owner_sid, _aces = perms._inspect_handle_security_win(
            handle, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
        )
        if owner_sid not in trusted_owners:
            report.skip(rel, f"owned by SID {owner_sid}")
            return None

        is_dir = bool(attrs & w32.FILE_ATTRIBUTE_DIRECTORY)
        if not is_dir and nlink > 1:
            report.skip(rel, "hardlinked")
            return None

        hook("before-chmod", rel)
        sddl = perms.build_private_sddl(user_sid, is_dir=is_dir)
        perms._apply_sddl_to_handle_win(
            handle, sddl, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
        )
        if is_dir:
            report.done(rel, ident, 0o700)
            hook("before-open-dir", rel)
            list_h = _open_dir_for_listing_win(
                parent_handle,
                name,
                ident,
                k32=k32,
                ntdll=ntdll,
                w32=w32,
                get_last_error_fn=get_last_error_fn,
            )
            if list_h is None:
                report.skip(rel, "directory changed during the walk, not descended")
                return None
            return list_h, ident

        report.done(rel, ident, 0o600)
        return None
    except OSError as exc:
        report.skip(rel, f"cannot repair ({type(exc).__name__})")
        return None
    finally:
        k32.CloseHandle(handle)


def _walk_win(
    root_handle: Any,
    root_ident: WinIdent,
    root_rel: str,
    visit: Callable[[Any, str, str], Optional[Tuple[Any, WinIdent]]],
    report: WinFixReport,
    *,
    k32: Any,
    ntdll: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Iterative handle-pinned walk below *root_handle* (closes every directory handle on exit/error)."""
    stack: List[Tuple[Any, WinIdent, str, List[str]]] = []
    try:
        try:
            root_names = list(reversed(_listdir_handle_win(root_handle, ntdll=ntdll, w32=w32)))
        except OSError as exc:
            report.skip(root_rel, f"cannot list ({type(exc).__name__})")
            return
        stack.append((root_handle, root_ident, root_rel, root_names))
        root_handle = None

        while stack:
            cur_h, cur_ident, cur_rel, names = stack[-1]
            if not names:
                stack.pop()
                k32.CloseHandle(cur_h)
                if stack:
                    parent_h, parent_ident, parent_rel, _ = stack[-1]
                    try:
                        got_ident, _, _ = _handle_ident(
                            parent_h, k32=k32, w32=w32, get_last_error_fn=get_last_error_fn
                        )
                    except OSError:
                        got_ident = None
                    if got_ident != parent_ident:
                        report.skip(parent_rel, "directory moved during the walk, rest not walked")
                        return
                continue

            name = names.pop()
            child_rel = f"{cur_rel}/{name}"
            child = visit(cur_h, name, child_rel)
            if child is None:
                continue
            child_h, child_ident = child
            try:
                child_names = list(reversed(_listdir_handle_win(child_h, ntdll=ntdll, w32=w32)))
            except OSError as exc:
                k32.CloseHandle(child_h)
                report.skip(child_rel, f"cannot list ({type(exc).__name__})")
                continue
            except BaseException:
                k32.CloseHandle(child_h)
                raise
            stack.append((child_h, child_ident, child_rel, child_names))
    except OSError as exc:
        report.skip(root_rel, f"walk stopped ({type(exc).__name__})")
    finally:
        if root_handle is not None:
            k32.CloseHandle(root_handle)
        while stack:
            h, _id, _r, _n = stack.pop()
            k32.CloseHandle(h)


def _verify_repaired_win(
    home_dir: str,
    report: WinFixReport,
    trusted_owners: set[str],
    trusted_aces: set[str],
    hook: Callable[[str, str], None],
    *,
    known_files: Tuple[str, ...],
    known_dirs: Tuple[str, ...],
    k32: Any,
    advapi: Any,
    ntdll: Any,
    w32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    if not report.repaired:
        return
    hook("before-verify", ".")
    pending = dict(report.repaired)
    home_want = pending.pop(".", None)

    try:
        h_home = _open_home_nofollow_win(
            home_dir,
            desired_access=w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.SYNCHRONIZE,
            k32=k32,
            ntdll=ntdll,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
    except OSError as exc:
        report.unsafe.append({"path": ".", "reason": f"changed after repair ({type(exc).__name__})"})
        return

    h_home_list = None
    try:
        ident, attrs, _ = _handle_ident(h_home, k32=k32, w32=w32, get_last_error_fn=get_last_error_fn)
        owner_sid, aces = perms._inspect_handle_security_win(
            h_home, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
        )
        viol = perms.evaluate_win_dacl(
            owner_sid,
            aces,
            trusted_owner_sids=trusted_owners,
            trusted_ace_sids=trusted_aces,
            write_only=False,
        )
        if (
            (attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT)
            or not (attrs & w32.FILE_ATTRIBUTE_DIRECTORY)
            or (home_want is not None and ident != home_want[0])
            or viol is not None
        ):
            report.unsafe.append({"path": ".", "reason": "changed after repair"})
            return
        h_home_list = _open_dir_for_listing_win(
            None,
            ".",
            ident,
            orig_handle=h_home,
            home_dir=home_dir,
            k32=k32,
            ntdll=ntdll,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
    except OSError as exc:
        report.unsafe.append({"path": ".", "reason": f"cannot re-verify ({type(exc).__name__})"})
        return
    finally:
        k32.CloseHandle(h_home)

    if h_home_list is None:
        report.unsafe.append({"path": ".", "reason": "changed after repair"})
        return

    def _visit_verify(parent_h: Any, name: str, rel: str) -> Optional[Tuple[Any, WinIdent]]:
        want = pending.pop(rel, None)
        if want is None:
            return None
        want_ident, _want_mode = want
        try:
            h_entry = _open_rel_nt(
                parent_h,
                name,
                desired_access=w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.SYNCHRONIZE,
                directory_only=False,
                ntdll=ntdll,
                k32=k32,
                w32=w32,
                get_last_error_fn=get_last_error_fn,
            )
        except OSError:
            h_entry = None
        if h_entry is None:
            report.unsafe.append({"path": rel, "reason": "changed after repair (gone or not openable)"})
            return None
        try:
            got_ident, e_attrs, _ = _handle_ident(
                h_entry, k32=k32, w32=w32, get_last_error_fn=get_last_error_fn
            )
            if (e_attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT) or got_ident != want_ident:
                report.unsafe.append({"path": rel, "reason": "changed after repair (another inode at this path)"})
                return None
            owner_sid, aces = perms._inspect_handle_security_win(
                h_entry, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
            )
            viol = perms.evaluate_win_dacl(
                owner_sid,
                aces,
                trusted_owner_sids=trusted_owners,
                trusted_ace_sids=trusted_aces,
                write_only=False,
            )
            if viol is not None:
                report.unsafe.append(
                    {"path": rel, "reason": f"changed after repair (insecure permissions: {viol['reason']})"}
                )
                return None
            if e_attrs & w32.FILE_ATTRIBUTE_DIRECTORY:
                sub_list = _open_dir_for_listing_win(
                    parent_h,
                    name,
                    got_ident,
                    k32=k32,
                    ntdll=ntdll,
                    w32=w32,
                    get_last_error_fn=get_last_error_fn,
                )
                if sub_list is None:
                    report.unsafe.append({"path": rel, "reason": "changed after repair (another inode at this path)"})
                    return None
                return sub_list, got_ident
            return None
        except OSError as exc:
            report.unsafe.append({"path": rel, "reason": f"cannot re-verify ({type(exc).__name__})"})
            return None
        finally:
            k32.CloseHandle(h_entry)

    try:
        for name in known_files + known_dirs:
            sub = _visit_verify(h_home_list, name, name)
            if sub is not None:
                _walk_win(
                    sub[0],
                    sub[1],
                    name,
                    _visit_verify,
                    report,
                    k32=k32,
                    ntdll=ntdll,
                    w32=w32,
                    get_last_error_fn=get_last_error_fn,
                )
    finally:
        k32.CloseHandle(h_home_list)

    listed = [u["path"] for u in report.unsafe]
    for rel in sorted(pending):
        if not any(rel == p or rel.startswith(p + "/") for p in listed):
            report.unsafe.append({"path": rel, "reason": "changed after repair (not found again)"})


def home_identity_win(
    home_dir: str,
    *,
    k32: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> Optional[WinIdent]:
    """Return ``(VolumeSerialNumber, FileIndex64)`` for *home_dir* without following links; None if absent."""
    from mesh.platform import _win32_ctypes as w32

    _split_win_root_and_parts(home_dir)
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
    try:
        handle = _open_home_nofollow_win(
            home_dir,
            desired_access=w32.FILE_READ_ATTRIBUTES | w32.SYNCHRONIZE,
            k32=dll_k32,
            ntdll=dll_nt,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
    except OSError:
        return None
    try:
        ident, attrs, _ = _handle_ident(handle, k32=dll_k32, w32=w32, get_last_error_fn=get_last_error_fn)
        if (attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT) or not (attrs & w32.FILE_ATTRIBUTE_DIRECTORY):
            return None
        return ident
    except OSError:
        return None
    finally:
        dll_k32.CloseHandle(handle)


class _HookOSError(Exception):
    """Internal sentinel so OSError raised by a test hook is never swallowed as a repair failure."""

    def __init__(self, exc: OSError) -> None:
        super().__init__(str(exc))
        self.exc = exc


def fix_home_permissions(
    home_dir: str,
    expected: Optional[WinIdent] = None,
    hook: Optional[Callable[[str, str], None]] = None,
    *,
    known_files: Tuple[str, ...] = _KNOWN_FILES,
    known_dirs: Tuple[str, ...] = _KNOWN_DIRS,
    k32: Any = None,
    advapi: Any = None,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> WinFixReport:
    """Repair Windows NTFS DACLs across *home_dir* safely via non-reparse kernel handles."""
    from mesh.platform import _win32_ctypes as w32

    _split_win_root_and_parts(home_dir)
    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    dll_nt = ntdll if ntdll is not None else w32.get_ntdll()

    def fire_hook(stage: str, rel: str) -> None:
        if hook is None:
            return
        try:
            hook(stage, rel)
        except OSError as exc:
            raise _HookOSError(exc) from exc

    user_sid, default_owner_sid = perms.get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    trusted_owners = set(perms.WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
    trusted_aces = set(perms.WELL_KNOWN_TRUSTED_ACE_SIDS) | {user_sid, default_owner_sid}

    report = WinFixReport()
    try:
        fire_hook("before-open-home", ".")
        try:
            h_home = _open_home_nofollow_win(
                home_dir,
                desired_access=w32.FILE_READ_ATTRIBUTES | w32.READ_CONTROL | w32.WRITE_DAC | w32.SYNCHRONIZE,
                k32=dll_k32,
                ntdll=dll_nt,
                w32=w32,
                get_last_error_fn=get_last_error_fn,
            )
        except FileNotFoundError:
            if expected is not None:
                report.skip(".", "home changed since the precheck (now absent)")
            return report
        except OSError as exc:
            report.skip(
                ".",
                f"home cannot be opened as a directory without following symlinks ({type(exc).__name__})",
            )
            return report

        h_home_list = None
        try:
            ident, attrs, _ = _handle_ident(h_home, k32=dll_k32, w32=w32, get_last_error_fn=get_last_error_fn)
            if expected is None or ident != expected:
                report.skip(".", "home is not the directory the precheck saw, not repaired")
                return report
            if attrs & w32.FILE_ATTRIBUTE_REPARSE_POINT:
                report.skip(".", "symlink, not followed")
                return report

            owner_sid, _aces = perms._inspect_handle_security_win(
                h_home, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
            )
            if owner_sid not in trusted_owners:
                report.skip(".", f"owned by SID {owner_sid}")
                return report

            fire_hook("before-chmod", ".")
            sddl = perms.build_private_sddl(user_sid, is_dir=True)
            perms._apply_sddl_to_handle_win(
                h_home, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
            )
            report.done(".", ident, 0o700)
            h_home_list = _open_dir_for_listing_win(
                None,
                ".",
                ident,
                orig_handle=h_home,
                home_dir=home_dir,
                k32=dll_k32,
                ntdll=dll_nt,
                w32=w32,
                get_last_error_fn=get_last_error_fn,
            )
        except OSError as exc:
            report.skip(".", f"cannot repair ({type(exc).__name__})")
            return report
        finally:
            dll_k32.CloseHandle(h_home)

        if h_home_list is None:
            report.skip(".", "home changed during the walk")
            return report

        def _visit(parent_h: Any, name: str, rel: str) -> Optional[Tuple[Any, WinIdent]]:
            return _fix_entry_win(
                parent_h,
                name,
                rel,
                user_sid,
                trusted_owners,
                report,
                fire_hook,
                k32=dll_k32,
                advapi=dll_adv,
                ntdll=dll_nt,
                w32=w32,
                get_last_error_fn=get_last_error_fn,
            )

        try:
            for name in known_files:
                _visit(h_home_list, name, name)
            for name in known_dirs:
                sub = _visit(h_home_list, name, name)
                if sub is not None:
                    _walk_win(
                        sub[0],
                        sub[1],
                        name,
                        _visit,
                        report,
                        k32=dll_k32,
                        ntdll=dll_nt,
                        w32=w32,
                        get_last_error_fn=get_last_error_fn,
                    )
        finally:
            dll_k32.CloseHandle(h_home_list)

        _verify_repaired_win(
            home_dir,
            report,
            trusted_owners,
            trusted_aces,
            fire_hook,
            known_files=known_files,
            known_dirs=known_dirs,
            k32=dll_k32,
            advapi=dll_adv,
            ntdll=dll_nt,
            w32=w32,
            get_last_error_fn=get_last_error_fn,
        )
        return report
    except _HookOSError as wrapped:
        raise wrapped.exc
