"""Approval command handlers per plan §7.4 and §13.3."""

import hashlib
import json
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.core.redact import redact
import mesh.constants as c
from mesh.core.approvals import (
    DEFAULT_POLICIES,
    compute_user_approval_mac,
    create_or_get_pending_approval,
    get_approval_policy,
)
from mesh.core.authz import can_principal_read_project, check_authz, is_project_lead, lead_run_token_scope
from mesh.core.canonical import canonical_json, check_no_floats
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import record_event
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _check_approval_run_scope(ctx: HandlerContext, row: sqlite3.Row) -> None:
    """Confine approval IDs before requester, project-read, or relay authority."""
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None and row["project_id"] != scope_project:
        raise MeshError(
            code=c.SCOPE_MISMATCH,
            exit_code=c.EXIT_FORBIDDEN,
            message="Lead run token cannot access an approval outside its run scope",
            details={"approval_id": row["id"], "project_id": row["project_id"],
                     "scope_project_id": scope_project},
        )


def _handle_approval_decision(ctx: HandlerContext, decision: str) -> HandlerResult:
    """Decide (approve or reject) a pending approval request per plan §7.3 and §7.4."""
    approval_id = ctx.args.get("approval_id")
    op_hash = ctx.args.get("op_hash")
    note = ctx.args.get("note")

    if not approval_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="approval_id is required",
        )
    if not op_hash:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--op-hash is required",
            details={"param": "op_hash"},
        )

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    # Auth check order per plan §7.3:
    # (1) agent caller without --relayed -> 4 CAPABILITY_MISSING
    if ctx.actor_kind != "user" and ctx.via != "relayed":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Agent caller must use --relayed for approval decisions",
            details={"actor_kind": ctx.actor_kind, "via": ctx.via},
        )

    # (2) --relayed from principal without relay, or from any run token -> 4 RELAY_NOT_ALLOWED
    if ctx.via == "relayed":
        if ctx.via == "run_token" or ctx.actor_kind == "run_token":
            raise MeshError(
                code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
                exit_code=c.EXIT_FORBIDDEN,
                message="Executor run tokens cannot decide approvals",
            )
        caps = ctx.principal.get("capabilities", [])
        if "relay" not in caps:
            raise MeshError(
                code=c.RELAY_NOT_ALLOWED,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Principal '{ctx.principal.get('name')}' lacks 'relay' capability",
                details={"principal": ctx.principal.get("name"), "missing": "relay"},
            )

        # (3) relayer is approval's requester or current lead of its project -> 4 SELF_APPROVAL
        if ctx.principal["id"] == row["requested_by"]:
            raise MeshError(
                code=c.SELF_APPROVAL,
                exit_code=c.EXIT_FORBIDDEN,
                message="Requester cannot decide its own request",
                details={"requested_by": row["requested_by"], "caller": ctx.principal["id"]},
            )
        if row["project_id"] and is_project_lead(ctx.conn, row["project_id"], ctx.principal["id"]):
            raise MeshError(
                code=c.SELF_APPROVAL,
                exit_code=c.EXIT_FORBIDDEN,
                message="Project lead cannot decide requests for its own project",
                details={"project_id": row["project_id"], "caller": ctx.principal["id"]},
            )

        # (4) category policy is direct_only -> 12 CHANNEL_NOT_ALLOWED
        policy = get_approval_policy(ctx.conn, row["category"])
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message=f"Category '{row['category']}' requires direct user approval; relayed decision disallowed",
                details={"category": row["category"], "policy": policy},
            )

    # (5) op_hash check:
    # First, verify stored op_json re-hash to detect DB corruption (defect d and plan §7.4 step 3)
    try:
        stored_op_doc = json.loads(row["op_json"])
        recomputed_hash = "sha256:" + hashlib.sha256(canonical_json(stored_op_doc)).hexdigest()
    except Exception:
        recomputed_hash = None
    if recomputed_hash != row["op_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Database corruption detected: stored op_json does not match stored op_hash",
            details={"stored": row["op_hash"], "recomputed": recomputed_hash},
        )

    if op_hash != row["op_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Provided op-hash does not match stored approval op-hash",
            details={"expected": row["op_hash"], "got": op_hash},
        )

    # (6) State checks:
    now = now_iso_utc()
    status = row["status"]

    if row["expires_at"] < now or status == "expired":
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has expired",
            details={"approval_id": approval_id, "expires_at": row["expires_at"]},
        )
    if status == "consumed":
        raise MeshError(
            code=c.APPROVAL_CONSUMED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was already consumed",
            details={"approval_id": approval_id},
        )
    if status == "cancelled":
        raise MeshError(
            code=c.APPROVAL_CANCELLED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was cancelled",
            details={"approval_id": approval_id},
        )

    # Idempotency: same decision again -> rc 0, already_decided: true, no new event
    if status == decision:
        decided_via = row["decided_via"]
        return HandlerResult(data={
            "approval_id": approval_id,
            "status": decision,
            "already_decided": True,
            "decided_via": decided_via,
            "trust": "direct_verified" if decided_via == "direct" else "relayed_unverified",
            "user_mac": row["user_mac"],
        })

    # Conflict with opposite decision
    if (decision == "approved" and status == "rejected") or (decision == "rejected" and status == "approved"):
        raise MeshError(
            code=c.DECISION_CONFLICT,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' was already {status}; cannot {decision}",
            details={"approval_id": approval_id, "status": status},
        )

    if ctx.via == "relayed":
        user_mac = None
        decided_via = "relayed"
        ctx.conn.execute(
            """UPDATE approvals SET
                status = ?,
                decided_by = ?,
                decided_via = 'relayed',
                relayed_by = ?,
                relayer_credential_id = ?,
                user_statement = ?,
                evidence_ref = ?,
                decision_note = ?,
                decided_at = ?,
                user_mac = NULL
            WHERE id = ?;""",
            (
                decision,
                ctx.principal["id"],
                ctx.principal["id"],
                ctx.credential_id,
                redact(ctx.user_statement) if ctx.user_statement else ctx.user_statement,
                ctx.evidence_ref,
                note,
                now,
                approval_id,
            ),
        )
    else:
        # Direct user approval
        decided_via = "direct"
        if not ctx.k_user:
            raise MeshError(
                code=c.PASSPHRASE_REQUIRED,
                exit_code=c.EXIT_UNAUTHENTICATED,
                message="Direct user approval requires passphrase key",
            )
        user_mac = compute_user_approval_mac(ctx.k_user, approval_id, op_hash, decision, now)

        ctx.conn.execute(
            """UPDATE approvals SET
                status = ?,
                decided_by = ?,
                decided_via = 'direct',
                relayed_by = NULL,
                relayer_credential_id = NULL,
                user_statement = NULL,
                evidence_ref = NULL,
                decision_note = ?,
                decided_at = ?,
                user_mac = ?
            WHERE id = ?;""",
            (
                decision,
                ctx.principal["id"],
                note,
                now,
                user_mac,
                approval_id,
            ),
        )

    record_event(
        conn=ctx.conn,
        type_="approval.decided",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        credential_id=ctx.credential_id,
        project_id=row["project_id"],
        approval_id=approval_id,
        payload={"decision": decision, "note": note},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={
        "approval_id": approval_id,
        "status": decision,
        "decided_via": decided_via,
        "trust": "direct_verified" if decided_via == "direct" else "relayed_unverified",
        "user_mac": user_mac,
    })


