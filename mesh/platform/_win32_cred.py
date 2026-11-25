"""Opt-in Windows Credential Manager (DPAPI-backed LSA vault) wrapper (S6).

Zero third-party dependencies: uses a private ``ctypes.WinDLL('advapi32')``
instance for ``CredWriteW``, ``CredReadW``, ``CredDeleteW``, and ``CredFree``.

Security model:
- Disabled by default (strictly opt-in; default behavior is memory-only).
- Never writes plaintext files to disk, ``MESH_HOME``, ``localStorage``, env, or argv.
- Disabled on non-Windows platforms and when ``MESH_TESTING=1``.
- Honest same-user boundary: Windows Credential Manager entries are encrypted by
  Windows LSA/DPAPI under the active Windows user account, so any other process
  running under the same Windows user account shares that trust boundary.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
from typing import Any, Optional

CRED_TYPE_GENERIC: int = 1
CRED_PERSIST_LOCAL_MACHINE: int = 2
ERROR_NOT_FOUND: int = 1168

_ADVAPI32_LOCK = threading.Lock()
_ADVAPI32_CACHE: Any = None


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def target_name_for_home(home: str) -> str:
    """Return a deterministic Windows Credential Manager target scoped to *home*."""
    try:
        real = os.path.realpath(home)
    except OSError:
        real = os.path.abspath(home)
    norm = os.path.normpath(real).lower()
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"AgentMesh:{digest}"


def _configure_advapi32(dll: Any) -> None:
    win_dll_cls = getattr(ctypes, "WinDLL", None)
    if not (isinstance(win_dll_cls, type) and isinstance(dll, win_dll_cls)):
        return
    specs = (
        ("CredWriteW", [_PCREDENTIALW, ctypes.c_uint32], ctypes.c_int),
        ("CredReadW", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_PCREDENTIALW)], ctypes.c_int),
        ("CredDeleteW", [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        ("CredFree", [ctypes.c_void_p], None),
    )
    for fn_name, argtypes, restype in specs:
        fn = getattr(dll, fn_name, None)
        if fn is not None:
            fn.argtypes = argtypes
            fn.restype = restype


def _get_advapi32() -> Any:
    global _ADVAPI32_CACHE
    with _ADVAPI32_LOCK:
        if _ADVAPI32_CACHE is not None:
            return _ADVAPI32_CACHE
        if sys.platform != "win32":
            return None
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if not callable(win_dll_cls):
            return None
        try:
            dll = win_dll_cls("advapi32", use_last_error=True)
        except OSError:
            return None
        _configure_advapi32(dll)
        if isinstance(win_dll_cls, type) and isinstance(dll, win_dll_cls):
            _ADVAPI32_CACHE = dll
        return dll


def is_supported(*, advapi32: Any = None) -> bool:
    """Return True only on Windows (non-testing mode) when ``advapi32`` Cred APIs are available."""
    if advapi32 is not None:
        return all(
            callable(getattr(advapi32, name, None))
            for name in ("CredWriteW", "CredReadW", "CredDeleteW", "CredFree")
        )
    if sys.platform != "win32":
        return False
    if os.environ.get("MESH_TESTING") == "1":
        return False
    dll = _get_advapi32()
    if dll is None:
        return False
    return all(
        callable(getattr(dll, name, None))
        for name in ("CredWriteW", "CredReadW", "CredDeleteW", "CredFree")
    )


def write_passphrase(home: str, passphrase: str, *, advapi32: Any = None) -> bool:
    """Store *passphrase* in Windows Credential Manager (local machine, current user DPAPI)."""
    if not isinstance(passphrase, str) or not passphrase:
        return False
    dll = advapi32 if advapi32 is not None else (_get_advapi32() if is_supported() else None)
    if dll is None:
        return False
    raw = passphrase.encode("utf-16-le")
    if len(raw) > 512 * 5:  # Windows CRED_MAX_CREDENTIAL_BLOB_SIZE = 2560 bytes
        return False
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    cred = _CREDENTIALW()
    cred.Flags = 0
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target_name_for_home(home)
    cred.Comment = "Agent Mesh desktop opt-in unlock credential"
    cred.CredentialBlobSize = len(raw)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.AttributeCount = 0
    cred.Attributes = None
    cred.TargetAlias = None
    cred.UserName = "agent-mesh"
    try:
        return bool(dll.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False
    finally:
        for i in range(len(buf)):
            buf[i] = 0


def read_passphrase(home: str, *, advapi32: Any = None) -> Optional[str]:
    """Read an opted-in passphrase from Windows Credential Manager into memory, or None."""
    dll = advapi32 if advapi32 is not None else (_get_advapi32() if is_supported() else None)
    if dll is None:
        return None
    pcred = _PCREDENTIALW()
    target = target_name_for_home(home)
    try:
        ok = bool(dll.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)))
        if not ok or not pcred:
            return None
        try:
            cred = pcred.contents
            size = int(cred.CredentialBlobSize)
            if size <= 0 or not cred.CredentialBlob:
                return None
            raw = ctypes.string_at(cred.CredentialBlob, size)
            try:
                text = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                return None
            return text if text else None
        finally:
            try:
                dll.CredFree(pcred)
            except Exception:
                pass
    except Exception:
        return None


def has_passphrase(home: str, *, advapi32: Any = None) -> bool:
    """Return True if an opted-in credential exists for *home* without returning its value."""
    dll = advapi32 if advapi32 is not None else (_get_advapi32() if is_supported() else None)
    if dll is None:
        return False
    pcred = _PCREDENTIALW()
    target = target_name_for_home(home)
    try:
        ok = bool(dll.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)))
        if not ok or not pcred:
            return False
        try:
            return int(pcred.contents.CredentialBlobSize) > 0
        finally:
            try:
                dll.CredFree(pcred)
            except Exception:
                pass
    except Exception:
        return False


def delete_passphrase(home: str, *, advapi32: Any = None) -> bool:
    """Delete any stored credential for *home*. Returns True on success or if already absent."""
    dll = advapi32 if advapi32 is not None else (_get_advapi32() if is_supported() else None)
    if dll is None:
        return False
    target = target_name_for_home(home)
    try:
        ok = bool(dll.CredDeleteW(target, CRED_TYPE_GENERIC, 0))
        if ok:
            return True
        get_err = getattr(ctypes, "get_last_error", None)
        if callable(get_err) and int(get_err()) == ERROR_NOT_FOUND:
            return True
        return False
    except Exception:
        return False
