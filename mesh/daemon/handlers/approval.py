"""Approval command handlers per plan §7.4 and §13.3."""

import json
import sqlite3
from typing import Any, Dict, List, Optional

from mesh.core.redact import redact, redact_value
import mesh.constants as c
from mesh.core.approvals import (
    DEFAULT_POLICIES,
    ensure_delegated_decider_current,
    record_consumed_event,
    HUMAN_CONFIRMED_MAX,
    compute_user_approval_mac,
    get_approval_mode,
    get_delegated_self_approve,
    create_or_get_pending_approval,
    get_approval_policy,
    get_row_approval_policy,
    validate_delegated_decision_at_consume,
    requested_event_problem,
    verify_row_for_consume,
    is_legacy_consumed_approval,
    verify_approval_event_history,
    verify_stored_op_json,
)
from mesh.core.authz import (
    can_principal_read_project,
    check_authz,
    get_agent_capabilities,
    get_project_active_lead,
    is_acting_project_lead,
    is_project_lead,
    lead_run_token_scope,
)
from mesh.core.canonical import check_no_floats
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import record_event
from mesh.core.workspaces import is_approval_pinned_by_ws_reservation
from mesh.daemon.handlers.base import HandlerContext, HandlerResult
from mesh.errors import MeshError


def _check_approval_run_scope(ctx: HandlerContext, row: sqlite3.Row) -> None:
    """Confine approval IDs before requester, project-read, or relay authority."""
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        if row["project_id"] != scope_project:
            raise MeshError(
                code=c.SCOPE_MISMATCH,
                exit_code=c.EXIT_FORBIDDEN,
                message="Lead run token cannot access an approval outside its run scope",
                details={"approval_id": row["id"], "project_id": row["project_id"],
                         "scope_project_id": scope_project},
            )
        check_authz(ctx.conn, ctx.cmd, ctx.principal, ctx.actor_kind, ctx.via, project_id=scope_project)


def _require_nonempty_str(value: Any, param: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=message,
            details={"param": param},
        )
    return value


def _validate_optional_str(value: Any, param: str, *, max_len: Optional[int] = None) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or (max_len is not None and len(value) > max_len):
        msg = (
            f"Field '{param}' must be a string up to {max_len} characters"
            if max_len is not None
            else f"Field '{param}' must be a string"
        )
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=msg,
            details={"param": param},
        )
    return value


def _validate_evidence_ref(value: Any) -> Optional[str]:
    validated = _validate_optional_str(value, "evidence_ref", max_len=4000)
    return redact(validated) if validated else validated


def _redact_opt(value: Any) -> Any:
    """Redact free text supplied with a decision (evidence references included)."""
    return redact(value) if isinstance(value, str) and value else value


