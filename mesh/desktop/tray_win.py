"""Zero-dependency Windows system tray & single-instance guard (S8).

Uses private ``ctypes.WinDLL`` instances (``user32``, ``shell32``, ``kernel32``)
so no third-party GUI libraries (like ``pystray`` / ``Pillow``) are needed.

Invariants:
- Closing/hiding the window keeps ``meshd`` and in-memory session alive.
- Quitting the tray/GUI NEVER stops ``meshd`` unless the user explicitly selects
  "退出并停止后台服务" (which still uses the authenticated ``daemon.stop`` RPC,
  never force-killing processes).
- Single-instance mutex activates the existing window instead of opening duplicates.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

# Win32 constants
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_USER = 0x0400
WM_APP = 0x8000

WM_TRAYICON = WM_USER + 20
WM_APP_ACTIVATE = WM_APP + 1
WM_APP_UPDATE_TIP = WM_APP + 2
WM_APP_BALLOON = WM_APP + 3
WM_APP_SHUTDOWN = WM_APP + 4

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_DISABLED = 0x00000002
MF_SEPARATOR = 0x00000800

TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
TPM_LEFTBUTTON = 0x0000
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

ERROR_ALREADY_EXISTS = 183

# Tray context menu command IDs
CMD_STATUS = 1000
CMD_TOGGLE_SHOW = 1001
CMD_LOCK = 1002
CMD_START_DAEMON = 1003
CMD_STOP_DAEMON = 1004
CMD_QUIT_ONLY = 1005
CMD_QUIT_AND_STOP = 1006


def tray_home_hash(home: str) -> str:
    try:
        real = os.path.realpath(home)
    except OSError:
        real = os.path.abspath(home)
    norm = os.path.normpath(real)
    if sys.platform == "win32":
        norm = norm.lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def single_instance_mutex_name(home: str) -> str:
    return f"Local\\AgentMeshDesktop-{tray_home_hash(home)}"


def tray_window_class_name(home: str) -> str:
    return f"AgentMeshTray_{tray_home_hash(home)}"


def resolve_app_icon_path() -> Optional[str]:
    """Locate the shipped ``.ico`` file for tray & window icon use."""
    desktop_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(desktop_dir, "assets", "agent-mesh.ico"),
        os.path.join(os.path.dirname(os.path.dirname(desktop_dir)), "packaging", "windows", "icon.ico"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def format_tray_tooltip(status: Dict[str, Any]) -> str:
    """Build a concise Win32 tray tooltip (<= 127 chars) reflecting daemon & auth state."""
    connected = bool(status.get("connected"))
    starting = bool(status.get("daemon_starting"))
    stopping = bool(status.get("daemon_stopping"))
    auth_verified = bool(status.get("auth_verified"))
    if starting:
        tip = "Agent Mesh — 正在启动后台服务..."
    elif stopping:
        tip = "Agent Mesh — 后台服务正在停止..."
    elif connected:
        tip = "Agent Mesh — 后台运行中（已解锁）" if auth_verified else "Agent Mesh — 后台运行中（未解锁）"
    else:
        tip = "Agent Mesh — 后台服务已断开"
    return tip[:126]


def build_tray_menu_spec(status: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a platform-independent specification for the tray right-click menu."""
    connected = bool(status.get("connected"))
    starting = bool(status.get("daemon_starting"))
    stopping = bool(status.get("daemon_stopping"))
    auth_verified = bool(status.get("auth_verified"))
    window_visible = bool(status.get("window_visible", True))
    pid = status.get("pid")

    if starting:
        status_label = "后台服务: 正在启动..."
    elif stopping:
        status_label = "后台服务: 正在停止..."
    elif connected:
        pid_part = f" (PID {pid})" if pid else ""
        auth_part = "已解锁" if auth_verified else "未解锁"
        status_label = f"后台服务: 运行中{pid_part} · {auth_part}"
    else:
        status_label = "后台服务: 未连接"

    items: List[Dict[str, Any]] = [
        {"kind": "item", "id": CMD_STATUS, "label": status_label, "enabled": False},
        {"kind": "separator"},
        {
            "kind": "item",
            "id": CMD_TOGGLE_SHOW,
            "label": "隐藏主窗口" if window_visible else "显示主窗口",
            "enabled": True,
        },
        {
            "kind": "item",
            "id": CMD_LOCK,
            "label": "锁定工作台（清除内存口令）",
            "enabled": auth_verified,
        },
        {"kind": "separator"},
    ]

    if connected:
        items.append(
            {
                "kind": "item",
                "id": CMD_STOP_DAEMON,
                "label": "停止后台服务 (meshd)",
                "enabled": auth_verified,
            }
        )
    else:
        items.append(
            {
                "kind": "item",
                "id": CMD_START_DAEMON,
                "label": "启动后台服务 (meshd)",
                "enabled": not starting and not stopping,
            }
        )

    items.extend(
        [
            {"kind": "separator"},
            {
                "kind": "item",
                "id": CMD_QUIT_ONLY,
                "label": "退出界面（保持后台服务运行）",
                "enabled": True,
            },
            {
                "kind": "item",
                "id": CMD_QUIT_AND_STOP,
                "label": "退出并停止后台服务",
                "enabled": bool(connected and auth_verified),
            },
        ]
    )
    return items