def handle_approval_approve(ctx: HandlerContext) -> HandlerResult:
    """Approve a pending approval request (direct or relayed) per plan §7.4."""
    return _handle_approval_decision(ctx, decision="approved")


def handle_approval_reject(ctx: HandlerContext) -> HandlerResult:
    """Reject a pending approval request (direct or relayed) per plan §7.4."""
    return _handle_approval_decision(ctx, decision="rejected")


def handle_approval_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel a pending approval request (requester only) per plan §7.4."""
    approval_id = ctx.args.get("approval_id")
    if not approval_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="approval_id is required",
        )

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    _check_approval_run_scope(ctx, row)

    # Requester only
    if row["requested_by"] != ctx.principal["id"]:
        raise MeshError(
            code=c.NOT_REQUESTER,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only the requester can cancel an approval request",
            details={"approval_id": approval_id, "requested_by": row["requested_by"], "caller": ctx.principal["id"]},
        )

    # pending -> cancelled only
    status = row["status"]
    if status != "pending":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' is '{status}'; only pending approvals can be cancelled",
            details={"approval_id": approval_id, "status": status},
        )

    now = now_iso_utc()
    ctx.conn.execute("UPDATE approvals SET status = 'cancelled' WHERE id = ?;", (approval_id,))

    record_event(
        conn=ctx.conn,
        type_="approval.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=row["project_id"],
        approval_id=approval_id,
        payload={"reason": ctx.args.get("reason", "cancelled_by_requester")},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"approval_id": approval_id, "status": "cancelled"})


def handle_approval_check(ctx: HandlerContext) -> HandlerResult:
    """Check approval status and trust level without consuming it per plan §7.4."""
    approval_id = ctx.args.get("approval_id")
    op_hash = ctx.args.get("op_hash")
    if not approval_id:
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="approval_id is required")
    if not op_hash:
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--op-hash is required", details={"param": "op_hash"})

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(code=c.NOT_FOUND, exit_code=c.EXIT_NOT_FOUND, message=f"Approval '{approval_id}' not found", details={"approval_id": approval_id})

    _check_approval_run_scope(ctx, row)

    # Authority: user or requester only per §7.3 (Item 7: else 4 NOT_REQUESTER)
    if ctx.actor_kind != "user" and row["requested_by"] != ctx.principal["id"]:
        raise MeshError(
            code=c.NOT_REQUESTER,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only requester or user can check approval",
            details={"caller": ctx.principal["id"], "requested_by": row["requested_by"]},
        )

    # Validate op_hash
    if op_hash != row["op_hash"]:
        raise MeshError(code=c.OP_HASH_MISMATCH, exit_code=c.EXIT_APPROVAL_INVALID, message="Provided op-hash does not match stored approval op-hash", details={"expected": row["op_hash"], "got": op_hash})

    decided_via = row["decided_via"]
    trust = "direct_verified" if decided_via == "direct" else ("relayed_unverified" if decided_via == "relayed" else None)
    return HandlerResult(data={
        "approval_id": approval_id,
        "status": row["status"],
        "decided_via": decided_via,
        "trust": trust,
        "expires_at": row["expires_at"],
    })


def handle_approval_consume(ctx: HandlerContext) -> HandlerResult:
    """Consume an approved approval token before external action execution per plan §7.4."""
    approval_id = ctx.args.get("approval_id")
    op_hash = ctx.args.get("op_hash")
    if not approval_id:
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="approval_id is required")
    if not op_hash:
        raise MeshError(code=c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--op-hash is required", details={"param": "op_hash"})

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(code=c.NOT_FOUND, exit_code=c.EXIT_NOT_FOUND, message=f"Approval '{approval_id}' not found", details={"approval_id": approval_id})

    _check_approval_run_scope(ctx, row)

    # 1. Authority: the requester only (else 4 NOT_REQUESTER, G19)
    if row["requested_by"] != ctx.principal["id"]:
        raise MeshError(
            code=c.NOT_REQUESTER,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only the requester can consume an approval",
            details={"approval_id": approval_id, "requested_by": row["requested_by"], "caller": ctx.principal["id"]},
        )

    # 2. op-hash match
    if op_hash != row["op_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Provided op-hash does not match stored approval op-hash",
            details={"expected": row["op_hash"], "got": op_hash},
        )

    # 3. State checks (expired, pending, consumed, rejected, cancelled, invalid)
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

    # 4. Policy check
    policy = get_approval_policy(ctx.conn, row["category"])
    if row["decided_via"] == "relayed" and policy == "direct_only":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Category '{row['category']}' requires direct approval; relayed decision disallowed",
            details={"category": row["category"], "policy": policy},
        )

    # 5. Consume
    ctx.conn.execute(
        """UPDATE approvals SET
            status = 'consumed',
            consumed_at = ?,
            consumed_by = ?,
            consumed_request_id = ?
        WHERE id = ?;""",
        (now, ctx.principal["id"], ctx.request_id, approval_id),
    )

    decided_via = row["decided_via"]
    trust = "direct_verified" if decided_via == "direct" else ("relayed_unverified" if decided_via == "relayed" else None)

    return HandlerResult(data={
        "approval_id": approval_id,
        "status": "consumed",
        "consumed_at": now,
        "decided_via": decided_via,
        "trust": trust,
    })


def _format_approval_dict(conn: sqlite3.Connection, r: sqlite3.Row) -> Dict[str, Any]:
    data = dict(r)
    decided_via = data.get("decided_via")
    if decided_via == "direct":
        data["trust"] = "direct_verified"
        data["via"] = "direct"
    elif decided_via == "relayed":
        data["trust"] = "relayed_unverified"
        relayer_name = data.get("relayed_by")
        if relayer_name:
            pcur = conn.execute("SELECT name FROM principals WHERE id = ?;", (relayer_name,))
            prow = pcur.fetchone()
            if prow and prow["name"]:
                relayer_name = prow["name"]
        data["via"] = f"relayed (unverified; by {relayer_name})"
    else:
        data["trust"] = None
        data["via"] = None

    if "op_json" in data and data["op_json"]:
        try:
            data["op"] = json.loads(data["op_json"])
        except Exception:
            pass
    return data


def handle_approval_request(ctx: HandlerContext) -> HandlerResult:
    """Create a pending approval request for a gated or external action per plan §7.4."""
    category = ctx.args.get("category")
    summary = ctx.args.get("summary")
    operation = ctx.args.get("operation")
    project_id = ctx.args.get("project_id") or ctx.args.get("project")
    task_id = ctx.args.get("task_id") or ctx.args.get("task")
    expires_in = ctx.args.get("expires_in")

    if not category:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--category is required",
            details={"param": "category"},
        )
    if category not in DEFAULT_POLICIES:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Unknown approval category: '{category}'",
            details={"param": "category", "category": category},
        )

    if not summary or not isinstance(summary, str) or not summary.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--summary is required and must be non-empty",
            details={"param": "summary"},
        )

    if operation is None:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--operation is required",
            details={"param": "operation"},
        )
    if isinstance(operation, str):
        try:
            operation = json.loads(operation)
        except Exception as e:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid JSON in --operation: {e}",
                details={"param": "operation"},
            )
    if not isinstance(operation, dict):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--operation must be a JSON object",
            details={"param": "operation"},
        )

    try:
        check_no_floats(operation)
    except ValueError as e:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=str(e),
            details={"param": "operation"},
        )

    if project_id:
        pcur = ctx.conn.execute("SELECT id FROM projects WHERE id = ?;", (project_id,))
        if not pcur.fetchone():
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Project '{project_id}' not found",
                details={"project_id": project_id},
            )

    if task_id:
        tcur = ctx.conn.execute("SELECT id FROM tasks WHERE id = ?;", (task_id,))
        if not tcur.fetchone():
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Task '{task_id}' not found",
                details={"task_id": task_id},
            )

    # Authority check per plan §7.3 and coordinator Q2:
    if ctx.actor_kind == "user":
        pass
    elif lead_run_token_scope(ctx.principal, ctx.via) is not None:
        # A lead run token (§7.3, §12.4): its scope project only, while its assignment
        # is current (check_authz raises LEAD_SUPERSEDED first, then SCOPE_MISMATCH).
        task_project = None
        if task_id:
            trow = ctx.conn.execute("SELECT project_id FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            task_project = trow["project_id"] if trow else None
        target_project = project_id or task_project
        check_authz(ctx.conn, "approval.request", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=target_project)
        if target_project is None or (task_id and task_project != target_project):
            raise MeshError(
                code=c.SCOPE_MISMATCH,
                exit_code=c.EXIT_FORBIDDEN,
                message="Lead run token can request approvals only for a project inside its run scope",
                details={"scope_project_id": lead_run_token_scope(ctx.principal, ctx.via),
                         "project_id": project_id, "task_id": task_id},
            )
        project_id = target_project
    elif ctx.via == "run_token":
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message="Executor run tokens cannot request approvals",
        )
    else:
        # Agent caller:
        caps = ctx.principal.get("capabilities", [])
        has_relay = "relay" in caps
        if project_id:
            if not (has_relay or is_project_lead(ctx.conn, project_id, ctx.principal["id"])):
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{ctx.principal.get('name')}' must lead project '{project_id}' or hold 'relay' to request approval",
                    details={"principal": ctx.principal.get("name"), "project_id": project_id},
                )
        else:
            cur_l = ctx.conn.execute("SELECT 1 FROM lead_assignments WHERE agent_id = ? AND ended_at IS NULL LIMIT 1;", (ctx.principal["id"],))
            leads_any = cur_l.fetchone() is not None
            if not (has_relay or leads_any):
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{ctx.principal.get('name')}' without project must lead at least one project or hold 'relay' to request approval",
                    details={"principal": ctx.principal.get("name")},
                )

    # TTL validation:
    ttl_seconds = None
    if expires_in:
        try:
            ttl_seconds = parse_duration(str(expires_in))
        except Exception as e:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid duration for --expires-in '{expires_in}': {e}",
                details={"param": "expires_in", "got": expires_in},
            )
        min_s = 1 if ctx.testing_mode else 60
        max_s = 30 * 86400
        if ttl_seconds < min_s or ttl_seconds > max_s:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"--expires-in must be between {min_s}s and {max_s}s; got {ttl_seconds}s",
                details={"param": "expires_in", "min": min_s, "max": max_s, "got": ttl_seconds},
            )

    op_string = f"external.{category}"
    apr_info = create_or_get_pending_approval(
        conn=ctx.conn,
        category=category,
        op=op_string,
        requested_by=ctx.principal["id"],
        params=operation,
        summary=summary,
        project_id=project_id,
        target_id=task_id,
        task_id=task_id,
        ttl_seconds=ttl_seconds,
        dry_run=ctx.dry_run,
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    if ctx.dry_run:
        return HandlerResult(data={
            "dry_run": True,
            "approval_id": None,
            "op_hash": apr_info["op_hash"],
            "category": category,
            "summary": summary,
        })

    return HandlerResult(data=apr_info)


def handle_approval_show(ctx: HandlerContext) -> HandlerResult:
    """Show details, hash, and status of an approval per plan §7.4."""
    approval_id = ctx.args.get("approval_id")
    if not approval_id:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="approval_id is required",
        )

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    _check_approval_run_scope(ctx, row)

    # Authz check per §7.3
    if ctx.actor_kind != "user":
        caps = ctx.principal.get("capabilities", [])
        if "relay" not in caps:
            agent_id = ctx.principal["id"]
            allowed = False
            if row["requested_by"] == agent_id:
                allowed = True
            elif row["project_id"] and can_principal_read_project(ctx.conn, agent_id, row["project_id"]):
                allowed = True
            if not allowed:
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{ctx.principal.get('name')}' cannot access approval '{approval_id}' outside its scope",
                    details={"approval_id": approval_id},
                )

    data = _format_approval_dict(ctx.conn, row)
    return HandlerResult(data=data)


def handle_approval_list(ctx: HandlerContext) -> HandlerResult:
    """List approvals with optional status and project filters."""
    status_filter = ctx.args.get("status")
    project_filter = ctx.args.get("project")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9c N1: a lead run token lists only its own project's approvals,
        # even with the relay capability. An explicit other --project is rc 4 here.
        check_authz(ctx.conn, "approval.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_filter)
        project_filter = scope_project

    query = "SELECT * FROM approvals WHERE 1=1"
    params: List[Any] = []
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if project_filter:
        query += " AND project_id = ?"
        params.append(project_filter)
    query += " ORDER BY requested_at ASC, id ASC;"

    cur = ctx.conn.execute(query, params)
    is_user_or_relayer = (ctx.actor_kind == "user") or ("relay" in ctx.principal.get("capabilities", []))
    agent_id = ctx.principal["id"] if ctx.actor_kind != "user" else None

    items = []
    for r in cur.fetchall():
        if not is_user_or_relayer:
            if r["requested_by"] != agent_id:
                if not r["project_id"] or not can_principal_read_project(ctx.conn, agent_id, r["project_id"]):
                    continue
        d = _format_approval_dict(ctx.conn, r)
        items.append(d)

    return HandlerResult(data={"items": items})
