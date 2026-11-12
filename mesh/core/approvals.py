"""Approvals lifecycle, op hashing, MAC verification, and policies per plan §7.4."""

import hashlib
import hmac
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import mesh.constants as c
from mesh.core.canonical import canonical_json, compute_op_hash
from mesh.core.clock import now_iso_utc, now_utc
from mesh.core.events import record_event
from mesh.core.ids import generate_id
from mesh.errors import MeshError

EXCLUDED_OP_PARAMS = {"idempotency_key", "dry_run", "approval", "format", "if_version"}

DEFAULT_POLICIES = {
    "config": "direct_only",
    "lead_change": "direct_or_relayed",
    "project_control": "direct_or_relayed",
    "scope_change": "direct_or_relayed",
    "permission_escalation": "direct_only",
    "retry_over_limit": "direct_or_relayed",
    "delete": "direct_or_relayed",
    "deliver_to_base": "not_required",
    "spend_money": "direct_or_relayed",
    "deploy": "direct_or_relayed",
    "delete_external": "direct_or_relayed",
    "external_other": "direct_or_relayed",
}


def get_approval_policy(conn: sqlite3.Connection, category: str) -> str:
    """Retrieve effective approval policy for a category."""
    if category == "config":
        return "direct_only"
    key = f"approval.policy.{category}"
    cur = conn.execute("SELECT value_json FROM settings WHERE key = ?;", (key,))
    row = cur.fetchone()
    if row and row["value_json"]:
        try:
            return json.loads(row["value_json"])
        except Exception:
            pass
    return DEFAULT_POLICIES.get(category, "direct_or_relayed")


def compute_user_approval_mac(k_user: bytes, approval_id: str, op_hash: str, decision: str, decided_at: str) -> str:
    """Compute HMAC-SHA256 user MAC for direct approval decisions per plan §7.4."""
    msg = f"mesh-approval-v1\n{approval_id}\n{op_hash}\n{decision}\n{decided_at}".encode("utf-8")
    return hmac.new(k_user, msg, hashlib.sha256).hexdigest()


