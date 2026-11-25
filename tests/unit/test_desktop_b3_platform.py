"""Unit tests for Batch 3 platform helpers: desktop state store, Win32 cred vault, and Win32 tray."""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from mesh.desktop.state import DesktopStateStore, home_state_key, sanitize_window_bounds
from mesh.desktop.tray_win import (
    CMD_LOCK,
    CMD_QUIT_AND_STOP,
    CMD_QUIT_ONLY,
    CMD_START_DAEMON,
    CMD_STOP_DAEMON,
    CMD_TOGGLE_SHOW,
    ERROR_ALREADY_EXISTS,
    WM_APP_ACTIVATE,
    SingleInstanceGuard,
    WindowsTrayController,
    build_tray_menu_spec,
    format_tray_tooltip,
)
from mesh.platform import _win32_cred as win_cred


class _FakeAdvapi32:
    """In-memory mock of Win32 Advapi32 CredWriteW / CredReadW / CredDeleteW / CredFree."""

    def __init__(self) -> None:
        self.store: Dict[str, bytes] = {}
        self.freed: List[int] = []
        self._alloc_refs: Dict[int, Any] = {}
        self.last_error: int = 0

    def CredWriteW(self, pcred: Any, _flags: int) -> int:
        cred = pcred._obj
        size = int(cred.CredentialBlobSize)
        raw = ctypes.string_at(cred.CredentialBlob, size)
        self.store[str(cred.TargetName)] = raw
        self.last_error = 0
        return 1

    def CredReadW(self, target: str, _ctype: int, _flags: int, ppcred: Any) -> int:
        raw = self.store.get(str(target))
        if raw is None:
            self.last_error = win_cred.ERROR_NOT_FOUND
            return 0
        buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        cred = win_cred._CREDENTIALW()
        cred.Type = win_cred.CRED_TYPE_GENERIC
        cred.TargetName = str(target)
        cred.CredentialBlobSize = len(raw)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        ptr = ctypes.pointer(cred)
        addr = ctypes.addressof(cred)
        self._alloc_refs[addr] = (cred, buf)
        ppcred._obj.contents = cred
        # Assign pointer value directly into caller's _PCREDENTIALW
        ctypes.memmove(ctypes.addressof(ppcred._obj), ctypes.addressof(ptr), ctypes.sizeof(ptr))
        self.last_error = 0
        return 1

    def CredDeleteW(self, target: str, _ctype: int, _flags: int) -> int:
        if str(target) in self.store:
            del self.store[str(target)]
            self.last_error = 0
            return 1
        self.last_error = win_cred.ERROR_NOT_FOUND
        return 0

    def CredFree(self, ptr: Any) -> None:
        addr = ctypes.cast(ptr, ctypes.c_void_p).value or 0
        self.freed.append(addr)
        self._alloc_refs.pop(addr, None)


class TestDesktopStateStore(unittest.TestCase):
    def test_window_bounds_and_prefs_persist_and_reject_unknown_or_secret_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mh-state-") as tmp:
            state_path = os.path.join(tmp, "desktop_state.json")
            home_a = os.path.join(tmp, "home-a")
            home_b = os.path.join(tmp, "home-b")
            os.makedirs(home_a)
            os.makedirs(home_b)

            store_a = DesktopStateStore(home_a, path=state_path)
            self.assertIsNone(store_a.load_window_bounds())
            self.assertEqual(store_a.load_prefs(), {"close_to_tray": True, "tray_hint_shown": False})

            ok = store_a.save_window_bounds(x=120, y=80, width=1400, height=900, maximized=False)
            self.assertTrue(ok)
            self.assertEqual(
                store_a.load_window_bounds(),
                {"x": 120, "y": 80, "width": 1400, "height": 900, "maximized": False},
            )

            # Secret / unknown keys must never be persisted
            prefs = store_a.update_prefs(
                {"close_to_tray": False, "passphrase": "super-secret-value", "token": "tok_123"}
            )
            self.assertEqual(prefs, {"close_to_tray": False, "tray_hint_shown": False})
            raw_text = Path(state_path).read_text(encoding="utf-8")
            self.assertNotIn("super-secret-value", raw_text)
            self.assertNotIn("passphrase", raw_text)
            self.assertNotIn("tok_123", raw_text)

            # Scoped per home
            store_b = DesktopStateStore(home_b, path=state_path)
            self.assertNotEqual(home_state_key(home_a), home_state_key(home_b))
            self.assertTrue(store_b.save_window_bounds(x=10, y=20, width=1100, height=720, maximized=True))
            self.assertEqual(store_a.load_window_bounds()["width"], 1400)
            self.assertEqual(store_b.load_window_bounds()["maximized"], True)

    def test_sanitize_window_bounds_rejects_malformed(self) -> None:
        self.assertIsNone(sanitize_window_bounds({"width": 100, "height": 100}))
        self.assertIsNone(sanitize_window_bounds({"width": True, "height": 800}))
        self.assertIsNone(sanitize_window_bounds("bad"))
        self.assertEqual(
            sanitize_window_bounds({"x": 50, "y": 60, "width": 1280, "height": 800, "maximized": 1}),
            {"x": 50, "y": 60, "width": 1280, "height": 800, "maximized": True},
        )


