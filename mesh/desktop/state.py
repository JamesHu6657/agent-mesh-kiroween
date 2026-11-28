"""Non-sensitive desktop UI & window geometry persistence (S6).

Stores strictly non-secret UI preferences (window bounds, maximized flag,
close-to-tray preference) in a user-private JSON file completely separate from
``mesh.db``. Passphrases, tokens, and daemon credentials are NEVER stored here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import threading
from typing import Any, Dict, Optional

from mesh.desktop.i18n import normalize_language_pref
from mesh.platform import perms
from mesh.platform.fs import atomic_replace

_ALLOWED_WINDOW_KEYS = frozenset({"x", "y", "width", "height", "maximized"})
_ALLOWED_PREF_KEYS = frozenset({"close_to_tray", "tray_hint_shown"})
_ALLOWED_STR_PREF_KEYS = frozenset({"language"})


def default_desktop_state_path() -> str:
    """Return the per-user non-sensitive desktop UI state file path."""
    if sys.platform == "win32":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_STATE_HOME")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
        return os.path.join(base, "agent-mesh", "desktop_state.json")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "agent-mesh", "desktop_state.json")


def home_state_key(home: str) -> str:
    """Stable hash key isolating UI preferences per ``MESH_HOME``."""
    try:
        real = os.path.realpath(home)
    except OSError:
        real = os.path.abspath(home)
    norm = os.path.normpath(real)
    if sys.platform == "win32":
        norm = norm.lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _finite_int(val: Any) -> Optional[int]:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float) and math.isfinite(val):
        return int(round(val))
    return None


def sanitize_window_bounds(bounds: Any) -> Optional[Dict[str, Any]]:
    """Validate and sanitize a window bounds dict, returning None if malformed."""
    if not isinstance(bounds, dict):
        return None
    x = _finite_int(bounds.get("x"))
    y = _finite_int(bounds.get("y"))
    w = _finite_int(bounds.get("width"))
    h = _finite_int(bounds.get("height"))
    maximized = bool(bounds.get("maximized", False))
    if w is None or h is None or w < 320 or h < 240 or w > 16384 or h > 16384:
        return None
    out: Dict[str, Any] = {
        "width": w,
        "height": h,
        "maximized": maximized,
    }
    if x is not None and -32768 <= x <= 32768 and y is not None and -32768 <= y <= 32768:
        out["x"] = x
        out["y"] = y
    return out


class DesktopStateStore:
    """Thread-safe atomic store for non-sensitive desktop window/UI state."""

    def __init__(self, home: str, *, path: Optional[str] = None) -> None:
        self.home = home
        self.home_key = home_state_key(home)
        self.path = path or default_desktop_state_path()
        self._lock = threading.Lock()

    def _read_raw_locked(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                doc = json.load(fp)
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError, TypeError):
            pass
        return {}

    def _write_raw_locked(self, doc: Dict[str, Any]) -> bool:
        parent = os.path.dirname(os.path.abspath(self.path))
        try:
            existed = os.path.isdir(parent)
            os.makedirs(parent, mode=0o700, exist_ok=True)
            if not existed and os.name == "nt":
                try:
                    perms.set_private_dir_acl(parent, 0o700)
                except (OSError, AttributeError):
                    pass
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".desktop-state-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fp:
                    json.dump(doc, fp, indent=2, sort_keys=True)
                    fp.write("\n")
                if os.name == "nt":
                    try:
                        perms.set_private_file_acl(tmp, 0o600)
                    except (OSError, AttributeError):
                        pass
                else:
                    os.chmod(tmp, 0o600)
                atomic_replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except OSError:
            return False

    def load_window_bounds(self) -> Optional[Dict[str, Any]]:
        """Load saved window geometry for this ``MESH_HOME`` (or global fallback)."""
        with self._lock:
            doc = self._read_raw_locked()
        by_home = doc.get("by_home")
        if isinstance(by_home, dict):
            entry = by_home.get(self.home_key)
            if isinstance(entry, dict):
                clean = sanitize_window_bounds(entry.get("window"))
                if clean is not None:
                    return clean
        return sanitize_window_bounds(doc.get("window"))

    def save_window_bounds(
        self,
        *,
        x: Optional[int],
        y: Optional[int],
        width: int,
        height: int,
        maximized: bool = False,
    ) -> bool:
        """Persist sanitized window bounds atomically."""
        clean = sanitize_window_bounds(
            {"x": x, "y": y, "width": width, "height": height, "maximized": bool(maximized)}
        )
        if clean is None:
            return False
        with self._lock:
            doc = self._read_raw_locked()
            by_home = doc.get("by_home")
            if not isinstance(by_home, dict):
                by_home = {}
            entry = by_home.get(self.home_key)
            if not isinstance(entry, dict):
                entry = {}
            entry["window"] = clean
            by_home[self.home_key] = entry
            doc["version"] = 1
            doc["window"] = clean
            doc["by_home"] = by_home
            return self._write_raw_locked(doc)

    def load_prefs(self) -> Dict[str, Any]:
        """Return sanitized non-sensitive desktop preferences with safe defaults."""
        prefs: Dict[str, Any] = {
            "close_to_tray": True,
            "tray_hint_shown": False,
            "language": "auto",
        }
        with self._lock:
            doc = self._read_raw_locked()
        by_home = doc.get("by_home")
        raw_prefs: Optional[Dict[str, Any]] = None
        scoped_prefs = False
        if isinstance(by_home, dict) and isinstance(by_home.get(self.home_key), dict):
            cand = by_home[self.home_key].get("prefs")
            if isinstance(cand, dict):
                raw_prefs = cand
                scoped_prefs = True
        if raw_prefs is None and isinstance(doc.get("prefs"), dict):
            raw_prefs = doc["prefs"]
        if isinstance(raw_prefs, dict):
            for key in _ALLOWED_PREF_KEYS:
                if key in raw_prefs and isinstance(raw_prefs[key], bool):
                    prefs[key] = raw_prefs[key]
            if scoped_prefs and "language" in raw_prefs:
                norm_lang = normalize_language_pref(raw_prefs["language"])
                if norm_lang is not None:
                    prefs["language"] = norm_lang
        return prefs

    def update_prefs(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Persist whitelisted non-sensitive desktop preferences and return the resulting prefs."""
        with self._lock:
            doc = self._read_raw_locked()
            by_home = doc.get("by_home")
            if not isinstance(by_home, dict):
                by_home = {}
            entry = by_home.get(self.home_key)
            if not isinstance(entry, dict):
                entry = {}
            cur = entry.get("prefs")
            prefs: Dict[str, Any] = {
                "close_to_tray": True,
                "tray_hint_shown": False,
                "language": "auto",
            }
            if isinstance(cur, dict):
                for key in _ALLOWED_PREF_KEYS:
                    if key in cur and isinstance(cur[key], bool):
                        prefs[key] = cur[key]
                if "language" in cur:
                    norm_lang = normalize_language_pref(cur["language"])
                    if norm_lang is not None:
                        prefs["language"] = norm_lang
            if isinstance(updates, dict):
                for key, val in updates.items():
                    if key in _ALLOWED_PREF_KEYS and isinstance(val, bool):
                        prefs[key] = val
                    elif key in _ALLOWED_STR_PREF_KEYS and key == "language":
                        norm_lang = normalize_language_pref(val)
                        if norm_lang is not None:
                            prefs["language"] = norm_lang
            entry["prefs"] = prefs
            by_home[self.home_key] = entry
            doc["version"] = 1
            global_prefs = {key: prefs[key] for key in _ALLOWED_PREF_KEYS if key in prefs}
            doc["prefs"] = global_prefs
            doc["by_home"] = by_home
            self._write_raw_locked(doc)
            return dict(prefs)