def check_approval_replay_authz(
    conn: sqlite3.Connection,
    cmd: str,
    principal: Dict[str, Any],
    actor_kind: str,
    via: str,
    args: Any,
    stored_envelope: Any,
) -> None:
    """Re-verify fine-grained approval command authority on idempotent replay."""
    if not cmd.startswith("approval."):
        return
    req_args = args if isinstance(args, dict) else {}
    data = stored_envelope.get("data") if isinstance(stored_envelope, dict) else {}
    raw_aid = req_args.get("approval_id")
    approval_id = raw_aid if isinstance(raw_aid, str) and raw_aid else (
        data.get("approval_id") if isinstance(data, dict) and isinstance(data.get("approval_id"), str) else None
    )
    if actor_kind != "user" and principal.get("id"):
        caps = get_agent_capabilities(conn, principal["id"])
        effective_principal = dict(principal)
        effective_principal["capabilities"] = caps
    else:
        caps = principal.get("capabilities")
        effective_principal = principal
    caps = caps if isinstance(caps, list) else []

    ctx = HandlerContext(
        conn=conn,
        principal=effective_principal,
        actor_kind=actor_kind,
        via=via,
        request_id="replay_authz",
        home="",
        cmd=cmd,
        args=dict(req_args),
    )

    if cmd == "approval.request":
        raw_proj = req_args.get("project_id") or req_args.get("project")
        project_id = raw_proj if isinstance(raw_proj, str) and raw_proj else None
        raw_task = req_args.get("task_id") or req_args.get("task")
        task_id = raw_task if isinstance(raw_task, str) and raw_task else None
        if task_id and not project_id:
            trow = conn.execute("SELECT project_id FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            project_id = trow["project_id"] if trow else None
        if not project_id and approval_id:
            arow = conn.execute("SELECT project_id FROM approvals WHERE id = ?;", (approval_id,)).fetchone()
            project_id = arow["project_id"] if arow else None
        if actor_kind == "user":
            return
        if lead_run_token_scope(effective_principal, via) is not None:
            check_authz(conn, "approval.request", effective_principal, actor_kind, via, project_id=project_id)
            if project_id is None:
                raise MeshError(
                    code=c.SCOPE_MISMATCH,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Lead run token can request approvals only for a project inside its run scope",
                )
            return
        if via == "run_token":
            raise MeshError(
                code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
                exit_code=c.EXIT_FORBIDDEN,
                message="Executor run tokens cannot request approvals",
            )
        check_authz(conn, "approval.request", effective_principal, actor_kind, via, project_id=project_id, task_id=task_id)
        has_relay = "relay" in caps
        if project_id:
            if not (has_relay or is_acting_project_lead(conn, project_id, effective_principal["id"])):
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{effective_principal.get('name')}' must lead project '{project_id}' or hold 'relay'",
                )
        else:
            leads_any = conn.execute(
                "SELECT 1 FROM lead_assignments la JOIN agents a ON a.id = la.agent_id "
                "WHERE la.agent_id = ? AND la.ended_at IS NULL AND a.can_lead = 1 LIMIT 1;",
                (effective_principal["id"],),
            ).fetchone() is not None
            if not (has_relay or leads_any):
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Agent without project must lead at least one project or hold 'relay'",
                )
        return
    if not approval_id:
        return
    row = conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,)).fetchone()
    if row is None:
        return
    _check_approval_run_scope(ctx, row)
    if cmd in ("approval.approve", "approval.reject"):
        human_confirmed = req_args.get("human_confirmed")
        delegated = human_confirmed is not None
        if delegated:
            _check_delegated_decision(ctx, row, human_confirmed)
            return
        if actor_kind != "user" and via != "relayed":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="Agent caller must use --relayed for approval decisions",
            )
        if via == "relayed":
            if "relay" not in caps:
                raise MeshError(
                    code=c.RELAY_NOT_ALLOWED,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Principal '{effective_principal.get('name')}' lacks 'relay' capability",
                )
            if effective_principal["id"] == row["requested_by"] or (row["project_id"] and is_project_lead(conn, row["project_id"], effective_principal["id"])):
                raise MeshError(
                    code=c.SELF_APPROVAL,
                    exit_code=c.EXIT_FORBIDDEN,
                    message="Relayer cannot decide its own or its led project's approval",
                )
            blocking_cat, policy = get_row_approval_policy(conn, row)
            if policy == "direct_only":
                raise MeshError(
                    code=c.CHANNEL_NOT_ALLOWED,
                    exit_code=c.EXIT_APPROVAL_INVALID,
                    message=f"Category '{blocking_cat}' requires direct user approval",
                )
    elif cmd == "approval.cancel":
        is_lead = bool(row["project_id"] and is_acting_project_lead(conn, row["project_id"], effective_principal["id"]))
        if actor_kind != "user" and row["requested_by"] != effective_principal["id"] and not is_lead:
            raise MeshError(
                code=c.NOT_REQUESTER,
                exit_code=c.EXIT_FORBIDDEN,
                message="Only the requester, project lead, or user can cancel an approval request",
            )
    elif cmd == "approval.consume":
        if row["requested_by"] != effective_principal["id"]:
            raise MeshError(
                code=c.NOT_REQUESTER,
                exit_code=c.EXIT_FORBIDDEN,
                message="Only the requester can consume an approval",
            )