class TestWin32CredVault(unittest.TestCase):
    def test_posix_is_not_supported_by_default(self) -> None:
        if os.name != "nt":
            self.assertFalse(win_cred.is_supported())

    def test_roundtrip_write_read_has_delete_with_mocked_advapi32(self) -> None:
        fake = _FakeAdvapi32()
        home = "/tmp/fake-mesh-home"
        secret = "correct-horse-battery-staple"

        self.assertTrue(win_cred.is_supported(advapi32=fake))
        self.assertFalse(win_cred.has_passphrase(home, advapi32=fake))
        self.assertIsNone(win_cred.read_passphrase(home, advapi32=fake))

        self.assertTrue(win_cred.write_passphrase(home, secret, advapi32=fake))
        self.assertTrue(win_cred.has_passphrase(home, advapi32=fake))
        self.assertEqual(win_cred.read_passphrase(home, advapi32=fake), secret)
        self.assertGreaterEqual(len(fake.freed), 2)

        with mock.patch.object(ctypes, "get_last_error", side_effect=lambda: fake.last_error, create=True):
            self.assertTrue(win_cred.delete_passphrase(home, advapi32=fake))
            self.assertFalse(win_cred.has_passphrase(home, advapi32=fake))
            # Deleting absent credential still returns True (ERROR_NOT_FOUND)
            self.assertTrue(win_cred.delete_passphrase(home, advapi32=fake))


class TestWindowsTrayAndSingleInstance(unittest.TestCase):
    def test_tooltip_and_menu_spec_reflect_daemon_and_window_state(self) -> None:
        st_online_unlocked = {
            "connected": True,
            "auth_verified": True,
            "window_visible": True,
            "pid": 4242,
        }
        tip = format_tray_tooltip(st_online_unlocked)
        self.assertIn("运行中", tip)
        self.assertIn("已解锁", tip)
        self.assertLessEqual(len(tip), 126)

        menu = build_tray_menu_spec(st_online_unlocked)
        by_id = {m["id"]: m for m in menu if m["kind"] == "item"}
        self.assertEqual(by_id[CMD_TOGGLE_SHOW]["label"], "隐藏主窗口")
        self.assertTrue(by_id[CMD_LOCK]["enabled"])
        self.assertTrue(by_id[CMD_STOP_DAEMON]["enabled"])
        self.assertTrue(by_id[CMD_QUIT_ONLY]["enabled"])
        self.assertTrue(by_id[CMD_QUIT_AND_STOP]["enabled"])

        st_offline_hidden = {
            "connected": False,
            "auth_verified": False,
            "window_visible": False,
        }
        menu2 = build_tray_menu_spec(st_offline_hidden)
        by_id2 = {m["id"]: m for m in menu2 if m["kind"] == "item"}
        self.assertEqual(by_id2[CMD_TOGGLE_SHOW]["label"], "显示主窗口")
        self.assertFalse(by_id2[CMD_LOCK]["enabled"])
        self.assertTrue(by_id2[CMD_START_DAEMON]["enabled"])
        self.assertTrue(by_id2[CMD_QUIT_ONLY]["enabled"])
        self.assertFalse(by_id2[CMD_QUIT_AND_STOP]["enabled"])

    def test_tray_quit_stops_nothing_by_default_and_stops_only_when_chosen(self) -> None:
        calls: List[Any] = []
        tray = WindowsTrayController(
            "/tmp/fake-home",
            on_show=lambda: calls.append("show"),
            on_hide=lambda: calls.append("hide"),
            on_toggle_visibility=lambda: calls.append("toggle"),
            on_lock=lambda: calls.append("lock"),
            on_start_daemon=lambda: calls.append("start_daemon"),
            on_stop_daemon=lambda: calls.append("stop_daemon"),
            on_quit=lambda stop_daemon: calls.append(("quit", stop_daemon)),
            get_status=lambda: {"connected": True, "auth_verified": True, "window_visible": True},
        )
        self.assertTrue(tray.dispatch_menu_command(CMD_TOGGLE_SHOW, synchronous=True))
        self.assertTrue(tray.dispatch_menu_command(CMD_LOCK, synchronous=True))
        self.assertTrue(tray.dispatch_menu_command(CMD_QUIT_ONLY, synchronous=True))
        self.assertTrue(tray.dispatch_menu_command(CMD_QUIT_AND_STOP, synchronous=True))
        self.assertEqual(calls, ["toggle", "lock", ("quit", False), ("quit", True)])

    def test_single_instance_guard_activates_existing_window_on_duplicate(self) -> None:
        posted: List[Any] = []
        closed: List[Any] = []

        class _FakeK32:
            def __init__(self, already: bool) -> None:
                self.already = already

            def CreateMutexW(self, _sa: Any, _owner: int, _name: str) -> int:
                return 999

            def CloseHandle(self, h: int) -> int:
                closed.append(h)
                return 1

        class _FakeU32:
            def FindWindowW(self, cls: str, _title: Any) -> int:
                return 777 if cls.startswith("AgentMeshTray_") else 0

            def PostMessageW(self, hwnd: int, msg: int, wp: int, lp: int) -> int:
                posted.append((hwnd, msg, wp, lp))
                return 1

        # First instance succeeds and keeps handle until release()
        g1 = SingleInstanceGuard(
            "/tmp/fake-home",
            kernel32=_FakeK32(False),
            user32=_FakeU32(),
            get_last_error=lambda: 0,
        )
        self.assertTrue(g1.acquire())
        self.assertEqual(posted, [])
        g1.release()
        self.assertEqual(closed, [999])

        # Second instance sees ERROR_ALREADY_EXISTS, signals existing hwnd, returns False
        closed.clear()
        g2 = SingleInstanceGuard(
            "/tmp/fake-home",
            kernel32=_FakeK32(True),
            user32=_FakeU32(),
            get_last_error=lambda: ERROR_ALREADY_EXISTS,
        )
        self.assertFalse(g2.acquire())
        self.assertEqual(closed, [999])
        self.assertEqual(posted, [(777, WM_APP_ACTIVATE, 0, 0)])


if __name__ == "__main__":
    unittest.main()