def create_or_get_pending_approval(
    conn: sqlite3.Connection,
    category: str,
    op: str,
    requested_by: str,
    params: Dict[str, Any],
    summary: str,
    project_id: Optional[str] = None,
    target_id: Optional[str] = None,
    task_id: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    actor_kind: str = "agent",
    via: str = "token",
    request_id: Optional[str] = None,
    k_user: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Create a pending approval row or return existing open approval with matching op_hash."""
    op_doc, op_hash = compute_op_hash(
        op=op,
        requested_by=requested_by,
        params=params,
        project_id=project_id,
        target_id=target_id,
        category=category,
    )
    if dry_run:
        return {
            "approval_id": None,
            "op_hash": op_hash,
            "category": category,
            "summary": summary,
            "expires_at": None,
        }

    # Check for existing open approval (unique index on op_hash where status in pending, approved)
    cur = conn.execute(
        "SELECT id, op_hash, category, summary, expires_at, status FROM approvals WHERE op_hash = ? AND status IN ('pending', 'approved');",
        (op_hash,),
    )
    row = cur.fetchone()
    if row:
        return {
            "approval_id": row["id"],
            "op_hash": row["op_hash"],
            "category": row["category"],
            "summary": row["summary"],
            "expires_at": row["expires_at"],
            "status": row["status"],
        }

    # Fetch TTL setting if not passed
    if ttl_seconds is None:
        cur_ttl = conn.execute("SELECT value_json FROM settings WHERE key = 'approval.ttl_seconds';")
        ttl_row = cur_ttl.fetchone()
        if ttl_row and ttl_row["value_json"]:
            try:
                ttl_seconds = int(json.loads(ttl_row["value_json"]))
            except Exception:
                ttl_seconds = 86400
        else:
            ttl_seconds = 86400

    import time
    now_ts = time.time()
    requested_at = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_seconds)
    apr_id = generate_id("apr")

    conn.execute(
        """INSERT INTO approvals (
            id, project_id, task_id, category, op_type, op_json, op_hash,
            summary, requested_by, requested_at, expires_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending');""",
        (
            apr_id,
            project_id,
            task_id,
            category,
            op,
            json.dumps(op_doc),
            op_hash,
            summary,
            requested_by,
            requested_at,
            expires_at,
        ),
    )

    record_event(
        conn=conn,
        type_="approval.requested",
        actor_id=requested_by,
        actor_kind=actor_kind,
        via=via,
        project_id=project_id,
        task_id=task_id,
        approval_id=apr_id,
        payload={
            "approval_id": apr_id,
            "category": category,
            "op": op,
            "summary": summary,
            "expires_at": expires_at,
            "op_hash": op_hash,
        },
        request_id=request_id,
        k_user=k_user,
    )

    return {
        "approval_id": apr_id,
        "op_hash": op_hash,
        "category": category,
        "summary": summary,
        "expires_at": expires_at,
        "status": "pending",
    }


def check_and_consume_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    op: str,
    category: str,
    caller_id: str,
    params: Dict[str, Any],
    project_id: Optional[str] = None,
    target_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify and consume an approval when re-executing with --approval."""
    cur = conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    # Recompute op_hash from incoming parameters using expected gate category and CALLER as requested_by (plan §7.4 step 4)
    _, computed_hash = compute_op_hash(
        op=op,
        requested_by=caller_id,
        params=params,
        project_id=project_id,
        target_id=target_id,
        category=category,
    )

    if computed_hash != row["op_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Operation parameters do not match approved operation hash",
            details={"approval_id": approval_id, "expected_hash": row["op_hash"], "computed_hash": computed_hash},
        )

    now = now_iso_utc()
    if row["status"] == "expired" or row["expires_at"] < now:
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Approval has expired",
            details={"approval_id": approval_id, "expires_at": row["expires_at"]},
        )

    status = row["status"]
    if status == "pending":
        raise MeshError(
            code=c.APPROVAL_REQUIRED,
            exit_code=c.EXIT_APPROVAL_REQUIRED,
            message=f"Approval '{approval_id}' is still pending decision",
            details={"approval_id": approval_id, "op_hash": row["op_hash"]},
        )
    elif status == "consumed":
        raise MeshError(
            code=c.APPROVAL_CONSUMED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has already been consumed",
            details={"approval_id": approval_id, "consumed_at": row["consumed_at"]},
        )
    elif status == "rejected":
        raise MeshError(
            code=c.APPROVAL_REJECTED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was rejected",
            details={"approval_id": approval_id, "decided_at": row["decided_at"]},
        )
    elif status == "cancelled":
        raise MeshError(
            code=c.APPROVAL_CANCELLED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was cancelled",
            details={"approval_id": approval_id},
        )
    elif status != "approved":
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' is in invalid state '{status}'",
            details={"approval_id": approval_id, "status": status},
        )

    if row["decided_via"] not in ("direct", "relayed"):
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has invalid decided_via '{row['decided_via']}'",
            details={"approval_id": approval_id, "decided_via": row["decided_via"]},
        )

    if not row["decided_at"]:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has missing decided_at timestamp",
            details={"approval_id": approval_id},
        )

    if row["decided_via"] == "direct" and not row["user_mac"]:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' direct decision is missing user_mac",
            details={"approval_id": approval_id},
        )

    # Policy channel check against expected gate category
    policy = get_approval_policy(conn, category)
    if row["decided_via"] == "relayed" and policy == "direct_only":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Category '{category}' requires direct approval; relayed decision disallowed",
            details={"category": category, "policy": policy},
        )

    # Consume
    conn.execute(
        """UPDATE approvals SET
            status = 'consumed',
            consumed_at = ?,
            consumed_by = ?,
            consumed_request_id = ?
        WHERE id = ?;""",
        (now, caller_id, request_id, approval_id),
    )
    return dict(row)
