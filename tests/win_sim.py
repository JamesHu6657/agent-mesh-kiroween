"""Shared prelude for ``-c`` subprocesses that simulate ``sys.platform == 'win32'``.

Setting ``sys.platform = 'win32'`` also fools stdlib modules gated on it:
on Python 3.12+ ``tempfile`` -> ``shutil`` runs ``import _winapi``, and any
``subprocess``/``asyncio`` imported afterwards reaches for Windows-only
modules too. ``SIMULATED_WIN32_PRELUDE`` poisons ``fcntl`` and installs
stand-ins for the Windows-only stdlib modules (the real module wins wherever
it exists, i.e. on actual Windows), so the hygiene checks still prove
fcntl/posix are never needed under win32.

Modules that must bind for POSIX (e.g. ``asyncio``) have to be imported
*before* the prelude runs.
"""

from __future__ import annotations

SIMULATED_WIN32_PRELUDE = (
    "import sys, types\n"
    "sys.platform = 'win32'\n"
    "sys.modules['fcntl'] = None\n"
    "for _m in ('_winapi', 'msvcrt', 'nt'):\n"
    "    try:\n"
    "        __import__(_m)\n"
    "    except ImportError:\n"
    "        sys.modules.setdefault(_m, types.ModuleType(_m))\n"
)
