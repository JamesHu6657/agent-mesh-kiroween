"""Cheap environment checks for the default `mesh doctor` report (productize).

Nothing here starts a process other than reading `git --version`; vendor CLIs are only
looked up on PATH, never executed (run `mesh adapter probe` for a live probe).
"""

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional

from mesh.core.db import sqlite_ro_uri

MIN_RECOMMENDED_GIT = (2, 36)
MIN_FALLBACK_GIT = (2, 31)


def _check(name: str, ok: bool, detail: str, hint: Optional[str] = None, level: str = "error") -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": name, "ok": bool(ok), "detail": detail}
    if not ok:
        out["level"] = level
        if hint:
            out["hint"] = hint
    return out


def _daemon_running(home_dir: str) -> Optional[bool]:
    from mesh.platform import ipc

    return ipc.probe_daemon_endpoint(home_dir, timeout=1.0)


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


def _scheduler_errors_check(home_dir: str) -> Dict[str, Any]:
    """Errors the running meshd's scheduler hit since it started ($home/run/scheduler_errors.json)."""
    path = os.path.join(home_dir, "run", "scheduler_errors.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = int(data.get("count") or 0)
        last = data.get("last") or {}
    except (OSError, ValueError, TypeError, AttributeError):
        return _check("scheduler_errors", True, "none since meshd started")
    if count <= 0:
        return _check("scheduler_errors", True, "none since meshd started")
    item = f" on {last.get('item')}" if last.get("item") else ""
    return _check("scheduler_errors", False,
                  f"{count} since meshd started; last: {last.get('step')}{item}: {last.get('error')} at {last.get('at')}",
                  "see the traceback in $MESH_HOME/logs/meshd.log; `mesh daemon status` shows the counts per step",
                  level="warning")


def environment_checks(home_dir: str, db_present: bool, testing_mode: bool = False) -> List[Dict[str, Any]]:
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
    from mesh.core.gitops import resolve_git_binary

    git, git_source = resolve_git_binary()
    gver = ""
    git_ok = False
    if git:
        try:
            gp = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=5,
                                stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
            gver = gp.stdout.strip()
            git_ok = gp.returncode == 0 and gver.startswith("git version")
            if not git_ok:
                gver = f"{git} does not work (git --version exit {gp.returncode})"
        except (OSError, subprocess.SubprocessError) as exc:
            gver = f"{git} does not run ({exc.__class__.__name__})"
    git_detail = (
        f"{gver} (bundled MinGit)" if (git_ok and git_source == "bundled_mingit") else (gver or "not found on PATH")
    )
    checks.append(_check("git", git_ok, git_detail,
                         "install git; repo projects and task worktrees need it", level="warning"))
    if git_ok:
        m = re.match(r"git version (\d+)\.(\d+)", gver)
        if m:
            gv = (int(m.group(1)), int(m.group(2)))
            if gv >= MIN_FALLBACK_GIT:
                checks.append(_check("git_version", gv >= MIN_RECOMMENDED_GIT, f"{gv[0]}.{gv[1]}",
                                     "git 2.36 or newer is recommended (`git worktree list -z`); "
                                     "git 2.31 to 2.35 uses the newline-delimited fallback", level="warning"))
            else:
                checks.append(_check("git_version", False, f"{gv[0]}.{gv[1]} (older than 2.31)",
                                     "git older than 2.31 is not supported: `git worktree list --porcelain` "
                                     "has no `prunable` line there, so a removed worktree that was not pruned "
                                     "blocks `mesh project deliver` until `git worktree prune`, and Mesh cannot "
                                     "turn `core.fsmonitor` off for its git calls (GIT_CONFIG_COUNT needs git "
                                     "2.31); upgrade to git 2.36 or newer", level="warning"))
    checks.append(_check("initialized", db_present, os.path.join(home_dir, "mesh.db"),
                         "run `mesh setup` (or `mesh init`)"))
    running = _daemon_running(home_dir) if db_present else False
    checks.append(_check("daemon", bool(running), "running" if running else "not running",
                         "start it with `mesh daemon start`", level="warning"))
    checks.append(_scheduler_errors_check(home_dir))

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
