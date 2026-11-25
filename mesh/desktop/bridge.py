"""Python-to-JS bridge for the Agent Mesh desktop application.

All communication with ``meshd`` uses :class:`mesh.client.MeshClient` over the
OS-native local IPC channel (Windows Named Pipe with user SID verification or
POSIX Unix domain socket). Direct database/log file reads and TCP transport are
never used. User passphrases are held only in memory and never written to disk,
environment variables, or logs.
"""

from __future__ import annotations

import collections
import concurrent.futures
import ctypes
import json
import math
import ntpath
import os
import platform
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

import mesh.constants as c
from mesh.client import MeshClient
from mesh.core.gitops import augment_env_with_bundled_git, resolve_git_binary, run_git
from mesh.core.paths import (
    check_install_path_secure,
    clear_autostart_hold,
    get_install_root,
    get_mesh_bin_dir,
    is_default_home,
    is_path_equal_or_parent,
    is_site_packages_dir,
    mesh_cli_argv,
    read_autostart_hold,
    resolve_home,
    write_autostart_hold,
)
from mesh.core.redact import redact, redact_value
from mesh.desktop.state import DesktopStateStore
from mesh.errors import MeshError
from mesh.platform import _win32_cred as win_cred
from mesh.platform import ipc as plat_ipc
from mesh.platform.paths import same_path


def compute_setup_hint(
    home: str,
    *,
    executable: Optional[str] = None,
    platform_name: Optional[str] = None,
    isfile_fn: Callable[[str], bool] = os.path.isfile,
) -> str:
    """Build the copy-pasteable ``mesh setup`` hint for the active install and home (N5)."""
    plat = platform_name if platform_name is not None else sys.platform
    exe = executable if executable is not None else sys.executable
    prog = "mesh"
    if plat == "win32" and exe:
        exe_dir = ntpath.dirname(exe)
        if ntpath.basename(exe_dir).lower() == "python":
            portable_root = ntpath.dirname(exe_dir)
            for launcher in ("mesh.exe", "mesh.cmd"):
                cand = ntpath.join(portable_root, launcher)
                if isfile_fn(cand):
                    prog = f'"{cand}"'
                    break
    if is_default_home(home):
        return f"{prog} setup"
    if plat == "win32":
        escaped = home.replace('"', '\\"')
        return f'{prog} --home "{escaped}" setup'
    if any(ch in home for ch in ('"', "$", "`", "\\", "\n", "\r")):
        return f"{prog} --home {shlex.quote(home)} setup"
    return f'{prog} --home "{home}" setup'

CAPABILITIES: Dict[str, Any] = {
    "confirm_plan": True,
    "task_diff": True,
    "project_conversation": True,
    "task_log_tail": False,
    "approval_policy_inline": True,
    "task_list_latest_run": False,
}

UNAUTH_RPC_METHODS: frozenset[str] = frozenset({"daemon.status", "version"})

ALLOWED_RPC_METHODS: frozenset[str] = frozenset(
    {
        "version",
        "whoami",
        "daemon.status",
        "daemon.stop",
        "config.list",
        "config.get",
        "config.set",
        "agent.list",
        "agent.show",
        "agent.create",
        "agent.update",
        "agent.disable",
        "agent.enable",
        "adapter.list",
        "adapter.show",
        "adapter.probe",
        "adapter.models",
        "project.list",
        "project.show",
        "project.diff",
        "project.leads",
        "project.set_lead",
        "project.pause",
        "project.resume",
        "project.cancel",
        "project.deliver",
        "project.complete",
        "project.update",
        "project.set_permission_mode",
        "chat.send",
        "chat.list",
        "chat.queue_cancel",
        "chat.queue_edit",
        "lead.interrupt",
        "plan.list",
        "plan.show",
        "plan.cancel",
        "grant.list",
        "grant.revoke",
        "intake.submit",
        "task.list",
        "task.show",
        "task.diff",
        "task.create",
        "task.assign",
        "task.reassign",
        "task.accept",
        "task.reject",
        "task.merge",
        "task.retry",
        "task.escalate",
        "task.cancel",
        "task.close",
        "task.log",
        "dep.list",
        "run.list",
        "run.show",
        "run.log",
        "run.activity",
        "run.release",
        "workspace.list",
        "workspace.show",
        "workspace.prune",
        "approval.list",
        "approval.show",
        "approval.decide",
        "event.list",
        "audit.verify",
    }
)

_LONG_RPC_TIMEOUTS: Dict[str, float] = {
    "adapter.probe": 60.0,
    "chat.send": 60.0,
    "intake.submit": 60.0,
    "task.merge": 60.0,
    "audit.verify": 60.0,
    "task.diff": 30.0,
    "project.diff": 30.0,
}

_SECRET_KEYS = frozenset({"passphrase", "password", "token", "secret", "k_user", "auth"})
_DAEMON_SECRET_ENV = frozenset({"MESH_PASSPHRASE", "MESH_PASSPHRASE_FILE", "MESH_TOKEN"})
_DEFAULT_BACKOFF: Sequence[float] = (0.5, 1.0, 2.0, 4.0, 8.0, 10.0)
_MAX_STREAM_POLL_TIMEOUT_S: float = 0.25
_BLOCKED_BINARY_KEYS = frozenset({"binary", "binary_path"})
_BLOCKED_SETTINGS = frozenset({"notify.command"})

DEFAULT_WINDOW_WIDTH: int = 1360
DEFAULT_WINDOW_HEIGHT: int = 860
MIN_WINDOW_WIDTH: int = 1040
MIN_WINDOW_HEIGHT: int = 680
MAX_WINDOW_WIDTH: int = 16384
MAX_WINDOW_HEIGHT: int = 16384
_VALID_RESIZE_EDGES: frozenset[str] = frozenset({"n", "s", "e", "w", "ne", "nw", "se", "sw"})
_MONITOR_DEFAULTTONEAREST: int = 2
_SPI_GETWORKAREA: int = 0x0030
_SWP_NOSIZE: int = 0x0001
_SWP_NOMOVE: int = 0x0002
_SWP_NOZORDER: int = 0x0004
_SWP_NOACTIVATE: int = 0x0010
_SWP_FRAMECHANGED: int = 0x0020
_SW_RESTORE: int = 9
_GWL_STYLE: int = -16
_WS_MINIMIZEBOX: int = 0x00020000
_WIN32_DEFAULT_DPI: int = 96
HOME_NOT_INITIALIZED: str = "HOME_NOT_INITIALIZED"

_WIN32_USER32_LOCK = threading.Lock()
_WIN32_USER32_CACHE: Any = None


class _WIN32_POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


