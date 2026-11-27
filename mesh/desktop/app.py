"""Desktop GUI launcher for Agent Mesh (optional ``pywebview`` / WebView2 shell)."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib
import inspect
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import mesh.constants as c
from mesh.core.paths import resolve_home
from mesh.core.redact import redact
from mesh.desktop.bridge import (
    CAPABILITIES,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    DesktopBridge,
    DesktopJsApi,
    append_desktop_log,
    asset_dir,
    compute_initial_window_geometry,
    compute_saved_window_placement,
    index_html_path,
)
from mesh.desktop.i18n import resolve_effective_language, tr as _tr
from mesh.desktop.tray_win import SingleInstanceGuard, WindowsTrayController, resolve_app_icon_path
from mesh.errors import MeshError

REQUIRED_ASSET_FILES = ("index.html", "styles.css", "i18n.js", "app.js")
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_WIN32_MB_OK = 0x00000000
_WIN32_MB_YESNO = 0x00000004
_WIN32_MB_ICONERROR = 0x00000010
_WIN32_MB_ICONWARNING = 0x00000030
_WIN32_MB_ICONINFORMATION = 0x00000040
_WIN32_IDYES = 6


def _load_win32_user32() -> Any:
    """Load a private ``user32`` WinDLL instance without mutating shared ``ctypes.windll``."""
    win_dll_cls = getattr(ctypes, "WinDLL", None)
    if callable(win_dll_cls):
        return win_dll_cls("user32", use_last_error=True)
    return None


def _show_win32_dialog(title: str, message: str, *, icon_flag: int = _WIN32_MB_ICONERROR) -> bool:
    """Show a native Windows MessageBox when running without a console (pythonw/gui-script)."""
    try:
        user32 = _load_win32_user32()
        if user32 is None:
            return False
        msgbox = getattr(user32, "MessageBoxW", None)
        if not callable(msgbox):
            return False
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if isinstance(win_dll_cls, type) and isinstance(user32, win_dll_cls):
            msgbox.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            msgbox.restype = ctypes.c_int
        msgbox(0, str(message), str(title), _WIN32_MB_OK | int(icon_flag))
        return True
    except Exception:
        return False


def _show_win32_error_dialog(title: str, message: str) -> bool:
    """Show a native Windows error MessageBox when running without stderr (pythonw/gui-script)."""
    return _show_win32_dialog(title, message, icon_flag=_WIN32_MB_ICONERROR)


def _show_win32_yesno_dialog(title: str, message: str, *, icon_flag: int = _WIN32_MB_ICONWARNING) -> Optional[bool]:
    """Show a native Windows Yes/No MessageBox, returning True/False or None if unavailable."""
    try:
        user32 = _load_win32_user32()
        if user32 is None:
            return None
        msgbox = getattr(user32, "MessageBoxW", None)
        if not callable(msgbox):
            return None
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if isinstance(win_dll_cls, type) and isinstance(user32, win_dll_cls):
            msgbox.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            msgbox.restype = ctypes.c_int
        res = int(msgbox(0, str(message), str(title), _WIN32_MB_YESNO | int(icon_flag)))
        return res == _WIN32_IDYES
    except Exception:
        return None


def _write_json_stream(stream: Any, payload: Dict[str, Any]) -> None:
    """Write an ASCII-safe JSON line so zh-CN messages never crash a cp1252 stream."""
    stream.write(json.dumps(payload, ensure_ascii=True) + "\n")
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def detect_webview2_runtime(winreg_mod: Any = None) -> Optional[str]:
    """Return the installed Edge WebView2 Runtime version from Windows registry, or None."""
    reg = winreg_mod
    if reg is None:
        try:
            reg = importlib.import_module("winreg")
        except ModuleNotFoundError:
            return None

    hklm = getattr(reg, "HKEY_LOCAL_MACHINE", None)
    hkcu = getattr(reg, "HKEY_CURRENT_USER", None)
    close_key = getattr(reg, "CloseKey", None)
    subkeys = (
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}",
    )
    for hive in (hklm, hkcu):
        if hive is None:
            continue
        for subkey in subkeys:
            try:
                key = reg.OpenKey(hive, subkey)
            except OSError:
                continue
            try:
                if hasattr(key, "__enter__") and hasattr(key, "__exit__"):
                    with key:
                        pv, _ = reg.QueryValueEx(key, "pv")
                else:
                    try:
                        pv, _ = reg.QueryValueEx(key, "pv")
                    finally:
                        if close_key is not None:
                            try:
                                close_key(key)
                            except OSError:
                                pass
            except OSError:
                continue
            if isinstance(pv, str):
                version = pv.strip()
                if version and version != "0.0.0.0":
                    return version
    return None


def ensure_webview2_runtime(winreg_mod: Any = None, *, language: Optional[str] = None) -> str:
    """Verify Microsoft Edge WebView2 Runtime is installed on Windows, or fail fast."""
    version = detect_webview2_runtime(winreg_mod=winreg_mod)
    if not version:
        lang = resolve_effective_language(language)
        raise MeshError(
            code=c.INTERNAL_ERROR,
            exit_code=c.EXIT_INTERNAL,
            message=_tr(
                lang,
                (
                    "未检测到 Microsoft Edge WebView2 Runtime（桌面端禁止静默回退到 MSHTML）。\n\n"
                    "请先下载并安装 WebView2 Runtime（可按 Ctrl+C 复制以下链接）：\n"
                    f"{WEBVIEW2_DOWNLOAD_URL}"
                ),
                (
                    "Microsoft Edge WebView2 Runtime was not detected "
                    "(silent fallback to legacy MSHTML is disabled).\n\n"
                    "Please download and install WebView2 Runtime (press Ctrl+C to copy the link):\n"
                    f"{WEBVIEW2_DOWNLOAD_URL}"
                ),
            ),
            details={
                "missing_runtime": "webview2",
                "client_guid": WEBVIEW2_CLIENT_GUID,
                "install_url": WEBVIEW2_DOWNLOAD_URL,
            },
        )
    return version


def verify_assets() -> Dict[str, Any]:
    """Verify static frontend asset files exist and are readable without starting a GUI."""
    root = asset_dir()
    files: Dict[str, int] = {}
    for name in REQUIRED_ASSET_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Desktop UI asset missing: {path}",
                details={"asset": name, "path": path},
            )
        files[name] = os.path.getsize(path)
    return {
        "asset_dir": root,
        "index_html": index_html_path(),
        "files": files,
        "capabilities": dict(CAPABILITIES),
    }


def _import_webview() -> Any:
    """Import optional ``pywebview`` only when opening a desktop window."""
    try:
        return importlib.import_module("webview")
    except ModuleNotFoundError as exc:
        raise MeshError(
            code=c.INTERNAL_ERROR,
            exit_code=c.EXIT_INTERNAL,
            message="pywebview is not installed; install desktop extras with: pip install agent-mesh[desktop]",
            details={"missing_package": "pywebview", "extra": "desktop"},
        ) from exc


def _load_saved_language_pref(home: Optional[str]) -> Optional[str]:
    """Best-effort lookup of persisted desktop UI language preference before bridge init."""
    if not home:
        return None
    try:
        from mesh.desktop.state import DesktopStateStore

        prefs = DesktopStateStore(home).load_prefs()
        lang = prefs.get("language")
        return str(lang) if isinstance(lang, str) and lang.strip() else None
    except Exception:
        return None


def launch_desktop(
    home: Optional[str] = None,
    *,
    title: str = "Agent Mesh",
    debug: bool = False,
) -> Dict[str, Any]:
    """Launch the Agent Mesh desktop window bound to local IPC via :class:`DesktopBridge`."""
    resolved_home = resolve_home(home or os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh"))
    saved_lang = _load_saved_language_pref(resolved_home)
    append_desktop_log(
        resolved_home,
        "INFO",
        "DESKTOP_LAUNCH",
        "Launching Agent Mesh desktop client",
        details={"pid": os.getpid(), "platform": sys.platform, "debug": bool(debug)},
    )
    assets = verify_assets()
    if sys.platform == "win32":
        wv2_ver = ensure_webview2_runtime(language=saved_lang)
        append_desktop_log(
            resolved_home,
            "INFO",
            "WEBVIEW2_READY",
            f"Detected WebView2 Runtime {wv2_ver}",
            details={"version": wv2_ver},
        )
    webview = _import_webview()

    bridge = DesktopBridge(home=resolved_home)
    instance_guard: Optional[SingleInstanceGuard] = None
    if sys.platform == "win32":
        instance_guard = SingleInstanceGuard(bridge.home)
        if not instance_guard.acquire():
            bridge.close()
            return {"status": "activated_existing", "home": bridge.home}
    js_api = DesktopJsApi(bridge)
    url = Path(assets["index_html"]).resolve().as_uri()
    saved_bounds = bridge.load_saved_window_bounds()
    placement = compute_saved_window_placement(saved_bounds)
    pos_kw: Dict[str, Any] = {}
    if placement.get("x") is not None and placement.get("y") is not None:
        pos_kw["x"] = placement["x"]
        pos_kw["y"] = placement["y"]
    if pos_kw:
        try:
            win_sig = inspect.signature(webview.create_window).parameters
            accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in win_sig.values())
            if not accepts_var_kw:
                pos_kw = {k: v for k, v in pos_kw.items() if k in win_sig}
        except (TypeError, ValueError):
            pass

    window = webview.create_window(
        title=title,
        url=url,
        js_api=js_api,
        width=placement["width"],
        height=placement["height"],
        min_size=placement["min_size"],
        background_color="#0b0b0c",
        frameless=True,
        easy_drag=False,
        **pos_kw,
    )
    bridge.attach_window(window)
    append_desktop_log(
        bridge.home,
        "INFO",
        "WINDOW_CREATED",
        "Desktop WebView window created",
        details={"width": placement["width"], "height": placement["height"]},
    )

    tray: Optional[WindowsTrayController] = None
    if sys.platform == "win32":
        tray = WindowsTrayController(
            bridge.home,
            on_show=lambda: bridge.window_show(),
            on_hide=lambda: bridge.window_hide(),
            on_toggle_visibility=lambda: bridge.window_toggle_visibility(),
            on_lock=lambda: bridge.clear_passphrase(forget_stored=False),
            on_start_daemon=lambda: bridge.start_daemon(),
            on_stop_daemon=lambda: bridge.stop_daemon(),
            on_quit=lambda stop_daemon: bridge.quit_app(stop_daemon=stop_daemon),
            get_status=bridge.tray_status_snapshot,
            icon_path=resolve_app_icon_path(),
        )
        if tray.start():
            bridge.attach_tray(tray)
        else:
            tray = None

    def _on_closing(*_args: Any) -> Optional[bool]:
        bridge.save_current_window_bounds()
        if bridge.should_hide_on_close():
            bridge.window_hide(from_close_button=True)
            return False
        return None

    def _on_closed(*_args: Any) -> None:
        bridge.save_current_window_bounds()
        bridge.request_stop()

    restore_maximized = bool(placement.get("maximized", False))

    def _on_shown(*_args: Any) -> None:
        bridge._on_window_shown()
        if restore_maximized:
            bridge.window_toggle_maximize()

    def _on_maximized(*_args: Any) -> None:
        bridge._on_window_maximized()

    def _on_restored(*_args: Any) -> None:
        bridge._on_window_restored()

    def _on_resized(*_args: Any) -> None:
        bridge._on_window_resized(*_args)

    if hasattr(window, "events"):
        if hasattr(window.events, "closing"):
            window.events.closing += _on_closing
        if hasattr(window.events, "closed"):
            window.events.closed += _on_closed
        if hasattr(window.events, "shown"):
            window.events.shown += _on_shown
        if hasattr(window.events, "maximized"):
            window.events.maximized += _on_maximized
        if hasattr(window.events, "restored"):
            window.events.restored += _on_restored
        if hasattr(window.events, "resized"):
            window.events.resized += _on_resized

    try:
        bridge.start()
        gui_kw: Dict[str, Any] = {"debug": debug}
        try:
            sig_params = inspect.signature(webview.start).parameters
            accepts_http_server = "http_server" in sig_params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
            )
        except (TypeError, ValueError):
            accepts_http_server = False
        if accepts_http_server:
            gui_kw["http_server"] = False
        if sys.platform == "win32":
            gui_kw["gui"] = "edgechromium"
            icon_path = resolve_app_icon_path()
            if icon_path and "icon" in (sig_params if "sig_params" in locals() else {}):
                gui_kw["icon"] = icon_path
        webview.start(**gui_kw)
    finally:
        bridge.save_current_window_bounds()
        if tray is not None:
            tray.stop()
        bridge.close()
        append_desktop_log(bridge.home, "INFO", "DESKTOP_CLOSED", "Desktop client exited")
        if instance_guard is not None:
            instance_guard.release()

    return {"status": "closed", "home": bridge.home}


def build_desktop_parser() -> argparse.ArgumentParser:
    """Build argument parser for the standalone ``mesh-desktop`` entry point."""
    parser = argparse.ArgumentParser(
        prog="mesh-desktop",
        description="Launch the Agent Mesh desktop client over local IPC.",
        epilog="Examples:\n  mesh-desktop\n  mesh-desktop --home ~/.agent-mesh\n  mesh-desktop --check\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Agent Mesh home directory (default: MESH_HOME or ~/.agent-mesh)",
    )
    parser.add_argument(
        "--title",
        default="Agent Mesh",
        help="Desktop window title",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable WebView developer tools when supported",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify desktop static assets without launching a GUI window",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for ``mesh-desktop``."""
    # Record headless / no-console launch BEFORE importing pywebview (which replaces
    # sys.stderr=None with a cp1252 NUL stream on Windows).
    no_console = sys.stderr is None or sys.stdout is None
    parser = build_desktop_parser()
    if no_console:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                args = parser.parse_args(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (c.EXIT_OK if exc.code is None else c.EXIT_USAGE)
            msg = redact((err_buf.getvalue() or out_buf.getvalue()).strip())
            if msg and sys.platform == "win32":
                icon_flag = _WIN32_MB_ICONINFORMATION if code == c.EXIT_OK else _WIN32_MB_ICONERROR
                _show_win32_dialog("Agent Mesh", msg, icon_flag=icon_flag)
            return code
    else:
        args = parser.parse_args(argv)
    import mesh.client as _client_mod

    _client_mod.EXPLICIT_HOME = args.home is not None
    home: Optional[str] = None
    try:
        home = resolve_home(args.home or os.environ.get("MESH_HOME") or os.path.expanduser("~/.agent-mesh"))
        if args.check:
            info = verify_assets()
            info["home"] = home
            if sys.stdout is not None:
                _write_json_stream(sys.stdout, {"ok": True, "data": info})
            return c.EXIT_OK
        launch_desktop(home=home, title=args.title, debug=args.debug)
        return c.EXIT_OK
    except MeshError as exc:
        if home and not args.check:
            append_desktop_log(
                home,
                "ERROR",
                "DESKTOP_ERROR",
                exc.message,
                details={"code": exc.code, "exit_code": exc.exit_code},
            )
        if no_console or sys.stderr is None:
            if sys.platform == "win32":
                _show_win32_error_dialog("Agent Mesh", exc.message)
        else:
            _write_json_stream(
                sys.stderr,
                {
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                    "exit_code": exc.exit_code,
                },
            )
        return exc.exit_code
    except Exception as exc:
        err_type = type(exc).__name__
        err_msg = redact(str(exc).strip())
        summary = f"{err_type}: {err_msg}" if err_msg else err_type
        if home and not args.check:
            append_desktop_log(
                home,
                "ERROR",
                "DESKTOP_CRASH",
                summary,
                details={"exception_type": err_type},
            )
        if no_console or sys.stderr is None:
            if sys.platform == "win32":
                crash_lang = resolve_effective_language(_load_saved_language_pref(home))
                _show_win32_error_dialog(
                    "Agent Mesh",
                    _tr(
                        crash_lang,
                        f"启动 Agent Mesh 桌面端时发生未预期错误：\n\n{summary}",
                        f"An unexpected error occurred while starting Agent Mesh Desktop:\n\n{summary}",
                    ),
                )
        else:
            _write_json_stream(
                sys.stderr,
                {
                    "ok": False,
                    "error": {
                        "code": c.INTERNAL_ERROR,
                        "message": summary,
                        "details": {"exception_type": err_type},
                    },
                    "exit_code": c.EXIT_INTERNAL,
                },
            )
        return c.EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