def _decision_channel(conn: sqlite3.Connection, ctx: HandlerContext, row: sqlite3.Row) -> str:
    """How the caller of `approval pending` can decide this approval.

    ``direct``: the caller is the user. ``delegated``: the caller is the current inbox
    lead and delegated mode allows it. ``relayed``: the caller holds ``relay`` and the
    policy allows relaying for a project it does not lead. ``user_only``: only the user
    with the passphrase can decide it for this caller.
    """
    if ctx.actor_kind == "user":
        return "direct"
    if ctx.via not in ("token", "relayed"):
        return "user_only"
    _, policy = get_row_approval_policy(conn, row)
    if policy == "direct_only":
        return "user_only"
    pid = row["project_id"]
    me = ctx.principal["id"]
    if (ctx.via == "token" and pid and get_approval_mode(conn) == "delegated"
            and is_project_lead(conn, pid, me)
            and (me != row["requested_by"] or get_delegated_self_approve(conn))):
        lead = get_project_active_lead(conn, pid)
        if (lead and lead.get("delivery") == "inbox" and lead.get("disabled_at") is None
                and lead.get("can_lead")):
            return "delegated"
    caps = ctx.principal.get("capabilities", [])
    if ("relay" in caps and me != row["requested_by"]
            and not (pid and is_project_lead(conn, pid, me))):
        return "relayed"
    return "user_only"


def _check_delegated_decision(ctx: HandlerContext, row: sqlite3.Row, human_confirmed: Any) -> None:
    """Authorize a delegated decision (``approval.mode = delegated``).

    The current lead of the approval's project, with its own agent token, decides on the
    human's behalf after asking them, and records the human's words. Nobody else can:
    not the user (they approve directly), not a relayer (--relayed is a different path),
    not an executor or lead run token, not a worker, not a former lead. Categories whose
    policy is ``direct_only`` (always ``config``) still need the passphrase.
    """
    if not isinstance(human_confirmed, str) or not human_confirmed.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--human-confirmed must quote the human's reply (non-empty)",
            details={"param": "human_confirmed"},
        )
    if len(human_confirmed) > HUMAN_CONFIRMED_MAX:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"--human-confirmed exceeds {HUMAN_CONFIRMED_MAX} characters",
            details={"param": "human_confirmed"},
        )
    if ctx.actor_kind == "user":
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--human-confirmed is for the project lead; the user approves directly with --user",
            details={"param": "human_confirmed"},
        )
    if ctx.via == "relayed":
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--human-confirmed and --relayed cannot be combined",
            details={"param": "human_confirmed"},
        )
    if ctx.via != "token":
        # Executor and lead run tokens never decide approvals.
        raise MeshError(
            code=c.CREDENTIAL_NOT_ALLOWED_IN_RUN,
            exit_code=c.EXIT_FORBIDDEN,
            message="Run tokens cannot decide approvals; the lead uses its own agent token",
            details={"via": ctx.via},
        )
    if get_approval_mode(ctx.conn) != "delegated":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=("Delegated approval is off (approval.mode is 'passphrase'). "
                     "The user can enable it: mesh config set approval.mode '\"delegated\"' --user"),
            details={"setting": "approval.mode", "expected": "delegated"},
        )
    project_id = row["project_id"]
    if not project_id:
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Approvals without a project can only be decided by the user directly",
            details={"approval_id": row["id"]},
        )
    if not is_project_lead(ctx.conn, project_id, ctx.principal["id"]):
        raise MeshError(
            code=c.NOT_PROJECT_LEAD,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only the current lead of the approval's project can decide it on the human's behalf",
            details={"project_id": project_id, "caller": ctx.principal.get("name")},
        )
    lead = get_project_active_lead(ctx.conn, project_id)
    if not lead or not lead.get("can_lead"):
        raise MeshError(code=c.NOT_PROJECT_LEAD, exit_code=c.EXIT_FORBIDDEN,
                        message="The current lead no longer has can_lead",
                        details={"project_id": project_id, "reason": "not_can_lead"})
    if lead.get("delivery") == "launch":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="A launch-mode lead has no channel to the human; delegated approval needs an inbox lead",
            details={"project_id": project_id},
        )
    blocking_cat, policy = get_row_approval_policy(ctx.conn, row)
    if policy == "direct_only":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Category '{blocking_cat}' requires direct user approval (passphrase), even in delegated mode",
            details={"category": blocking_cat, "policy": policy},
        )
    if row["requested_by"] == ctx.principal["id"] and not get_delegated_self_approve(ctx.conn):
        # Sakana PR #4 P1-1: the lead deciding its own request is allowed by default (it
        # asked the human first) but can be turned off; then it is SELF_APPROVAL exactly
        # as in direct/relayed mode.
        raise MeshError(
            code=c.SELF_APPROVAL,
            exit_code=c.EXIT_FORBIDDEN,
            message=("Requester cannot decide its own request: approval.delegated_self_approve is false. "
                     "The user decides it directly (--user), or can allow it again: "
                     "mesh config set approval.delegated_self_approve true --user"),
            details={"requested_by": row["requested_by"], "caller": ctx.principal["id"],
                     "setting": "approval.delegated_self_approve"},
        )


