"""Locations a credential file must never live in: the configured workspaces.root and
every registered project repository (Linear round 4: shared by `mesh setup` (write side)
and the client (read side))."""

import json
import os
import sqlite3
from typing import List, Optional, Tuple

import mesh.constants as c
from mesh.core.db import sqlite_ro_uri
from mesh.errors import MeshError


def configured_roots(home: str) -> Tuple[Optional[str], List[str]]:
    """(workspaces.root or None, registered repo paths) read from ``<home>/mesh.db``.

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
                if isinstance(val, str) and val:
                    ws = os.path.expanduser(val)
            repos = [rp for (rp,) in conn.execute(
                "SELECT DISTINCT repo_path FROM projects WHERE repo_path IS NOT NULL;") if rp]
            return ws, repos
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
