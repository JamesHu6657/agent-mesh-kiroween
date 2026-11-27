"""Lightweight i18n helpers for the Agent Mesh desktop client (Python side).

Supports ``"en"`` (English) and ``"zh-CN"`` (Simplified Chinese), plus the
``"auto"`` preference which follows ``MESH_UI_LANG`` or the OS UI locale.
Strictly standard-library only.
"""

from __future__ import annotations

import ctypes
import locale
import os
import sys
from typing import Optional

VALID_LANGUAGE_PREFS = ("auto", "en", "zh-CN")
SUPPORTED_LANGUAGES = ("en", "zh-CN")

# Windows primary language ID for Chinese (LANG_CHINESE = 0x04)
_WIN32_PRIMARY_LANG_CHINESE = 0x04


def normalize_language_pref(raw: object) -> Optional[str]:
    """Normalize a language preference to ``'auto'``, ``'en'``, or ``'zh-CN'``.

    Returns ``None`` for invalid or non-string inputs.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    low = s.lower().replace("_", "-")
    if low == "auto":
        return "auto"
    if low == "en" or low.startswith("en-"):
        return "en"
    if low == "zh" or low.startswith("zh-"):
        return "zh-CN"
    return None


def _detect_win32_ui_language() -> Optional[str]:
    """Read the current Windows user UI language without mutating shared ``ctypes.windll``."""
    if sys.platform != "win32":
        return None
    try:
        win_dll_cls = getattr(ctypes, "WinDLL", None)
        if not callable(win_dll_cls):
            return None
        kernel32 = win_dll_cls("kernel32", use_last_error=True)
        getter = getattr(kernel32, "GetUserDefaultUILanguage", None)
        if not callable(getter):
            return None
        getter.argtypes = []
        getter.restype = ctypes.c_uint16
        lang_id = int(getter())
        if lang_id == 0:
            return None
        primary_lang = lang_id & 0x03FF
        return "zh-CN" if primary_lang == _WIN32_PRIMARY_LANG_CHINESE else "en"
    except Exception:
        return None


def detect_system_language() -> str:
    """Detect effective UI language (``'en'`` or ``'zh-CN'``) from env or OS locale."""
    env_override = normalize_language_pref(os.environ.get("MESH_UI_LANG"))
    if env_override in SUPPORTED_LANGUAGES:
        return env_override

    win_lang = _detect_win32_ui_language()
    if win_lang in SUPPORTED_LANGUAGES:
        return win_lang

    for env_key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (os.environ.get(env_key) or "").strip()
        if not val:
            continue
        low = val.lower().replace("_", "-")
        if low in ("c", "posix") or low.startswith("c.") or low.startswith("posix."):
            return "en"
        if low.startswith("zh"):
            return "zh-CN"
        return "en"

    try:
        loc, _enc = locale.getlocale()
        if isinstance(loc, str) and loc.strip():
            low = loc.strip().lower().replace("_", "-")
            if low.startswith("zh"):
                return "zh-CN"
            return "en"
    except Exception:
        pass

    return "en"


def resolve_effective_language(pref: Optional[str] = None) -> str:
    """Resolve a stored preference (``'auto'`` / ``'en'`` / ``'zh-CN'``) to ``'en'`` or ``'zh-CN'``."""
    norm = normalize_language_pref(pref) if pref is not None else "auto"
    if norm in SUPPORTED_LANGUAGES:
        return norm
    return detect_system_language()


def tr(lang: Optional[str], zh: str, en: str) -> str:
    """Return ``en`` when ``lang`` resolves to English, otherwise ``zh``."""
    return en if resolve_effective_language(lang) == "en" else zh
