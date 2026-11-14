"""Locations a credential file must never live in: the configured workspaces.root and
every registered project repository (Linear round 4: shared by `mesh setup` (write side)
and the client (read side))."""

import hashlib
import json
import os
import sqlite3
from typing import List, Optional, Tuple

import mesh.constants as c
from mesh.core.db import sqlite_ro_uri
from mesh.errors import MeshError


PROTECTED_PATH_META_PREFIX = "protected_path:"


def remember_protected_path(conn: sqlite3.Connection, path: Optional[str]) -> None:
    """Record a workspace root / task workspace path unredacted in ``meta``.

    Events are redacted before storage (plan §15.2), so a path that happens to match a
    redaction pattern (e.g. ``/srv/secret=x/ws``) is unusable when read back from
    ``config.updated`` / ``task.retried`` payloads. This side record keeps the real path
    so the token guard keeps protecting it after a root change (dogfood P3).
    """
    if not isinstance(path, str) or not path.strip():
        return
    norm = os.path.abspath(path.strip())
    key = PROTECTED_PATH_META_PREFIX + hashlib.sha256(norm.encode("utf-8")).hexdigest()
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?);", (key, norm))


def configured_roots(home: str) -> Tuple[Optional[str], List[str]]:
    """(workspaces.root or None, protected paths) read from ``<home>/mesh.db``.

    workspaces.root is normalised exactly as at runtime (``get_workspaces_root``: strip,
    abspath). Protected paths are the registered repositories plus the project directory
    of every persisted task workspace, so a workspace created under an earlier root stays
    protected after the root changes (Linear round 5 P2).

    No database yet -> (None, []). A database that exists but cannot be inspected
    fails closed with TOKEN_FILE_INSECURE rather than silently protecting nothing.
    """
    db = os.path.join(home, "mesh.db")
    if not os.path.exists(db):
        return None, []
    try:
        conn = sqlite3.connect(sqlite_ro_uri(db), uri=True, timeout=5.0)
        try:
            ws: Optional[str] = None
            row = conn.execute("SELECT value_json FROM settings WHERE key = 'workspaces.root';").fetchone()
            if row and row[0]:
                val = json.loads(row[0])
                if isinstance(val, str) and val.strip():
                    ws = os.path.abspath(val.strip())
            paths = [rp for (rp,) in conn.execute(
                "SELECT DISTINCT repo_path FROM projects WHERE repo_path IS NOT NULL;") if rp]
            # Linear round 6 P2: every workspaces.root ever configured (config.updated events
            # are append-only), so lead-only workspaces and earlier attempts under an old root
            # stay protected after the root changes.
            for (pj,) in conn.execute(
                    "SELECT payload_json FROM events WHERE type = 'config.updated';"):
                try:
                    pl = json.loads(pj or "{}")
                except ValueError:
                    continue
                v = pl.get("value") if isinstance(pl, dict) and pl.get("key") == "workspaces.root" else None
                if isinstance(v, str) and v.strip():
                    paths.append(os.path.abspath(v.strip()))
            # Unredacted side record (remember_protected_path): event payloads above are
            # redacted, so a root like /srv/secret=x/ws reads back as "[REDACTED]".
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta';").fetchone()
            if has_meta:
                for (mv,) in conn.execute("SELECT value FROM meta WHERE key LIKE ?;",
                                          (PROTECTED_PATH_META_PREFIX + "%",)):
                    if isinstance(mv, str) and mv.strip():
                        paths.append(os.path.abspath(mv.strip()))
            # Every task workspace path, current and from earlier attempts (reassign/retry
            # payloads). The full path is kept as well as its project directory: the check
            # resolves each with realpath, so a symlinked workspace's real target is covered.
            wps = [wp for (wp,) in conn.execute(
                "SELECT DISTINCT workspace_path FROM tasks WHERE workspace_path IS NOT NULL;") if wp]
            for (pj,) in conn.execute(
                    "SELECT payload_json FROM events WHERE payload_json LIKE '%workspace_path%';"):
                try:
                    pl = json.loads(pj or "{}")
                except ValueError:
                    continue
                if isinstance(pl, dict):
                    if isinstance(pl.get("workspace_path"), str) and pl["workspace_path"]:
                        wps.append(pl["workspace_path"])
            for wp in wps:
                paths.append(wp)
                # <root>/<prj>/tasks/<tsk>[.aN]/ -> <root>/<prj> (also covers lead/ dirs)
                parent = os.path.dirname(os.path.normpath(wp))
                if os.path.basename(parent) == "tasks":
                    paths.append(os.path.dirname(parent))
            return ws, sorted(set(paths))
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise MeshError(
            code=c.TOKEN_FILE_INSECURE,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message=(f"Cannot read the configured workspace root and repositories from {db} ({exc}); "
                     "refusing to use a token file without that check"),
            details={"db": db},
        )