class _WIN32_RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _WIN32_MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _WIN32_RECT),
        ("rcWork", _WIN32_RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


def _configure_win32_user32(user32: Any) -> None:
    """Configure ctypes prototypes on a private ``WinDLL('user32')`` instance only."""
    win_dll_cls = getattr(ctypes, "WinDLL", None)
    if not (isinstance(win_dll_cls, type) and isinstance(user32, win_dll_cls)):
        return
    specs = (
        ("MonitorFromWindow", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p),
        ("GetMonitorInfoW", [ctypes.c_void_p, ctypes.POINTER(_WIN32_MONITORINFO)], ctypes.c_int),
        ("SystemParametersInfoW", [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        ("GetWindowRect", [ctypes.c_void_p, ctypes.POINTER(_WIN32_RECT)], ctypes.c_int),
        (
            "SetWindowPos",
            [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint32,
            ],
            ctypes.c_int,
        ),
        ("GetDpiForWindow", [ctypes.c_void_p], ctypes.c_uint32),
        ("GetDpiForSystem", [], ctypes.c_uint32),
        ("GetWindowLongW", [ctypes.c_void_p, ctypes.c_int], ctypes.c_long),
        ("SetWindowLongW", [ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long),
        ("IsIconic", [ctypes.c_void_p], ctypes.c_int),
        ("IsZoomed", [ctypes.c_void_p], ctypes.c_int),
        ("ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        ("GetCursorPos", [ctypes.POINTER(_WIN32_POINT)], ctypes.c_int),
    )
    for fn_name, argtypes, restype in specs:
        fn = getattr(user32, fn_name, None)
        if fn is not None:
            fn.argtypes = argtypes
            fn.restype = restype


def _get_win32_user32() -> Any:
    """Return a private ``user32`` WinDLL instance (never mutating shared ``ctypes.windll``)."""
    global _WIN32_USER32_CACHE
    with _WIN32_USER32_LOCK:
        if _WIN32_USER32_CACHE is not None:
            return _WIN32_USER32_CACHE
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if not callable(win_dll_cls):
            return None
        dll = win_dll_cls("user32", use_last_error=True)
        _configure_win32_user32(dll)
        if isinstance(win_dll_cls, type) and isinstance(dll, win_dll_cls):
            _WIN32_USER32_CACHE = dll
        return dll


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_window_bounds(window: Any) -> Optional[Tuple[int, int, int, int]]:
    """Return ``(x, y, width, height)`` from a pywebview window when all values are valid."""
    x = getattr(window, "x", None)
    y = getattr(window, "y", None)
    w = getattr(window, "width", None)
    h = getattr(window, "height", None)
    if all(_is_finite_number(v) for v in (x, y, w, h)):
        wi = int(round(w))
        hi = int(round(h))
        if wi > 0 and hi > 0:
            return (int(round(x)), int(round(y)), wi, hi)
    return None


def _resolve_window_min_size(window: Any) -> Tuple[int, int]:
    """Return ``(min_width, min_height)`` for *window*, defaulting to desktop minimums."""
    raw = getattr(window, "min_size", None)
    if isinstance(raw, (tuple, list)) and len(raw) == 2:
        mw, mh = raw
        if _is_finite_number(mw) and _is_finite_number(mh):
            mwi, mhi = int(round(mw)), int(round(mh))
            if mwi > 0 and mhi > 0:
                return (mwi, mhi)
    return (MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)


def _extract_win32_hwnd(window: Any) -> Optional[int]:
    """Extract an integer HWND from ``window.native`` when exposed by pywebview/WinForms."""
    native = getattr(window, "native", None)
    if native is None:
        return None
    if isinstance(native, int) and not isinstance(native, bool):
        return native if native > 0 else None
    handle = getattr(native, "Handle", None)
    if handle is None:
        return None
    for meth in ("ToInt64", "ToInt32"):
        fn = getattr(handle, meth, None)
        if callable(fn):
            try:
                val = int(fn())
                return val if val > 0 else None
            except Exception:
                continue
    try:
        val = int(handle)
        return val if val > 0 else None
    except Exception:
        return None


def _get_win32_dpi_for_window(hwnd: int, user32: Any) -> int:
    """Return per-monitor DPI for *hwnd* (defaults to 96 when unavailable)."""
    try:
        fn = getattr(user32, "GetDpiForWindow", None)
        if callable(fn):
            dpi = int(fn(hwnd))
            if dpi >= 48:
                return dpi
    except Exception:
        pass
    return _WIN32_DEFAULT_DPI


def _get_win32_system_dpi(user32: Any) -> int:
    """Return system DPI on Windows (defaults to 96 when unavailable)."""
    try:
        fn = getattr(user32, "GetDpiForSystem", None)
        if callable(fn):
            dpi = int(fn())
            if dpi >= 48:
                return dpi
    except Exception:
        pass
    return _WIN32_DEFAULT_DPI


def _ensure_win32_minimize_box(hwnd: int, user32: Any) -> bool:
    """Add ``WS_MINIMIZEBOX`` to a frameless window so taskbar-icon click minimizes it."""
    try:
        get_wl = getattr(user32, "GetWindowLongW", None)
        set_wl = getattr(user32, "SetWindowLongW", None)
        if not callable(get_wl) or not callable(set_wl):
            return False
        style = int(get_wl(hwnd, _GWL_STYLE))
        new_style = style | _WS_MINIMIZEBOX
        if new_style != style:
            set_wl(hwnd, _GWL_STYLE, new_style)
            swp = getattr(user32, "SetWindowPos", None)
            if callable(swp):
                swp(
                    hwnd,
                    0,
                    0,
                    0,
                    0,
                    0,
                    _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
                )
        return True
    except Exception:
        return False


def _get_win32_window_rect(hwnd: int, user32: Any) -> Optional[Tuple[int, int, int, int]]:
    """Return physical ``(x, y, width, height)`` of *hwnd* via ``GetWindowRect``."""
    try:
        fn = getattr(user32, "GetWindowRect", None)
        if not callable(fn):
            return None
        rect = _WIN32_RECT()
        if fn(hwnd, ctypes.byref(rect)):
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if w > 0 and h > 0:
                return (int(rect.left), int(rect.top), w, h)
    except Exception:
        return None
    return None


def _is_win32_iconic(hwnd: int, user32: Any) -> bool:
    try:
        fn = getattr(user32, "IsIconic", None)
        return bool(fn(hwnd)) if callable(fn) else False
    except Exception:
        return False


def _is_win32_zoomed(hwnd: int, user32: Any) -> bool:
    try:
        fn = getattr(user32, "IsZoomed", None)
        return bool(fn(hwnd)) if callable(fn) else False
    except Exception:
        return False


def _set_win32_window_pos(
    hwnd: int,
    x: int,
    y: int,
    w: int,
    h: int,
    user32: Any,
    *,
    flags: int = _SWP_NOZORDER | _SWP_NOACTIVATE,
    ensure_unminimized: bool = True,
) -> bool:
    """Apply physical pixel bounds to *hwnd* via ``SetWindowPos``."""
    try:
        swp = getattr(user32, "SetWindowPos", None)
        if not callable(swp):
            return False
        if ensure_unminimized and (_is_win32_iconic(hwnd, user32) or _is_win32_zoomed(hwnd, user32)):
            show_win = getattr(user32, "ShowWindow", None)
            if callable(show_win):
                show_win(hwnd, _SW_RESTORE)
        return bool(swp(hwnd, 0, int(x), int(y), int(w), int(h), int(flags)))
    except Exception:
        return False


def _get_win32_cursor_pos(user32: Any) -> Optional[Tuple[int, int]]:
    """Return physical cursor ``(x, y)`` via ``GetCursorPos``."""
    try:
        fn = getattr(user32, "GetCursorPos", None)
        if not callable(fn):
            return None
        pt = _WIN32_POINT()
        if fn(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
    except Exception:
        return None
    return None


def _default_restore_bounds(
    work_area: Tuple[int, int, int, int],
    *,
    scale: float = 1.0,
) -> Tuple[int, int, int, int]:
    """Compute centered fallback normal bounds inside *work_area*."""
    wx, wy, ww, wh = work_area
    scaled_w = max(1, int(round(DEFAULT_WINDOW_WIDTH * scale)))
    scaled_h = max(1, int(round(DEFAULT_WINDOW_HEIGHT * scale)))
    rw = scaled_w if scaled_w < ww else max(1, int(round(ww * 0.85)))
    rh = scaled_h if scaled_h < wh else max(1, int(round(wh * 0.85)))
    return (
        wx + max(0, (ww - rw) // 2),
        wy + max(0, (wh - rh) // 2),
        rw,
        rh,
    )


def _get_win32_work_area(window: Any, user32: Any = None) -> Optional[Tuple[int, int, int, int]]:
    """Query the current monitor physical work area ``(x, y, w, h)`` on Windows (excludes taskbar)."""
    try:
        dll = user32 if user32 is not None else _get_win32_user32()
        if dll is None:
            return None

        hwnd = _extract_win32_hwnd(window)
        if hwnd is not None:
            mfw = getattr(dll, "MonitorFromWindow", None)
            gmi = getattr(dll, "GetMonitorInfoW", None)
            if callable(mfw) and callable(gmi):
                hmon = mfw(hwnd, _MONITOR_DEFAULTTONEAREST)
                if hmon:
                    mi = _WIN32_MONITORINFO()
                    mi.cbSize = ctypes.sizeof(_WIN32_MONITORINFO)
                    if gmi(hmon, ctypes.byref(mi)):
                        rc = mi.rcWork
                        w = int(rc.right - rc.left)
                        h = int(rc.bottom - rc.top)
                        if w > 0 and h > 0:
                            return (int(rc.left), int(rc.top), w, h)

        spi = getattr(dll, "SystemParametersInfoW", None)
        if callable(spi):
            rect = _WIN32_RECT()
            if spi(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                w = int(rect.right - rect.left)
                h = int(rect.bottom - rect.top)
                if w > 0 and h > 0:
                    return (int(rect.left), int(rect.top), w, h)
    except Exception:
        return None
    return None


def compute_initial_window_geometry(user32: Any = None) -> Tuple[int, int, Tuple[int, int]]:
    """Return DPI-aware initial ``(width, height, (min_w, min_h))`` clamped to work area."""
    width = DEFAULT_WINDOW_WIDTH
    height = DEFAULT_WINDOW_HEIGHT
    min_w = MIN_WINDOW_WIDTH
    min_h = MIN_WINDOW_HEIGHT
    if sys.platform != "win32":
        return (width, height, (min_w, min_h))
    dll = user32 if user32 is not None else _get_win32_user32()
    if dll is None:
        return (width, height, (min_w, min_h))
    work_area = _get_win32_work_area(None, user32=dll)
    if work_area is None:
        return (width, height, (min_w, min_h))
    _wx, _wy, ww, wh = work_area
    dpi = _get_win32_system_dpi(dll)
    scale = dpi / _WIN32_DEFAULT_DPI
    logical_wa_w = max(1, int(ww / scale))
    logical_wa_h = max(1, int(wh / scale))
    clamped_w = min(width, logical_wa_w)
    clamped_h = min(height, logical_wa_h)
    clamped_min_w = min(min_w, clamped_w)
    clamped_min_h = min(min_h, clamped_h)
    return (clamped_w, clamped_h, (clamped_min_w, clamped_min_h))


def compute_saved_window_placement(
    saved_bounds: Optional[Dict[str, Any]],
    *,
    user32: Any = None,
) -> Dict[str, Any]:
    """Compute safe initial window placement from *saved_bounds*, clamped to the active work area."""
    base_w, base_h, (min_w, min_h) = compute_initial_window_geometry(user32=user32)
    out: Dict[str, Any] = {
        "x": None,
        "y": None,
        "width": base_w,
        "height": base_h,
        "min_size": (min_w, min_h),
        "maximized": False,
    }
    if not isinstance(saved_bounds, dict):
        return out
    sw = saved_bounds.get("width")
    sh = saved_bounds.get("height")
    sx = saved_bounds.get("x")
    sy = saved_bounds.get("y")
    maximized = bool(saved_bounds.get("maximized", False))
    if not (_is_finite_number(sw) and _is_finite_number(sh)):
        return out

    w = max(min_w, min(int(round(sw)), MAX_WINDOW_WIDTH))
    h = max(min_h, min(int(round(sh)), MAX_WINDOW_HEIGHT))
    x: Optional[int] = int(round(sx)) if _is_finite_number(sx) else None
    y: Optional[int] = int(round(sy)) if _is_finite_number(sy) else None

    if sys.platform == "win32":
        dll = user32 if user32 is not None else _get_win32_user32()
        work_area = _get_win32_work_area(None, user32=dll) if dll is not None else None
        if work_area is not None and dll is not None:
            wx, wy, ww, wh = work_area
            dpi = _get_win32_system_dpi(dll)
            scale = dpi / _WIN32_DEFAULT_DPI
            log_wx = int(round(wx / scale))
            log_wy = int(round(wy / scale))
            log_ww = max(1, int(ww / scale))
            log_wh = max(1, int(wh / scale))
            w = max(min(min_w, log_ww), min(w, log_ww))
            h = max(min(min_h, log_wh), min(h, log_wh))
            if x is not None and y is not None:
                x = max(log_wx, min(x, log_wx + max(0, log_ww - w)))
                y = max(log_wy, min(y, log_wy + max(0, log_wh - h)))

    out["width"] = w
    out["height"] = h
    out["x"] = x
    out["y"] = y
    out["maximized"] = maximized
    return out


def _validate_bridge_call_args(method: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reject high-risk executable/command arguments from WebView JS callers."""
    if method.startswith("adapter.") or method.startswith("agent."):
        for k, v in args.items():
            if isinstance(k, str) and k.lower() in _BLOCKED_BINARY_KEYS and v is not None:
                return _error_envelope(
                    "ARGUMENT_NOT_ALLOWED",
                    f"桌面端不允许在 '{method}' 中指定 '{k}'",
                    exit_code=c.EXIT_USAGE,
                    details={"method": method, "param": k},
                )
        for opt_key in ("options", "option"):
            opts = args.get(opt_key)
            if isinstance(opts, dict):
                for k, v in opts.items():
                    if isinstance(k, str) and k.lower() in _BLOCKED_BINARY_KEYS and v is not None:
                        return _error_envelope(
                            "ARGUMENT_NOT_ALLOWED",
                            f"桌面端不允许在 '{method}' 中指定 'options.{k}'",
                            exit_code=c.EXIT_USAGE,
                            details={"method": method, "param": f"{opt_key}.{k}"},
                        )
            elif isinstance(opts, list):
                for item in opts:
                    if isinstance(item, str) and "=" in item:
                        k = item.split("=", 1)[0].strip()
                        if k.lower() in _BLOCKED_BINARY_KEYS:
                            return _error_envelope(
                                "ARGUMENT_NOT_ALLOWED",
                                f"桌面端不允许在 '{method}' 中指定 'options.{k}'",
                                exit_code=c.EXIT_USAGE,
                                details={"method": method, "param": f"{opt_key}.{k}"},
                            )
    if method == "config.set":
        setting_key = args.get("key")
        if setting_key in _BLOCKED_SETTINGS:
            return _error_envelope(
                "SETTING_NOT_ALLOWED",
                f"桌面端不允许修改高危设置 '{setting_key}'",
                exit_code=c.EXIT_USAGE,
                details={"method": method, "key": setting_key},
            )
    return None


def asset_dir() -> str:
    """Return the absolute path to static UI assets shipped inside ``mesh.desktop``."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def index_html_path() -> str:
    """Return the absolute path to the desktop ``index.html`` entry document."""
    return os.path.join(asset_dir(), "index.html")


def _new_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


def _sanitize_value(value: Any) -> Any:
    """Redact secret-looking keys and secret patterns before returning/storing errors."""
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = _sanitize_value(v)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return redact_value(value)


def _parse_bridge_int(value: Any, *, name: str) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Parse an integer bridge argument or return an ``INVALID_ARGUMENT`` error envelope."""
    if isinstance(value, bool):
        return (
            None,
            _error_envelope(
                c.INVALID_ARGUMENT,
                f"参数 '{name}' 必须为整数",
                exit_code=c.EXIT_USAGE,
                details={"param": name},
            ),
        )
    if isinstance(value, int):
        return (value, None)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return (int(value), None)
        return (
            None,
            _error_envelope(
                c.INVALID_ARGUMENT,
                f"参数 '{name}' 必须为整数",
                exit_code=c.EXIT_USAGE,
                details={"param": name},
            ),
        )
    if isinstance(value, str):
        try:
            return (int(value.strip()), None)
        except ValueError:
            pass
    return (
        None,
        _error_envelope(
            c.INVALID_ARGUMENT,
            f"参数 '{name}' 必须为整数",
            exit_code=c.EXIT_USAGE,
            details={"param": name},
        ),
    )


def _success_envelope(
    data: Any,
    *,
    warnings: Optional[List[str]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "warnings": list(warnings or []),
        "request_id": request_id,
    }


def _error_envelope(
    code: str,
    message: str,
    *,
    exit_code: int = c.EXIT_USAGE,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    safe_msg = redact(message) if isinstance(message, str) else str(message)
    safe_details = _sanitize_value(details or {})
    return {
        "ok": False,
        "error": {
            "code": code,
            "exit_code": exit_code,
            "message": safe_msg,
            "details": safe_details,
        },
        "request_id": request_id,
    }


DESKTOP_LOG_FILENAME = "desktop.log"

_DESKTOP_LOG_ALWAYS_METHODS: frozenset[str] = frozenset(
    {
        "chat.send",
        "chat.queue_cancel",
        "chat.queue_edit",
        "lead.interrupt",
        "intake.submit",
        "daemon.stop",
        "config.set",
        "agent.create",
        "agent.update",
        "agent.disable",
        "agent.enable",
        "project.set_lead",
        "project.set_permission_mode",
        "project.pause",
        "project.resume",
        "project.cancel",
        "project.deliver",
        "project.complete",
        "plan.cancel",
        "approval.decide",
        "grant.revoke",
    }
)


def _scrub_secret_text(text: str, secrets_to_scrub: Sequence[Optional[str]] = ()) -> str:
    cleaned = redact(text)
    for sec in secrets_to_scrub:
        if isinstance(sec, str) and len(sec) >= 4 and sec in cleaned:
            cleaned = cleaned.replace(sec, "[REDACTED]")
    return cleaned


def append_desktop_log(
    home: str,
    level: str,
    event: str,
    message: str = "",
    *,
    details: Optional[Dict[str, Any]] = None,
    secrets_to_scrub: Sequence[Optional[str]] = (),
) -> None:
    """Append a redacted, rotated log line to ``$MESH_HOME/logs/desktop.log`` (never raises)."""
    if not home:
        return
    try:
        from mesh.core.clock import now_iso_utc
        from mesh.core.logfile import append_line, escape_field

        lvl = (level or "INFO").upper()
        if lvl == "WARN":
            lvl = "WARNING"
        evt = escape_field(event or "EVENT")
        parts = [now_iso_utc(), lvl, f"event={evt}"]
        if message:
            parts.append(_scrub_secret_text(str(message), secrets_to_scrub))
        if isinstance(details, dict) and details:
            safe_details = _sanitize_value(details)
            detail_str = json.dumps(safe_details, ensure_ascii=False, sort_keys=True)
            parts.append(f"details={_scrub_secret_text(detail_str, secrets_to_scrub)}")
        append_line(home, " ".join(parts), name=DESKTOP_LOG_FILENAME)
    except Exception:
        pass


def _strip_daemon_secret_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    source = os.environ if base_env is None else base_env
    return {k: v for k, v in source.items() if k.upper() not in _DAEMON_SECRET_ENV}


class DesktopBridge:
    """Thread-safe JSON bridge exposed to the desktop WebView via ``js_api``."""

    def __init__(
        self,
        home: Optional[str] = None,
        *,
        client_factory: Optional[Callable[[str, float], Any]] = None,
        backoff_schedule: Sequence[float] = _DEFAULT_BACKOFF,
        rpc_timeout_s: float = 30.0,
        probe_timeout_s: float = 2.0,
        probe_interval_s: float = 2.5,
        stream_poll_timeout_s: float = 0.5,
        subprocess_runner: Optional[Callable[..., Any]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        push_enabled: bool = True,
        auto_start_daemon: bool = True,
        auto_start_max_attempts: int = 3,
        auto_start_window_s: float = 300.0,
        state_store: Optional[DesktopStateStore] = None,
        cred_store: Any = None,
    ) -> None:
        raw_home = home or os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh")
        self.home: str = resolve_home(raw_home)
        self._client_factory = client_factory or MeshClient
        self._state_store: DesktopStateStore = state_store or DesktopStateStore(self.home)
        self._cred_store: Any = cred_store if cred_store is not None else win_cred
        self._backoff: Sequence[float] = tuple(backoff_schedule) or _DEFAULT_BACKOFF
        self._rpc_timeout_s: float = float(rpc_timeout_s)
        self._probe_timeout_s: float = float(probe_timeout_s)
        self._probe_interval_s: float = float(probe_interval_s)
        self._stream_poll_timeout_s: float = min(max(0.05, float(stream_poll_timeout_s)), _MAX_STREAM_POLL_TIMEOUT_S)
        self._subprocess_runner = subprocess_runner or subprocess.run
        self._on_event = on_event
        self._push_enabled = push_enabled
        self._auto_start_daemon: bool = bool(auto_start_daemon)
        self._auto_start_max_attempts: int = max(1, int(auto_start_max_attempts))
        self._auto_start_window_s: float = max(1.0, float(auto_start_window_s))

        self._lock = threading.RLock()
        self._start_daemon_lock = threading.Lock()
        self._init_home_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        self._passphrase: Optional[str] = None
        self._auth_verified: bool = False
        self._principal: Optional[Dict[str, Any]] = None
        self._last_auth_error: Optional[Dict[str, Any]] = None
        self._auto_unlock_attempted: bool = False

        self._connected: bool = False
        self._daemon_status: Optional[Dict[str, Any]] = None
        self._daemon_version: Optional[str] = None
        self._last_error: Optional[Dict[str, Any]] = None
        self._last_start_error: Optional[Dict[str, Any]] = None
        self._daemon_starting: bool = False
        self._daemon_stopping: bool = False
        self._auto_start_attempted: bool = False
        self._onboarding_pushed: bool = False
        self._user_stopped_daemon: bool = False
        self._stopped_daemon_pid: Optional[int] = None
        self._stopped_daemon_boot_id: Optional[str] = None
        self._stop_generation: int = 0
        self._daemon_stop_in_flight: int = 0
        self._last_probe_saw_stopped_daemon: bool = False
        self._first_probe_round: bool = True
        self._auto_start_timestamps: Deque[float] = collections.deque()
        self._home_initialized_cache: bool = self._is_home_initialized()
        self._daemon_diag_log: Deque[Dict[str, Any]] = collections.deque(maxlen=40)
        self._reconnect_attempt: int = 0
        self._next_retry_s: float = 0.0
        self._last_connected_at: Optional[float] = None
        self._last_seq: Optional[int] = None

        self._pending_events: Deque[Dict[str, Any]] = collections.deque(maxlen=2000)
        self._window: Any = None
        self._tray: Any = None
        self._window_visible: bool = True
        self._quitting: bool = False
        self._window_maximized: bool = False
        self._window_bridge_maximized: bool = False
        self._window_restore_bounds: Optional[Tuple[int, int, int, int]] = None
        self._window_maximized_bounds: Optional[Tuple[int, int, int, int]] = None
        self._window_bounds_physical: bool = False
        self._window_drag_active: bool = False
        self._window_drag_anchor: Optional[Tuple[int, int, int, int]] = None
        self._window_resize_anchor: Optional[Tuple[str, int, int, int, int, int, int]] = None
        self._window_last_normal_bounds: Optional[Tuple[int, int, int, int]] = None
        self._window_converting_max: bool = False
        self._window_reapplying_max: bool = False
        self._window_restoring: bool = False
        self._push_lock = threading.Lock()
        self._push_needed: bool = False

        self._supervisor_thread: Optional[threading.Thread] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_stop_event: Optional[threading.Event] = None
        self._stream_sock: Any = None

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"<DesktopBridge home={self.home!r} connected={self._connected} "
                f"auth_configured={self._passphrase is not None}>"
            )

    def _clear_window_max_state_locked(self) -> None:
        self._window_maximized = False
        self._window_bridge_maximized = False
        self._window_restore_bounds = None
        self._window_maximized_bounds = None
        self._window_bounds_physical = False
        self._window_drag_active = False
        self._window_drag_anchor = None

    def _clear_window_resize_state_locked(self) -> None:
        self._window_resize_anchor = None

    def attach_window(self, window: Any) -> None:
        """Bind a ``pywebview`` window so events can be pushed via ``evaluate_js``."""
        with self._lock:
            self._window = window
            self._clear_window_max_state_locked()
            self._clear_window_resize_state_locked()
            self._window_last_normal_bounds = None
            self._window_converting_max = False
            self._window_reapplying_max = False
            self._window_restoring = False

    def _push_window_state(self, maximized: bool) -> None:
        """Notify the WebView page of a window maximize/restore transition off the GUI thread."""
        event: Dict[str, Any] = {"type": "_desktop.window_state", "maximized": bool(maximized)}
        with self._lock:
            self._pending_events.append(event)
            self._push_needed = True
            win = self._window
        cb = self._on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:
                pass
        if win is not None and self._push_enabled:
            threading.Thread(
                target=self._push_queued_to_window,
                args=(win,),
                name="mesh-desktop-winstate-push",
                daemon=True,
            ).start()

    def _on_window_shown(self) -> None:
        """Apply frameless Win32 styles (``WS_MINIMIZEBOX``) and clamp initial bounds to work area."""
        with self._lock:
            win = self._window
            is_max = self._window_maximized
        if win is None or sys.platform != "win32":
            return

        user32 = _get_win32_user32()
        hwnd = _extract_win32_hwnd(win)
        if hwnd is None or user32 is None:
            return

        _ensure_win32_minimize_box(hwnd, user32)

        if is_max or _is_win32_iconic(hwnd, user32):
            return

        work_area = _get_win32_work_area(win, user32=user32)
        cur = _get_win32_window_rect(hwnd, user32)
        if work_area is None or cur is None:
            return

        wx, wy, ww, wh = work_area
        cur_x, cur_y, cur_w, cur_h = cur
        target_w = min(cur_w, ww)
        target_h = min(cur_h, wh)
        target_x = max(wx, min(cur_x, wx + ww - target_w))
        target_y = max(wy, min(cur_y, wy + wh - target_h))
        if (target_x, target_y, target_w, target_h) != (cur_x, cur_y, cur_w, cur_h):
            _set_win32_window_pos(
                hwnd,
                target_x,
                target_y,
                target_w,
                target_h,
                user32,
                ensure_unminimized=False,
            )
        with self._lock:
            if self._window is win and not self._window_maximized:
                self._window_last_normal_bounds = (target_x, target_y, target_w, target_h)

    def _on_window_maximized(self) -> None:
        """Handle native maximize (e.g. Win+Up) by converting to work-area maximize and syncing UI."""
        with self._lock:
            win = self._window
            already_bridge_max = self._window_bridge_maximized and self._window_maximized
            restore_bounds = self._window_restore_bounds
            last_normal = self._window_last_normal_bounds
            converting = self._window_converting_max
        if converting:
            return
        if win is None:
            with self._lock:
                self._window_maximized = True
            return

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            has_swp = hwnd is not None and user32 is not None and callable(getattr(user32, "SetWindowPos", None))
            if has_swp:
                assert hwnd is not None
                zoomed = _is_win32_zoomed(hwnd, user32)
                if already_bridge_max and not zoomed:
                    return
                work_area = _get_win32_work_area(win, user32=user32)
                if work_area is not None:
                    wx, wy, ww, wh = work_area
                    with self._lock:
                        if self._window is not win:
                            return
                        self._window_converting_max = True
                    try:
                        if zoomed:
                            show_win = getattr(user32, "ShowWindow", None)
                            if callable(show_win):
                                show_win(hwnd, _SW_RESTORE)
                        prev_bounds = restore_bounds
                        if not already_bridge_max:
                            cur_rect = _get_win32_window_rect(hwnd, user32)
                            if cur_rect is not None and (cur_rect[2] < ww or cur_rect[3] < wh):
                                prev_bounds = cur_rect
                            elif last_normal is not None:
                                prev_bounds = last_normal
                            else:
                                dpi = _get_win32_dpi_for_window(hwnd, user32)
                                prev_bounds = _default_restore_bounds(work_area, scale=dpi / _WIN32_DEFAULT_DPI)
                        ok = _set_win32_window_pos(
                            hwnd,
                            wx,
                            wy,
                            ww,
                            wh,
                            user32,
                            ensure_unminimized=False,
                        )
                        with self._lock:
                            if self._window is win:
                                if ok:
                                    if not already_bridge_max:
                                        self._window_restore_bounds = prev_bounds
                                    self._window_maximized_bounds = work_area
                                    self._window_bridge_maximized = True
                                    self._window_bounds_physical = True
                                self._window_maximized = True
                                self._window_drag_active = False
                                self._window_drag_anchor = None
                                self._clear_window_resize_state_locked()
                    finally:
                        with self._lock:
                            self._window_converting_max = False
                    self._push_window_state(True)
                    return

        with self._lock:
            if self._window is win:
                self._window_maximized = True
                self._window_drag_active = False
                self._window_drag_anchor = None
                self._clear_window_resize_state_locked()
        self._push_window_state(True)

    def _set_window_maximized(self, maximized: bool) -> None:
        """Track native maximize/restore transitions without clobbering bridge-maximized state."""
        if not maximized:
            self._on_window_restored()
            return
        self._on_window_maximized()

    def _on_window_restored(self) -> None:
        """Handle native ``restored`` events (including taskbar restore from minimize).

        When the window is in bridge-managed maximized state, restoring from minimized
        fires ``restored`` because WinForms transitions Minimized -> Normal. Preserve
        ``_window_maximized`` and ``_window_restore_bounds`` and re-apply the current
        monitor's physical work area so the window returns maximized and the next toggle
        restores to the saved normal bounds.
        """
        with self._lock:
            if self._window_converting_max or self._window_restoring:
                return
            win = self._window
            bridge_max = self._window_bridge_maximized
            is_max = self._window_maximized
            saved_work_area = self._window_maximized_bounds
            is_physical = self._window_bounds_physical
            if not (bridge_max and is_max):
                was_max = is_max
                self._clear_window_max_state_locked()
                self._clear_window_resize_state_locked()
                should_push_unmax = was_max
            else:
                should_push_unmax = False

        if not (bridge_max and is_max):
            if should_push_unmax:
                self._push_window_state(False)
            return

        if win is None or sys.platform != "win32":
            return

        user32 = _get_win32_user32()
        hwnd = _extract_win32_hwnd(win)
        if is_physical and hwnd is not None and user32 is not None:
            if _is_win32_iconic(hwnd, user32):
                return
            work_area = _get_win32_work_area(win, user32=user32) or saved_work_area
            if work_area is not None:
                wx, wy, ww, wh = work_area
                with self._lock:
                    self._window_reapplying_max = True
                try:
                    if _set_win32_window_pos(hwnd, wx, wy, ww, wh, user32, ensure_unminimized=False):
                        with self._lock:
                            if self._window is win and self._window_bridge_maximized:
                                self._window_maximized_bounds = work_area
                finally:
                    with self._lock:
                        self._window_reapplying_max = False
        elif not is_physical and saved_work_area is not None:
            move_fn = getattr(win, "move", None)
            resize_fn = getattr(win, "resize", None)
            if callable(move_fn) and callable(resize_fn):
                wx, wy, ww, wh = saved_work_area
                with self._lock:
                    self._window_reapplying_max = True
                try:
                    move_fn(wx, wy)
                    resize_fn(ww, wh)
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._window_reapplying_max = False

    def _on_window_resized(self, *_args: Any) -> None:
        """Re-apply the current monitor work area when DPI scale or screen changes while maximized."""
        with self._lock:
            if self._window_converting_max or self._window_reapplying_max or self._window_restoring:
                return
            win = self._window
            bridge_max = self._window_bridge_maximized
            is_max = self._window_maximized
            is_physical = self._window_bounds_physical
            saved_work_area = self._window_maximized_bounds
        if win is None or not (bridge_max and is_max) or sys.platform != "win32":
            return

        user32 = _get_win32_user32()
        hwnd = _extract_win32_hwnd(win)
        if is_physical and hwnd is not None and user32 is not None:
            if _is_win32_iconic(hwnd, user32):
                return
            work_area = _get_win32_work_area(win, user32=user32)
            if work_area is None:
                return
            cur_rect = _get_win32_window_rect(hwnd, user32)
            zoomed = _is_win32_zoomed(hwnd, user32)
            if cur_rect == work_area and not zoomed:
                with self._lock:
                    if self._window is win and self._window_bridge_maximized:
                        self._window_maximized_bounds = work_area
                return
            wx, wy, ww, wh = work_area
            with self._lock:
                self._window_reapplying_max = True
            try:
                if _set_win32_window_pos(hwnd, wx, wy, ww, wh, user32, ensure_unminimized=True):
                    with self._lock:
                        if self._window is win and self._window_bridge_maximized:
                            self._window_maximized_bounds = work_area
            finally:
                with self._lock:
                    self._window_reapplying_max = False
        elif not is_physical:
            work_area = _get_win32_work_area(win, user32=user32)
            if work_area is None or work_area == saved_work_area:
                return
            move_fn = getattr(win, "move", None)
            resize_fn = getattr(win, "resize", None)
            if callable(move_fn) and callable(resize_fn):
                wx, wy, ww, wh = work_area
                with self._lock:
                    self._window_reapplying_max = True
                try:
                    move_fn(wx, wy)
                    resize_fn(ww, wh)
                    with self._lock:
                        if self._window is win and self._window_bridge_maximized:
                            self._window_maximized_bounds = work_area
                except Exception:
                    pass
                finally:
                    with self._lock:
                        self._window_reapplying_max = False

    def window_minimize(self) -> Dict[str, Any]:
        """Minimize the currently attached ``pywebview`` window."""
        with self._lock:
            win = self._window
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        fn = getattr(win, "minimize", None)
        if not callable(fn):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "当前窗口不支持最小化操作",
                exit_code=c.EXIT_INTERNAL,
            )
        try:
            fn()
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"最小化窗口失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )
        with self._lock:
            if self._window is win:
                self._window_drag_active = False
                self._window_drag_anchor = None
                self._clear_window_resize_state_locked()
        return _success_envelope({"minimized": True})

    def window_toggle_maximize(self) -> Dict[str, Any]:
        """Toggle maximize/restore state on the currently attached ``pywebview`` window.

        On Windows with an exposed native HWND, geometry uses Win32 physical pixels
        directly (``MonitorFromWindow``/``GetMonitorInfoW`` ``rcWork`` + ``GetWindowRect``
        + ``SetWindowPos``) so per-monitor DPI scaling never mixes logical and physical
        coordinates and the OS taskbar is never covered. Restore bounds are captured only
        when not in bridge-maximized state and are preserved across minimize/restore.
        """
        with self._lock:
            win = self._window
            was_maximized = self._window_maximized
            was_bridge_maximized = self._window_bridge_maximized
            restore_bounds = self._window_restore_bounds
            bounds_physical = self._window_bounds_physical
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )

        target_state = not was_maximized
        move_fn = getattr(win, "move", None)
        resize_fn = getattr(win, "resize", None)

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            has_swp = hwnd is not None and user32 is not None and callable(getattr(user32, "SetWindowPos", None))

            if has_swp:
                assert hwnd is not None
                if target_state:
                    work_area = _get_win32_work_area(win, user32=user32)
                    if work_area is not None:
                        wx, wy, ww, wh = work_area
                        prev_bounds = restore_bounds
                        if not was_bridge_maximized:
                            if not _is_win32_iconic(hwnd, user32):
                                prev_bounds = _get_win32_window_rect(hwnd, user32)
                            if prev_bounds is None:
                                dpi = _get_win32_dpi_for_window(hwnd, user32)
                                prev_bounds = _default_restore_bounds(work_area, scale=dpi / _WIN32_DEFAULT_DPI)
                        with self._lock:
                            if self._window is win:
                                self._window_reapplying_max = True
                        try:
                            ok = _set_win32_window_pos(hwnd, wx, wy, ww, wh, user32, ensure_unminimized=True)
                            if ok:
                                with self._lock:
                                    if self._window is win:
                                        if not self._window_bridge_maximized:
                                            self._window_restore_bounds = prev_bounds
                                        self._window_maximized_bounds = work_area
                                        self._window_maximized = True
                                        self._window_bridge_maximized = True
                                        self._window_bounds_physical = True
                                        self._window_drag_active = False
                                        self._window_drag_anchor = None
                                        self._clear_window_resize_state_locked()
                                    self._window_reapplying_max = False
                        finally:
                            with self._lock:
                                self._window_reapplying_max = False
                        if not ok:
                            return _error_envelope(
                                c.INTERNAL_ERROR,
                                "切换窗口最大化状态失败: SetWindowPos 调用失败",
                                exit_code=c.EXIT_INTERNAL,
                            )
                        return _success_envelope(
                            {
                                "maximized": True,
                                "work_area": {"x": wx, "y": wy, "width": ww, "height": wh},
                            }
                        )
                elif was_bridge_maximized and bounds_physical:
                    target_bounds = restore_bounds
                    if target_bounds is None:
                        wa = _get_win32_work_area(win, user32=user32) or (
                            0,
                            0,
                            DEFAULT_WINDOW_WIDTH,
                            DEFAULT_WINDOW_HEIGHT,
                        )
                        dpi = _get_win32_dpi_for_window(hwnd, user32)
                        target_bounds = _default_restore_bounds(wa, scale=dpi / _WIN32_DEFAULT_DPI)
                    rx, ry, rw, rh = target_bounds
                    with self._lock:
                        if self._window is win:
                            self._window_restoring = True
                    try:
                        ok = _set_win32_window_pos(hwnd, rx, ry, rw, rh, user32, ensure_unminimized=True)
                        if ok:
                            with self._lock:
                                if self._window is win:
                                    self._clear_window_max_state_locked()
                                    self._clear_window_resize_state_locked()
                                    self._window_last_normal_bounds = (rx, ry, rw, rh)
                                self._window_restoring = False
                    finally:
                        with self._lock:
                            self._window_restoring = False
                    if not ok:
                        return _error_envelope(
                            c.INTERNAL_ERROR,
                            "切换窗口最大化状态失败: SetWindowPos 调用失败",
                            exit_code=c.EXIT_INTERNAL,
                        )
                    return _success_envelope(
                        {
                            "maximized": False,
                            "bounds": {"x": rx, "y": ry, "width": rw, "height": rh},
                        }
                    )

            # Fallback Win32 path when HWND/SetWindowPos is unavailable
            if callable(move_fn) and callable(resize_fn):
                if target_state:
                    work_area = _get_win32_work_area(win, user32=user32)
                    if work_area is not None:
                        wx, wy, ww, wh = work_area
                        prev_bounds = restore_bounds
                        if not was_bridge_maximized:
                            prev_bounds = _read_window_bounds(win)
                            if prev_bounds is None:
                                prev_bounds = _default_restore_bounds(work_area, scale=1.0)
                        with self._lock:
                            if self._window is win:
                                self._window_reapplying_max = True
                        try:
                            move_fn(wx, wy)
                            resize_fn(ww, wh)
                            with self._lock:
                                if self._window is win:
                                    if not self._window_bridge_maximized:
                                        self._window_restore_bounds = prev_bounds
                                    self._window_maximized_bounds = work_area
                                    self._window_maximized = True
                                    self._window_bridge_maximized = True
                                    self._window_bounds_physical = False
                                    self._window_drag_active = False
                                    self._window_drag_anchor = None
                                    self._clear_window_resize_state_locked()
                                self._window_reapplying_max = False
                        except Exception as exc:
                            return _error_envelope(
                                c.INTERNAL_ERROR,
                                f"切换窗口最大化状态失败: {exc}",
                                exit_code=c.EXIT_INTERNAL,
                            )
                        finally:
                            with self._lock:
                                self._window_reapplying_max = False
                        return _success_envelope(
                            {
                                "maximized": True,
                                "work_area": {"x": wx, "y": wy, "width": ww, "height": wh},
                            }
                        )
                elif was_bridge_maximized and restore_bounds is not None:
                    rx, ry, rw, rh = restore_bounds
                    with self._lock:
                        if self._window is win:
                            self._window_restoring = True
                    try:
                        move_fn(rx, ry)
                        resize_fn(rw, rh)
                        with self._lock:
                            if self._window is win:
                                self._clear_window_max_state_locked()
                                self._clear_window_resize_state_locked()
                                self._window_last_normal_bounds = (rx, ry, rw, rh)
                            self._window_restoring = False
                    except Exception as exc:
                        return _error_envelope(
                            c.INTERNAL_ERROR,
                            f"切换窗口最大化状态失败: {exc}",
                            exit_code=c.EXIT_INTERNAL,
                        )
                    finally:
                        with self._lock:
                            self._window_restoring = False
                    return _success_envelope({"maximized": False})

        action_name = "restore" if was_maximized else "maximize"
        fn = getattr(win, action_name, None)
        if not callable(fn):
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"当前窗口不支持 {action_name} 操作",
                exit_code=c.EXIT_INTERNAL,
            )
        if not target_state:
            with self._lock:
                if self._window is win:
                    self._window_restoring = True
        try:
            fn()
            with self._lock:
                if self._window is win:
                    if target_state:
                        self._window_maximized = True
                        self._window_bridge_maximized = False
                        self._window_drag_active = False
                        self._window_drag_anchor = None
                        self._clear_window_resize_state_locked()
                    else:
                        self._clear_window_max_state_locked()
                        self._clear_window_resize_state_locked()
                if not target_state:
                    self._window_restoring = False
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"切换窗口最大化状态失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )
        finally:
            if not target_state:
                with self._lock:
                    self._window_restoring = False
        return _success_envelope({"maximized": target_state})

    def window_restore_for_drag(self, ratio_x: float = 0.5, offset_y: float = 18.0) -> Dict[str, Any]:
        """Restore a maximized window to its saved size positioned under the cursor for dragging."""
        with self._lock:
            win = self._window
            was_maximized = self._window_maximized
            was_bridge_maximized = self._window_bridge_maximized
            restore_bounds = self._window_restore_bounds
            bounds_physical = self._window_bounds_physical
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        if not _is_finite_number(ratio_x) or not _is_finite_number(offset_y):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "拖动还原参数必须为有限数值",
                exit_code=c.EXIT_USAGE,
                details={"param": "ratio_x/offset_y"},
            )
        if not was_maximized:
            return _success_envelope({"maximized": False, "restored": False})

        clamped_ratio = max(0.05, min(0.95, float(ratio_x)))
        clamped_off_y = max(4.0, min(60.0, float(offset_y)))

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None and callable(getattr(user32, "SetWindowPos", None)):
                dpi = _get_win32_dpi_for_window(hwnd, user32)
                scale = dpi / _WIN32_DEFAULT_DPI
                work_area = _get_win32_work_area(win, user32=user32)
                if restore_bounds is not None and bounds_physical:
                    rx, ry, rw, rh = restore_bounds
                else:
                    fallback_wa = work_area or (0, 0, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
                    rx, ry, rw, rh = _default_restore_bounds(fallback_wa, scale=scale)

                off_y_phys = max(4, min(max(4, rh // 2), int(round(clamped_off_y * scale))))
                cursor = _get_win32_cursor_pos(user32)
                anchor: Optional[Tuple[int, int, int, int]] = None
                if cursor is not None:
                    cx, cy = cursor
                    new_x = int(round(cx - rw * clamped_ratio))
                    new_y = int(round(cy - off_y_phys))
                    if work_area is not None:
                        _wx, wy, _ww, wh = work_area
                        new_y = max(wy, min(wy + max(0, wh - off_y_phys * 2), new_y))
                    anchor = (cx - new_x, cy - new_y, rw, rh)
                else:
                    new_x, new_y = rx, ry

                with self._lock:
                    if self._window is win:
                        self._window_restoring = True
                try:
                    ok = _set_win32_window_pos(hwnd, new_x, new_y, rw, rh, user32, ensure_unminimized=True)
                    if ok:
                        with self._lock:
                            if self._window is win:
                                self._clear_window_max_state_locked()
                                self._clear_window_resize_state_locked()
                                self._window_drag_active = True
                                self._window_drag_anchor = anchor
                                self._window_last_normal_bounds = (new_x, new_y, rw, rh)
                            self._window_restoring = False
                finally:
                    with self._lock:
                        self._window_restoring = False
                if not ok:
                    return _error_envelope(
                        c.INTERNAL_ERROR,
                        "拖动还原窗口失败: SetWindowPos 调用失败",
                        exit_code=c.EXIT_INTERNAL,
                    )
                return _success_envelope(
                    {
                        "maximized": False,
                        "restored": True,
                        "bounds": {"x": new_x, "y": new_y, "width": rw, "height": rh},
                    }
                )

            move_fn = getattr(win, "move", None)
            resize_fn = getattr(win, "resize", None)
            if was_bridge_maximized and restore_bounds is not None and callable(move_fn) and callable(resize_fn):
                rx, ry, rw, rh = restore_bounds
                with self._lock:
                    if self._window is win:
                        self._window_restoring = True
                try:
                    move_fn(rx, ry)
                    resize_fn(rw, rh)
                    with self._lock:
                        if self._window is win:
                            self._clear_window_max_state_locked()
                            self._clear_window_resize_state_locked()
                            self._window_drag_active = True
                            self._window_last_normal_bounds = (rx, ry, rw, rh)
                        self._window_restoring = False
                except Exception as exc:
                    return _error_envelope(
                        c.INTERNAL_ERROR,
                        f"拖动还原窗口失败: {exc}",
                        exit_code=c.EXIT_INTERNAL,
                    )
                finally:
                    with self._lock:
                        self._window_restoring = False
                return _success_envelope({"maximized": False, "restored": True})

        restore_fn = getattr(win, "restore", None)
        if not callable(restore_fn):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "当前窗口不支持还原操作",
                exit_code=c.EXIT_INTERNAL,
            )
        with self._lock:
            if self._window is win:
                self._window_restoring = True
        try:
            restore_fn()
            with self._lock:
                if self._window is win:
                    self._clear_window_max_state_locked()
                    self._clear_window_resize_state_locked()
                    self._window_drag_active = True
                self._window_restoring = False
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"拖动还原窗口失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )
        finally:
            with self._lock:
                self._window_restoring = False
        return _success_envelope({"maximized": False, "restored": True})

    def window_drag_move(self, dx: float = 0.0, dy: float = 0.0) -> Dict[str, Any]:
        """Continue dragging a window restored by :meth:`window_restore_for_drag`."""
        with self._lock:
            win = self._window
            drag_active = self._window_drag_active
            anchor = self._window_drag_anchor
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        if not _is_finite_number(dx) or not _is_finite_number(dy):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "窗口拖动偏移必须为有限数值",
                exit_code=c.EXIT_USAGE,
                details={"param": "dx/dy"},
            )
        if not drag_active:
            return _error_envelope(
                c.INVALID_TRANSITION,
                "当前未处于窗口拖动状态",
                exit_code=c.EXIT_CONFLICT,
            )

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None and anchor is not None:
                cursor = _get_win32_cursor_pos(user32)
                if cursor is not None:
                    cx, cy = cursor
                    ax, ay, rw, rh = anchor
                    new_x = cx - ax
                    new_y = cy - ay
                    if _set_win32_window_pos(
                        hwnd,
                        new_x,
                        new_y,
                        0,
                        0,
                        user32,
                        flags=_SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
                        ensure_unminimized=False,
                    ):
                        with self._lock:
                            if self._window is win and not self._window_maximized:
                                self._window_last_normal_bounds = (new_x, new_y, rw, rh)
                        return _success_envelope({"moved": True, "x": new_x, "y": new_y})

        if dx != 0 or dy != 0:
            cur = _read_window_bounds(win)
            move_fn = getattr(win, "move", None)
            if cur is not None and callable(move_fn):
                nx = cur[0] + int(round(dx))
                ny = cur[1] + int(round(dy))
                try:
                    move_fn(nx, ny)
                    return _success_envelope({"moved": True, "x": nx, "y": ny})
                except Exception as exc:
                    return _error_envelope(
                        c.INTERNAL_ERROR,
                        f"拖动窗口失败: {exc}",
                        exit_code=c.EXIT_INTERNAL,
                    )
        return _success_envelope({"moved": False})

    def window_drag_end(self) -> Dict[str, Any]:
        """End an active titlebar drag session and clear the stored drag anchor."""
        with self._lock:
            win = self._window
            was_active = self._window_drag_active or self._window_drag_anchor is not None
            self._window_drag_active = False
            self._window_drag_anchor = None
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        return _success_envelope({"ended": True, "was_active": was_active})

    def window_resize_start(self, edge: str) -> Dict[str, Any]:
        """Capture physical cursor and window geometry at the start of an edge resize gesture."""
        with self._lock:
            win = self._window
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        if not isinstance(edge, str):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "无效的缩放方向参数",
                exit_code=c.EXIT_USAGE,
                details={"param": "edge"},
            )
        norm_edge = edge.strip().lower()
        if norm_edge not in _VALID_RESIZE_EDGES:
            return _error_envelope(
                c.INVALID_ARGUMENT,
                f"不支持的缩放方向: {edge}",
                exit_code=c.EXIT_USAGE,
                details={"param": "edge"},
            )

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None:
                rect = _get_win32_window_rect(hwnd, user32)
                cursor = _get_win32_cursor_pos(user32)
                if rect is not None and cursor is not None:
                    rx, ry, rw, rh = rect
                    cx, cy = cursor
                    with self._lock:
                        if self._window is win:
                            self._window_drag_active = False
                            self._window_drag_anchor = None
                            self._window_resize_anchor = (norm_edge, cx, cy, rx, ry, rw, rh)
                    return _success_envelope(
                        {
                            "started": True,
                            "edge": norm_edge,
                            "physical": True,
                            "bounds": {"x": rx, "y": ry, "width": rw, "height": rh},
                        }
                    )

        with self._lock:
            if self._window is win:
                self._window_drag_active = False
                self._window_drag_anchor = None
                self._window_resize_anchor = None
        return _success_envelope({"started": True, "edge": norm_edge, "physical": False})

    def window_resize_end(self) -> Dict[str, Any]:
        """End an active edge resize session and clear the captured physical resize anchor."""
        with self._lock:
            win = self._window
            was_active = self._window_resize_anchor is not None
            self._window_resize_anchor = None
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        return _success_envelope({"ended": True, "was_active": was_active})

    def window_resize(
        self,
        width: int,
        height: int,
        edge: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resize the attached frameless window, clamped to ``min_size``, moving for N/W edges."""
        with self._lock:
            win = self._window
            resize_anchor = self._window_resize_anchor
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        if not _is_finite_number(width) or not _is_finite_number(height):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "窗口宽高必须为有限数值",
                exit_code=c.EXIT_USAGE,
                details={"param": "width/height"},
            )
        norm_edge: Optional[str] = None
        if edge is not None:
            if not isinstance(edge, str):
                return _error_envelope(
                    c.INVALID_ARGUMENT,
                    "无效的缩放方向参数",
                    exit_code=c.EXIT_USAGE,
                    details={"param": "edge"},
                )
            stripped = edge.strip().lower()
            if stripped:
                if stripped not in _VALID_RESIZE_EDGES:
                    return _error_envelope(
                        c.INVALID_ARGUMENT,
                        f"不支持的缩放方向: {edge}",
                        exit_code=c.EXIT_USAGE,
                        details={"param": "edge"},
                    )
                norm_edge = stripped

        min_w, min_h = _resolve_window_min_size(win)
        target_w = max(min_w, min(int(round(width)), MAX_WINDOW_WIDTH))
        target_h = max(min_h, min(int(round(height)), MAX_WINDOW_HEIGHT))

        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None and callable(getattr(user32, "SetWindowPos", None)):
                dpi = _get_win32_dpi_for_window(hwnd, user32)
                scale = dpi / _WIN32_DEFAULT_DPI
                phys_min_w = max(1, int(round(min_w * scale)))
                phys_min_h = max(1, int(round(min_h * scale)))

                # When a physical resize gesture is active, compute directly from GetCursorPos
                # in physical pixels so 150%/non-integer DPI never rounds or drifts edges.
                if (
                    resize_anchor is not None
                    and norm_edge is not None
                    and resize_anchor[0] == norm_edge
                ):
                    cursor = _get_win32_cursor_pos(user32)
                    if cursor is not None:
                        a_edge, start_cx, start_cy, start_x, start_y, start_w, start_h = resize_anchor
                        start_right = start_x + start_w
                        start_bottom = start_y + start_h
                        dx_phys = cursor[0] - start_cx
                        dy_phys = cursor[1] - start_cy

                        if "e" in a_edge:
                            new_x = start_x
                            phys_w = max(phys_min_w, min(start_w + dx_phys, MAX_WINDOW_WIDTH))
                        elif "w" in a_edge:
                            phys_w = max(phys_min_w, min(start_w - dx_phys, MAX_WINDOW_WIDTH))
                            new_x = start_right - phys_w
                        else:
                            new_x = start_x
                            phys_w = start_w

                        if "s" in a_edge:
                            new_y = start_y
                            phys_h = max(phys_min_h, min(start_h + dy_phys, MAX_WINDOW_HEIGHT))
                        elif "n" in a_edge:
                            phys_h = max(phys_min_h, min(start_h - dy_phys, MAX_WINDOW_HEIGHT))
                            new_y = start_bottom - phys_h
                        else:
                            new_y = start_y
                            phys_h = start_h

                        with self._lock:
                            if self._window is win:
                                self._window_restoring = True
                        try:
                            ok = _set_win32_window_pos(
                                hwnd,
                                new_x,
                                new_y,
                                phys_w,
                                phys_h,
                                user32,
                                ensure_unminimized=True,
                            )
                            if ok:
                                with self._lock:
                                    if self._window is win:
                                        self._clear_window_max_state_locked()
                                        self._window_last_normal_bounds = (new_x, new_y, phys_w, phys_h)
                                    self._window_restoring = False
                        finally:
                            with self._lock:
                                self._window_restoring = False
                        if not ok:
                            return _error_envelope(
                                c.INTERNAL_ERROR,
                                "调整窗口大小失败: SetWindowPos 调用失败",
                                exit_code=c.EXIT_INTERNAL,
                            )
                        return _success_envelope(
                            {
                                "width": max(1, int(round(phys_w / scale))),
                                "height": max(1, int(round(phys_h / scale))),
                                "physical_bounds": {"x": new_x, "y": new_y, "width": phys_w, "height": phys_h},
                                "edge": norm_edge,
                                "maximized": False,
                            }
                        )

                phys_w = max(phys_min_w, min(int(round(target_w * scale)), MAX_WINDOW_WIDTH))
                phys_h = max(phys_min_h, min(int(round(target_h * scale)), MAX_WINDOW_HEIGHT))
                cur = _get_win32_window_rect(hwnd, user32)
                with self._lock:
                    if self._window is win:
                        self._window_restoring = True
                try:
                    if cur is not None:
                        cur_x, cur_y, cur_w, cur_h = cur
                        new_x = cur_x + (cur_w - phys_w) if (norm_edge and "w" in norm_edge) else cur_x
                        new_y = cur_y + (cur_h - phys_h) if (norm_edge and "n" in norm_edge) else cur_y
                        ok = _set_win32_window_pos(hwnd, new_x, new_y, phys_w, phys_h, user32, ensure_unminimized=True)
                        updated_bounds: Optional[Tuple[int, int, int, int]] = (new_x, new_y, phys_w, phys_h)
                    else:
                        ok = _set_win32_window_pos(
                            hwnd,
                            0,
                            0,
                            phys_w,
                            phys_h,
                            user32,
                            flags=_SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE,
                            ensure_unminimized=True,
                        )
                        updated_bounds = None
                    if ok:
                        with self._lock:
                            if self._window is win:
                                self._clear_window_max_state_locked()
                                if updated_bounds is not None:
                                    self._window_last_normal_bounds = updated_bounds
                            self._window_restoring = False
                finally:
                    with self._lock:
                        self._window_restoring = False
                if not ok:
                    return _error_envelope(
                        c.INTERNAL_ERROR,
                        "调整窗口大小失败: SetWindowPos 调用失败",
                        exit_code=c.EXIT_INTERNAL,
                    )
                return _success_envelope(
                    {
                        "width": target_w,
                        "height": target_h,
                        "edge": norm_edge,
                        "maximized": False,
                    }
                )

        resize_fn = getattr(win, "resize", None)
        if not callable(resize_fn):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "当前窗口不支持调整大小操作",
                exit_code=c.EXIT_INTERNAL,
            )

        with self._lock:
            if self._window is win:
                self._window_restoring = True
        try:
            if norm_edge and ("w" in norm_edge or "n" in norm_edge):
                cur = _read_window_bounds(win)
                move_fn = getattr(win, "move", None)
                if cur is not None and callable(move_fn):
                    cur_x, cur_y, cur_w, cur_h = cur
                    new_x = cur_x + (cur_w - target_w) if "w" in norm_edge else cur_x
                    new_y = cur_y + (cur_h - target_h) if "n" in norm_edge else cur_y
                    if new_x != cur_x or new_y != cur_y:
                        move_fn(new_x, new_y)
            resize_fn(target_w, target_h)
            with self._lock:
                if self._window is win:
                    self._clear_window_max_state_locked()
                self._window_restoring = False
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"调整窗口大小失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )
        finally:
            with self._lock:
                self._window_restoring = False

        return _success_envelope(
            {
                "width": target_w,
                "height": target_h,
                "edge": norm_edge,
                "maximized": False,
            }
        )

    def window_close(self) -> Dict[str, Any]:
        """Close or hide-to-tray the currently attached ``pywebview`` window."""
        self.save_current_window_bounds()
        if self.should_hide_on_close():
            res = self.window_hide(from_close_button=True)
            if res.get("ok"):
                return _success_envelope({"closing": False, "hidden_to_tray": True})
            return res
        with self._lock:
            win = self._window
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口",
                exit_code=c.EXIT_INTERNAL,
            )
        fn = getattr(win, "destroy", None)
        if not callable(fn):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "当前窗口不支持关闭操作",
                exit_code=c.EXIT_INTERNAL,
            )
        try:
            fn()
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"关闭窗口失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )
        with self._lock:
            if self._window is win:
                self._window_drag_active = False
                self._window_drag_anchor = None
                self._clear_window_resize_state_locked()
        return _success_envelope({"closing": True, "hidden_to_tray": False})

    def attach_tray(self, tray: Any) -> None:
        """Bind an optional Windows system tray controller."""
        with self._lock:
            self._tray = tray
        self._notify_tray_status()

    def tray_status_snapshot(self) -> Dict[str, Any]:
        """Return thread-safe daemon/auth/window status for system tray display."""
        with self._lock:
            ds = self._daemon_status or {}
            return {
                "connected": bool(self._connected),
                "daemon_starting": bool(self._daemon_starting),
                "daemon_stopping": bool(self._daemon_stopping),
                "auth_verified": bool(self._auth_verified),
                "window_visible": bool(self._window_visible),
                "pid": ds.get("pid") if isinstance(ds, dict) else None,
            }

    def _notify_tray_status(self) -> None:
        with self._lock:
            tray = self._tray
        if tray is None:
            return
        fn = getattr(tray, "update_status", None)
        if callable(fn):
            try:
                fn(self.tray_status_snapshot())
            except Exception:
                pass

    def should_hide_on_close(self) -> bool:
        """Return True when closing the window should hide to the system tray instead of exiting."""
        with self._lock:
            if self._quitting:
                return False
            tray = self._tray
        if tray is None or not bool(getattr(tray, "active", False)):
            return False
        return bool(self._state_store.load_prefs().get("close_to_tray", True))

    def window_hide(self, *, from_close_button: bool = False) -> Dict[str, Any]:
        """Hide the desktop window to the system tray while keeping in-memory session & meshd alive."""
        self.save_current_window_bounds()
        with self._lock:
            win = self._window
            tray = self._tray
        if win is None:
            return _error_envelope(c.INTERNAL_ERROR, "未绑定桌面窗口", exit_code=c.EXIT_INTERNAL)
        hide_fn = getattr(win, "hide", None)
        if not callable(hide_fn):
            return _error_envelope(c.INTERNAL_ERROR, "当前窗口不支持隐藏到托盘", exit_code=c.EXIT_INTERNAL)
        try:
            hide_fn()
        except Exception as exc:
            return _error_envelope(c.INTERNAL_ERROR, f"隐藏窗口失败: {exc}", exit_code=c.EXIT_INTERNAL)
        with self._lock:
            self._window_visible = False
            self._window_drag_active = False
            self._window_drag_anchor = None
            self._clear_window_resize_state_locked()
        self._notify_tray_status()
        if from_close_button and tray is not None:
            prefs = self._state_store.load_prefs()
            if not prefs.get("tray_hint_shown", False):
                self._state_store.update_prefs({"tray_hint_shown": True})
                balloon_fn = getattr(tray, "show_balloon", None)
                if callable(balloon_fn):
                    try:
                        balloon_fn(
                            "Agent Mesh 已最小化到托盘",
                            "后台服务继续运行，会话保持解锁。点击托盘图标可随时恢复窗口。",
                        )
                    except Exception:
                        pass
        return _success_envelope({"hidden": True})

    def window_show(self) -> Dict[str, Any]:
        """Show and bring the desktop window back to the foreground."""
        with self._lock:
            win = self._window
        if win is None:
            return _error_envelope(c.INTERNAL_ERROR, "未绑定桌面窗口", exit_code=c.EXIT_INTERNAL)
        show_fn = getattr(win, "show", None)
        if callable(show_fn):
            try:
                show_fn()
            except Exception as exc:
                return _error_envelope(c.INTERNAL_ERROR, f"显示窗口失败: {exc}", exit_code=c.EXIT_INTERNAL)
        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None:
                if _is_win32_iconic(hwnd, user32):
                    sw = getattr(user32, "ShowWindow", None)
                    if callable(sw):
                        try:
                            sw(hwnd, _SW_RESTORE)
                        except Exception:
                            pass
                sfg = getattr(user32, "SetForegroundWindow", None)
                if callable(sfg):
                    try:
                        sfg(hwnd)
                    except Exception:
                        pass
        with self._lock:
            self._window_visible = True
        self._notify_tray_status()
        return _success_envelope({"visible": True})

    def window_toggle_visibility(self) -> Dict[str, Any]:
        """Toggle window visibility between shown and hidden-in-tray."""
        with self._lock:
            visible = self._window_visible
        return self.window_hide() if visible else self.window_show()

    def load_saved_window_bounds(self) -> Optional[Dict[str, Any]]:
        """Load last saved window bounds for this MESH_HOME (S6)."""
        return self._state_store.load_window_bounds()

    def save_current_window_bounds(self) -> bool:
        """Persist current window geometry safely (S6, non-sensitive UI state only)."""
        with self._lock:
            win = self._window
            is_max = bool(self._window_maximized)
            restore_bounds = self._window_restore_bounds
            last_normal = self._window_last_normal_bounds
            is_physical = bool(self._window_bounds_physical)
        if win is None:
            return False
        bounds = restore_bounds if (is_max and restore_bounds is not None) else last_normal
        scale = 1.0
        if sys.platform == "win32":
            user32 = _get_win32_user32()
            hwnd = _extract_win32_hwnd(win)
            if hwnd is not None and user32 is not None:
                dpi = _get_win32_dpi_for_window(hwnd, user32)
                scale = max(0.5, dpi / _WIN32_DEFAULT_DPI)
                if not is_max and not _is_win32_iconic(hwnd, user32):
                    rect = _get_win32_window_rect(hwnd, user32)
                    if rect is not None:
                        bounds = rect
                        is_physical = True
        if bounds is not None:
            bx, by, bw, bh = bounds
            if is_physical and scale != 1.0:
                bx = int(round(bx / scale))
                by = int(round(by / scale))
                bw = int(round(bw / scale))
                bh = int(round(bh / scale))
            return self._state_store.save_window_bounds(
                x=bx, y=by, width=bw, height=bh, maximized=is_max
            )
        cur = _read_window_bounds(win)
        if cur is not None:
            return self._state_store.save_window_bounds(
                x=cur[0], y=cur[1], width=cur[2], height=cur[3], maximized=is_max
            )
        return False

    def desktop_prefs(self) -> Dict[str, Any]:
        """Return safe non-sensitive desktop preferences and tray/vault capability flags."""
        prefs = self._state_store.load_prefs()
        with self._lock:
            tray_active = bool(self._tray is not None and getattr(self._tray, "active", False))
        cred_supported = self._cred_supported()
        cred_present = self._cred_present() if cred_supported else False
        return _success_envelope(
            {
                "close_to_tray": bool(prefs.get("close_to_tray", True)),
                "tray_active": tray_active,
                "tray_supported": sys.platform == "win32",
                "stored_credential_supported": cred_supported,
                "stored_credential_present": cred_present,
            }
        )

    def set_desktop_prefs(self, prefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Update whitelisted non-sensitive desktop preferences (e.g. ``close_to_tray``)."""
        if not isinstance(prefs, dict):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "偏好设置参数必须为 JSON 对象",
                exit_code=c.EXIT_USAGE,
                details={"param": "prefs"},
            )
        allowed_updates: Dict[str, Any] = {}
        if "close_to_tray" in prefs:
            if not isinstance(prefs["close_to_tray"], bool):
                return _error_envelope(
                    c.INVALID_ARGUMENT,
                    "参数 'close_to_tray' 必须为布尔值",
                    exit_code=c.EXIT_USAGE,
                    details={"param": "close_to_tray"},
                )
            allowed_updates["close_to_tray"] = prefs["close_to_tray"]
        self._state_store.update_prefs(allowed_updates)
        return self.desktop_prefs()

    def quit_app(self, stop_daemon: bool = False) -> Dict[str, Any]:
        """Explicitly quit the desktop GUI. Stops ``meshd`` ONLY when ``stop_daemon=True``."""
        if not isinstance(stop_daemon, bool):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "参数 'stop_daemon' 必须为布尔值",
                exit_code=c.EXIT_USAGE,
                details={"param": "stop_daemon"},
            )
        self.save_current_window_bounds()
        daemon_stopped = False
        daemon_stop_error: Optional[Dict[str, Any]] = None
        if stop_daemon:
            stop_res = self.stop_daemon()
            if stop_res.get("ok"):
                daemon_stopped = True
            else:
                daemon_stop_error = stop_res.get("error")
        with self._lock:
            self._quitting = True
            win = self._window
            tray = self._tray
        if tray is not None:
            stop_fn = getattr(tray, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                except Exception:
                    pass
        if win is not None:
            destroy_fn = getattr(win, "destroy", None)
            if callable(destroy_fn):
                try:
                    destroy_fn()
                except Exception:
                    pass
        self.request_stop()
        return _success_envelope(
            {
                "quitting": True,
                "daemon_stopped": daemon_stopped,
                "daemon_stop_error": daemon_stop_error,
            }
        )

    def start(self) -> None:
        """Start the background daemon health & reconnect supervisor."""
        with self._lock:
            if self._stop_event.is_set():
                return
            if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
                return
            self._supervisor_thread = threading.Thread(
                target=self._supervisor_loop,
                name="mesh-desktop-supervisor",
                daemon=True,
            )
            self._supervisor_thread.start()
        self._log_desktop("INFO", "BRIDGE_START", "DesktopBridge supervisor started", details={"home": self.home})

    def request_stop(self) -> None:
        """Non-blocking shutdown signal safe to invoke from the GUI thread on window close."""
        with self._lock:
            self._window = None
            self._clear_window_max_state_locked()
            self._clear_window_resize_state_locked()
            self._window_last_normal_bounds = None
            self._window_converting_max = False
            self._window_reapplying_max = False
            self._window_restoring = False
            self._passphrase = None
            self._auth_verified = False
            self._principal = None
            self._push_needed = False
            stop_ev = self._stream_stop_event
        self._stop_event.set()
        self._wake_event.set()
        if stop_ev is not None:
            stop_ev.set()

    def close(self) -> None:
        """Stop all background threads, close streaming sockets, and wipe in-memory secrets."""
        self._log_desktop("INFO", "BRIDGE_CLOSE", "DesktopBridge closing")
        self.request_stop()
        self.stop_event_stream()
        sup = self._supervisor_thread
        if sup is not None and sup.is_alive() and sup is not threading.current_thread():
            sup.join(timeout=3.0)
        with self._lock:
            self._window = None
            self._passphrase = None
            self._auth_verified = False
            self._principal = None
            self._push_needed = False

    # ---- Public metadata & auth API -------------------------------------

    def capabilities(self) -> Dict[str, Any]:
        """Return backend UI capability gates for graceful feature degradation."""
        return dict(CAPABILITIES)

    def _cred_supported(self) -> bool:
        try:
            fn = getattr(self._cred_store, "is_supported", None)
            return bool(fn()) if callable(fn) else False
        except Exception:
            return False

    def _cred_present(self) -> bool:
        try:
            fn = getattr(self._cred_store, "has_passphrase", None)
            return bool(fn(self.home)) if callable(fn) else False
        except Exception:
            return False

    def auth_status(self) -> Dict[str, Any]:
        """Return safe authentication state without exposing credentials."""
        with self._lock:
            data = self._auth_snapshot_unlocked()
        return _success_envelope(data)

    def set_passphrase(
        self,
        passphrase: str,
        verify: bool = True,
        remember: bool = False,
    ) -> Dict[str, Any]:
        """Store user passphrase in memory (default) and verify it via ``whoami`` when online.

        When ``remember=True`` on Windows (explicit user opt-in), the passphrase is also
        stored in Windows Credential Manager (DPAPI-protected per user, never plaintext).
        """
        if not isinstance(passphrase, str) or len(passphrase) == 0:
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "口令不能为空",
                exit_code=c.EXIT_USAGE,
                details={"param": "passphrase"},
            )
        want_remember = bool(remember)

        if verify:
            res = self._raw_rpc(
                "whoami",
                {},
                auth={"type": "user", "passphrase": passphrase},
                timeout=self._probe_timeout_s,
            )
            if res["ok"]:
                principal = res.get("data") or {}
                with self._lock:
                    self._passphrase = passphrase
                    self._auth_verified = True
                    self._principal = dict(principal)
                    self._last_auth_error = None
                    self._connected = True
                    self._reconnect_attempt = 0
                    self._auto_unlock_attempted = True
                self._log_desktop(
                    "INFO",
                    "AUTH_UNLOCK_OK",
                    "User passphrase verified in memory",
                    details={"principal": principal.get("name") or principal.get("id")},
                    extra_secrets=(passphrase,),
                )
                if want_remember and self._cred_supported():
                    try:
                        self._cred_store.write_passphrase(self.home, passphrase)
                    except Exception:
                        pass
                self.start_event_stream()
                self._wake_event.set()
                self._notify_tray_status()
                return _success_envelope(self._auth_snapshot_unlocked())

            err = res["error"]
            if err.get("code") != c.DAEMON_UNAVAILABLE:
                with self._lock:
                    self._passphrase = None
                    self._auth_verified = False
                    self._principal = None
                    self._last_auth_error = err
                self._log_desktop(
                    "WARNING",
                    "AUTH_UNLOCK_FAILED",
                    "User passphrase verification failed",
                    details={"code": err.get("code"), "exit_code": err.get("exit_code")},
                    extra_secrets=(passphrase,),
                )
                self._notify_tray_status()
                return res

        # Daemon is offline or verification skipped: hold in memory until daemon comes online.
        with self._lock:
            self._passphrase = passphrase
            self._auth_verified = False
            self._principal = None
            self._last_auth_error = None
        self._log_desktop(
            "INFO",
            "AUTH_DEFERRED",
            "User passphrase held in memory pending daemon verification",
            extra_secrets=(passphrase,),
        )
        if verify:
            self._wake_event.set()
        self._notify_tray_status()
        return _success_envelope(self._auth_snapshot_unlocked())

    def clear_passphrase(self, forget_stored: bool = True) -> Dict[str, Any]:
        """Clear in-memory user passphrase, optionally delete any opted-in OS credential, and stop event stream."""
        self.stop_event_stream()
        if forget_stored and self._cred_supported():
            try:
                self._cred_store.delete_passphrase(self.home)
            except Exception:
                pass
        with self._lock:
            self._passphrase = None
            self._auth_verified = False
            self._principal = None
            self._last_auth_error = None
            self._last_seq = None
            self._pending_events.clear()
            self._push_needed = False
            self._auto_unlock_attempted = True
            snap = self._auth_snapshot_unlocked()
            conn_snap = self._connection_snapshot_unlocked()
        self._log_desktop("INFO", "AUTH_LOCKED", "In-memory user passphrase cleared")
        self._notify_tray_status()
        self._enqueue_and_push(
            {
                "type": "_desktop.connection",
                "connected": conn_snap["connected"],
                "auth_locked": True,
                "connection": conn_snap,
            }
        )
        return _success_envelope(snap)

    def _try_auto_unlock_from_store(self) -> bool:
        """Attempt one-shot opt-in unlock from Windows Credential Manager into Python memory."""
        with self._lock:
            if self._passphrase is not None or self._auto_unlock_attempted or not self._connected:
                return False
            self._auto_unlock_attempted = True
        if not self._cred_supported():
            return False
        try:
            stored = self._cred_store.read_passphrase(self.home)
        except Exception:
            return False
        if not stored:
            return False
        res = self.set_passphrase(stored, verify=True, remember=False)
        if res.get("ok"):
            return True
        err_code = (res.get("error") or {}).get("code")
        if err_code == c.PASSPHRASE_INVALID:
            # Wipe stale credential immediately so it never triggers lockout on future launches.
            try:
                self._cred_store.delete_passphrase(self.home)
            except Exception:
                pass
        return False

    def pipe_info(self) -> Dict[str, Any]:
        """Return safe local IPC endpoint metadata for Settings and footer display."""
        endpoint = self._resolve_endpoint()
        with self._lock:
            return _success_envelope(
                {
                    "home": self.home,
                    "platform": sys.platform,
                    "transport": "windows_named_pipe" if sys.platform == "win32" else "unix_domain_socket",
                    "endpoint": endpoint,
                    "connected": self._connected,
                    "auth_configured": self._passphrase is not None,
                    "auth_verified": self._auth_verified,
                    "reconnect_attempt": self._reconnect_attempt,
                    "next_retry_s": self._next_retry_s,
                    "daemon_log_path": self._daemon_log_path(),
                    "desktop_log_path": self._desktop_log_path(),
                }
            )

    def connection_state(self) -> Dict[str, Any]:
        """Return a snapshot of daemon connectivity, version, auth, and backoff state."""
        self._refresh_home_initialized()
        with self._lock:
            return _success_envelope(self._connection_snapshot_unlocked(include_diag_log=True))

    def home_status(self) -> Dict[str, Any]:
        """Return safe read-only initialization and daemon state without leaking DB contents."""
        home_init = self._refresh_home_initialized()
        with self._lock:
            return _success_envelope(
                {
                    "home": self.home,
                    "home_initialized": home_init,
                    "daemon_running": bool(self._connected),
                    "daemon_starting": bool(self._daemon_starting),
                    "auth_configured": self._passphrase is not None,
                    "auth_verified": bool(self._auth_verified),
                }
            )

    def initialize_home(self, passphrase: str) -> Dict[str, Any]:
        """Initialize an uninitialized MESH_HOME, start meshd, and unlock in memory only.

        Strictly refused when ``mesh.db`` already exists. The passphrase is never
        written to argv, environment variables, logs, disk, or returned to JS.
        """
        if not isinstance(passphrase, str) or len(passphrase) < 12:
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "用户口令长度至少需要 12 个字符",
                exit_code=c.EXIT_USAGE,
                details={"param": "passphrase", "min_length": 12},
            )

        if not self._init_home_lock.acquire(blocking=False):
            return _error_envelope(
                c.CONCURRENCY_LIMIT,
                "正在初始化数据目录，请稍候",
                exit_code=c.EXIT_CONFLICT,
                details={"home": self.home},
            )
        try:
            if self._refresh_home_initialized():
                return _error_envelope(
                    c.ALREADY_EXISTS,
                    "当前数据目录已初始化，请直接输入口令解锁",
                    exit_code=c.EXIT_CONFLICT,
                    details={"home": self.home, "home_initialized": True},
                )

            from mesh.core import init as core_init

            try:
                init_res = core_init.init_mesh_home(self.home, passphrase)
            except MeshError as exc:
                self._log_desktop(
                    "ERROR",
                    "HOME_INIT_FAILED",
                    exc.message,
                    details={"code": exc.code, "exit_code": exc.exit_code},
                    extra_secrets=(passphrase,),
                )
                return _error_envelope(
                    exc.code,
                    exc.message,
                    exit_code=exc.exit_code,
                    details=exc.details,
                )
            except OSError as exc:
                self._log_desktop(
                    "ERROR",
                    "HOME_INIT_FAILED",
                    f"OSError: {exc}",
                    details={"home": self.home},
                    extra_secrets=(passphrase,),
                )
                return _error_envelope(
                    c.INTERNAL_ERROR,
                    f"初始化数据目录失败: {exc}",
                    exit_code=c.EXIT_INTERNAL,
                    details={"home": self.home},
                )

            if not isinstance(init_res, dict) or not init_res.get("initialized"):
                self._refresh_home_initialized()
                return _error_envelope(
                    c.ALREADY_EXISTS,
                    "当前数据目录已初始化，请直接输入口令解锁",
                    exit_code=c.EXIT_CONFLICT,
                    details={"home": self.home, "home_initialized": True},
                )

            self._refresh_home_initialized()
            with self._lock:
                self._onboarding_pushed = False
                self._user_stopped_daemon = False
                self._stopped_daemon_pid = None
                self._stopped_daemon_boot_id = None
                self._daemon_stopping = False
                self._auto_start_attempted = False
                self._auto_start_timestamps.clear()
                self._last_start_error = None
                self._last_error = None
            self._record_diag("info", "HOME_INITIALIZED", "已在桌面端完成 MESH_HOME 初始化", {"home": self.home})

            start_res = self._start_daemon_impl(auto=False)
            if start_res.get("ok") and (start_res.get("data") or {}).get("in_progress"):
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not self._stop_event.is_set():
                    if self._probe_once():
                        break
                    time.sleep(0.1)

            daemon_running = bool(self._connected) or self._probe_once()
            if not start_res.get("ok") or not daemon_running:
                err = start_res.get("error") or {
                    "code": c.DAEMON_UNAVAILABLE,
                    "message": "目录已初始化，但后台服务尚未就绪，请稍后重试或手动启动服务",
                    "exit_code": c.EXIT_DAEMON_UNAVAILABLE,
                }
                return _error_envelope(
                    err.get("code", c.INTERNAL_ERROR),
                    err.get("message", "目录已初始化，但启动后台服务失败"),
                    exit_code=err.get("exit_code", c.EXIT_INTERNAL),
                    details={
                        "home": self.home,
                        "initialized": True,
                        "daemon_started": False,
                        "start_error": err,
                    },
                )

            auth_res = self.set_passphrase(passphrase, verify=True)
            auth_data = auth_res.get("data") if isinstance(auth_res.get("data"), dict) else {}
            auth_verified = bool(auth_res.get("ok") and auth_data.get("auth_verified"))
            if not auth_verified:
                err = auth_res.get("error") or {
                    "code": c.DAEMON_UNAVAILABLE,
                    "message": "目录已初始化，但未能校验自动解锁状态，请手动输入口令解锁",
                    "exit_code": c.EXIT_UNAUTHENTICATED,
                }
                return _error_envelope(
                    err.get("code", c.INTERNAL_ERROR),
                    err.get("message", "目录已初始化并启动服务，但自动解锁失败，请手动输入口令解锁"),
                    exit_code=err.get("exit_code", c.EXIT_UNAUTHENTICATED),
                    details={
                        "home": self.home,
                        "initialized": True,
                        "daemon_started": bool(self._connected),
                        "auth_verified": False,
                        "auth_error": err,
                    },
                )

            return _success_envelope(
                {
                    "home": self.home,
                    "initialized": True,
                    "daemon_started": bool(self._connected),
                    "auth": auth_data,
                    "connection": self._connection_snapshot(include_diag_log=True),
                }
            )
        finally:
            self._init_home_lock.release()

    @staticmethod
    def _passive_cli_auth_probe(adapter: Any, bin_path: Optional[str]) -> Dict[str, Any]:
        """Strictly passive CLI auth readiness check (B5): NEVER spawns logins or browsers.

        Never reads or returns raw secret values, emails, or organizations.
        """
        aid = str(getattr(adapter, "id", "") or "")
        if not bin_path:
            return {"state": "unknown", "method": "none", "detail": "未安装可执行文件"}

        env = c.build_external_env()

        # 1. Adapters with an explicit read-only status subcommand (`claude auth status`, `codex login status`)
        auth_fn = getattr(adapter, "auth_status", None)
        if callable(auth_fn):
            try:
                info = auth_fn(bin_path, timeout_s=3.0)
                if isinstance(info, dict):
                    logged_in = bool(info.get("logged_in"))
                    method = str(info.get("method") or "unknown")
                    env_keys = list(info.get("credential_env") or info.get("api_key_env") or [])
                    if logged_in:
                        return {
                            "state": "ready",
                            "method": method,
                            "detail": f"已通过 {method} 登录就绪",
                        }
                    if env_keys:
                        return {
                            "state": "ready",
                            "method": "env_key",
                            "detail": f"已检测到环境变量 {env_keys[0]}",
                        }
                    if method == "none":
                        return {
                            "state": "needs_login",
                            "method": "none",
                            "detail": "尚未登录，请先在终端完成登录或配置 API Key",
                        }
            except Exception:
                pass
            return {"state": "unknown", "method": "unknown", "detail": "无法被动确认登录状态"}

        # 2. Purely passive env-var & local config-dir existence checks for other CLIs (no login spawns)
        passive_rules: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
            "agy": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), ("~/.gemini/antigravity-cli", "~/.config/antigravity")),
            "droid": (("FACTORY_API_KEY", "ANTHROPIC_API_KEY"), ("~/.factory/auth.json", "~/.factory/config.json")),
            "kiro": (("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "KIRO_API_KEY"), ("~/.kiro", "~/.aws/sso/cache")),
            "vibe": (("MISTRAL_API_KEY",), ("~/.vibe/config.toml", "~/.vibe/.env")),
        }
        env_names, cfg_paths = passive_rules.get(aid, ((), ()))
        for ename in env_names:
            if env.get(ename):
                return {
                    "state": "ready",
                    "method": "env_key",
                    "detail": f"已检测到环境变量 {ename}",
                }
        for raw_p in cfg_paths:
            expanded = os.path.expanduser(raw_p)
            if os.path.exists(expanded):
                return {
                    "state": "ready",
                    "method": "local_config",
                    "detail": "已检测到本机配置目录",
                }
        if env_names or cfg_paths:
            return {
                "state": "needs_login",
                "method": "none",
                "detail": "未检测到本机登录配置或 API Key 环境变量",
            }
        return {"state": "unknown", "method": "unknown", "detail": "已安装"}

    def detect_local_clis(self) -> Dict[str, Any]:
        """Detect installed local vendor CLIs safely (no caller binary paths, <=3s version check, passive auth probe)."""
        from mesh.adapters.base import parse_version_output, run_readonly_cmd
        from mesh.adapters.evidence import held_milestone
        from mesh.adapters.registry import list_adapters

        display_order = {
            "claude": 0,
            "codex": 1,
            "agy": 2,
            "droid": 3,
            "kiro": 4,
            "vibe": 5,
            "devin": 6,
            "hermes": 7,
            "openclaw": 8,
        }
        vendor_adapters = [
            a for a in list_adapters(testing_mode=False) if getattr(a, "kind", "vendor") == "vendor"
        ]
        vendor_adapters.sort(key=lambda a: (display_order.get(a.id, 99), a.id))

        def _inspect_one(adapter: Any) -> Dict[str, Any]:
            implemented = bool(getattr(adapter, "implemented", True))
            supported_profiles = list(getattr(adapter, "supported_profiles", ()))
            default_safe = adapter.default_safe_profile()
            held = held_milestone(adapter.id, False)
            bin_path: Optional[str] = None
            try:
                bin_path = adapter.resolve_binary(None)
            except Exception:
                bin_path = None
            installed = bool(bin_path)
            version: Optional[str] = None
            auth_status: Dict[str, Any] = {"state": "unknown", "method": "none", "detail": "未安装"}
            if installed and implemented and bin_path:
                try:
                    res = run_readonly_cmd([bin_path, "--version"], timeout_s=3.0)
                    if res.returncode == 0:
                        version = parse_version_output(res.stdout) or parse_version_output(res.stderr)
                except Exception:
                    version = None
                auth_status = self._passive_cli_auth_probe(adapter, bin_path)

            raw_presets = getattr(adapter, "model_presets", None)
            presets: Optional[Dict[str, Any]] = None
            if isinstance(raw_presets, dict):
                chips = raw_presets.get("chips")
                default_allowed = raw_presets.get("default_allowed")
                note = raw_presets.get("note")
                presets = {
                    "chips": list(chips) if isinstance(chips, (list, tuple)) else [],
                    "default_allowed": list(default_allowed) if isinstance(default_allowed, (list, tuple)) else [],
                    "note": str(note) if note is not None else "",
                }

            can_lead_eligible = bool(
                installed
                and implemented
                and held is None
                and ("readonly" in supported_profiles)
            )
            ineligible_reason: Optional[str] = None
            if not implemented:
                ineligible_reason = "该工具集成尚未完成，暂不支持启动"
            elif held is not None:
                ineligible_reason = f"当前处于 {held} 暂停状态且不支持只读 Lead 档位，不可作为 Lead"
            elif "readonly" not in supported_profiles:
                ineligible_reason = "不支持只读 (readonly) 权限档位，不可作为 Lead"
            elif not installed:
                ineligible_reason = "未在本机 PATH 中检测到可执行文件"

            return {
                "id": adapter.id,
                "installed": installed,
                "binary_path": bin_path,
                "version": version,
                "implemented": implemented,
                "supported_profiles": supported_profiles,
                "default_safe_profile": default_safe,
                "model_presets": presets,
                "launch_held": held is not None,
                "launch_milestone": held,
                "can_lead_eligible": can_lead_eligible,
                "ineligible_reason": ineligible_reason,
                "auth_status": auth_status,
            }

        results: List[Dict[str, Any]] = []
        if vendor_adapters:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(vendor_adapters))) as pool:
                futures = [pool.submit(_inspect_one, ad) for ad in vendor_adapters]
                for fut in futures:
                    results.append(fut.result())

        eligible_leads = [item["id"] for item in results if item["can_lead_eligible"]]
        installed_count = sum(1 for item in results if item["installed"] and item["implemented"])
        self._log_desktop(
            "INFO",
            "CLI_SCAN",
            f"Detected {installed_count} local CLI(s)",
            details={"installed_count": installed_count, "eligible_leads": eligible_leads},
        )
        return _success_envelope(
            {
                "clis": results,
                "eligible_leads": eligible_leads,
                "installed_count": installed_count,
                "home_initialized": self._refresh_home_initialized(),
            }
        )

    # ---- S4: Native folder picker & read-only Git/test auto-detection ----

    def pick_folder(self, initial_dir: Optional[str] = None) -> Dict[str, Any]:
        """Open a native OS folder selection dialog via ``pywebview`` and inspect the chosen folder."""
        with self._lock:
            win = self._window
        if win is None:
            return _error_envelope(
                c.INTERNAL_ERROR,
                "未绑定桌面窗口，无法打开文件夹选择器",
                exit_code=c.EXIT_INTERNAL,
            )
        create_dialog = getattr(win, "create_file_dialog", None)
        if not callable(create_dialog):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "当前桌面窗口不支持原生文件夹选择器",
                exit_code=c.EXIT_INTERNAL,
            )
        dialog_type: Any = 20  # webview.FOLDER_DIALOG compat fallback
        try:
            import importlib

            wv = importlib.import_module("webview")
            file_dialog_enum = getattr(wv, "FileDialog", None)
            if file_dialog_enum is not None and hasattr(file_dialog_enum, "FOLDER"):
                dialog_type = file_dialog_enum.FOLDER
            elif hasattr(wv, "FOLDER_DIALOG"):
                dialog_type = getattr(wv, "FOLDER_DIALOG")
        except Exception:
            pass

        directory = ""
        if isinstance(initial_dir, str) and initial_dir.strip() and "\x00" not in initial_dir:
            try:
                if os.path.isdir(initial_dir.strip()):
                    directory = os.path.realpath(initial_dir.strip())
            except OSError:
                directory = ""

        try:
            selected = create_dialog(dialog_type, directory=directory)
        except TypeError:
            try:
                selected = create_dialog(dialog_type)
            except Exception as exc:
                return _error_envelope(
                    c.INTERNAL_ERROR,
                    f"打开文件夹选择器失败: {exc}",
                    exit_code=c.EXIT_INTERNAL,
                )
        except Exception as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"打开文件夹选择器失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
            )

        chosen: Optional[str] = None
        if isinstance(selected, str) and selected.strip():
            chosen = selected.strip()
        elif isinstance(selected, (list, tuple)) and selected:
            first = selected[0]
            if isinstance(first, str) and first.strip():
                chosen = first.strip()

        if not chosen:
            return _success_envelope({"cancelled": True, "path": None, "inspection": None})

        inspect_res = self.inspect_repo_folder(chosen)
        inspection = inspect_res.get("data") if inspect_res.get("ok") else None
        return _success_envelope({"cancelled": False, "path": chosen, "inspection": inspection})

    def _check_repo_path_conflict(self, candidate: str) -> Optional[str]:
        ws_root = os.path.join(self.home, "workspaces")
        if is_path_equal_or_parent(self.home, candidate) or is_path_equal_or_parent(candidate, self.home):
            return f"所选目录与 Agent Mesh 数据目录 ({self.home}) 存在包含重叠，请选择独立的代码仓库目录"
        if is_path_equal_or_parent(ws_root, candidate) or is_path_equal_or_parent(candidate, ws_root):
            return f"所选目录与 Agent Mesh 工作区目录 ({ws_root}) 存在包含重叠，请选择独立的代码仓库目录"
        try:
            check_install_path_secure(
                install_root=get_install_root(),
                home=self.home,
                workspaces_root=ws_root,
                repo_paths=[candidate],
            )
        except MeshError as exc:
            return f"目录路径安全校验未通过：{exc.message}"
        return None

    @staticmethod
    def _detect_test_command_candidates(root_dir: str) -> List[Dict[str, Any]]:
        """Read-only heuristic detection of common test commands (never executes repo code)."""
        candidates: List[Dict[str, Any]] = []
        if any(
            os.path.isfile(os.path.join(root_dir, fn))
            for fn in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
        ):
            candidates.append(
                {
                    "label": "pytest (Python)",
                    "argv": ["pytest"],
                    "reason": "检测到 Python 项目配置文件",
                }
            )
        pkg_json = os.path.join(root_dir, "package.json")
        if os.path.isfile(pkg_json):
            try:
                if os.path.getsize(pkg_json) <= 262144:
                    with open(pkg_json, "r", encoding="utf-8", errors="replace") as fp:
                        pkg = json.load(fp)
                    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
                    test_script = scripts.get("test") if isinstance(scripts, dict) else None
                    if (
                        isinstance(test_script, str)
                        and test_script.strip()
                        and "no test specified" not in test_script.lower()
                    ):
                        candidates.append(
                            {
                                "label": "npm test (Node.js)",
                                "argv": ["npm", "test"],
                                "reason": "检测到 package.json scripts.test",
                            }
                        )
            except (OSError, ValueError):
                pass
        if os.path.isfile(os.path.join(root_dir, "Cargo.toml")):
            candidates.append(
                {
                    "label": "cargo test (Rust)",
                    "argv": ["cargo", "test"],
                    "reason": "检测到 Cargo.toml",
                }
            )
        if os.path.isfile(os.path.join(root_dir, "go.mod")):
            candidates.append(
                {
                    "label": "go test ./... (Go)",
                    "argv": ["go", "test", "./..."],
                    "reason": "检测到 go.mod",
                }
            )
        return candidates

    def inspect_repo_folder(self, path: str) -> Dict[str, Any]:
        """Inspect a local folder read-only using allow-listed ``run_git`` subcommands (S4)."""
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "文件夹路径不能为空",
                exit_code=c.EXIT_USAGE,
                details={"param": "path"},
            )
        raw = path.strip()
        if not os.path.isabs(raw):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                f"仓库路径必须为绝对路径: {raw}",
                exit_code=c.EXIT_USAGE,
                details={"param": "path"},
            )
        try:
            real = os.path.realpath(raw)
        except OSError as exc:
            return _error_envelope(
                c.INVALID_ARGUMENT,
                f"无法解析目录路径: {exc}",
                exit_code=c.EXIT_USAGE,
                details={"param": "path"},
            )
        if not os.path.isdir(real):
            return _success_envelope(
                {
                    "path": real,
                    "exists": os.path.exists(real),
                    "is_dir": False,
                    "git_available": True,
                    "is_git_repo": False,
                    "is_toplevel": False,
                    "toplevel": None,
                    "current_branch": None,
                    "branches": [],
                    "recommended_base_branch": "",
                    "unborn_head": False,
                    "suggested_title": os.path.basename(real.rstrip("\\/")) or "",
                    "test_candidates": [],
                    "default_test_command": "none",
                    "path_conflict": None,
                    "git_error": "所选路径不存在或不是文件夹",
                }
            )

        path_conflict = self._check_repo_path_conflict(real)
        git_available = True
        is_git_repo = False
        is_toplevel = False
        toplevel: Optional[str] = None
        current_branch: Optional[str] = None
        branches: List[str] = []
        recommended_branch = ""
        unborn_head = False
        git_error: Optional[str] = None

        try:
            top_cp = run_git(["rev-parse", "--show-toplevel"], cwd=real, timeout_s=5.0)
            top_out = (top_cp.stdout or "").strip()
            if top_cp.returncode == 0 and top_out:
                toplevel = os.path.realpath(top_out)
                is_git_repo = True
                is_toplevel = same_path(toplevel, real)
                if path_conflict is None and not is_toplevel:
                    path_conflict = self._check_repo_path_conflict(toplevel)

                refs_cp = run_git(["show-ref", "--heads"], cwd=toplevel, timeout_s=5.0)
                if refs_cp.returncode == 0:
                    seen_branches: List[str] = []
                    for line in (refs_cp.stdout or "").splitlines():
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                            bname = parts[1][len("refs/heads/") :]
                            if bname and not bname.startswith("mesh/"):
                                seen_branches.append(bname)
                    branches = sorted(set(seen_branches))

                head_cp = run_git(["rev-parse", "--symbolic-full-name", "HEAD"], cwd=toplevel, timeout_s=5.0)
                head_ref = (head_cp.stdout or "").strip() if head_cp.returncode == 0 else ""
                if head_ref.startswith("refs/heads/"):
                    current_branch = head_ref[len("refs/heads/") :]
                if current_branch and current_branch in branches:
                    recommended_branch = current_branch
                else:
                    if current_branch and current_branch not in branches:
                        unborn_head = True
                    for pref in ("main", "master", "develop"):
                        if pref in branches:
                            recommended_branch = pref
                            break
                    if not recommended_branch and branches:
                        recommended_branch = branches[0]
            else:
                git_error = "该目录不是 Git 仓库（留空仓库路径仍可创建通用对话项目）"
        except FileNotFoundError:
            git_available = False
            git_error = "未检测到 Git，请安装 Git for Windows 或使用含内置 MinGit 的发行包"
        except subprocess.TimeoutExpired:
            git_error = "Git 仓库检测超时（5s），请稍后重试"
        except OSError as exc:
            git_available = False
            git_error = f"无法运行 Git: {exc}"

        inspect_root = toplevel if (is_git_repo and toplevel) else real
        test_candidates = self._detect_test_command_candidates(inspect_root)
        suggested_title = os.path.basename(inspect_root.rstrip("\\/")) or ""

        return _success_envelope(
            {
                "path": real,
                "exists": True,
                "is_dir": True,
                "git_available": git_available,
                "is_git_repo": is_git_repo,
                "is_toplevel": is_toplevel,
                "toplevel": toplevel,
                "current_branch": current_branch,
                "branches": branches,
                "recommended_base_branch": recommended_branch,
                "unborn_head": unborn_head,
                "suggested_title": suggested_title,
                "test_candidates": test_candidates,
                "default_test_command": "none",
                "path_conflict": path_conflict,
                "git_error": git_error,
            }
        )

    # ---- Whitelisted RPC call & daemon lifecycle ------------------------

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a whitelisted daemon RPC over ``MeshClient`` and return a sanitized envelope."""
        if not isinstance(method, str) or method not in ALLOWED_RPC_METHODS:
            return _error_envelope(
                "RPC_NOT_ALLOWED",
                f"RPC 方法 '{method}' 不在桌面端白名单中",
                exit_code=c.EXIT_USAGE,
                details={"method": method},
            )
        if params is not None and not isinstance(params, dict):
            return _error_envelope(
                c.INVALID_ARGUMENT,
                "RPC 参数必须为 JSON 对象",
                exit_code=c.EXIT_USAGE,
                details={"method": method},
            )
        args = dict(params or {})
        arg_err = _validate_bridge_call_args(method, args)
        if arg_err is not None:
            return arg_err
        auth_res = self._auth_for_method(method)
        if not auth_res["ok"]:
            return auth_res

        is_stop = method == "daemon.stop"
        prev_stop_state: Optional[Tuple[bool, Optional[int], Optional[str]]] = None
        stop_start_gen: int = 0
        stop_finalized = False
        with self._lock:
            if is_stop:
                prev_stop_state = (
                    self._user_stopped_daemon,
                    self._stopped_daemon_pid,
                    self._stopped_daemon_boot_id,
                )
                self._stopped_daemon_pid = (self._daemon_status or {}).get("pid")
                self._stopped_daemon_boot_id = (self._daemon_status or {}).get("boot_id")
                self._user_stopped_daemon = True
                self._stop_generation += 1
                self._daemon_stop_in_flight += 1
                stop_start_gen = self._stop_generation
            req_stop_gen = self._stop_generation

        timeout = _LONG_RPC_TIMEOUTS.get(method, self._rpc_timeout_s)
        started_mono = time.monotonic()
        try:
            res = self._raw_rpc(
                method,
                args,
                auth=auth_res["data"],
                idempotency_key=idempotency_key,
                timeout=timeout,
            )
            dur_ms = round((time.monotonic() - started_mono) * 1000.0, 2)
            if res["ok"]:
                if method in _DESKTOP_LOG_ALWAYS_METHODS:
                    self._log_desktop(
                        "INFO",
                        "RPC",
                        f"cmd={method} rc=0 dur_ms={dur_ms:.2f}",
                        details={"request_id": res.get("request_id")},
                    )
                if is_stop:
                    self._finalize_user_daemon_stop(decrement_in_flight=True)
                    stop_finalized = True
                else:
                    self._handle_rpc_success(method, res["data"], probe_stop_gen=req_stop_gen)
            else:
                err_obj_log = res.get("error") or {}
                self._log_desktop(
                    "WARNING",
                    "RPC_FAILED",
                    f"cmd={method} rc={err_obj_log.get('exit_code')} code={err_obj_log.get('code')} dur_ms={dur_ms:.2f}",
                    details={"request_id": res.get("request_id")},
                )
                if is_stop and prev_stop_state is not None:
                    err_obj = res.get("error") or {}
                    is_transport_err = (
                        err_obj.get("code") == c.DAEMON_UNAVAILABLE
                        or err_obj.get("exit_code") == c.EXIT_DAEMON_UNAVAILABLE
                    )
                    if is_transport_err:
                        # N6: meshd may have accepted the stop and closed the connection before
                        # replying (or was already down). Keep stop suppression and persist hold.
                        from mesh.platform import locks as plat_locks

                        lock_held = plat_locks.is_exclusive_lock_held(
                            os.path.join(self.home, "run", "meshd.lock")
                        )
                        with self._lock:
                            stopped_pid = self._stopped_daemon_pid
                            stopped_boot_id = self._stopped_daemon_boot_id
                            self._user_stopped_daemon = True
                            self._daemon_stopping = bool(lock_held)
                            self._last_start_error = None
                            self._stop_generation += 1
                            self._daemon_stop_in_flight = max(0, self._daemon_stop_in_flight - 1)
                            stop_finalized = True
                            write_autostart_hold(
                                self.home,
                                pid=stopped_pid,
                                boot_id=stopped_boot_id,
                                source="desktop_stop_transport_err",
                            )
                    else:
                        with self._lock:
                            self._daemon_stop_in_flight = max(0, self._daemon_stop_in_flight - 1)
                            stop_finalized = True
                            if self._daemon_stop_in_flight == 0 and self._stop_generation == stop_start_gen:
                                (
                                    self._user_stopped_daemon,
                                    self._stopped_daemon_pid,
                                    self._stopped_daemon_boot_id,
                                ) = prev_stop_state
                self._handle_rpc_error(res["error"])
            return res
        finally:
            if is_stop and not stop_finalized:
                with self._lock:
                    self._daemon_stop_in_flight = max(0, self._daemon_stop_in_flight - 1)
                    if (
                        prev_stop_state is not None
                        and self._daemon_stop_in_flight == 0
                        and self._stop_generation == stop_start_gen
                    ):
                        (
                            self._user_stopped_daemon,
                            self._stopped_daemon_pid,
                            self._stopped_daemon_boot_id,
                        ) = prev_stop_state

    def daemon_status(self) -> Dict[str, Any]:
        """Probe daemon health via unauthenticated ``daemon.status`` + ``version``."""
        ok = self._probe_once()
        self._refresh_home_initialized()
        with self._lock:
            snap = self._connection_snapshot_unlocked(include_diag_log=True)
            err = self._last_error
        if ok:
            return _success_envelope(snap)
        return _error_envelope(
            (err or {}).get("code", c.DAEMON_UNAVAILABLE),
            (err or {}).get("message", "meshd 未连接"),
            exit_code=(err or {}).get("exit_code", c.EXIT_DAEMON_UNAVAILABLE),
            details={"connection": snap},
        )

    def start_daemon(self) -> Dict[str, Any]:
        """Spawn ``mesh daemon start`` cleanly (single-flight, no secret env vars, hidden window on Windows)."""
        return self._start_daemon_impl(auto=False)

    def _start_daemon_impl(self, *, auto: bool = False) -> Dict[str, Any]:
        if not self._start_daemon_lock.acquire(blocking=False):
            self._refresh_home_initialized()
            return _success_envelope(
                {
                    "started": {"status": "starting"},
                    "in_progress": True,
                    "connection": self._connection_snapshot(include_diag_log=True),
                }
            )

        push_onboarding_payload: Optional[Dict[str, Any]] = None
        aborted_for_user_stop = False
        try:
            self._refresh_home_initialized()
            with self._lock:
                if auto and self._user_stopped_daemon:
                    aborted_for_user_stop = True
                    return _error_envelope(
                        c.DAEMON_UNAVAILABLE,
                        "用户已请求停止 meshd，跳过自动启动",
                        exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                        details={"user_stopped": True},
                    )
                self._daemon_starting = True
                if not auto:
                    self._user_stopped_daemon = False
                    self._stopped_daemon_pid = None
                    self._stopped_daemon_boot_id = None
                    self._last_probe_saw_stopped_daemon = False
                    self._last_start_error = None
                    self._auto_start_attempted = False
                    self._auto_start_timestamps.clear()
                clear_autostart_hold(self.home)
                starting_snap = self._connection_snapshot_unlocked()
            self._record_diag(
                "info",
                "DAEMON_STARTING",
                "自动启动 meshd 守护进程..." if auto else "手动启动 meshd 守护进程...",
                {"auto": auto},
            )
            self._enqueue_and_push(
                {
                    "type": "_desktop.connection",
                    "connected": False,
                    "starting": True,
                    "connection": starting_snap,
                }
            )

            if self._resolve_bin_mesh() is not None and sys.platform != "win32":
                argv = [self._resolve_bin_mesh(), "--home", self.home, "daemon", "start"]
            else:
                argv = mesh_cli_argv("--home", self.home, "daemon", "start")

            env = {k: v for k, v in _strip_daemon_secret_env().items() if k.upper() != "PYTHONPATH"}
            env = augment_env_with_bundled_git(env)
            install_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if not is_site_packages_dir(install_root):
                env["PYTHONPATH"] = install_root
            env["PYTHONUTF8"] = "1"

            run_kw: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "stdin": subprocess.DEVNULL,
                "timeout": 35.0,
                "env": env,
            }
            if sys.platform == "win32":
                env["_MESH_DAEMON_NO_WINDOW"] = "1"
                create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                run_kw["creationflags"] = create_no_window

            try:
                proc = self._subprocess_runner(argv, **run_kw)
            except OSError as exc:
                err_env = _error_envelope(
                    c.INTERNAL_ERROR,
                    f"启动 meshd 失败: {exc}",
                    exit_code=c.EXIT_INTERNAL,
                )
                err_with_meta = dict(err_env["error"])
                err_with_meta["auto"] = bool(auto)
                with self._lock:
                    self._daemon_stopping = False
                    self._last_start_error = err_with_meta
                    self._last_error = dict(err_env["error"])
                self._record_diag("error", c.INTERNAL_ERROR, err_env["error"]["message"], {"auto": auto})
                return err_env
            except subprocess.TimeoutExpired:
                err_env = _error_envelope(
                    c.WAIT_TIMEOUT,
                    "启动 meshd 超时 (35s)",
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                )
                err_with_meta = dict(err_env["error"])
                err_with_meta["auto"] = bool(auto)
                with self._lock:
                    self._daemon_stopping = False
                    self._last_start_error = err_with_meta
                    self._last_error = dict(err_env["error"])
                self._record_diag("error", c.WAIT_TIMEOUT, err_env["error"]["message"], {"auto": auto})
                return err_env

            parsed = self._parse_cli_json(getattr(proc, "stdout", ""))
            rc = getattr(proc, "returncode", 1)
            if rc != 0:
                err_dict = (
                    parsed["error"]
                    if parsed and not parsed.get("ok", True) and isinstance(parsed.get("error"), dict)
                    else None
                )
                err_code = err_dict.get("code", c.INTERNAL_ERROR) if err_dict else c.INTERNAL_ERROR
                err_details = err_dict.get("details") if err_dict and isinstance(err_dict.get("details"), dict) else {}
                db_path = os.path.join(self.home, "mesh.db")
                home_init_now = self._refresh_home_initialized()
                if not home_init_now and err_code in (c.INTERNAL_ERROR, c.NOT_FOUND):
                    hint = self._setup_command_hint()
                    err_env = _error_envelope(
                        HOME_NOT_INITIALIZED,
                        f"当前 MESH_HOME 尚未初始化，请先运行 `{hint}`（路径: {self.home}）",
                        exit_code=c.EXIT_NOT_FOUND,
                        details={"home": self.home, "db_path": db_path, "hint": hint},
                    )
                    with self._lock:
                        self._daemon_stopping = False
                        self._onboarding_pushed = True
                        self._last_error = dict(err_env["error"])
                        snap = self._connection_snapshot_unlocked()
                    self._record_diag("warn", HOME_NOT_INITIALIZED, err_env["error"]["message"], err_env["error"]["details"])
                    push_onboarding_payload = {
                        "type": "_desktop.onboarding_required",
                        "home": self.home,
                        "home_initialized": False,
                        "hint": hint,
                        "connection": snap,
                    }
                    return err_env

                if err_code == c.DAEMON_ALREADY_RUNNING:
                    if err_details.get("status") == "stopping":
                        err_env = _error_envelope(
                            c.DAEMON_ALREADY_RUNNING,
                            "守护进程正在完成收尾，稍后自动重连",
                            exit_code=parsed.get("exit_code", c.EXIT_INTERNAL) if parsed else c.EXIT_INTERNAL,
                            details=err_details,
                        )
                        with self._lock:
                            self._daemon_stopping = True
                            self._last_start_error = None
                            self._last_error = dict(err_env["error"])
                        self._record_diag("info", c.DAEMON_ALREADY_RUNNING, err_env["error"]["message"], err_details)
                        return err_env
                    # Already running normally -> clear starting flag (N4) before probing and connecting
                    with self._lock:
                        self._daemon_starting = False
                    if self._probe_once():
                        self._wake_event.set()
                        self._record_diag("info", "DAEMON_ALREADY_RUNNING", "meshd 已在运行，已自动连接")
                        return _success_envelope(
                            {
                                "started": {"status": "already_running"},
                                "connection": self._connection_snapshot(include_diag_log=True),
                            }
                        )

                if err_dict is not None:
                    err_env = _error_envelope(
                        err_code,
                        err_dict.get("message", "启动 meshd 失败"),
                        exit_code=parsed.get("exit_code", c.EXIT_INTERNAL),
                        details=err_dict.get("details"),
                    )
                else:
                    stderr = (getattr(proc, "stderr", "") or "").strip()
                    err_env = _error_envelope(
                        c.INTERNAL_ERROR,
                        stderr or f"mesh daemon start 退出码 {rc}",
                        exit_code=c.EXIT_INTERNAL,
                        details={"returncode": rc, "stderr": stderr[:1000]} if stderr else {"returncode": rc},
                    )
                err_with_meta = dict(err_env["error"])
                err_with_meta["auto"] = bool(auto)
                with self._lock:
                    self._daemon_stopping = False
                    self._last_start_error = err_with_meta
                    self._last_error = dict(err_env["error"])
                self._record_diag("error", err_env["error"]["code"], err_env["error"]["message"], err_env["error"].get("details"))
                return err_env

            with self._lock:
                self._daemon_starting = False
                self._daemon_stopping = False
                self._last_start_error = None
                self._auto_start_attempted = False
            self._probe_once()
            self._wake_event.set()
            self._record_diag("info", "DAEMON_STARTED", "meshd 守护进程已启动")
            data = parsed.get("data") if isinstance(parsed, dict) else None
            return _success_envelope(
                {
                    "started": data if isinstance(data, dict) else {"status": "running"},
                    "connection": self._connection_snapshot(include_diag_log=True),
                }
            )
        finally:
            with self._lock:
                self._daemon_starting = False
                connected_now = self._connected
                stopping_now = self._daemon_stopping
                start_err_now = dict(self._last_start_error) if self._last_start_error else None
                snap_now = self._connection_snapshot_unlocked()
            self._start_daemon_lock.release()
            if push_onboarding_payload is not None:
                push_onboarding_payload["connection"] = snap_now
                self._enqueue_and_push(push_onboarding_payload)
            elif not connected_now and not aborted_for_user_stop:
                self._enqueue_and_push(
                    {
                        "type": "_desktop.connection",
                        "connected": False,
                        "starting": False,
                        "stopping": stopping_now,
                        "error": start_err_now,
                        "connection": snap_now,
                    }
                )

    def _finalize_user_daemon_stop(self, *, decrement_in_flight: bool = False) -> None:
        with self._lock:
            self._user_stopped_daemon = True
            self._daemon_stopping = True
            self._last_start_error = None
            stopped_pid = self._stopped_daemon_pid
            stopped_boot_id = self._stopped_daemon_boot_id
            self._stop_generation += 1
            if decrement_in_flight:
                self._daemon_stop_in_flight = max(0, self._daemon_stop_in_flight - 1)
            write_autostart_hold(
                self.home,
                pid=stopped_pid,
                boot_id=stopped_boot_id,
                source="desktop",
            )
        self._record_diag("info", "DAEMON_STOPPING", "用户请求优雅停止 meshd 守护进程")
        self.stop_event_stream()
        self._mark_disconnected(
            {
                "code": c.DAEMON_UNAVAILABLE,
                "exit_code": c.EXIT_DAEMON_UNAVAILABLE,
                "message": "守护进程正在停止",
                "details": {"status": "stopping"},
            },
            clear_backoff=True,
            force_push=True,
        )

    def stop_daemon(self) -> Dict[str, Any]:
        """Stop ``meshd`` gracefully via the authenticated ``daemon.stop`` RPC."""
        return self.call("daemon.stop", {})

    def doctor_report(self) -> Dict[str, Any]:
        """Run IPC-safe environment diagnostics when meshd is active, or CLI locally when offline."""
        lock_path = os.path.join(self.home, "run", "meshd.lock")
        online = self._probe_once()
        if online:
            with self._lock:
                pw = self._passphrase
            if pw is not None:
                rpc_res = self._raw_rpc(
                    "doctor",
                    {},
                    auth={"type": "user", "passphrase": pw},
                    timeout=self._rpc_timeout_s,
                )
                if rpc_res["ok"]:
                    return rpc_res
                err_code = (rpc_res.get("error") or {}).get("code")
                if err_code not in (c.INVALID_ARGUMENT, c.NOT_IMPLEMENTED_IN_M1):
                    return rpc_res
            return _success_envelope(self._ipc_doctor_report())

        from mesh.platform import locks

        if locks.is_exclusive_lock_held(lock_path):
            return _success_envelope(self._offline_locked_doctor_report())

        import argparse
        from mesh.cli.commands.doctor import handle_doctor

        ns = argparse.Namespace(home=self.home, fix_perms=False, check_invariants=False)
        try:
            report = handle_doctor(ns)
            return _success_envelope(report)
        except MeshError as exc:
            return _error_envelope(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
                details=exc.details,
            )
        except OSError as exc:
            return _error_envelope(
                c.INTERNAL_ERROR,
                f"环境诊断失败: {exc}",
                exit_code=c.EXIT_INTERNAL,
                details={"error": str(exc)},
            )

    def _safe_env_checks(self, *, testing_mode: bool) -> List[Dict[str, Any]]:
        """Run read-only OS/tool checks without opening mesh.db or $MESH_HOME/run files."""
        from mesh.adapters.registry import list_adapters
        from mesh.cli.commands.doctor_env import MIN_FALLBACK_GIT, MIN_RECOMMENDED_GIT, _check

        checks: List[Dict[str, Any]] = []
        plat = platform.system()
        v = sys.version_info
        if plat == "Windows":
            plat_ok = v >= (3, 12)
            plat_hint = "Agent Mesh on Windows requires Python 3.12 or newer"
            py_ok = v >= (3, 12)
            py_hint = "Python 3.12 or newer is required on Windows"
        else:
            plat_ok = plat == "Linux"
            plat_hint = "Agent Mesh supports Linux and Windows 10/11 (Python 3.12+ on Windows)"
            py_ok = v >= (3, 11)
            py_hint = "Python 3.11 or newer is required"
        checks.append(_check("platform", plat_ok, f"{plat} {platform.release()}", plat_hint))
        checks.append(_check("python", py_ok, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})", py_hint))

        git, git_source = resolve_git_binary()
        gver = ""
        git_ok = False
        if git:
            try:
                git_kw: Dict[str, Any] = {
                    "capture_output": True,
                    "text": True,
                    "timeout": 5,
                    "stdin": subprocess.DEVNULL,
                    "encoding": "utf-8",
                    "errors": "replace",
                }
                if sys.platform == "win32":
                    git_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                gp = subprocess.run([git, "--version"], **git_kw)
                gver = gp.stdout.strip()
                git_ok = gp.returncode == 0 and gver.startswith("git version")
                if not git_ok:
                    gver = f"{git} does not work (git --version exit {gp.returncode})"
            except (OSError, subprocess.SubprocessError) as exc:
                gver = f"{git} does not run ({exc.__class__.__name__})"
        git_detail = (
            f"{gver} (bundled MinGit)" if (git_ok and git_source == "bundled_mingit") else (gver or "not found on PATH")
        )
        checks.append(
            _check(
                "git",
                git_ok,
                git_detail,
                "install git; repo projects and task worktrees need it",
                level="warning",
            )
        )
        if git_ok:
            m = re.match(r"git version (\d+)\.(\d+)", gver)
            if m:
                gv = (int(m.group(1)), int(m.group(2)))
                if gv >= MIN_FALLBACK_GIT:
                    checks.append(
                        _check(
                            "git_version",
                            gv >= MIN_RECOMMENDED_GIT,
                            f"{gv[0]}.{gv[1]}",
                            "git 2.36 or newer is recommended (`git worktree list -z`)",
                            level="warning",
                        )
                    )
                else:
                    checks.append(
                        _check(
                            "git_version",
                            False,
                            f"{gv[0]}.{gv[1]} (older than 2.31)",
                            "upgrade to git 2.36 or newer",
                            level="warning",
                        )
                    )

        found = []
        for adapter in list_adapters(testing_mode=testing_mode):
            if getattr(adapter, "kind", "vendor") != "vendor":
                continue
            path = adapter.resolve_binary(None)
            if path:
                found.append(f"{adapter.id} ({path})")
        checks.append(
            _check(
                "agent_clis",
                True,
                ("found: " + ", ".join(found))
                if found
                else "no supported agent CLI on PATH (inbox agents still work; see docs/examples/)",
            )
        )
        return checks

    def _ipc_doctor_report(self) -> Dict[str, Any]:
        """Build diagnostics purely over MeshClient IPC + OS/tool probes (no mesh.db read)."""
        from mesh.cli.commands.doctor_env import _check
        from mesh.core.paths import get_install_root

        with self._lock:
            status = dict(self._daemon_status or {})
            pw = self._passphrase
            verified = self._auth_verified

        testing_mode = bool(status.get("testing_mode", False))
        checks = self._safe_env_checks(testing_mode=testing_mode)
        checks.append(_check("initialized", True, f"{os.path.join(self.home, 'mesh.db')} (verified by meshd)"))
        pid = status.get("pid")
        checks.append(_check("daemon", True, f"running (pid={pid})" if pid else "running"))

        sched = status.get("scheduler") if isinstance(status.get("scheduler"), dict) else {}
        sweep_errors = int(sched.get("sweep_errors") or 0)
        last_sweep_err = sched.get("last_sweep_error")
        if sweep_errors > 0:
            if isinstance(last_sweep_err, dict):
                item_part = f" on {last_sweep_err.get('item')}" if last_sweep_err.get("item") else ""
                last_desc = (
                    f"{last_sweep_err.get('step')}{item_part}: "
                    f"{last_sweep_err.get('error')} at {last_sweep_err.get('at')}"
                )
            else:
                last_desc = str(last_sweep_err)
            checks.append(
                _check(
                    "scheduler_errors",
                    False,
                    f"{sweep_errors} since meshd started; last: {last_desc}",
                    "see $MESH_HOME/logs/meshd.log",
                    level="warning",
                )
            )
        else:
            checks.append(_check("scheduler_errors", True, "none since meshd started"))

        mode = None
        if pw is not None and verified:
            cfg_res = self._raw_rpc(
                "config.get",
                {"key": "adapter.evidence_mode"},
                auth={"type": "user", "passphrase": pw},
                timeout=self._probe_timeout_s,
            )
            if cfg_res.get("ok") and isinstance(cfg_res.get("data"), dict):
                mode = cfg_res["data"].get("value")
        if mode in ("warn", "strict"):
            checks.append(
                _check(
                    "evidence_mode",
                    True,
                    f"{mode}: "
                    + (
                        "vendor adapters launch without matching smoke evidence (warning only)"
                        if mode == "warn"
                        else "vendor adapters launch only with matching committed smoke evidence"
                    ),
                )
            )
        else:
            checks.append(
                _check(
                    "evidence_mode",
                    True,
                    "unlock passphrase to query adapter.evidence_mode via IPC",
                )
            )

        return {
            "status": "healthy",
            "source": "ipc",
            "install_root": get_install_root(),
            "home": self.home,
            "checks": checks,
            "problems": [f"{x['name']}: {x['detail']} -> {x.get('hint', '')}" for x in checks if not x["ok"]],
        }

    def _offline_locked_doctor_report(self) -> Dict[str, Any]:
        """Diagnostics while meshd.lock is held offline (avoids opening mesh.db)."""
        from mesh.cli.commands.doctor_env import _check
        from mesh.core.paths import get_install_root

        testing_mode = os.environ.get("MESH_TESTING") == "1"
        checks = self._safe_env_checks(testing_mode=testing_mode)
        checks.append(
            _check(
                "daemon_lock",
                False,
                "meshd.lock is held while daemon endpoint is offline; skipped direct mesh.db read",
                "wait for meshd startup/shutdown to finish and retry",
                level="warning",
            )
        )
        return {
            "status": "daemon_lock_active",
            "source": "offline_lock_safe",
            "install_root": get_install_root(),
            "home": self.home,
            "checks": checks,
            "problems": [f"{x['name']}: {x['detail']} -> {x.get('hint', '')}" for x in checks if not x["ok"]],
        }

    # ---- Event stream & queue -------------------------------------------

    def poll_events(self, limit: int = 200) -> Dict[str, Any]:
        """Drain up to *limit* queued daemon/desktop events for JS polling."""
        limit_val, err = _parse_bridge_int(limit, name="limit")
        if err is not None:
            return err
        assert limit_val is not None
        max_n = max(1, min(limit_val, 1000))
        items: List[Dict[str, Any]] = []
        with self._lock:
            while self._pending_events and len(items) < max_n:
                items.append(self._pending_events.popleft())
            has_more = bool(self._pending_events)
            last_seq = self._last_seq if self._last_seq is not None else 0
        return _success_envelope(
            {"items": items, "has_more": has_more, "last_seq": last_seq}
        )

    def start_event_stream(self, since: Optional[int] = None) -> Dict[str, Any]:
        """Ensure background ``event.tail`` subscription is running when authenticated."""
        since_i: Optional[int] = None
        if since is not None:
            since_i, err = _parse_bridge_int(since, name="since")
            if err is not None:
                return err
        with self._lock:
            if since_i is not None:
                if self._last_seq is None or since_i > self._last_seq:
                    self._last_seq = since_i
            if self._passphrase is None:
                return _error_envelope(
                    c.TOKEN_MISSING,
                    "需要先解锁口令才能订阅实时事件流",
                    exit_code=c.EXIT_UNAUTHENTICATED,
                )
            if self._stream_thread is not None and self._stream_thread.is_alive():
                if self._stream_stop_event is not None and self._stream_stop_event.is_set():
                    return _success_envelope({"streaming": False, "stopping": True, "since": self._last_seq})
                return _success_envelope({"streaming": True, "since": self._last_seq})
            stop_ev = threading.Event()
            self._stream_stop_event = stop_ev
            thread = threading.Thread(
                target=self._event_stream_loop,
                args=(stop_ev,),
                name="mesh-desktop-event-tail",
                daemon=True,
            )
            self._stream_thread = thread
            thread.start()
            return _success_envelope({"streaming": True, "since": self._last_seq})

    def stop_event_stream(self) -> Dict[str, Any]:
        """Stop the active ``event.tail`` stream thread and force-close its stream on timeout."""
        with self._lock:
            stop_ev = self._stream_stop_event
            sock = self._stream_sock
            thread = self._stream_thread
        if stop_ev is not None:
            stop_ev.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                with self._lock:
                    if self._stream_stop_event is stop_ev:
                        sock = self._stream_sock
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                thread.join(timeout=0.5)
        with self._lock:
            if self._stream_thread is thread and (thread is None or not thread.is_alive()):
                self._stream_stop_event = None
                self._stream_sock = None
                self._stream_thread = None
        return _success_envelope({"streaming": False})

    def reconnect_now(self) -> Dict[str, Any]:
        """Trigger an immediate daemon probe and return the updated connection state."""
        self._probe_once()
        self._wake_event.set()
        return self.connection_state()

    # ---- Internal transport & background threads ------------------------

    def _build_client(self, timeout: float) -> Any:
        return self._client_factory(self.home, timeout)

    def _resolve_bin_mesh(self) -> Optional[str]:
        candidate = os.path.join(get_mesh_bin_dir(), "mesh")
        return candidate if os.path.isfile(candidate) else None

    def _resolve_endpoint(self) -> str:
        try:
            client = self._build_client(self._probe_timeout_s)
            sock_path = getattr(client, "socket_path", None)
            if sock_path:
                return str(sock_path)
        except Exception:
            pass
        try:
            return plat_ipc.daemon_endpoint_path(self.home)
        except Exception:
            return os.path.join(self.home, "run", "mesh.sock")

    def _parse_cli_json(self, raw_stdout: str) -> Optional[Dict[str, Any]]:
        text = (raw_stdout or "").strip()
        if not text:
            return None
        try:
            doc = json.loads(text)
            return doc if isinstance(doc, dict) else None
        except json.JSONDecodeError:
            return None

    def _auth_for_method(self, method: str) -> Dict[str, Any]:
        if method in UNAUTH_RPC_METHODS:
            return _success_envelope({"type": "none"})
        with self._lock:
            pw = self._passphrase
        if pw is None:
            return _error_envelope(
                c.TOKEN_MISSING,
                "需要输入用户口令 (Passphrase) 才能执行操作",
                exit_code=c.EXIT_UNAUTHENTICATED,
                details={"reason": "passphrase_not_set"},
            )
        return _success_envelope({"type": "user", "passphrase": pw})

    def _raw_rpc(
        self,
        method: str,
        args: Dict[str, Any],
        *,
        auth: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        req_id = _new_request_id()
        payload: Dict[str, Any] = {
            "v": 1,
            "request_id": req_id,
            "cmd": method,
            "auth": auth,
            "args": args,
            "idempotency_key": idempotency_key,
            "dry_run": False,
        }
        client = self._build_client(timeout if timeout is not None else self._rpc_timeout_s)
        try:
            resp = client.send_request(payload)
        except MeshError as exc:
            return _error_envelope(
                exc.code,
                exc.message,
                exit_code=exc.exit_code,
                details=exc.details,
                request_id=req_id,
            )
        except OSError as exc:
            return _error_envelope(
                c.DAEMON_UNAVAILABLE,
                f"无法连接守护进程: {exc}",
                exit_code=c.EXIT_DAEMON_UNAVAILABLE,
                details={"error": str(exc)},
                request_id=req_id,
            )

        if not isinstance(resp, dict):
            return _error_envelope(
                c.INTERNAL_ERROR,
                "守护进程返回了无效响应",
                exit_code=c.EXIT_INTERNAL,
                request_id=req_id,
            )

        if resp.get("ok"):
            return _success_envelope(
                resp.get("data"),
                warnings=resp.get("warnings"),
                request_id=resp.get("request_id", req_id),
            )

        err = resp.get("error") or {}
        return _error_envelope(
            err.get("code", c.INTERNAL_ERROR),
            err.get("message", "请求失败"),
            exit_code=resp.get("exit_code", c.EXIT_INTERNAL),
            details=err.get("details"),
            request_id=resp.get("request_id", req_id),
        )

    def _auth_snapshot_unlocked(self) -> Dict[str, Any]:
        cred_supported = self._cred_supported()
        cred_present = self._cred_present() if cred_supported else False
        return {
            "auth_configured": self._passphrase is not None,
            "auth_verified": self._auth_verified,
            "principal": dict(self._principal) if self._principal else None,
            "last_auth_error": dict(self._last_auth_error) if self._last_auth_error else None,
            "stored_credential_supported": cred_supported,
            "stored_credential_present": cred_present,
        }

    def _is_home_initialized(self) -> bool:
        db_path = os.path.join(self.home, "mesh.db")
        if not os.path.exists(db_path):
            return False
        # Never touch mesh.db directly while a daemon is connected or holding meshd.lock.
        if self._connected:
            return True
        lock_path = os.path.join(self.home, "run", "meshd.lock")
        try:
            from mesh.platform import locks as plat_locks

            if plat_locks.is_exclusive_lock_held(lock_path):
                return True
        except Exception:
            return True
        # Fail closed on non-SQLite or populated files, but recover if a crash mid-init
        # left a valid Mesh schema with zero user principals.
        try:
            if os.path.getsize(db_path) < 100:
                return True
            from mesh.core.db import connect as connect_db

            conn = connect_db(db_path, read_only=True, timeout_ms=500)
            try:
                has_tbl = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'principals' LIMIT 1;"
                ).fetchone()
                if has_tbl is None:
                    return True
                has_user = conn.execute(
                    "SELECT 1 FROM principals WHERE kind = 'user' LIMIT 1;"
                ).fetchone()
                return has_user is not None
            finally:
                conn.close()
        except Exception:
            return True

    def _refresh_home_initialized(self) -> bool:
        val = self._is_home_initialized()
        with self._lock:
            self._home_initialized_cache = val
        return val

    def _daemon_log_path(self) -> str:
        return os.path.join(self.home, "logs", "meshd.log")

    def _desktop_log_path(self) -> str:
        return os.path.join(self.home, "logs", DESKTOP_LOG_FILENAME)

    def _log_desktop(
        self,
        level: str,
        event: str,
        message: str = "",
        *,
        details: Optional[Dict[str, Any]] = None,
        extra_secrets: Sequence[Optional[str]] = (),
    ) -> None:
        with self._lock:
            pw = self._passphrase
        secrets_list: List[Optional[str]] = list(extra_secrets)
        if pw:
            secrets_list.append(pw)
        append_desktop_log(
            self.home,
            level,
            event,
            message,
            details=details,
            secrets_to_scrub=secrets_list,
        )

    def _setup_command_hint(self) -> str:
        return compute_setup_hint(self.home)

    def _record_diag(
        self,
        level: str,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "code": code,
            "message": redact(message) if isinstance(message, str) else str(message),
            "details": _sanitize_value(details or {}),
        }
        with self._lock:
            self._daemon_diag_log.append(entry)
        self._log_desktop(level, code, entry["message"], details=entry["details"])

    def _connection_snapshot_unlocked(self, *, include_diag_log: bool = False) -> Dict[str, Any]:
        snap: Dict[str, Any] = {
            "connected": self._connected,
            "home": self.home,
            "home_initialized": self._home_initialized_cache,
            "setup_hint": self._setup_command_hint(),
            "endpoint": self._resolve_endpoint(),
            "daemon_status": dict(self._daemon_status) if self._daemon_status else None,
            "daemon_version": self._daemon_version,
            "daemon_starting": self._daemon_starting,
            "daemon_stopping": self._daemon_stopping,
            "user_stopped": self._user_stopped_daemon,
            "auth_configured": self._passphrase is not None,
            "auth_verified": self._auth_verified,
            "reconnect_attempt": self._reconnect_attempt,
            "next_retry_s": self._next_retry_s,
            "last_seq": self._last_seq if self._last_seq is not None else 0,
            "last_error": dict(self._last_error) if self._last_error else None,
            "last_start_error": dict(self._last_start_error) if self._last_start_error else None,
            "daemon_log_path": self._daemon_log_path(),
            "desktop_log_path": self._desktop_log_path(),
            "streaming": bool(self._stream_thread and self._stream_thread.is_alive()),
        }
        if include_diag_log:
            snap["daemon_diag_log"] = [dict(entry) for entry in self._daemon_diag_log]
        return snap

    def _connection_snapshot(self, *, include_diag_log: bool = False) -> Dict[str, Any]:
        with self._lock:
            return self._connection_snapshot_unlocked(include_diag_log=include_diag_log)

    def _is_same_stopped_daemon_unlocked(
        self,
        status_data: Dict[str, Any],
        *,
        probe_stop_gen: Optional[int] = None,
    ) -> bool:
        """Return True if *status_data* belongs to the daemon the user just asked to stop."""
        if not self._user_stopped_daemon:
            return False
        if self._daemon_stop_in_flight > 0:
            return True
        if probe_stop_gen is not None and probe_stop_gen != self._stop_generation:
            return True
        pid = status_data.get("pid") if isinstance(status_data, dict) else None
        boot_id = status_data.get("boot_id") if isinstance(status_data, dict) else None
        if self._stopped_daemon_pid is not None:
            if pid != self._stopped_daemon_pid:
                return False
            if (
                self._stopped_daemon_boot_id is not None
                and boot_id is not None
                and boot_id != self._stopped_daemon_boot_id
            ):
                return False
            return True
        if self._stopped_daemon_boot_id is not None and boot_id is not None:
            return boot_id == self._stopped_daemon_boot_id
        return self._daemon_stopping

    def _handle_rpc_success(
        self,
        method: str,
        data: Any,
        *,
        probe_stop_gen: Optional[int] = None,
    ) -> None:
        if method == "daemon.status" and isinstance(data, dict):
            with self._lock:
                if self._is_same_stopped_daemon_unlocked(data, probe_stop_gen=probe_stop_gen):
                    return
                self._daemon_status = dict(data)
                self._connected = True
                self._last_error = None
                self._reconnect_attempt = 0
                self._next_retry_s = 0.0
                self._last_connected_at = time.time()
        elif method == "version" and isinstance(data, dict):
            with self._lock:
                self._daemon_version = data.get("version")

    def _handle_rpc_error(self, err: Dict[str, Any]) -> None:
        code = err.get("code")
        exit_code = err.get("exit_code")
        if code == c.DAEMON_UNAVAILABLE or exit_code == c.EXIT_DAEMON_UNAVAILABLE:
            self._mark_disconnected(err)
        elif exit_code == c.EXIT_UNAUTHENTICATED or code in (
            c.PASSPHRASE_INVALID,
            c.PASSPHRASE_REQUIRED,
            c.TOKEN_INVALID,
            c.LOCKED_OUT,
        ):
            with self._lock:
                self._auth_verified = False
                self._principal = None
                self._last_auth_error = err
                if code in (c.PASSPHRASE_INVALID, c.PASSPHRASE_REQUIRED, c.TOKEN_INVALID):
                    self._passphrase = None

    def _mark_disconnected(
        self,
        err: Dict[str, Any],
        *,
        clear_backoff: bool = False,
        force_push: bool = False,
    ) -> None:
        changed = False
        with self._lock:
            if self._connected:
                changed = True
            self._connected = False
            self._last_error = dict(err)
            if clear_backoff:
                self._reconnect_attempt = 0
                self._next_retry_s = 0.0
            snap = self._connection_snapshot_unlocked() if (changed or force_push) else None
        self.stop_event_stream()
        if changed:
            self._log_desktop(
                "WARNING",
                "DAEMON_DISCONNECTED",
                err.get("message") or "Disconnected from meshd",
                details={"code": err.get("code"), "exit_code": err.get("exit_code")},
            )
        if changed or force_push:
            self._notify_tray_status()
            self._enqueue_and_push(
                {
                    "type": "_desktop.connection",
                    "connected": False,
                    "stopping": bool(snap and snap.get("daemon_stopping")),
                    "user_stopped": bool(snap and snap.get("user_stopped")),
                    "error": dict(err),
                    "connection": snap,
                }
            )
            self._wake_event.set()

    def _mark_connected(
        self,
        status_data: Dict[str, Any],
        version: Optional[str],
        *,
        probe_stop_gen: Optional[int] = None,
    ) -> bool:
        was_disconnected = False
        with self._lock:
            if self._is_same_stopped_daemon_unlocked(status_data, probe_stop_gen=probe_stop_gen):
                self._last_probe_saw_stopped_daemon = True
                return False
            if not self._connected:
                was_disconnected = True
            was_user_stopped = self._user_stopped_daemon
            self._connected = True
            self._daemon_status = dict(status_data)
            if version is not None:
                self._daemon_version = version
            self._last_error = None
            self._last_start_error = None
            self._daemon_starting = False
            self._daemon_stopping = False
            self._auto_start_attempted = False
            self._onboarding_pushed = False
            self._user_stopped_daemon = False
            self._stopped_daemon_pid = None
            self._stopped_daemon_boot_id = None
            self._last_probe_saw_stopped_daemon = False
            self._reconnect_attempt = 0
            self._next_retry_s = 0.0
            self._last_connected_at = time.time()
            if was_user_stopped:
                clear_autostart_hold(self.home)
        if was_disconnected:
            self._log_desktop(
                "INFO",
                "DAEMON_CONNECTED",
                "Connected to meshd",
                details={
                    "pid": status_data.get("pid"),
                    "boot_id": status_data.get("boot_id"),
                    "version": version,
                },
            )
        return was_disconnected

    def _probe_once(self) -> bool:
        """Check daemon reachability and resume auth/event streaming if needed."""
        with self._lock:
            probe_stop_gen = self._stop_generation
        status_res = self._raw_rpc(
            "daemon.status",
            {},
            auth={"type": "none"},
            timeout=self._probe_timeout_s,
        )
        if not status_res["ok"]:
            with self._lock:
                self._last_probe_saw_stopped_daemon = False
            self._mark_disconnected(status_res["error"])
            return False

        status_data = status_res.get("data") or {}
        with self._lock:
            if self._is_same_stopped_daemon_unlocked(status_data, probe_stop_gen=probe_stop_gen):
                self._last_probe_saw_stopped_daemon = True
                return False
            need_version = self._daemon_version is None or not self._connected

        ver: Optional[str] = None
        if need_version:
            ver_res = self._raw_rpc(
                "version",
                {},
                auth={"type": "none"},
                timeout=self._probe_timeout_s,
            )
            if ver_res["ok"] and isinstance(ver_res.get("data"), dict):
                ver = ver_res["data"].get("version")

        reconnected = self._mark_connected(status_data, ver, probe_stop_gen=probe_stop_gen)
        with self._lock:
            if not self._connected or self._user_stopped_daemon:
                return False
            pw = self._passphrase
            verified = self._auth_verified
            last_seq = self._last_seq

        if pw is None:
            self._try_auto_unlock_from_store()
            with self._lock:
                pw = self._passphrase
                verified = self._auth_verified
                last_seq = self._last_seq

        if pw is not None and (reconnected or not verified):
            who = self._raw_rpc(
                "whoami",
                {},
                auth={"type": "user", "passphrase": pw},
                timeout=self._probe_timeout_s,
            )
            if who["ok"]:
                with self._lock:
                    if not self._connected or self._user_stopped_daemon:
                        return False
                    self._auth_verified = True
                    self._principal = dict(who.get("data") or {})
                    self._last_auth_error = None
                if reconnected and last_seq is not None and last_seq > 0:
                    self._catch_up_events(pw, last_seq)
                self.start_event_stream()
            else:
                self._handle_rpc_error(who["error"])
        elif pw is not None and verified:
            # Ensure stream stays running after a brief stream drop.
            self.start_event_stream()

        if reconnected:
            self._refresh_home_initialized()
            with self._lock:
                if not self._connected or self._user_stopped_daemon:
                    return False
                reconn_snap = self._connection_snapshot_unlocked()
            self._notify_tray_status()
            self._enqueue_and_push(
                {
                    "type": "_desktop.connection",
                    "connected": True,
                    "resync": True,
                    "connection": reconn_snap,
                }
            )
        return True

    def _catch_up_events(self, passphrase: str, since_seq: int) -> None:
        cursor = since_seq
        while not self._stop_event.is_set():
            res = self._raw_rpc(
                "event.list",
                {"since": cursor, "limit": 1000},
                auth={"type": "user", "passphrase": passphrase},
                timeout=self._rpc_timeout_s,
            )
            if not res["ok"]:
                return
            items = (res.get("data") or {}).get("items") or []
            if not items:
                return
            max_batch_seq = cursor
            for ev in items:
                if isinstance(ev, dict):
                    self._enqueue_and_push(ev)
                    seq = ev.get("seq")
                    if isinstance(seq, int) and seq > max_batch_seq:
                        max_batch_seq = seq
            if len(items) < 1000 or max_batch_seq <= cursor:
                return
            cursor = max_batch_seq

    def _supervisor_loop(self) -> None:
        from mesh.platform import locks as plat_locks

        while not self._stop_event.is_set():
            self._wake_event.clear()
            is_first_round = self._first_probe_round
            self._first_probe_round = False
            ok = self._probe_once()
            if self._stop_event.is_set():
                break
            if ok:
                wait_s = self._probe_interval_s
            else:
                home_init = self._refresh_home_initialized()
                if not home_init:
                    push_onboarding = False
                    hint = self._setup_command_hint()
                    with self._lock:
                        self._daemon_stopping = False
                        if not self._onboarding_pushed:
                            self._onboarding_pushed = True
                            push_onboarding = True
                        snap = self._connection_snapshot_unlocked()
                    if push_onboarding:
                        self._enqueue_and_push(
                            {
                                "type": "_desktop.onboarding_required",
                                "home": self.home,
                                "home_initialized": False,
                                "hint": hint,
                                "connection": snap,
                            }
                        )
                else:
                    lock_path = os.path.join(self.home, "run", "meshd.lock")
                    lock_held = plat_locks.is_exclusive_lock_held(lock_path)
                    hold = read_autostart_hold(self.home)

                    # N1: honor home-level autostart.hold (CLI stop or another desktop window)
                    # on every probe round after the initial app-launch round.
                    external_stop_snap: Optional[Dict[str, Any]] = None
                    if hold is not None and not is_first_round:
                        with self._lock:
                            was_user_stopped = self._user_stopped_daemon
                            self._user_stopped_daemon = True
                            if self._stopped_daemon_pid is None:
                                hold_pid = hold.get("pid")
                                self._stopped_daemon_pid = (
                                    hold_pid
                                    if isinstance(hold_pid, int)
                                    else (self._daemon_status or {}).get("pid")
                                )
                            if self._stopped_daemon_boot_id is None:
                                hold_boot = hold.get("boot_id")
                                self._stopped_daemon_boot_id = (
                                    hold_boot
                                    if isinstance(hold_boot, str) and hold_boot
                                    else (self._daemon_status or {}).get("boot_id")
                                )
                            if not was_user_stopped:
                                self._daemon_stopping = bool(lock_held)
                                self._last_start_error = None
                                external_stop_snap = self._connection_snapshot_unlocked()
                        if external_stop_snap is not None:
                            self._record_diag(
                                "info",
                                "DAEMON_STOPPED_EXTERNAL",
                                "检测到外部请求停止 meshd，暂停自动重启",
                            )
                            self._enqueue_and_push(
                                {
                                    "type": "_desktop.connection",
                                    "connected": False,
                                    "starting": False,
                                    "stopping": bool(lock_held),
                                    "user_stopped": True,
                                    "connection": external_stop_snap,
                                }
                            )

                    # F3: clear _daemon_stopping once a user-stopped daemon has exited and freed meshd.lock
                    check_user_stop_clear = False
                    with self._lock:
                        if (
                            self._user_stopped_daemon
                            and self._daemon_stopping
                            and not self._last_probe_saw_stopped_daemon
                        ):
                            check_user_stop_clear = True
                    if check_user_stop_clear and not lock_held:
                        cleared_snap: Optional[Dict[str, Any]] = None
                        with self._lock:
                            if self._user_stopped_daemon and self._daemon_stopping:
                                self._daemon_stopping = False
                                cleared_snap = self._connection_snapshot_unlocked()
                        if cleared_snap is not None:
                            self._record_diag("info", "DAEMON_STOPPED", "meshd 已按请求完成停止")
                            self._enqueue_and_push(
                                {
                                    "type": "_desktop.connection",
                                    "connected": False,
                                    "starting": False,
                                    "stopping": False,
                                    "user_stopped": True,
                                    "connection": cleared_snap,
                                }
                            )

                    onboarding_cleared = False
                    should_auto_start = False
                    rate_limited_event: Optional[Dict[str, Any]] = None
                    busy_stopping_event: Optional[Dict[str, Any]] = None
                    now_mono = time.monotonic()
                    with self._lock:
                        if self._onboarding_pushed:
                            self._onboarding_pushed = False
                            onboarding_cleared = True
                        last_err = self._last_error or {}
                        last_err_code = last_err.get("code")
                        is_unavailable = last_err_code == c.DAEMON_UNAVAILABLE
                        if (
                            not self._user_stopped_daemon
                            and not self._daemon_starting
                            and lock_held
                        ):
                            # N3: meshd.lock is held by a live (busy/hung/draining) daemon — do not spawn.
                            was_stopping = self._daemon_stopping
                            self._daemon_stopping = True
                            self._last_start_error = None
                            self._auto_start_attempted = False
                            if not was_stopping:
                                busy_stopping_event = {
                                    "type": "_desktop.connection",
                                    "connected": False,
                                    "starting": False,
                                    "stopping": True,
                                    "user_stopped": False,
                                    "connection": self._connection_snapshot_unlocked(),
                                }
                        elif (
                            self._auto_start_daemon
                            and not self._user_stopped_daemon
                            and not self._daemon_starting
                            and is_unavailable
                            and (not self._auto_start_attempted or self._daemon_stopping)
                        ):
                            cutoff = now_mono - self._auto_start_window_s
                            while self._auto_start_timestamps and self._auto_start_timestamps[0] < cutoff:
                                self._auto_start_timestamps.popleft()
                            if len(self._auto_start_timestamps) >= self._auto_start_max_attempts:
                                self._auto_start_attempted = True
                                self._daemon_stopping = False
                                cap_err = {
                                    "code": "AUTO_START_RATE_LIMITED",
                                    "exit_code": c.EXIT_INTERNAL,
                                    "message": (
                                        f"meshd 在 {int(self._auto_start_window_s)}s 内已自动重启 "
                                        f"{self._auto_start_max_attempts} 次后仍异常退出，已暂停自动启动，请查看日志或手动启动"
                                    ),
                                    "details": {
                                        "max_attempts": self._auto_start_max_attempts,
                                        "window_s": self._auto_start_window_s,
                                    },
                                    "auto": True,
                                }
                                self._last_start_error = dict(cap_err)
                                self._last_error = dict(cap_err)
                                rate_limited_event = {
                                    "type": "_desktop.connection",
                                    "connected": False,
                                    "starting": False,
                                    "stopping": False,
                                    "error": dict(cap_err),
                                    "connection": self._connection_snapshot_unlocked(),
                                }
                            else:
                                should_auto_start = True
                                self._auto_start_attempted = True
                                self._auto_start_timestamps.append(now_mono)
                        snap = self._connection_snapshot_unlocked()
                    if onboarding_cleared:
                        self._enqueue_and_push(
                            {
                                "type": "_desktop.connection",
                                "connected": False,
                                "connection": snap,
                            }
                        )
                    if busy_stopping_event is not None:
                        self._record_diag(
                            "info",
                            "DAEMON_BUSY_OR_STOPPING",
                            "meshd.lock 仍被持有（进程繁忙或正在收尾），跳过自动启动并等待重试",
                        )
                        self._enqueue_and_push(busy_stopping_event)
                    if rate_limited_event is not None:
                        err_info = rate_limited_event["error"]
                        self._record_diag("error", err_info["code"], err_info["message"], err_info.get("details"))
                        self._enqueue_and_push(rate_limited_event)
                    if should_auto_start and not self._stop_event.is_set():
                        try:
                            start_res = self._start_daemon_impl(auto=True)
                        except Exception as exc:
                            err_env = _error_envelope(
                                c.INTERNAL_ERROR,
                                f"自动启动 meshd 发生未预期异常: {exc}",
                                exit_code=c.EXIT_INTERNAL,
                                details={"exception": type(exc).__name__},
                            )
                            err_with_meta = dict(err_env["error"])
                            err_with_meta["auto"] = True
                            with self._lock:
                                self._daemon_starting = False
                                self._daemon_stopping = False
                                self._last_start_error = err_with_meta
                                self._last_error = dict(err_env["error"])
                                snap_err = self._connection_snapshot_unlocked()
                            self._record_diag(
                                "error",
                                c.INTERNAL_ERROR,
                                err_env["error"]["message"],
                                err_env["error"].get("details"),
                            )
                            self._enqueue_and_push(
                                {
                                    "type": "_desktop.connection",
                                    "connected": False,
                                    "starting": False,
                                    "stopping": False,
                                    "error": err_with_meta,
                                    "connection": snap_err,
                                }
                            )
                            start_res = err_env
                        if self._stop_event.is_set():
                            break
                        if start_res.get("ok") and self._connected:
                            wait_s = self._probe_interval_s
                            self._wake_event.wait(timeout=wait_s)
                            continue
                        if isinstance(start_res, dict) and start_res.get("ok") and (start_res.get("data") or {}).get("in_progress"):
                            with self._lock:
                                if self._auto_start_timestamps:
                                    self._auto_start_timestamps.pop()
                        err = start_res.get("error") if isinstance(start_res, dict) else None
                        if (
                            isinstance(err, dict)
                            and isinstance(err.get("details"), dict)
                            and err["details"].get("user_stopped") is True
                        ):
                            with self._lock:
                                self._auto_start_attempted = False
                                if self._auto_start_timestamps:
                                    self._auto_start_timestamps.pop()
                        elif (
                            isinstance(err, dict)
                            and err.get("code") == c.DAEMON_ALREADY_RUNNING
                            and isinstance(err.get("details"), dict)
                            and err["details"].get("status") == "stopping"
                        ):
                            with self._lock:
                                self._auto_start_attempted = False
                                if self._auto_start_timestamps:
                                    self._auto_start_timestamps.pop()

                with self._lock:
                    idx = min(self._reconnect_attempt, len(self._backoff) - 1)
                    wait_s = float(self._backoff[idx])
                    self._reconnect_attempt += 1
                    self._next_retry_s = wait_s
            self._wake_event.wait(timeout=wait_s)

    def _event_stream_loop(self, stop_ev: threading.Event) -> None:
        with self._lock:
            pw = self._passphrase
            since = self._last_seq
        if pw is None:
            return

        client = self._build_client(self._rpc_timeout_s)
        sock = None
        try:
            sock = client._connect_socket(self._probe_timeout_s)
            with self._lock:
                if (
                    self._stop_event.is_set()
                    or stop_ev.is_set()
                    or self._passphrase is None
                    or self._stream_stop_event is not stop_ev
                ):
                    return
                pw = self._passphrase
                self._stream_sock = sock
            tail_args: Dict[str, Any] = {}
            if since is not None:
                tail_args["since"] = since
            req = {
                "v": 1,
                "request_id": _new_request_id(),
                "cmd": "event.tail",
                "auth": {"type": "user", "passphrase": pw},
                "args": tail_args,
                "idempotency_key": None,
                "dry_run": False,
            }
            sock.sendall((json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8"))
            if hasattr(sock, "settimeout"):
                sock.settimeout(self._stream_poll_timeout_s)

            buf = b""
            while not self._stop_event.is_set() and not stop_ev.is_set():
                try:
                    chunk = sock.recv(4096)
                except (TimeoutError, socket.timeout):
                    continue
                if not chunk:
                    if not self._stop_event.is_set() and not stop_ev.is_set():
                        self._mark_disconnected(
                            {
                                "code": c.DAEMON_UNAVAILABLE,
                                "exit_code": c.EXIT_DAEMON_UNAVAILABLE,
                                "message": "实时事件流已断开",
                                "details": {},
                            }
                        )
                    break
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    self._handle_stream_line(line, stop_ev)
        except (MeshError, OSError, ValueError) as exc:
            if not self._stop_event.is_set() and not stop_ev.is_set():
                code = exc.code if isinstance(exc, MeshError) else c.DAEMON_UNAVAILABLE
                msg = exc.message if isinstance(exc, MeshError) else f"事件流连接失败: {exc}"
                self._log_desktop(
                    "WARNING",
                    "EVENT_STREAM_ERROR",
                    msg,
                    details={"code": code, "exception": type(exc).__name__},
                )
                self._mark_disconnected(
                    {
                        "code": code,
                        "exit_code": c.EXIT_DAEMON_UNAVAILABLE,
                        "message": msg,
                        "details": {},
                    }
                )
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            with self._lock:
                if self._stream_sock is sock:
                    self._stream_sock = None
                if self._stream_stop_event is stop_ev:
                    self._stream_thread = None
                    self._stream_stop_event = None

    def _handle_stream_line(self, line: str, stop_ev: threading.Event) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        # Error envelope returned from daemon before closing stream (e.g. UNAUTHENTICATED)
        if msg.get("ok") is False and isinstance(msg.get("error"), dict):
            err = dict(msg["error"])
            if "exit_code" in msg and "exit_code" not in err:
                err["exit_code"] = msg["exit_code"]
            self._handle_rpc_error(err)
            stop_ev.set()
            return
        # Ignore synthetic ack envelopes without event fields if any
        if "seq" in msg or "type" in msg:
            self._enqueue_and_push(msg)

    def _enqueue_and_push(self, event: Dict[str, Any]) -> None:
        with self._lock:
            seq = event.get("seq")
            if isinstance(seq, int) and (self._last_seq is None or seq > self._last_seq):
                self._last_seq = seq
            self._pending_events.append(event)
            self._push_needed = True
            win = self._window
        cb = self._on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:
                pass
        if win is not None and self._push_enabled:
            self._push_queued_to_window(win)

    def _push_queued_to_window(self, win: Any) -> None:
        while not self._stop_event.is_set():
            if not self._push_lock.acquire(blocking=False):
                return
            try:
                while not self._stop_event.is_set():
                    with self._lock:
                        self._push_needed = False
                        cur_win = self._window
                        if self._stop_event.is_set() or not self._pending_events or cur_win is None:
                            return
                        batch = list(self._pending_events)
                        self._pending_events.clear()
                    js = (
                        "typeof window.__meshOnEvents === 'function' && "
                        f"(window.__meshOnEvents({json.dumps(batch, separators=(',', ':'))}), true)"
                    )
                    accepted = False
                    try:
                        accepted = bool(cur_win.evaluate_js(js))
                    except Exception:
                        accepted = False
                    if not accepted:
                        with self._lock:
                            if not self._stop_event.is_set() and self._window is not None:
                                combined = batch + list(self._pending_events)
                                maxlen = self._pending_events.maxlen or 2000
                                if len(combined) > maxlen:
                                    combined = combined[-maxlen:]
                                self._pending_events.clear()
                                self._pending_events.extend(combined)
                            self._push_needed = False
                        return
                    with self._lock:
                        if not self._pending_events and not self._push_needed:
                            break
            finally:
                self._push_lock.release()
            with self._lock:
                if not self._pending_events or not self._push_needed or self._window is None:
                    return


_JS_API_PUBLIC_METHODS: frozenset[str] = frozenset(
    {
        "capabilities",
        "auth_status",
        "set_passphrase",
        "clear_passphrase",
        "pipe_info",
        "connection_state",
        "home_status",
        "initialize_home",
        "detect_local_clis",
        "pick_folder",
        "inspect_repo_folder",
        "desktop_prefs",
        "set_desktop_prefs",
        "quit_app",
        "call",
        "daemon_status",
        "start_daemon",
        "stop_daemon",
        "doctor_report",
        "poll_events",
        "reconnect_now",
        "window_minimize",
        "window_toggle_maximize",
        "window_restore_for_drag",
        "window_drag_move",
        "window_drag_end",
        "window_resize_start",
        "window_resize",
        "window_resize_end",
        "window_close",
    }
)

_EMPTY_BUILTINS: types.MappingProxyType[str, Any] = types.MappingProxyType({})


def _make_sealed_endpoint_factory(
    name: str,
    target: Callable[..., Any],
) -> Callable[[], Callable[..., Any]]:
    """Return a factory minting a fresh, default-free, builtins-stripped function per lookup.

    Each attribute access on :class:`DesktopJsApi` receives an ephemeral function taking
    ``(*args, **kwargs)`` with ``__defaults__ is None``, ``__kwdefaults__ is None``,
    zero global lookups, and a fresh ``{"__builtins__": MappingProxyType({})}`` globals
    mapping. Mutating ``__defaults__``, ``__kwdefaults__``, ``__code__``, ``__globals__``,
    or attributes on a fetched function cannot persist or affect subsequent calls.
    """

    def _template(*args: Any, **kwargs: Any) -> Any:
        return target(*args, **kwargs)

    code = _template.__code__
    closure = _template.__closure__

    def _mint() -> Callable[..., Any]:
        return types.FunctionType(
            code,
            {"__builtins__": _EMPTY_BUILTINS},
            name,
            None,
            closure,
        )

    return _mint


class DesktopJsApi:
    """Sealed, immutable JS-facing facade exposed via pywebview ``js_api``."""

    __slots__ = ()

    def __new__(cls, bridge: DesktopBridge) -> "DesktopJsApi":
        if cls is not DesktopJsApi:
            return super().__new__(cls)

        factories = types.MappingProxyType(
            {
                "capabilities": _make_sealed_endpoint_factory("capabilities", bridge.capabilities),
                "auth_status": _make_sealed_endpoint_factory("auth_status", bridge.auth_status),
                "set_passphrase": _make_sealed_endpoint_factory("set_passphrase", bridge.set_passphrase),
                "clear_passphrase": _make_sealed_endpoint_factory("clear_passphrase", bridge.clear_passphrase),
                "pipe_info": _make_sealed_endpoint_factory("pipe_info", bridge.pipe_info),
                "connection_state": _make_sealed_endpoint_factory("connection_state", bridge.connection_state),
                "home_status": _make_sealed_endpoint_factory("home_status", bridge.home_status),
                "initialize_home": _make_sealed_endpoint_factory("initialize_home", bridge.initialize_home),
                "detect_local_clis": _make_sealed_endpoint_factory("detect_local_clis", bridge.detect_local_clis),
                "pick_folder": _make_sealed_endpoint_factory("pick_folder", bridge.pick_folder),
                "inspect_repo_folder": _make_sealed_endpoint_factory("inspect_repo_folder", bridge.inspect_repo_folder),
                "desktop_prefs": _make_sealed_endpoint_factory("desktop_prefs", bridge.desktop_prefs),
                "set_desktop_prefs": _make_sealed_endpoint_factory("set_desktop_prefs", bridge.set_desktop_prefs),
                "quit_app": _make_sealed_endpoint_factory("quit_app", bridge.quit_app),
                "call": _make_sealed_endpoint_factory("call", bridge.call),
                "daemon_status": _make_sealed_endpoint_factory("daemon_status", bridge.daemon_status),
                "start_daemon": _make_sealed_endpoint_factory("start_daemon", bridge.start_daemon),
                "stop_daemon": _make_sealed_endpoint_factory("stop_daemon", bridge.stop_daemon),
                "doctor_report": _make_sealed_endpoint_factory("doctor_report", bridge.doctor_report),
                "poll_events": _make_sealed_endpoint_factory("poll_events", bridge.poll_events),
                "reconnect_now": _make_sealed_endpoint_factory("reconnect_now", bridge.reconnect_now),
                "window_minimize": _make_sealed_endpoint_factory("window_minimize", bridge.window_minimize),
                "window_toggle_maximize": _make_sealed_endpoint_factory(
                    "window_toggle_maximize", bridge.window_toggle_maximize
                ),
                "window_restore_for_drag": _make_sealed_endpoint_factory(
                    "window_restore_for_drag", bridge.window_restore_for_drag
                ),
                "window_drag_move": _make_sealed_endpoint_factory("window_drag_move", bridge.window_drag_move),
                "window_drag_end": _make_sealed_endpoint_factory("window_drag_end", bridge.window_drag_end),
                "window_resize_start": _make_sealed_endpoint_factory(
                    "window_resize_start", bridge.window_resize_start
                ),
                "window_resize": _make_sealed_endpoint_factory("window_resize", bridge.window_resize),
                "window_resize_end": _make_sealed_endpoint_factory("window_resize_end", bridge.window_resize_end),
                "window_close": _make_sealed_endpoint_factory("window_close", bridge.window_close),
            }
        )

        class _SealedJsApi(DesktopJsApi):
            __slots__ = ()

            def __getattribute__(self, name: str) -> Any:
                factory = factories.get(name)
                if factory is None:
                    raise AttributeError(name)
                return factory()

            def __setattr__(self, name: str, value: Any) -> None:
                raise AttributeError(f"DesktopJsApi is read-only: cannot set {name!r}")

            def __delattr__(self, name: str) -> None:
                raise AttributeError(f"DesktopJsApi is read-only: cannot delete {name!r}")

            def __dir__(self) -> List[str]:
                return sorted(factories)

            def __repr__(self) -> str:
                return "<DesktopJsApi>"

        return object.__new__(_SealedJsApi)