def _handle_approval_decision(ctx: HandlerContext, decision: str) -> HandlerResult:
    """Decide (approve or reject) a pending approval request per plan §7.3 and §7.4."""
    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")
    op_hash = _require_nonempty_str(ctx.args.get("op_hash"), "op_hash", "--op-hash is required")
    raw_note = _validate_optional_str(ctx.args.get("note"), "note", max_len=4000)
    note = redact(raw_note) if raw_note else raw_note  # blind review P2 / Carbon: never store a recognized secret
    arg_ev_ref = _validate_optional_str(ctx.args.get("evidence_ref"), "evidence_ref", max_len=4000)
    env_ev_ref = _validate_optional_str(ctx.evidence_ref, "evidence_ref", max_len=4000)
    if arg_ev_ref is not None and env_ev_ref is not None and arg_ev_ref != env_ev_ref:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'evidence_ref' values in args and request envelope",
            details={"param": "evidence_ref"},
        )
    raw_ev_ref = arg_ev_ref if arg_ev_ref is not None else env_ev_ref
    validated_ev_ref = redact(raw_ev_ref) if raw_ev_ref else raw_ev_ref
    arg_stmt = _validate_optional_str(ctx.args.get("user_statement"), "user_statement", max_len=4000)
    env_stmt = _validate_optional_str(ctx.user_statement, "user_statement", max_len=4000)
    if arg_stmt is not None and env_stmt is not None and arg_stmt != env_stmt:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'user_statement' values in args and request envelope",
            details={"param": "user_statement"},
        )
    raw_stmt = env_stmt if env_stmt is not None else arg_stmt
    relayed_stmt = redact(raw_stmt) if raw_stmt else raw_stmt

    cur = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,))
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    human_confirmed = ctx.args.get("human_confirmed")
    delegated = human_confirmed is not None
    if delegated:
        _check_delegated_decision(ctx, row, human_confirmed)

    # Auth check order per plan §7.3:
    # (1) agent caller without --relayed -> 4 CAPABILITY_MISSING
    if not delegated and ctx.actor_kind != "user" and ctx.via != "relayed":
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
        blocking_cat, policy = get_row_approval_policy(ctx.conn, row)
        if policy == "direct_only":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message=f"Category '{blocking_cat}' requires direct user approval; relayed decision disallowed",
                details={"category": blocking_cat, "policy": policy},
            )
        if not delegated and (not isinstance(raw_stmt, str) or not raw_stmt.strip()):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field '--user-statement' is required and non-empty when using --relayed",
                details={"param": "user_statement"},
            )

    # (5) op_hash check:
    # First, verify stored op_json re-hash to detect DB corruption (defect d and plan §7.4 step 3)
    # R2 P1-2: the shared check (also used by audit verify); a forged `_redacted`
    # envelope or a document that does not describe this row vouches for nothing.
    try:
        recomputed_hash = verify_stored_op_json(row)
    except Exception:
        recomputed_hash = None
    # R2 P1-3: the row (category, op, requester, project, task, op_hash) must be the one
    # the hash-chained approval.requested event recorded; a rewritten category could
    # otherwise dodge a direct_only policy checked above.
    req_problem = requested_event_problem(ctx.conn, row)
    if req_problem:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Database corruption detected: {req_problem}",
            details={"approval_id": row["id"], "reason": req_problem},
        )
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

    if is_approval_pinned_by_ws_reservation(ctx.conn, approval_id):
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Approval '{approval_id}' is pinned by an in-progress workspace operation",
            details={"approval_id": approval_id},
        )

    # (6) State checks (cross-checked against hash-chained approval.* events):
    now = now_iso_utc()
    status = row["status"]
    ev_rows = ctx.conn.execute(
        "SELECT seq, type, actor_id, via, payload_json FROM events "
        "WHERE approval_id = ? AND type IN ('approval.decided', 'approval.cancelled', 'approval.expired', 'approval.consumed') "
        "ORDER BY seq ASC;",
        (approval_id,),
    ).fetchall()
    ev_dec, ev_terminal, hist_err = verify_approval_event_history(ev_rows)
    if hist_err:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Database corruption detected: {hist_err}",
            details={"approval_id": approval_id, "reason": "decision_event_mismatch"},
        )
    if ev_terminal == "expired":
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' has expired",
            details={"approval_id": approval_id, "expires_at": row["expires_at"]},
        )
    if ev_terminal == "consumed":
        raise MeshError(
            code=c.APPROVAL_CONSUMED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was already consumed",
            details={"approval_id": approval_id},
        )
    if ev_terminal == "cancelled":
        raise MeshError(
            code=c.APPROVAL_CANCELLED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was cancelled",
            details={"approval_id": approval_id},
        )
    if ev_terminal in ("approved", "rejected") and ev_terminal != decision:
        raise MeshError(
            code=c.DECISION_CONFLICT,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' was already {ev_terminal}; cannot {decision}",
            details={"approval_id": approval_id, "status": ev_terminal},
        )
    has_consumed_ev = any(r["type"] == "approval.consumed" for r in ev_rows)
    if ev_terminal == decision and status != decision:
        ev_chan = ("relayed" if ev_dec["via"] == "delegated" else ev_dec["via"]) if ev_dec else None
        is_valid_upgrade = (
            decision == "approved"
            and ev_chan == "relayed"
            and not delegated
            and ctx.actor_kind == "user"
            and ctx.via == "direct"
        )
        if not is_valid_upgrade and not is_legacy_consumed_approval(status, ev_terminal, ev_dec, has_consumed_ev):
            raise MeshError(
                code=c.APPROVAL_INVALID,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Database corruption detected: approval row status differs from event chain",
                details={"approval_id": approval_id, "row_status": status, "event_state": ev_terminal},
            )

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
    upgrade_to_direct = (status == decision == "approved" and row["decided_via"] == "relayed"
                         and not delegated and ctx.actor_kind == "user" and ctx.via == "direct")
    if status == decision and not upgrade_to_direct:
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

    if delegated:
        # Recorded like a relayed decision (unverified, no user MAC): relayed_by is the
        # lead, user_statement the human's words. The approval.decided event carries
        # via "delegated" so audit and `approval show` can tell the two apart.
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
                redact(human_confirmed),
                validated_ev_ref,
                note,
                now,
                approval_id,
            ),
        )
    elif ctx.via == "relayed":
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
                relayed_stmt,
                validated_ev_ref,
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

    # Release-note follow-up (c): the requester can tell which approval this is without a lookup.
    payload: Dict[str, Any] = {"decision": decision, "note": note, "approval_id": approval_id,
                               "category": row["category"], "op": row["op_type"], "op_hash": row["op_hash"]}
    if delegated or ctx.via == "relayed":
        payload["user_statement"] = redact(human_confirmed) if delegated else relayed_stmt
        payload["evidence_ref"] = validated_ev_ref
    if upgrade_to_direct:
        payload["upgraded_from"] = "relayed"
    if delegated:
        payload["mode"] = "delegated"
        payload["human_confirmed"] = redact(human_confirmed)
        # Sakana PR #4 P1-1: say plainly when the lead decided a request it raised itself.
        # The approvals row records it too (relayed_by == requested_by), see self_requested.
        payload["self_requested"] = row["requested_by"] == ctx.principal["id"]
    record_event(
        conn=ctx.conn,
        type_="approval.decided",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via="delegated" if delegated else ctx.via,
        relayed_by=ctx.principal["id"] if delegated else ctx.relayed_by,
        credential_id=ctx.credential_id,
        project_id=row["project_id"],
        approval_id=approval_id,
        payload=payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    data = {
        "approval_id": approval_id,
        "status": decision,
        "decided_via": decided_via,
        "trust": "direct_verified" if decided_via == "direct" else "relayed_unverified",
        "user_mac": user_mac,
    }
    if upgrade_to_direct:
        data["upgraded_from"] = "relayed"
    if delegated:
        data["delegated"] = True
        data["human_confirmed"] = redact(human_confirmed)
        data["self_requested"] = payload["self_requested"]
    return HandlerResult(data=data)


def handle_approval_approve(ctx: HandlerContext) -> HandlerResult:
    """Approve a pending approval request (direct or relayed) per plan §7.4."""
    return _handle_approval_decision(ctx, decision="approved")


def handle_approval_reject(ctx: HandlerContext) -> HandlerResult:
    """Reject a pending approval request (direct or relayed) per plan §7.4."""
    return _handle_approval_decision(ctx, decision="rejected")


def handle_approval_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel a pending approval request (requester only) per plan §7.4."""
    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")
    raw_reason = _validate_optional_str(ctx.args.get("reason"), "reason", max_len=4000)
    cancel_reason = redact(raw_reason) if (raw_reason and raw_reason.strip()) else None

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

    # User, requester, or current project lead may cancel pending or unconsumed approved rows.
    # Full review r2 P2-C: a lead whose can_lead was revoked keeps no lead right here either.
    is_lead = bool(row["project_id"] and is_acting_project_lead(ctx.conn, row["project_id"], ctx.principal["id"]))
    is_requester = row["requested_by"] == ctx.principal["id"]
    if ctx.actor_kind != "user" and not is_requester and not is_lead:
        raise MeshError(
            code=c.NOT_REQUESTER,
            exit_code=c.EXIT_FORBIDDEN,
            message="Only the requester, project lead, or user can cancel an approval request",
            details={"approval_id": approval_id, "requested_by": row["requested_by"], "caller": ctx.principal["id"]},
        )

    if is_approval_pinned_by_ws_reservation(ctx.conn, approval_id):
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Approval '{approval_id}' is pinned by an in-progress workspace operation",
            details={"approval_id": approval_id},
        )

    # pending / unconsumed approved -> cancelled only
    status = row["status"]
    if status not in ("pending", "approved") or row["consumed_at"] is not None:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' is '{status}'; only pending or unconsumed approved approvals can be cancelled",
            details={"approval_id": approval_id, "status": status},
        )

    default_reason = (
        "cancelled_by_requester" if is_requester
        else ("cancelled_by_user" if ctx.actor_kind == "user" else "cancelled_by_lead")
    )
    ctx.conn.execute("UPDATE approvals SET status = 'cancelled' WHERE id = ?;", (approval_id,))

    record_event(
        conn=ctx.conn,
        type_="approval.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=row["project_id"],
        approval_id=approval_id,
        payload={"reason": cancel_reason or default_reason, "approval_id": approval_id,
                 "from_status": status, "category": row["category"], "op": row["op_type"]},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(data={"approval_id": approval_id, "status": "cancelled"})


def handle_approval_check(ctx: HandlerContext) -> HandlerResult:
    """Check approval status and trust level without consuming it per plan §7.4."""
    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")
    op_hash = _require_nonempty_str(ctx.args.get("op_hash"), "op_hash", "--op-hash is required")

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

    if row["status"] == "expired" or (
        row["status"] in ("pending", "approved") and row["expires_at"] < now_iso_utc()
    ):
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Approval has expired",
            details={"approval_id": approval_id, "expires_at": row["expires_at"]},
        )

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
    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")
    op_hash = _require_nonempty_str(ctx.args.get("op_hash"), "op_hash", "--op-hash is required")

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

    if is_approval_pinned_by_ws_reservation(ctx.conn, approval_id):
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Approval '{approval_id}' is pinned by an in-progress workspace operation",
            details={"approval_id": approval_id},
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

    # Carbon review (public PR #2): a delegated decision cannot outlive its lead assignment
    # (A->B->A included) or a lead that lost can_lead.
    verify_row_for_consume(ctx.conn, row)  # R3 P1-1
    ensure_delegated_decider_current(ctx.conn, row)

    # 4. Policy and delegated authority checks
    blocking_cat, policy = get_row_approval_policy(ctx.conn, row)
    if row["decided_via"] == "relayed" and policy == "direct_only":
        raise MeshError(
            code=c.CHANNEL_NOT_ALLOWED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Category '{blocking_cat}' requires direct approval; relayed decision disallowed",
            details={"category": blocking_cat, "policy": policy},
        )

    validate_delegated_decision_at_consume(ctx.conn, row)

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
    record_consumed_event(ctx.conn, row, ctx.principal["id"], ctx.actor_kind,
                          ctx.via, ctx.request_id, ctx.k_user)

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
        ev = conn.execute(
            "SELECT via FROM events WHERE type = 'approval.decided' AND approval_id = ? ORDER BY seq DESC LIMIT 1;",
            (data.get("id"),),
        ).fetchone()
        if ev and ev["via"] == "delegated":
            data["delegated"] = True
            # Sakana PR #4 P1-1: the row itself shows it (the deciding lead is the requester).
            data["self_requested"] = bool(data.get("relayed_by")) and data.get("relayed_by") == data.get("requested_by")
            suffix = ", its own request" if data["self_requested"] else ""
            data["via"] = f"delegated (unverified; lead {relayer_name} on the human's word{suffix})"
        else:
            data["via"] = f"relayed (unverified; by {relayer_name})"
    else:
        data["trust"] = None
        data["via"] = None

    if "op_json" in data and data["op_json"]:
        try:
            stored = json.loads(data["op_json"])
            if isinstance(stored, dict) and stored.get("_redacted") is True:
                stored = stored.get("op")
            data["op"] = redact_value(stored)
            # PR #40 r1: op_json is re-serialized from the redacted decoded values (text
            # redaction misses a secret whose quotes JSON escaped).
            data["op_json"] = json.dumps(data["op"])
        except Exception:
            data["op_json"] = redact(data["op_json"])
    for k in ("summary", "decision_note", "user_statement", "evidence_ref"):
        if isinstance(data.get(k), str):
            data[k] = redact(data[k])
    return data


def handle_approval_request(ctx: HandlerContext) -> HandlerResult:
    """Create a pending approval request for a gated or external action per plan §7.4."""
    category = ctx.args.get("category")
    summary = ctx.args.get("summary")
    operation = ctx.args.get("operation")
    raw_proj_id = _validate_optional_str(ctx.args.get("project_id"), "project_id")
    raw_proj = _validate_optional_str(ctx.args.get("project"), "project")
    if raw_proj_id and raw_proj and raw_proj_id != raw_proj:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'project_id' and 'project' arguments",
            details={"param": "project"},
        )
    project_id = raw_proj_id or raw_proj
    raw_task_id = _validate_optional_str(ctx.args.get("task_id"), "task_id")
    raw_task = _validate_optional_str(ctx.args.get("task"), "task")
    if raw_task_id and raw_task and raw_task_id != raw_task:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'task_id' and 'task' arguments",
            details={"param": "task"},
        )
    task_id = raw_task_id or raw_task
    expires_in = ctx.args.get("expires_in")

    if not category or not isinstance(category, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--category is required and must be a string",
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

    # Blind review P2: the operation is stored verbatim (its hash binds the approval), so
    # it must not carry a secret; name the secret by reference (an env var or file) instead.
    # PR #40 r1: checked on the decoded values (escaped quotes in JSON text hid a token).
    if redact_value(operation) != operation:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=("--operation contains what looks like a secret; it would be stored as is. "
                     "Refer to the secret by name (an env var or a file path) instead"),
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
        tcur = ctx.conn.execute("SELECT id, project_id FROM tasks WHERE id = ?;", (task_id,))
        trow0 = tcur.fetchone()
        if not trow0:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Task '{task_id}' not found",
                details={"task_id": task_id},
            )
        # Full review P1: a task approval always belongs to the task's project.
        if project_id and trow0["project_id"] != project_id:
            raise MeshError(
                code=c.SCOPE_MISMATCH,
                exit_code=c.EXIT_FORBIDDEN,
                message=f"Task '{task_id}' is not in project '{project_id}'",
                details={"task_id": task_id, "project_id": project_id},
            )
        project_id = project_id or trow0["project_id"]

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
        # Agent caller (full review P1: project-scoped authz, incl. relayed self-approval):
        check_authz(ctx.conn, "approval.request", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_id, task_id=task_id)
        caps = ctx.principal.get("capabilities", [])
        has_relay = "relay" in caps
        if project_id:
            if not (has_relay or is_acting_project_lead(ctx.conn, project_id, ctx.principal["id"])):
                raise MeshError(
                    code=c.CAPABILITY_MISSING,
                    exit_code=c.EXIT_FORBIDDEN,
                    message=f"Agent '{ctx.principal.get('name')}' must lead project '{project_id}' or hold 'relay' to request approval",
                    details={"principal": ctx.principal.get("name"), "project_id": project_id},
                )
        else:
            cur_l = ctx.conn.execute(
                """SELECT 1 FROM lead_assignments la JOIN agents a ON a.id = la.agent_id
                   WHERE la.agent_id = ? AND la.ended_at IS NULL AND a.can_lead = 1 LIMIT 1;""",
                (ctx.principal["id"],),
            )
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
    if expires_in is not None:
        if not isinstance(expires_in, str) or not expires_in.strip():
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid duration for --expires-in '{expires_in}': expected a duration string",
                details={"param": "expires_in", "got": expires_in},
            )
        try:
            ttl_seconds = parse_duration(expires_in)
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
        testing_mode=ctx.testing_mode,
    )

    if ctx.dry_run:
        return HandlerResult(data={
            "dry_run": True,
            "approval_id": None,
            "op_hash": apr_info["op_hash"],
            "category": category,
            "summary": redact(summary),
        })

    return HandlerResult(data=apr_info)


def handle_approval_show(ctx: HandlerContext) -> HandlerResult:
    """Show details, hash, and status of an approval per plan §7.4."""
    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")

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
    status_filter = _validate_optional_str(ctx.args.get("status"), "status")
    project_filter = _validate_optional_str(ctx.args.get("project"), "project")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9c N1: a lead run token lists only its own project's approvals,
        # even with the relay capability. An explicit other --project is rc 4 here.
        check_authz(ctx.conn, "approval.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_filter)
        project_filter = scope_project
    elif project_filter is not None:
        check_authz(ctx.conn, "approval.list", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=project_filter)

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
        if ctx.args.get("with_channel"):
            d["decision_channel"] = _decision_channel(ctx.conn, ctx, r)
        items.append(d)

    return HandlerResult(data={"items": items})