class SingleInstanceGuard:
    """Per-home Windows single-instance named mutex + existing window activator."""

    def __init__(
        self,
        home: str,
        *,
        kernel32: Any = None,
        user32: Any = None,
        get_last_error: Optional[Callable[[], int]] = None,
    ) -> None:
        self.home = home
        self.mutex_name = single_instance_mutex_name(home)
        self.window_class = tray_window_class_name(home)
        self._kernel32 = kernel32
        self._user32 = user32
        self._get_last_error = get_last_error
        self._handle: Any = None

    def _resolve_dlls(self) -> tuple[Any, Any]:
        k32 = self._kernel32
        u32 = self._user32
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if k32 is None and sys.platform == "win32" and callable(win_dll_cls):
            try:
                cand_k32 = win_dll_cls("kernel32", use_last_error=True)
            except OSError:
                cand_k32 = None
            if cand_k32 is not None and callable(getattr(cand_k32, "CreateMutexW", None)) and callable(getattr(cand_k32, "CloseHandle", None)):
                if isinstance(win_dll_cls, type) and isinstance(cand_k32, win_dll_cls):
                    cand_k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
                    cand_k32.CreateMutexW.restype = ctypes.c_void_p
                    cand_k32.CloseHandle.argtypes = [ctypes.c_void_p]
                    cand_k32.CloseHandle.restype = ctypes.c_int
                k32 = cand_k32
        if u32 is None and sys.platform == "win32" and callable(win_dll_cls):
            try:
                cand_u32 = win_dll_cls("user32", use_last_error=True)
            except OSError:
                cand_u32 = None
            if cand_u32 is not None and callable(getattr(cand_u32, "FindWindowW", None)) and callable(getattr(cand_u32, "PostMessageW", None)):
                if isinstance(win_dll_cls, type) and isinstance(cand_u32, win_dll_cls):
                    cand_u32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
                    cand_u32.FindWindowW.restype = ctypes.c_void_p
                    cand_u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
                    cand_u32.PostMessageW.restype = ctypes.c_int
                u32 = cand_u32
        return k32, u32

    def acquire(self) -> bool:
        """Acquire the per-home mutex. Returns False if another instance is already running."""
        k32, u32 = self._resolve_dlls()
        if k32 is None:
            return True
        handle = k32.CreateMutexW(None, 0, self.mutex_name)
        if not handle:
            return True
        err_fn = self._get_last_error or getattr(ctypes, "get_last_error", lambda: 0)
        if int(err_fn()) == ERROR_ALREADY_EXISTS:
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass
            if u32 is not None:
                try:
                    existing_hwnd = u32.FindWindowW(self.window_class, None)
                    if existing_hwnd:
                        u32.PostMessageW(existing_hwnd, WM_APP_ACTIVATE, 0, 0)
                except Exception:
                    pass
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if not handle:
            return
        k32, _ = self._resolve_dlls()
        if k32 is not None:
            try:
                k32.CloseHandle(handle)
            except Exception:
                pass


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _POINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint32),
        ("uFlags", ctypes.c_uint32),
        ("uCallbackMessage", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint32),
        ("dwStateMask", ctypes.c_uint32),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", ctypes.c_uint32),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint32),
        ("guidItem", _GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class WindowsTrayController:
    """Background-thread Win32 ``Shell_NotifyIconW`` tray controller."""

    def __init__(
        self,
        home: str,
        *,
        on_show: Callable[[], None],
        on_hide: Callable[[], None],
        on_toggle_visibility: Callable[[], None],
        on_lock: Callable[[], None],
        on_start_daemon: Callable[[], None],
        on_stop_daemon: Callable[[], None],
        on_quit: Callable[[bool], None],
        get_status: Callable[[], Dict[str, Any]],
        icon_path: Optional[str] = None,
    ) -> None:
        self.home = home
        self.window_class = tray_window_class_name(home)
        self._on_show = on_show
        self._on_hide = on_hide
        self._on_toggle_visibility = on_toggle_visibility
        self._on_lock = on_lock
        self._on_start_daemon = on_start_daemon
        self._on_stop_daemon = on_stop_daemon
        self._on_quit = on_quit
        self._get_status = get_status
        self._icon_path = icon_path if icon_path is not None else resolve_app_icon_path()

        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._started_ok = False
        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self._hwnd: Any = None
        self._hicon: Any = None
        self._icon_owned = False
        self._taskbar_created_msg: int = 0
        self._pending_tip: str = format_tray_tooltip({})
        self._pending_balloon: Optional[tuple[str, str]] = None
        self._user32: Any = None
        self._shell32: Any = None
        self._kernel32: Any = None
        self._wndproc_ref: Any = None

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._started_ok and not self._stopped and self._hwnd)

    def dispatch_menu_command(self, cmd_id: int, *, synchronous: bool = False) -> bool:
        """Dispatch a tray menu command without blocking the Win32 message loop."""
        action: Optional[Callable[[], None]] = None
        if cmd_id == CMD_TOGGLE_SHOW:
            action = self._on_toggle_visibility
        elif cmd_id == CMD_LOCK:
            action = self._on_lock
        elif cmd_id == CMD_START_DAEMON:
            action = self._on_start_daemon
        elif cmd_id == CMD_STOP_DAEMON:
            action = self._on_stop_daemon
        elif cmd_id == CMD_QUIT_ONLY:
            action = lambda: self._on_quit(False)
        elif cmd_id == CMD_QUIT_AND_STOP:
            action = lambda: self._on_quit(True)
        else:
            return False

        def _run() -> None:
            try:
                action()
            except Exception:
                pass

        if synchronous:
            _run()
        else:
            threading.Thread(target=_run, name=f"mesh-tray-cmd-{cmd_id}", daemon=True).start()
        return True

    def start(self, *, timeout_s: float = 3.0) -> bool:
        """Start the Win32 tray thread and register the notification icon."""
        if sys.platform != "win32":
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._started_ok
            self._stopped = False
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="mesh-desktop-tray",
                daemon=True,
            )
            self._thread.start()
        self._ready.wait(timeout=timeout_s)
        with self._lock:
            return bool(self._started_ok)

    def update_status(self, status: Dict[str, Any]) -> None:
        """Update the tray icon tooltip safely from any thread."""
        tip = format_tray_tooltip(status)
        with self._lock:
            self._pending_tip = tip
            hwnd = self._hwnd
            u32 = self._user32
        if hwnd and u32 is not None:
            try:
                u32.PostMessageW(hwnd, WM_APP_UPDATE_TIP, 0, 0)
            except Exception:
                pass

    def show_balloon(self, title: str, text: str) -> None:
        """Display a non-blocking informational balloon tip from the tray icon."""
        with self._lock:
            self._pending_balloon = (str(title)[:63], str(text)[:255])
            hwnd = self._hwnd
            u32 = self._user32
        if hwnd and u32 is not None:
            try:
                u32.PostMessageW(hwnd, WM_APP_BALLOON, 0, 0)
            except Exception:
                pass

    def stop(self) -> None:
        """Remove the tray icon and cleanly terminate the Win32 message loop."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            hwnd = self._hwnd
            u32 = self._user32
            th = self._thread
        if hwnd and u32 is not None:
            try:
                u32.PostMessageW(hwnd, WM_APP_SHUTDOWN, 0, 0)
            except Exception:
                pass
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=2.5)
        with self._lock:
            self._started_ok = False
            self._hwnd = None

    def _load_win32_dlls(self) -> bool:
        win_dll = getattr(ctypes, "WinDLL", None)
        if not callable(win_dll):
            return False
        try:
            u32 = win_dll("user32", use_last_error=True)
            s32 = win_dll("shell32", use_last_error=True)
            k32 = win_dll("kernel32", use_last_error=True)
        except OSError:
            return False

        req_u32 = (
            "DefWindowProcW", "RegisterWindowMessageW", "CreateWindowExW", "DestroyWindow",
            "PostQuitMessage", "GetMessageW", "TranslateMessage", "DispatchMessageW",
            "PostMessageW", "LoadImageW", "LoadIconW", "DestroyIcon", "CreatePopupMenu",
            "AppendMenuW", "DestroyMenu", "SetForegroundWindow", "GetCursorPos",
            "TrackPopupMenuEx", "UnregisterClassW", "RegisterClassExW",
        )
        if not all(callable(getattr(u32, name, None)) for name in req_u32):
            return False
        if not callable(getattr(s32, "Shell_NotifyIconW", None)):
            return False
        if not callable(getattr(k32, "GetModuleHandleW", None)):
            return False

        if isinstance(win_dll, type) and isinstance(u32, win_dll):
            u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
            u32.DefWindowProcW.restype = ctypes.c_ssize_t
            u32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
            u32.RegisterWindowMessageW.restype = ctypes.c_uint32
            u32.CreateWindowExW.argtypes = [
                ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]
            u32.CreateWindowExW.restype = ctypes.c_void_p
            u32.DestroyWindow.argtypes = [ctypes.c_void_p]
            u32.DestroyWindow.restype = ctypes.c_int
            u32.PostQuitMessage.argtypes = [ctypes.c_int]
            u32.PostQuitMessage.restype = None
            u32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
            u32.GetMessageW.restype = ctypes.c_int
            u32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
            u32.TranslateMessage.restype = ctypes.c_int
            u32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
            u32.DispatchMessageW.restype = ctypes.c_ssize_t
            u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
            u32.PostMessageW.restype = ctypes.c_int
            u32.LoadImageW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_uint32,
            ]
            u32.LoadImageW.restype = ctypes.c_void_p
            u32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            u32.LoadIconW.restype = ctypes.c_void_p
            u32.DestroyIcon.argtypes = [ctypes.c_void_p]
            u32.DestroyIcon.restype = ctypes.c_int
            u32.CreatePopupMenu.argtypes = []
            u32.CreatePopupMenu.restype = ctypes.c_void_p
            u32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_wchar_p]
            u32.AppendMenuW.restype = ctypes.c_int
            u32.DestroyMenu.argtypes = [ctypes.c_void_p]
            u32.DestroyMenu.restype = ctypes.c_int
            u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            u32.SetForegroundWindow.restype = ctypes.c_int
            u32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
            u32.GetCursorPos.restype = ctypes.c_int
            u32.TrackPopupMenuEx.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ]
            u32.TrackPopupMenuEx.restype = ctypes.c_int
            u32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
            u32.UnregisterClassW.restype = ctypes.c_int
        if isinstance(win_dll, type) and isinstance(s32, win_dll):
            s32.Shell_NotifyIconW.argtypes = [ctypes.c_uint32, ctypes.POINTER(_NOTIFYICONDATAW)]
            s32.Shell_NotifyIconW.restype = ctypes.c_int
        if isinstance(win_dll, type) and isinstance(k32, win_dll):
            k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            k32.GetModuleHandleW.restype = ctypes.c_void_p

        self._user32 = u32
        self._shell32 = s32
        self._kernel32 = k32
        return True

    def _add_or_modify_icon(self, hwnd: Any, *, add: bool, tip: Optional[str] = None, balloon: Optional[tuple[str, str]] = None) -> bool:
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        flags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = (tip if tip is not None else self._pending_tip)[:126]
        if balloon is not None:
            flags |= NIF_INFO
            nid.szInfoTitle = balloon[0][:63]
            nid.szInfo = balloon[1][:255]
            nid.dwInfoFlags = NIIF_INFO
        nid.uFlags = flags
        return bool(self._shell32.Shell_NotifyIconW(NIM_ADD if add else NIM_MODIFY, ctypes.byref(nid)))

    def _delete_icon(self, hwnd: Any) -> None:
        if not hwnd or self._shell32 is None:
            return
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        try:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass

    def _show_context_menu(self, hwnd: Any) -> None:
        u32 = self._user32
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            try:
                status = self._get_status()
            except Exception:
                status = {}
            spec = build_tray_menu_spec(status)
            for entry in spec:
                if entry["kind"] == "separator":
                    u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    flags = MF_STRING
                    if not entry.get("enabled", True):
                        flags |= MF_GRAYED | MF_DISABLED
                    u32.AppendMenuW(hmenu, flags, int(entry["id"]), str(entry["label"]))
            pt = _POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            u32.SetForegroundWindow(hwnd)
            cmd = int(
                u32.TrackPopupMenuEx(
                    hmenu,
                    TPM_RIGHTALIGN | TPM_BOTTOMALIGN | TPM_LEFTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                    pt.x,
                    pt.y,
                    hwnd,
                    None,
                )
            )
            u32.PostMessageW(hwnd, 0, 0, 0)
            if cmd > 0:
                self.dispatch_menu_command(cmd)
        finally:
            u32.DestroyMenu(hmenu)

    def _run_loop(self) -> None:
        if not self._load_win32_dlls():
            self._ready.set()
            return
        u32 = self._user32
        k32 = self._kernel32

        WNDPROCTYPE = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint32),
                ("style", ctypes.c_uint32),
                ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]

        u32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        u32.RegisterClassExW.restype = ctypes.c_uint16

        self._taskbar_created_msg = int(u32.RegisterWindowMessageW("TaskbarCreated"))
        hinstance = k32.GetModuleHandleW(None)

        if self._icon_path and os.path.isfile(self._icon_path):
            hicon = u32.LoadImageW(None, self._icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if hicon:
                self._hicon = hicon
                self._icon_owned = True
        if not self._hicon:
            self._hicon = u32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            self._icon_owned = False

        def _wndproc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                self._add_or_modify_icon(hwnd, add=True)
                return 0
            if msg == WM_TRAYICON:
                ev = lparam & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    threading.Thread(target=self._on_show, name="mesh-tray-show", daemon=True).start()
                elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_context_menu(hwnd)
                return 0
            if msg == WM_APP_ACTIVATE:
                threading.Thread(target=self._on_show, name="mesh-tray-activate", daemon=True).start()
                return 0
            if msg == WM_APP_UPDATE_TIP:
                with self._lock:
                    tip = self._pending_tip
                self._add_or_modify_icon(hwnd, add=False, tip=tip)
                return 0
            if msg == WM_APP_BALLOON:
                with self._lock:
                    balloon = self._pending_balloon
                    self._pending_balloon = None
                if balloon is not None:
                    self._add_or_modify_icon(hwnd, add=False, balloon=balloon)
                return 0
            if msg == WM_APP_SHUTDOWN:
                self._delete_icon(hwnd)
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION:
                if wparam:
                    self._delete_icon(hwnd)
                    self.dispatch_menu_command(CMD_QUIT_ONLY)
                return 0
            if msg == WM_DESTROY:
                self._delete_icon(hwnd)
                u32.PostQuitMessage(0)
                return 0
            return int(u32.DefWindowProcW(hwnd, msg, wparam, lparam))

        self._wndproc_ref = WNDPROCTYPE(_wndproc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinstance
        wc.lpszClassName = self.window_class
        wc.hIcon = self._hicon
        wc.hIconSm = self._hicon

        atom = u32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            self._ready.set()
            return

        hwnd = u32.CreateWindowExW(
            0, self.window_class, "Agent Mesh Tray", 0,
            0, 0, 0, 0, None, None, hinstance, None,
        )
        if not hwnd:
            u32.UnregisterClassW(self.window_class, hinstance)
            self._ready.set()
            return

        added = self._add_or_modify_icon(hwnd, add=True)
        with self._lock:
            self._hwnd = hwnd
            self._started_ok = bool(added)
        self._ready.set()

        if not added:
            u32.DestroyWindow(hwnd)
            u32.UnregisterClassW(self.window_class, hinstance)
            return

        msg = _MSG()
        while True:
            ret = int(u32.GetMessageW(ctypes.byref(msg), None, 0, 0))
            if ret <= 0:
                break
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

        self._delete_icon(hwnd)
        if self._icon_owned and self._hicon:
            try:
                u32.DestroyIcon(self._hicon)
            except Exception:
                pass
        u32.UnregisterClassW(self.window_class, hinstance)
        with self._lock:
            self._hwnd = None
            self._started_ok = False
