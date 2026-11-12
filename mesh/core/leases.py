"""Task lease management and fencing verification per plan §11.5."""

import json
import sqlite3
from typing import Any, Dict, Optional

import mesh.constants as c
from mesh.errors import MeshError


def verify_task_lease_and_fence(
    conn: sqlite3.Connection,
    task_id: str,
    presented_run_id: Optional[str] = None,
    presented_fence: Optional[int] = None,
) -> Dict[str, Any]:
    """Verify stale-write fence and active lease in the exact order specified by plan §11.5.

    Check order (§11.5):
    1. If no active lease exists for task:<task_id> -> exit 9 LEASE_EXPIRED.
    2. If lease exists but presented_fence != lease.fence -> exit 9 FENCE_MISMATCH.
    3. If lease exists and presented_run_id is given but != lease.run_id -> exit 9 FENCE_MISMATCH.

    Returns the lease row as a dictionary on success.
    """
    lease_key = f"task:{task_id}"
    cur = conn.execute("SELECT * FROM leases WHERE key = ?;", (lease_key,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.LEASE_EXPIRED,
            exit_code=c.EXIT_STALE,  # 9
            message=f"No active lease held for task '{task_id}'",
            details={"task_id": task_id, "lease_key": lease_key},
        )

    lease_dict = dict(row)
    lease_fence = lease_dict.get("fence")
    lease_run_id = lease_dict.get("run_id")

    if presented_fence is not None and presented_fence != lease_fence:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,  # 9
            message=f"Fence mismatch on task '{task_id}': presented fence {presented_fence} does not match current lease fence {lease_fence}",
            details={"task_id": task_id, "presented_fence": presented_fence, "current_fence": lease_fence},
        )

    if presented_run_id is not None and presented_run_id != lease_run_id:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,  # 9
            message=f"Run mismatch on task '{task_id}': presented run '{presented_run_id}' does not match current lease run '{lease_run_id}'",
            details={"task_id": task_id, "presented_run_id": presented_run_id, "current_run_id": lease_run_id},
        )

    return lease_dict


def get_lease_ttl_s(conn: sqlite3.Connection) -> int:
    """Retrieve lease.ttl_s setting from database (default 120s)."""
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'lease.ttl_s';")
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return int(json.loads(row["value_json"]))
        except Exception:
            return 120
    return 120
