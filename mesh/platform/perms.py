"""Cross-platform private file/directory permissions (POSIX modes and Windows NTFS DACLs).

Windows-specific imports (``msvcrt`` and Win32 ctypes loaders) are guarded so
importing this module on Linux never loads them.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import ntpath
import os
import re
import stat
import sys
from typing import Any, Callable, Optional, Sequence, Union

import mesh.constants as c
from mesh.errors import MeshError

if sys.platform != "win32":
    from mesh.platform import _posix
else:
    _posix: Any = None


def _posix_mod() -> Any:
    """Lazily import POSIX helpers (supports mocked Linux tests on Windows)."""
    global _posix
    if _posix is None:
        from mesh.platform import _posix as posix_mod

        _posix = posix_mod
    return _posix

# Well-known Windows SID strings
LOCAL_SYSTEM_SID = "S-1-5-18"
BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
CREATOR_OWNER_SID = "S-1-3-0"
EVERYONE_SID = "S-1-1-0"
AUTHENTICATED_USERS_SID = "S-1-5-11"
BUILTIN_USERS_SID = "S-1-5-32-545"

WELL_KNOWN_TRUSTED_OWNER_SIDS = frozenset({LOCAL_SYSTEM_SID, BUILTIN_ADMINISTRATORS_SID})
WELL_KNOWN_TRUSTED_ACE_SIDS = frozenset(
    {LOCAL_SYSTEM_SID, BUILTIN_ADMINISTRATORS_SID, OWNER_RIGHTS_SID}
)

# Win32 write/ownership mask bits for directory other-writable checks (0o022 equiv)
WIN_WRITE_ACCESS_MASK = (
    0x0002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x0004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x0010  # FILE_WRITE_EA
    | 0x0040  # FILE_DELETE_CHILD
    | 0x0100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x01000000  # ACCESS_SYSTEM_SECURITY
    | 0x40000000  # GENERIC_WRITE
    | 0x10000000  # GENERIC_ALL
)

_SID_RE = re.compile(r"^S-1-\d+(?:-\d+)+$")
_INHERIT_ONLY_ACE = 0x08
_ALLOW_ACE_TYPES = (0, 5)  # ACCESS_ALLOWED_ACE_TYPE, ACCESS_ALLOWED_OBJECT_ACE_TYPE
_DENY_ACE_TYPES = (1, 6, 10, 12)  # ACCESS_DENIED*_ACE_TYPE


def _validate_sid_str(sid: str) -> str:
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        raise ValueError(f"Invalid Windows SID string: {sid!r}")
    return sid


def build_private_sddl(user_sid: str, *, is_dir: bool) -> str:
    """Build protected owner-only SDDL for *user_sid* (+ SYSTEM + Administrators)."""
    sid = _validate_sid_str(user_sid)
    trustees = [sid]
    if sid != LOCAL_SYSTEM_SID:
        trustees.append("SY")
    if sid != BUILTIN_ADMINISTRATORS_SID:
        trustees.append("BA")
    if is_dir:
        aces = "".join(f"(A;OICI;FA;;;{t})" for t in trustees)
        return f"D:PAI{aces}"
    aces = "".join(f"(A;;FA;;;{t})" for t in trustees)
    return f"D:P{aces}"


def build_everyone_sddl(user_sid: str, *, is_dir: bool, access: str = "FR") -> str:
    """Build a test SDDL granting Everyone (WD) *access* alongside the private trustees."""
    sid = _validate_sid_str(user_sid)
    trustees = [sid]
    if sid != LOCAL_SYSTEM_SID:
        trustees.append("SY")
    if sid != BUILTIN_ADMINISTRATORS_SID:
        trustees.append("BA")
    if is_dir:
        aces = "".join(f"(A;OICI;FA;;;{t})" for t in trustees) + f"(A;OICI;{access};;;WD)"
        return f"D:PAI{aces}"
    aces = "".join(f"(A;;FA;;;{t})" for t in trustees) + f"(A;;{access};;;WD)"
    return f"D:P{aces}"


def evaluate_win_dacl(
    owner_sid: str,
    aces: Optional[Sequence[tuple[int, int, int, Optional[str]]]],
    *,
    trusted_owner_sids: frozenset[str] | set[str],
    trusted_ace_sids: frozenset[str] | set[str],
    write_only: bool = False,
) -> Optional[dict[str, Any]]:
    """Evaluate a Windows owner SID and parsed DACL ACEs against private ACL rules.

    Returns ``None`` when secure, or a diagnostic dict describing the violation.
    Each ACE tuple is ``(ace_type, ace_flags, mask, sid_str)``.
    """
    if owner_sid not in trusted_owner_sids:
        return {
            "reason": "owner",
            "owner_sid": owner_sid,
            "bad_sids": [],
            "null_dacl": False,
        }
    if aces is None:
        return {
            "reason": "null_dacl",
            "owner_sid": owner_sid,
            "bad_sids": [EVERYONE_SID],
            "null_dacl": True,
        }

    bad_sids: list[str] = []
    for ace_type, ace_flags, mask, sid in aces:
        if ace_type in _DENY_ACE_TYPES:
            continue
        if ace_type not in _ALLOW_ACE_TYPES:
            return {
                "reason": "unknown_ace",
                "owner_sid": owner_sid,
                "bad_sids": [f"ACE_TYPE_{ace_type}"],
                "null_dacl": False,
            }
        if mask == 0:
            continue
        if sid is None:
            return {
                "reason": "unknown_ace",
                "owner_sid": owner_sid,
                "bad_sids": [f"ACE_TYPE_{ace_type}"],
                "null_dacl": False,
            }
        if sid in trusted_ace_sids:
            continue
        if write_only:
            if sid == CREATOR_OWNER_SID and (ace_flags & _INHERIT_ONLY_ACE):
                continue
            if (mask & WIN_WRITE_ACCESS_MASK) == 0:
                continue
        bad_sids.append(sid)

    if bad_sids:
        return {
            "reason": "perms",
            "owner_sid": owner_sid,
            "bad_sids": sorted(set(bad_sids)),
            "null_dacl": False,
        }
    return None


def _sid_ptr_to_str(
    psid: Any,
    *,
    advapi: Any,
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> str:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    if not psid or not advapi.IsValidSid(psid):
        raise OSError(errno.EINVAL, "Invalid Win32 SID pointer")
    str_ptr = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(str_ptr)):
        raise w32.win_error(err_fn())
    try:
        if not str_ptr.value:
            raise OSError(errno.EINVAL, "ConvertSidToStringSidW returned NULL")
        return ctypes.wstring_at(str_ptr.value)
    finally:
        if str_ptr.value:
            k32.LocalFree(str_ptr)


def _query_token_sid(
    h_tok: Any,
    info_class: int,
    *,
    advapi: Any,
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> str:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    needed = wintypes.DWORD(0)
    ok = advapi.GetTokenInformation(h_tok, info_class, None, 0, ctypes.byref(needed))
    if ok:
        raise OSError(errno.EINVAL, "Unexpected GetTokenInformation success with 0 buffer")
    err = err_fn()
    if err != w32.ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
        raise w32.win_error(err)
    buf = ctypes.create_string_buffer(needed.value)
    if not advapi.GetTokenInformation(h_tok, info_class, buf, needed.value, ctypes.byref(needed)):
        raise w32.win_error(err_fn())
    if info_class == w32.TokenUser:
        psid = w32.TOKEN_USER.from_buffer(buf).User.Sid
    else:
        psid = w32.TOKEN_OWNER.from_buffer(buf).Owner
    return _sid_ptr_to_str(psid, advapi=advapi, k32=k32, get_last_error_fn=err_fn)


def get_current_user_and_owner_sids_win(
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> tuple[str, str]:
    """Return ``(TokenUserSID, TokenOwnerSID)`` strings for the current process."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error

    h_proc = dll_k32.GetCurrentProcess()
    h_tok = wintypes.HANDLE()
    if not dll_adv.OpenProcessToken(h_proc, w32.TOKEN_QUERY, ctypes.byref(h_tok)):
        raise w32.win_error(err_fn())
    try:
        user_sid = _query_token_sid(
            h_tok, w32.TokenUser, advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
        )
        owner_sid = _query_token_sid(
            h_tok, w32.TokenOwner, advapi=dll_adv, k32=dll_k32, get_last_error_fn=err_fn
        )
        return user_sid, owner_sid
    finally:
        if h_tok.value:
            dll_k32.CloseHandle(h_tok)


