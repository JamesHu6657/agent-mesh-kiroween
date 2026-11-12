"""Cheap environment checks for the default `mesh doctor` report (productize).

Nothing here starts a process other than reading `git --version`; vendor CLIs are only
looked up on PATH, never executed (run `mesh adapter probe` for a live probe).
"""

import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional

from mesh.core.db import sqlite_ro_uri


def _check(name: str, ok: bool, detail: str, hint: Optional[str] = None, level: str = "error") -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
    if not ok:
        out["level"] = level
        if hint:
            out["hint"] = hint
    return out


def _daemon_running(home_dir: str) -> Optional[bool]:
    sock_path = os.path.join(home_dir, "run", "mesh.sock")
    if not os.path.exists(sock_path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _setting(db_path: str, key: str) -> Any:
    try:
        conn = sqlite3.connect(sqlite_ro_uri(db_path), uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row and row[0] is not None else None
    except (sqlite3.Error, ValueError):
        return None


def environment_checks(home_dir: str, db_present: bool, testing_mode: bool = False) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    plat = platform.system()
    checks.append(_check("platform", plat == "Linux", f"{plat} {platform.release()}",
                         "Agent Mesh supports Linux only (it relies on /proc, Unix sockets and process groups)"))
    v = sys.version_info
    checks.append(_check("python", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
                         "Python 3.11 or newer is required"))
    git = shutil.which("git")
    gver = ""
    if git:
        try:
            gver = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=5,
                                  stdin=subprocess.DEVNULL).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            gver = git
    checks.append(_check("git", bool(git), gver or "not found on PATH",
                         "install git; repo projects and task worktrees need it", level="warning"))
    checks.append(_check("initialized", db_present, os.path.join(home_dir, "mesh.db"),
                         "run `mesh setup` (or `mesh init`)"))
    running = _daemon_running(home_dir) if db_present else False
    checks.append(_check("daemon", bool(running), "running" if running else "not running",
                         "start it with `mesh daemon start`", level="warning"))

    mode = None
    if db_present:
        mode = _setting(os.path.join(home_dir, "mesh.db"), "adapter.evidence_mode")
    mode = mode or ("strict" if testing_mode else "warn")
    from mesh.adapters.registry import list_adapters

    found = []
    for adapter in list_adapters(testing_mode=testing_mode):
        aid = adapter.id
        if getattr(adapter, "kind", "vendor") != "vendor":
            continue
        path = adapter.resolve_binary(None)
        if path:
            found.append(f"{aid} ({path})")
    checks.append(_check("agent_clis", True,
                         ("found: " + ", ".join(found)) if found else
                         "no supported agent CLI on PATH (inbox agents still work; see docs/examples/)"))
    checks.append(_check("evidence_mode", True,
                         f"{mode}: " + ("vendor adapters launch without matching smoke evidence (warning only)"
                                        if mode == "warn" else
                                        "vendor adapters launch only with matching committed smoke evidence")))
    return checks