def _dos_path_to_nt_path_win(path: str) -> str:
    raw = os.fspath(path)
    if sys.platform == "win32":
        raw = os.path.abspath(raw)
    elif ntpath.isabs(raw):
        raw = ntpath.normpath(raw)
    else:
        raw = os.path.abspath(raw)
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\??\\UNC\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return "\\??\\" + raw[4:]
    if raw.startswith("\\\\"):
        return "\\??\\UNC\\" + raw[2:]
    return "\\??\\" + raw


def _open_security_nt_fallback_win(
    fspath: str,
    *,
    desired_access: int,
    reject_reparse: bool,
    expected_is_dir: Optional[bool],
    k32: Any,
    ntdll: Any,
    err_fn: Callable[[], int],
) -> tuple[Any, bool]:
    """Open *fspath* with exact security-only access via ``NtOpenFile``.

    Win32 ``CreateFileW`` implicitly requests ``SYNCHRONIZE``/``FILE_READ_ATTRIBUTES``,
    which fails with ``ERROR_ACCESS_DENIED`` on files with an empty protected DACL
    (``D:P``). ``NtOpenFile`` requests only ``desired_access`` (``READ_CONTROL`` /
    ``WRITE_DAC``), which Windows grants implicitly to the object owner.
    """
    from mesh.platform import _win32_ctypes as w32

    try:
        st = os.lstat(fspath)
    except OSError:
        raise w32.win_error(w32.ERROR_ACCESS_DENIED, fspath)
    is_rep = stat.S_ISLNK(st.st_mode) or bool(
        (getattr(st, "st_file_attributes", 0) or 0) & w32.FILE_ATTRIBUTE_REPARSE_POINT
    )
    if reject_reparse and is_rep:
        raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
    is_dir = stat.S_ISDIR(st.st_mode)
    if expected_is_dir is True and not is_dir:
        raise OSError(errno.ENOTDIR, "Expected directory", fspath)
    if expected_is_dir is False and is_dir:
        raise OSError(errno.EISDIR, "Expected regular file", fspath)

    nt_path = _dos_path_to_nt_path_win(fspath)
    name_raw = nt_path.encode("utf-16-le")
    name_buf = ctypes.create_string_buffer(name_raw + b"\x00\x00")
    byte_len = len(name_raw)
    us = w32.UNICODE_STRING(
        Length=byte_len,
        MaximumLength=byte_len + 2,
        Buffer=ctypes.addressof(name_buf),
    )
    oa = w32.OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(w32.OBJECT_ATTRIBUTES),
        RootDirectory=None,
        ObjectName=ctypes.pointer(us),
        Attributes=w32.OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_sb = w32.IO_STATUS_BLOCK()
    h_nt = wintypes.HANDLE()
    open_opts = w32.FILE_OPEN_FOR_BACKUP_INTENT | (w32.FILE_OPEN_REPARSE_POINT if reject_reparse else 0)
    if expected_is_dir is True:
        open_opts |= w32.FILE_DIRECTORY_FILE
    share = w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE

    status = int(
        ntdll.NtOpenFile(
            ctypes.byref(h_nt),
            desired_access,
            ctypes.byref(oa),
            ctypes.byref(io_sb),
            share,
            open_opts,
        )
    )
    if status < 0:
        dos_err = int(ntdll.RtlNtStatusToDosError(status))
        if dos_err == 0:
            dos_err = w32.ERROR_ACCESS_DENIED
        raise w32.win_error(dos_err, fspath)

    h_val = h_nt.value if hasattr(h_nt, "value") else h_nt
    try:
        ftype = k32.GetFileType(h_val)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err2 = err_fn()
            if err2 != 0:
                raise w32.win_error(err2, fspath)
        if ftype != w32.FILE_TYPE_DISK:
            raise OSError(errno.EINVAL, "Refusing non-disk file", fspath)
        if reject_reparse:
            reopened_ok = False
            if hasattr(k32, "ReOpenFile") and hasattr(k32, "GetFileInformationByHandle"):
                h_attr = k32.ReOpenFile(
                    h_val,
                    w32.FILE_READ_ATTRIBUTES,
                    share,
                    w32.FILE_FLAG_BACKUP_SEMANTICS | w32.FILE_FLAG_OPEN_REPARSE_POINT,
                )
                if not w32.is_invalid_handle(h_attr):
                    try:
                        info = w32.BY_HANDLE_FILE_INFORMATION()
                        if k32.GetFileInformationByHandle(h_attr, ctypes.byref(info)):
                            reopened_ok = True
                            if info.dwFileAttributes & w32.FILE_ATTRIBUTE_REPARSE_POINT:
                                raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
                            actual_is_dir = bool(info.dwFileAttributes & w32.FILE_ATTRIBUTE_DIRECTORY)
                            if expected_is_dir is True and not actual_is_dir:
                                raise OSError(errno.ENOTDIR, "Expected directory", fspath)
                            if expected_is_dir is False and actual_is_dir:
                                raise OSError(errno.EISDIR, "Expected regular file", fspath)
                            is_dir = actual_is_dir
                    finally:
                        k32.CloseHandle(h_attr)
            if not reopened_ok:
                st_after = os.lstat(fspath)
                if stat.S_ISLNK(st_after.st_mode) or bool(
                    (getattr(st_after, "st_file_attributes", 0) or 0) & w32.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
        return h_val, is_dir
    except BaseException:
        k32.CloseHandle(h_val)
        raise


def _open_path_for_security_win(
    path: str,
    *,
    desired_access: int,
    reject_reparse: bool,
    expected_is_dir: Optional[bool],
    k32: Any,
    ntdll: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> tuple[Any, bool]:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    fspath = os.fspath(path)
    flags = w32.FILE_FLAG_BACKUP_SEMANTICS
    if reject_reparse:
        flags |= w32.FILE_FLAG_OPEN_REPARSE_POINT

    handle = k32.CreateFileW(
        fspath,
        desired_access,
        w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
        None,
        w32.OPEN_EXISTING,
        flags,
        None,
    )
    if w32.is_invalid_handle(handle):
        err = err_fn()
        sec_only = (desired_access & ~(w32.READ_CONTROL | w32.WRITE_DAC)) == 0
        is_real_k32 = sys.platform == "win32" and k32 is w32.get_kernel32()
        if err == w32.ERROR_ACCESS_DENIED and sec_only and (ntdll is not None or is_real_k32):
            dll_nt = ntdll if ntdll is not None else w32.get_ntdll()
            return _open_security_nt_fallback_win(
                fspath,
                desired_access=desired_access,
                reject_reparse=reject_reparse,
                expected_is_dir=expected_is_dir,
                k32=k32,
                ntdll=dll_nt,
                err_fn=err_fn,
            )
        raise w32.win_error(err, fspath)

    try:
        ftype = k32.GetFileType(handle)
        if ftype == w32.FILE_TYPE_UNKNOWN:
            err = err_fn()
            if err != 0:
                raise w32.win_error(err, fspath)
        if ftype != w32.FILE_TYPE_DISK:
            raise OSError(errno.EINVAL, "Refusing non-disk file", fspath)

        info = w32.BY_HANDLE_FILE_INFORMATION()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise w32.win_error(err_fn(), fspath)
        if reject_reparse and (info.dwFileAttributes & w32.FILE_ATTRIBUTE_REPARSE_POINT):
            raise OSError(errno.ELOOP, "Refusing symlink or reparse point", fspath)
        is_dir = bool(info.dwFileAttributes & w32.FILE_ATTRIBUTE_DIRECTORY)
        if expected_is_dir is True and not is_dir:
            raise OSError(errno.ENOTDIR, "Expected directory", fspath)
        if expected_is_dir is False and is_dir:
            raise OSError(errno.EISDIR, "Expected regular file", fspath)
        return handle, is_dir
    except BaseException:
        k32.CloseHandle(handle)
        raise


def _inspect_open_handle_attrs_win(
    handle: Any,
    *,
    expected_is_dir: Optional[bool],
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    ftype = k32.GetFileType(handle)
    if ftype == w32.FILE_TYPE_UNKNOWN:
        err = err_fn()
        if err != 0:
            raise w32.win_error(err)
    if ftype != w32.FILE_TYPE_DISK:
        raise OSError(errno.EINVAL, "Refusing non-disk file descriptor")

    info = w32.BY_HANDLE_FILE_INFORMATION()
    if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise w32.win_error(err_fn())
    if info.dwFileAttributes & w32.FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(errno.ELOOP, "Refusing symlink or reparse point")
    is_dir = bool(info.dwFileAttributes & w32.FILE_ATTRIBUTE_DIRECTORY)
    if expected_is_dir is True and not is_dir:
        raise OSError(errno.ENOTDIR, "Expected directory descriptor")
    if expected_is_dir is False and is_dir:
        raise OSError(errno.EISDIR, "Expected regular file descriptor")
    return is_dir


def _apply_sddl_to_handle_win(
    handle: Any,
    sddl: str,
    *,
    advapi: Any,
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    sd_ptr = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, w32.SDDL_REVISION_1, ctypes.byref(sd_ptr), None
    ):
        raise w32.win_error(err_fn())
    try:
        dacl_present = wintypes.BOOL(0)
        dacl_defaulted = wintypes.BOOL(0)
        dacl_ptr = ctypes.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(
            sd_ptr,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl_ptr),
            ctypes.byref(dacl_defaulted),
        ):
            raise w32.win_error(err_fn())
        if not dacl_present.value or not dacl_ptr.value:
            raise OSError(errno.EINVAL, "SDDL produced no DACL")

        sec_info = w32.DACL_SECURITY_INFORMATION | w32.PROTECTED_DACL_SECURITY_INFORMATION
        rc = advapi.SetSecurityInfo(
            handle,
            w32.SE_FILE_OBJECT,
            sec_info,
            None,
            None,
            dacl_ptr.value,
            None,
        )
        if rc == w32.ERROR_ACCESS_DENIED and hasattr(k32, "ReOpenFile"):
            reopened = k32.ReOpenFile(
                handle,
                w32.READ_CONTROL | w32.WRITE_DAC,
                w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
                w32.FILE_FLAG_BACKUP_SEMANTICS | w32.FILE_FLAG_OPEN_REPARSE_POINT,
            )
            if not w32.is_invalid_handle(reopened):
                try:
                    rc = advapi.SetSecurityInfo(
                        reopened,
                        w32.SE_FILE_OBJECT,
                        sec_info,
                        None,
                        None,
                        dacl_ptr.value,
                        None,
                    )
                finally:
                    k32.CloseHandle(reopened)
        if rc != 0:
            raise w32.win_error(rc)
    finally:
        if sd_ptr.value:
            k32.LocalFree(sd_ptr)


def _inspect_handle_security_win(
    handle: Any,
    *,
    advapi: Any,
    k32: Any,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> tuple[str, Optional[list[tuple[int, int, int, Optional[str]]]]]:
    from mesh.platform import _win32_ctypes as w32

    err_fn = get_last_error_fn if get_last_error_fn is not None else w32.get_last_error
    sec_info = w32.OWNER_SECURITY_INFORMATION | w32.DACL_SECURITY_INFORMATION
    owner_ptr = ctypes.c_void_p()
    dacl_ptr = ctypes.c_void_p()
    sd_ptr = ctypes.c_void_p()

    rc = advapi.GetSecurityInfo(
        handle,
        w32.SE_FILE_OBJECT,
        sec_info,
        ctypes.byref(owner_ptr),
        None,
        ctypes.byref(dacl_ptr),
        None,
        ctypes.byref(sd_ptr),
    )
    if rc == w32.ERROR_ACCESS_DENIED and hasattr(k32, "ReOpenFile"):
        if sd_ptr.value:
            k32.LocalFree(sd_ptr)
            sd_ptr = ctypes.c_void_p()
        reopened = k32.ReOpenFile(
            handle,
            w32.READ_CONTROL,
            w32.FILE_SHARE_READ | w32.FILE_SHARE_WRITE | w32.FILE_SHARE_DELETE,
            w32.FILE_FLAG_BACKUP_SEMANTICS | w32.FILE_FLAG_OPEN_REPARSE_POINT,
        )
        if not w32.is_invalid_handle(reopened):
            try:
                rc = advapi.GetSecurityInfo(
                    reopened,
                    w32.SE_FILE_OBJECT,
                    sec_info,
                    ctypes.byref(owner_ptr),
                    None,
                    ctypes.byref(dacl_ptr),
                    None,
                    ctypes.byref(sd_ptr),
                )
            finally:
                k32.CloseHandle(reopened)

    if rc != 0:
        if sd_ptr.value:
            k32.LocalFree(sd_ptr)
        raise w32.win_error(rc)

    try:
        owner_sid = _sid_ptr_to_str(
            owner_ptr.value, advapi=advapi, k32=k32, get_last_error_fn=err_fn
        )
        if not dacl_ptr.value:
            return owner_sid, None
        acl = w32.ACL.from_address(dacl_ptr.value)
        aces: list[tuple[int, int, int, Optional[str]]] = []
        for idx in range(acl.AceCount):
            ace_ptr = ctypes.c_void_p()
            if not advapi.GetAce(dacl_ptr.value, idx, ctypes.byref(ace_ptr)):
                raise w32.win_error(err_fn())
            if not ace_ptr.value:
                raise OSError(errno.EINVAL, "GetAce returned NULL")
            hdr = w32.ACE_HEADER.from_address(ace_ptr.value)
            ace_type = hdr.AceType
            ace_flags = hdr.AceFlags
            ace_size = hdr.AceSize
            if ace_type == w32.ACCESS_ALLOWED_ACE_TYPE:
                sid_off = w32.ACCESS_ALLOWED_ACE.SidStart.offset
                if ace_size < sid_off + 8:
                    raise OSError(errno.EINVAL, "Truncated ACCESS_ALLOWED_ACE")
                ace = w32.ACCESS_ALLOWED_ACE.from_address(ace_ptr.value)
                sid_str = _sid_ptr_to_str(
                    ace_ptr.value + sid_off,
                    advapi=advapi,
                    k32=k32,
                    get_last_error_fn=err_fn,
                )
                aces.append((ace_type, ace_flags, ace.Mask, sid_str))
            elif ace_type == w32.ACCESS_ALLOWED_OBJECT_ACE_TYPE:
                if ace_size < 12 + 8:
                    raise OSError(errno.EINVAL, "Truncated ACCESS_ALLOWED_OBJECT_ACE")
                obj_ace = w32.ACCESS_ALLOWED_OBJECT_ACE.from_address(ace_ptr.value)
                sid_off = 12
                if obj_ace.Flags & 0x1:
                    sid_off += 16
                if obj_ace.Flags & 0x2:
                    sid_off += 16
                if ace_size < sid_off + 8:
                    raise OSError(errno.EINVAL, "Truncated ACCESS_ALLOWED_OBJECT_ACE SID")
                sid_str = _sid_ptr_to_str(
                    ace_ptr.value + sid_off,
                    advapi=advapi,
                    k32=k32,
                    get_last_error_fn=err_fn,
                )
                aces.append((ace_type, ace_flags, obj_ace.Mask, sid_str))
            elif ace_type in _DENY_ACE_TYPES:
                aces.append((ace_type, ace_flags, 0, None))
            else:
                aces.append((ace_type, ace_flags, 1, None))
        return owner_sid, aces
    finally:
        if sd_ptr.value:
            k32.LocalFree(sd_ptr)


def _set_private_acl_win(
    path_or_fd: Union[str, int],
    *,
    expected_is_dir: Optional[bool],
    advapi: Any = None,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, _owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )

    if isinstance(path_or_fd, int) and not isinstance(path_or_fd, bool):
        if msvcrt_mod is None:
            import msvcrt as msvcrt_mod  # lazy Windows-only import
        handle = msvcrt_mod.get_osfhandle(path_or_fd)
        if w32.is_invalid_handle(handle):
            raise OSError(errno.EBADF, "Invalid file descriptor handle")
        is_dir = _inspect_open_handle_attrs_win(
            handle,
            expected_is_dir=expected_is_dir,
            k32=dll_k32,
            get_last_error_fn=get_last_error_fn,
        )
        sddl = build_private_sddl(user_sid, is_dir=is_dir)
        _apply_sddl_to_handle_win(
            handle, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
        return

    handle, is_dir = _open_path_for_security_win(
        path_or_fd,
        desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
        reject_reparse=True,
        expected_is_dir=expected_is_dir,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        sddl = build_private_sddl(user_sid, is_dir=is_dir)
        _apply_sddl_to_handle_win(
            handle, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)


def set_private_dir_acl_win(
    path_or_fd: Union[str, int],
    mode: int = 0o700,
    *,
    advapi: Any = None,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Apply protected inheritable owner-only DACL (0700 equivalent) to directory *path_or_fd*."""
    _ = mode
    _set_private_acl_win(
        path_or_fd,
        expected_is_dir=True,
        advapi=advapi,
        k32=k32,
        msvcrt_mod=msvcrt_mod,
        get_last_error_fn=get_last_error_fn,
    )


def set_private_file_acl_win(
    path_or_fd: Union[str, int],
    mode: int = 0o600,
    *,
    advapi: Any = None,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Apply protected owner-only DACL (0600 equivalent) to regular file *path_or_fd*."""
    _ = mode
    _set_private_acl_win(
        path_or_fd,
        expected_is_dir=False,
        advapi=advapi,
        k32=k32,
        msvcrt_mod=msvcrt_mod,
        get_last_error_fn=get_last_error_fn,
    )


def fchmod_private_win(
    fd: int,
    mode: int = 0o600,
    *,
    advapi: Any = None,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Apply protected owner-only DACL directly to open descriptor *fd* (fchmod equivalent)."""
    _ = mode
    if not isinstance(fd, int) or isinstance(fd, bool):
        raise TypeError(f"Expected integer file descriptor, got {type(fd).__name__}")
    _set_private_acl_win(
        fd,
        expected_is_dir=None,
        advapi=advapi,
        k32=k32,
        msvcrt_mod=msvcrt_mod,
        get_last_error_fn=get_last_error_fn,
    )


def chmod_private_win(
    path: str,
    mode: int = 0o600,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Apply protected owner-only DACL to file or directory *path* without following links."""
    _ = mode
    if isinstance(path, int) and not isinstance(path, bool):
        raise TypeError("chmod_private expects a path string, not an integer fd")
    _set_private_acl_win(
        path,
        expected_is_dir=None,
        advapi=advapi,
        k32=k32,
        get_last_error_fn=get_last_error_fn,
    )


def _raise_insecure_win(
    desc: str,
    violation: dict[str, Any],
    *,
    is_dir: bool,
    write_only: bool,
    error_code: str,
    exit_code: int,
) -> None:
    reason = violation["reason"]
    owner_sid = violation["owner_sid"]
    bad_sids = violation["bad_sids"]
    grant_perm = "(OI)(CI)F" if is_dir else "F"
    remediation = (
        f'run `mesh doctor --fix-permissions` or '
        f'`icacls "{desc}" /inheritance:r /grant:r "%USERNAME%:{grant_perm}"`'
    )
    if reason == "owner":
        msg = f"Path '{desc}' is not owned by the current Windows user (owner SID {owner_sid}); {remediation}"
    elif reason == "null_dacl":
        msg = f"Path '{desc}' has a NULL Windows DACL granting access to Everyone; {remediation}"
    elif write_only:
        msg = (
            f"Directory '{desc}' allows write access to other Windows principals "
            f"({', '.join(bad_sids)}); {remediation}"
        )
    else:
        msg = (
            f"Path '{desc}' has insecure Windows DACL granting access to "
            f"{', '.join(bad_sids)}; {remediation}"
        )
    raise MeshError(
        code=error_code,
        exit_code=exit_code,
        message=msg,
        details={
            "path": desc,
            "reason": reason,
            "owner_sid": owner_sid,
            "bad_sids": bad_sids,
            "null_dacl": violation["null_dacl"],
        },
    )


def verify_private_owner_and_perms_win(
    fd_or_path: Union[int, str],
    *,
    is_dir: Optional[bool] = None,
    label: Optional[str] = None,
    error_code: str = c.INSECURE_PERMISSIONS,
    exit_code: int = c.EXIT_USAGE,
    advapi: Any = None,
    k32: Any = None,
    msvcrt_mod: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Verify Windows owner and strict private DACL (0600/0700 equivalent) on *fd_or_path*."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, default_owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    trusted_owners = set(WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
    trusted_aces = set(WELL_KNOWN_TRUSTED_ACE_SIDS) | {user_sid, default_owner_sid}

    is_fd = isinstance(fd_or_path, int) and not isinstance(fd_or_path, bool)
    desc = label or (f"fd {fd_or_path}" if is_fd else str(fd_or_path))
    if is_fd:
        if msvcrt_mod is None:
            import msvcrt as msvcrt_mod  # lazy Windows-only import
        handle = msvcrt_mod.get_osfhandle(fd_or_path)
        if w32.is_invalid_handle(handle):
            raise OSError(errno.EBADF, "Invalid file descriptor handle")
        actual_is_dir = _inspect_open_handle_attrs_win(
            handle,
            expected_is_dir=is_dir,
            k32=dll_k32,
            get_last_error_fn=get_last_error_fn,
        )
        owner_sid, aces = _inspect_handle_security_win(
            handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    else:
        handle, actual_is_dir = _open_path_for_security_win(
            fd_or_path,
            desired_access=w32.READ_CONTROL,
            reject_reparse=True,
            expected_is_dir=is_dir,
            k32=dll_k32,
            get_last_error_fn=get_last_error_fn,
        )
        try:
            owner_sid, aces = _inspect_handle_security_win(
                handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
            )
        finally:
            dll_k32.CloseHandle(handle)

    violation = evaluate_win_dacl(
        owner_sid,
        aces,
        trusted_owner_sids=trusted_owners,
        trusted_ace_sids=trusted_aces,
        write_only=False,
    )
    if violation is not None:
        _raise_insecure_win(
            desc,
            violation,
            is_dir=actual_is_dir,
            write_only=False,
            error_code=error_code,
            exit_code=exit_code,
        )


def verify_dir_not_other_writable_win(
    path: str,
    *,
    label: Optional[str] = None,
    error_code: str = c.INSECURE_PERMISSIONS,
    exit_code: int = c.EXIT_USAGE,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Verify directory *path* is owned privately and not writable by untrusted Windows SIDs."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, default_owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    trusted_owners = set(WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
    trusted_aces = set(WELL_KNOWN_TRUSTED_ACE_SIDS) | {user_sid, default_owner_sid}

    handle, _is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL,
        reject_reparse=False,
        expected_is_dir=True,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        owner_sid, aces = _inspect_handle_security_win(
            handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)

    violation = evaluate_win_dacl(
        owner_sid,
        aces,
        trusted_owner_sids=trusted_owners,
        trusted_ace_sids=trusted_aces,
        write_only=True,
    )
    if violation is not None:
        _raise_insecure_win(
            label or str(path),
            violation,
            is_dir=True,
            write_only=True,
            error_code=error_code,
            exit_code=exit_code,
        )


def _grant_everyone_access_for_test_win(
    path: str,
    *,
    access: str,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, _owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    handle, is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
        reject_reparse=True,
        expected_is_dir=None,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        sddl = build_everyone_sddl(user_sid, is_dir=is_dir, access=access)
        _apply_sddl_to_handle_win(
            handle, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)


def grant_everyone_read_for_test_win(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Grant Everyone (S-1-1-0) read access on *path* for Windows permission tests."""
    _grant_everyone_access_for_test_win(
        path, access="FR", advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
    )


def grant_everyone_write_for_test_win(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Grant Everyone (S-1-1-0) full/write access on *path* for Windows permission tests."""
    _grant_everyone_access_for_test_win(
        path, access="FA", advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
    )


def set_private_dir_acl(path_or_fd: Union[str, int], mode: int = 0o700) -> None:
    """Set private owner-only permissions (0700 / protected inheritable DACL) on a directory."""
    if sys.platform == "win32":
        set_private_dir_acl_win(path_or_fd, mode)
        return
    _posix_mod().set_private_dir_acl(path_or_fd, mode)


def set_private_file_acl(path_or_fd: Union[str, int], mode: int = 0o600) -> None:
    """Set private owner-only permissions (0600 / protected DACL) on a regular file."""
    if sys.platform == "win32":
        set_private_file_acl_win(path_or_fd, mode)
        return
    _posix_mod().set_private_file_acl(path_or_fd, mode)


def fchmod_private(fd: int, mode: int = 0o600) -> None:
    """Apply private permissions directly on open descriptor *fd*."""
    if sys.platform == "win32":
        fchmod_private_win(fd, mode)
        return
    _posix_mod().fchmod_private(fd, mode)


def chmod_private(path: str, mode: int = 0o600) -> None:
    """Apply private permissions to *path* (``os.chmod`` on POSIX, DACL on Windows)."""
    if sys.platform == "win32":
        chmod_private_win(path, mode)
        return
    _posix_mod().chmod_private(path, mode)


def verify_private_owner_and_perms(
    fd_or_path: Union[int, str],
    *,
    is_dir: Optional[bool] = None,
    label: Optional[str] = None,
    error_code: str = c.INSECURE_PERMISSIONS,
    exit_code: int = c.EXIT_USAGE,
) -> None:
    """Verify owner and private (0600/0700) permissions on *fd_or_path*."""
    if sys.platform == "win32":
        verify_private_owner_and_perms_win(
            fd_or_path,
            is_dir=is_dir,
            label=label,
            error_code=error_code,
            exit_code=exit_code,
        )
        return
    _posix_mod().verify_private_owner_and_perms(
        fd_or_path,
        is_dir=is_dir,
        label=label,
        error_code=error_code,
        exit_code=exit_code,
    )


def verify_dir_not_other_writable(
    path: str,
    *,
    label: Optional[str] = None,
    error_code: str = c.INSECURE_PERMISSIONS,
    exit_code: int = c.EXIT_USAGE,
) -> None:
    """Verify directory *path* is privately owned and not writable by other users."""
    if sys.platform == "win32":
        verify_dir_not_other_writable_win(
            path,
            label=label,
            error_code=error_code,
            exit_code=exit_code,
        )
        return
    _posix_mod().verify_dir_not_other_writable(
        path,
        label=label,
        error_code=error_code,
        exit_code=exit_code,
    )


def grant_everyone_read_for_test(path: str) -> None:
    """Grant Everyone read access on *path* (Windows-only permission test helper)."""
    if sys.platform != "win32":
        raise NotImplementedError("grant_everyone_read_for_test is Windows-only")
    grant_everyone_read_for_test_win(path)


def grant_everyone_write_for_test(path: str) -> None:
    """Grant Everyone write access on *path* (Windows-only permission test helper)."""
    if sys.platform != "win32":
        raise NotImplementedError("grant_everyone_write_for_test is Windows-only")
    grant_everyone_write_for_test_win(path)


def set_inaccessible_for_test_win(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Apply an empty protected DACL (``D:P``) so read/write/list fails while owner keeps ``WRITE_DAC``."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    handle, _is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
        reject_reparse=True,
        expected_is_dir=None,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        _apply_sddl_to_handle_win(
            handle, "D:P", advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)


def restore_accessible_for_test_win(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    chmod_fn: Callable[[str, int], None] = os.chmod,
) -> None:
    """Restore owner-only private DACL and clear read-only bit on *path* after ``set_inaccessible_for_test``."""
    import stat as _stat
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, _owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    handle, is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
        reject_reparse=True,
        expected_is_dir=None,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        sddl = build_private_sddl(user_sid, is_dir=is_dir)
        _apply_sddl_to_handle_win(
            handle, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)
    try:
        chmod_fn(path, _stat.S_IWRITE | _stat.S_IREAD)
    except OSError:
        pass


def set_inaccessible_for_test(path: str) -> None:
    """Make *path* inaccessible to normal reads/writes/listing in tests (0o000 / empty protected DACL)."""
    if sys.platform == "win32":
        set_inaccessible_for_test_win(path)
        return
    os.chmod(path, 0o000)


def restore_accessible_for_test(path: str) -> None:
    """Restore private owner access on *path* (0o700/0o600 or private NTFS DACL)."""
    if sys.platform == "win32":
        restore_accessible_for_test_win(path)
        return
    os.chmod(path, 0o700 if os.path.isdir(path) else 0o600)


def set_world_readable_for_test(path: str) -> None:
    """Make *path* world-readable in tests to provoke ``INSECURE_PERMISSIONS`` (0o644/0o755 / Everyone FR)."""
    if sys.platform == "win32":
        grant_everyone_read_for_test_win(path)
        return
    os.chmod(path, 0o755 if os.path.isdir(path) else 0o644)


def is_owned_by_current_user_win(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True if *path* (non-reparse) is owned by the current Windows user or trusted system/admin SID."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, default_owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    trusted_owners = set(WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
    handle, _is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL,
        reject_reparse=True,
        expected_is_dir=None,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        owner_sid, _aces = _inspect_handle_security_win(
            handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)
    return owner_sid in trusted_owners


def is_owned_by_current_user(path: str) -> bool:
    """Return True if *path* is owned by the current user."""
    if sys.platform == "win32":
        return is_owned_by_current_user_win(path)
    return _posix_mod().is_owned_by_current_user(path)


def verify_owner_and_set_private_dir_acl_win(
    path: str,
    mode: int = 0o700,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> None:
    """Verify non-reparse directory ownership and apply private DACL on a single handle."""
    _ = mode
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, default_owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    trusted_owners = set(WELL_KNOWN_TRUSTED_OWNER_SIDS) | {user_sid, default_owner_sid}
    handle, _is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL | w32.WRITE_DAC,
        reject_reparse=True,
        expected_is_dir=True,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        owner_sid, _aces = _inspect_handle_security_win(
            handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
        if owner_sid not in trusted_owners:
            raise ValueError("directory is not owned by this user")
        sddl = build_private_sddl(user_sid, is_dir=True)
        _apply_sddl_to_handle_win(
            handle, sddl, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)


def _win_dir_has_owner_rw_acl(
    path: str,
    *,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
) -> bool:
    """Return True when non-reparse *path* grants list + add/write access in its DACL."""
    from mesh.platform import _win32_ctypes as w32

    dll_k32 = k32 if k32 is not None else w32.get_kernel32()
    dll_adv = advapi if advapi is not None else w32.get_advapi32()
    user_sid, default_owner_sid = get_current_user_and_owner_sids_win(
        advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
    )
    self_sids = {
        user_sid,
        default_owner_sid,
        LOCAL_SYSTEM_SID,
        BUILTIN_ADMINISTRATORS_SID,
        EVERYONE_SID,
        AUTHENTICATED_USERS_SID,
        OWNER_RIGHTS_SID,
    }
    handle, _is_dir = _open_path_for_security_win(
        path,
        desired_access=w32.READ_CONTROL,
        reject_reparse=True,
        expected_is_dir=True,
        k32=dll_k32,
        get_last_error_fn=get_last_error_fn,
    )
    try:
        _owner_sid, aces = _inspect_handle_security_win(
            handle, advapi=dll_adv, k32=dll_k32, get_last_error_fn=get_last_error_fn
        )
    finally:
        dll_k32.CloseHandle(handle)
    if aces is None:
        return True
    if any(ace_type in _DENY_ACE_TYPES for ace_type, _f, _m, _s in aces):
        return False
    needed = w32.FILE_LIST_DIRECTORY | w32.FILE_WRITE_DATA
    allowed_mask = 0
    for ace_type, ace_flags, mask, sid in aces:
        if ace_type in _ALLOW_ACE_TYPES and not (ace_flags & _INHERIT_ONLY_ACE) and sid in self_sids:
            eff = int(mask)
            if (eff & w32.GENERIC_ALL) or ((eff & 0x001F01FF) == 0x001F01FF):
                return True
            if eff & w32.GENERIC_READ:
                eff |= w32.FILE_LIST_DIRECTORY
            if eff & w32.GENERIC_WRITE:
                eff |= w32.FILE_WRITE_DATA
            allowed_mask |= eff
    return (allowed_mask & needed) == needed


def ensure_owned_dir_writable_win(
    path: str,
    *,
    st: Optional[os.stat_result] = None,
    advapi: Any = None,
    k32: Any = None,
    get_last_error_fn: Optional[Callable[[], int]] = None,
    chmod_fn: Callable[[str, int], None] = os.chmod,
    lstat_fn: Callable[[str], os.stat_result] = os.lstat,
) -> bool:
    """Restore owner-writable DACL and clear read-only bit on an owned non-reparse Windows directory."""
    import stat as _stat

    if not is_owned_by_current_user_win(
        path, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
    ):
        return False
    changed = False
    if (os.name == "nt" and sys.platform == "win32") or advapi is not None or k32 is not None:
        if not _win_dir_has_owner_rw_acl(
            path, advapi=advapi, k32=k32, get_last_error_fn=get_last_error_fn
        ):
            set_private_dir_acl_win(
                path,
                0o700,
                advapi=advapi,
                k32=k32,
                get_last_error_fn=get_last_error_fn,
            )
            changed = True
    cur_st = lstat_fn(path) if st is None else st
    if not (cur_st.st_mode & _stat.S_IWRITE):
        chmod_fn(path, _stat.S_IWRITE)
        changed = True
    return changed


def ensure_owned_dir_writable(
    path: str,
    *,
    st: Optional[os.stat_result] = None,
    uid: Optional[int] = None,
) -> bool:
    """Ensure owned directory *path* is writable; return True if permissions changed."""
    if sys.platform == "win32":
        return ensure_owned_dir_writable_win(path, st=st)
    return _posix_mod().ensure_owned_dir_writable(path, st=st, uid=uid)
