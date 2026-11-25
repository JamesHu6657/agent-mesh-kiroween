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
    sync_plan_on_approval_terminal,
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
    if cmd == "approval.decide":
        if actor_kind != "user" or via != "direct":
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="Command 'approval.decide' requires direct user authentication",
                details={"cmd": "approval.decide", "required": "user_direct"},
            )
        return
    if cmd in ("approval.approve", "approval.reject"):
        if row["category"] in ("plan_dispatch", "step_dispatch"):
            if actor_kind != "user" or via != "direct":
                raise MeshError(
                    code=c.CHANNEL_NOT_ALLOWED,
                    exit_code=c.EXIT_APPROVAL_INVALID,
                    message=f"Category '{row['category']}' requires direct user card approval",
                    details={"approval_id": approval_id, "category": row["category"]},
                )
            return
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
        if row["status"] == "approved" and actor_kind != "user" and (
            row["category"] in ("plan_dispatch", "step_dispatch", "permission_escalation")
            or row["decided_via"] == "direct"
        ):
            raise MeshError(
                code=c.CAPABILITY_MISSING,
                exit_code=c.EXIT_FORBIDDEN,
                message="Only the direct user can cancel an approved user-signed approval",
                details={"approval_id": approval_id, "category": row["category"], "required": "user_direct"},
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


def _load_and_verify_plan_for_approval(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    provided_plan_hash: Optional[str],
    *,
    provided_plan_id: Optional[str] = None,
    provided_plan_version: Optional[Any] = None,
) -> sqlite3.Row:
    """Load the plan bound to a `plan_dispatch` approval and verify exact plan_hash & integrity."""
    from mesh.daemon.handlers.plan import compute_plan_hash

    plan = conn.execute(
        "SELECT * FROM plans WHERE approval_id = ?;",
        (row["id"],),
    ).fetchone()
    if plan is None:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Plan bound to approval '{row['id']}' was not found",
            details={"approval_id": row["id"]},
        )
    if provided_plan_id is not None and provided_plan_id != plan["id"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Provided plan_id does not match the plan bound to this approval",
            details={"approval_id": row["id"], "expected_plan_id": plan["id"], "got": provided_plan_id},
        )
    if provided_plan_version is not None:
        if isinstance(provided_plan_version, bool) or not isinstance(provided_plan_version, int) or provided_plan_version != plan["version"]:
            raise MeshError(
                code=c.OP_HASH_MISMATCH,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Provided plan_version does not match the plan bound to this approval",
                details={"approval_id": row["id"], "expected_version": plan["version"], "got": provided_plan_version},
            )
    if not isinstance(provided_plan_hash, str) or not provided_plan_hash.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'plan_hash' is required to decide a plan_dispatch approval card",
            details={"param": "plan_hash", "approval_id": row["id"], "plan_id": plan["id"]},
        )
    try:
        steps = json.loads(plan["steps_json"] or "[]")
        if not isinstance(steps, list) or not steps:
            raise ValueError("empty steps")
        recomputed_plan_hash = compute_plan_hash(plan["title"], plan["summary"], steps)
    except Exception as exc:
        raise MeshError(
            code=c.APPROVAL_INVALID,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Stored plan document is corrupt: {exc}",
            details={"approval_id": row["id"], "plan_id": plan["id"]},
        )
    if recomputed_plan_hash != plan["plan_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Database corruption detected: stored plan_hash does not match immutable plan steps",
            details={"plan_id": plan["id"], "expected": plan["plan_hash"], "computed": recomputed_plan_hash},
        )
    if provided_plan_hash.strip() != plan["plan_hash"]:
        raise MeshError(
            code=c.OP_HASH_MISMATCH,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message="Provided plan_hash does not match the current plan version shown",
            details={"plan_id": plan["id"], "expected": plan["plan_hash"], "got": provided_plan_hash.strip()},
        )
    return plan


def _expire_if_past_ttl(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    now: str,
    *,
    dry_run: bool = False,
) -> None:
    """If an open approval is past TTL, persist expiry + plan sync and raise APPROVAL_EXPIRED.

    Callers MUST invoke this before any other database mutation in the transaction so that
    `commit_tx=True` commits strictly the expiry transition and plan cancellation sync.
    """
    if row["status"] == "expired":
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{row['id']}' has expired",
            details={"approval_id": row["id"], "expires_at": row["expires_at"]},
        )
    if row["status"] in ("pending", "approved") and row["expires_at"] < now:
        expired_written = False
        if not dry_run:
            upd = conn.execute(
                "UPDATE approvals SET status = 'expired' WHERE id = ? AND status IN ('pending', 'approved');",
                (row["id"],),
            )
            if upd.rowcount == 1:
                expired_written = True
                record_event(
                    conn=conn,
                    type_="approval.expired",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    approval_id=row["id"],
                    payload={
                        "approval_id": row["id"],
                        "category": row["category"],
                        "op": row["op_type"],
                        "requested_by": row["requested_by"],
                        "expires_at": row["expires_at"],
                        "status": "expired",
                        "expired_on": "decide",
                    },
                )
                sync_plan_on_approval_terminal(
                    conn,
                    row["id"],
                    "approval_expired",
                    actor_id="system",
                    actor_kind="system",
                    via="system",
                )
        raise MeshError(
            code=c.APPROVAL_EXPIRED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{row['id']}' has expired",
            details={"approval_id": row["id"], "expires_at": row["expires_at"]},
            commit_tx=expired_written,
        )


def _enforce_approval_liveness_for_approve(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    action_label: str,
    *,
    allow_paused_project: bool = False,
) -> None:
    """Refuse approving an approval bound to a dead project or dead (terminal/progressed) task.

    Shared by ``handle_approval_decide`` and legacy ``_handle_approval_decision`` so server-side
    liveness refusal stays aligned across both RPC surfaces. ``handle_approval_decide`` (inline
    cards) always requires ``project.status == 'active'``; legacy ``approval.approve`` allows
    ``paused`` projects only for non-plan/non-step task ops (``task.assign`` / ``task.reassign`` /
    ``task.retry`` / ``task.reject``), which intentionally permit staging work while paused, while
    still blocking terminal projects (``completed`` / ``cancelled``) and all dead/progressed tasks.
    """
    from mesh.core.grants import evaluate_bound_task_for_approval

    approval_id = row["id"]
    if row["project_id"]:
        prow = conn.execute(
            "SELECT id, status FROM projects WHERE id = ?;",
            (row["project_id"],),
        ).fetchone()
        if prow is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Project '{row['project_id']}' not found",
                details={"project_id": row["project_id"]},
            )
        allowed_statuses = ("active", "paused") if allow_paused_project else ("active",)
        if prow["status"] not in allowed_statuses:
            raise MeshError(
                code=c.PROJECT_NOT_ACTIVE,
                exit_code=c.EXIT_CONFLICT,
                message=f"Project '{row['project_id']}' is {prow['status']}, not active",
                details={"project_id": row["project_id"], "status": prow["status"]},
            )
    if row["task_id"]:
        task_eval = evaluate_bound_task_for_approval(conn, row)
        if task_eval["task_terminal"]:
            t_status = task_eval["task_status"] if task_eval["task_status"] is not None else "missing"
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=(
                    f"Task '{row['task_id']}' is in terminal state '{t_status}'; "
                    f"cannot '{action_label}' a dead-task approval card"
                ),
                details={
                    "approval_id": approval_id,
                    "task_id": row["task_id"],
                    "task_status": t_status,
                    "task_kind": task_eval["task_kind"],
                    "reason": "task_terminal",
                },
            )
        if task_eval["task_progressed"]:
            t_status = task_eval["task_status"]
            expected_list = list(task_eval["expected_statuses"] or ())
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=(
                    f"Task '{row['task_id']}' has progressed to '{t_status}' "
                    f"(expected {', '.join(expected_list)} for '{row['op_type']}'); "
                    f"cannot '{action_label}' a stale task approval card"
                ),
                details={
                    "approval_id": approval_id,
                    "task_id": row["task_id"],
                    "task_status": t_status,
                    "task_kind": task_eval["task_kind"],
                    "op_type": row["op_type"],
                    "expected_task_statuses": expected_list,
                    "reason": "task_progressed",
                },
            )


def handle_approval_decide(ctx: HandlerContext) -> HandlerResult:
    """Inline approval card button handler: allow / always_allow / reject (direct user ONLY)."""
    if ctx.actor_kind != "user" or ctx.via != "direct":
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message="Command 'approval.decide' requires direct user authentication",
            details={"cmd": "approval.decide", "required": "user_direct", "actor_kind": ctx.actor_kind, "via": ctx.via},
        )
    if not ctx.k_user:
        raise MeshError(
            code=c.PASSPHRASE_REQUIRED,
            exit_code=c.EXIT_UNAUTHENTICATED,
            message="Direct user approval requires passphrase key",
        )

    approval_id = _require_nonempty_str(ctx.args.get("approval_id"), "approval_id", "approval_id is required")
    action = _require_nonempty_str(ctx.args.get("action"), "action", "action is required ('allow', 'always_allow', or 'reject')")
    if action not in ("allow", "always_allow", "reject"):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'action' must be 'allow', 'always_allow', or 'reject'",
            details={"param": "action", "expected": ["allow", "always_allow", "reject"], "got": action},
        )

    raw_reason = _validate_optional_str(ctx.args.get("reason"), "reason", max_len=4000)
    raw_note = _validate_optional_str(ctx.args.get("note"), "note", max_len=4000)
    if raw_reason is not None and raw_note is not None and raw_reason != raw_note:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Conflicting 'reason' and 'note' arguments",
            details={"param": "reason"},
        )
    chosen_note = raw_reason if raw_reason is not None else raw_note
    note = redact(chosen_note) if chosen_note else chosen_note

    confirm_auto_mode = ctx.args.get("confirm_auto_mode", False)
    if not isinstance(confirm_auto_mode, bool):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'confirm_auto_mode' must be a boolean",
            details={"param": "confirm_auto_mode", "got": confirm_auto_mode},
        )

    row = ctx.conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,)).fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Approval '{approval_id}' not found",
            details={"approval_id": approval_id},
        )

    # Verify stored op_json & approval.requested integrity
    try:
        recomputed_hash = verify_stored_op_json(row)
    except Exception:
        recomputed_hash = None
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

    is_plan_card = (row["category"] == "plan_dispatch" and row["op_type"] == "plan.dispatch")
    plan_row: Optional[sqlite3.Row] = None
    provided_op_hash = _validate_optional_str(ctx.args.get("op_hash"), "op_hash")
    provided_plan_hash = _validate_optional_str(ctx.args.get("plan_hash"), "plan_hash")
    provided_plan_id = _validate_optional_str(ctx.args.get("plan_id"), "plan_id")
    provided_plan_version = ctx.args.get("plan_version")

    if is_plan_card:
        plan_row = _load_and_verify_plan_for_approval(
            ctx.conn,
            row,
            provided_plan_hash,
            provided_plan_id=provided_plan_id,
            provided_plan_version=provided_plan_version,
        )
        if provided_op_hash is not None and provided_op_hash.strip() != row["op_hash"]:
            raise MeshError(
                code=c.OP_HASH_MISMATCH,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Provided op_hash does not match stored approval op_hash",
                details={"expected": row["op_hash"], "got": provided_op_hash.strip()},
            )
    else:
        req_op_hash = _require_nonempty_str(provided_op_hash, "op_hash", "Field 'op_hash' is required")
        if req_op_hash.strip() != row["op_hash"]:
            raise MeshError(
                code=c.OP_HASH_MISMATCH,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Provided op_hash does not match stored approval op_hash",
                details={"expected": row["op_hash"], "got": req_op_hash.strip()},
            )

    if is_approval_pinned_by_ws_reservation(ctx.conn, approval_id):
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Approval '{approval_id}' is pinned by an in-progress workspace operation",
            details={"approval_id": approval_id},
        )

    now = now_iso_utc()
    status = row["status"]
    decision = "rejected" if action == "reject" else "approved"

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
    if ev_terminal == "consumed" or status == "consumed":
        raise MeshError(
            code=c.APPROVAL_CONSUMED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was already consumed",
            details={"approval_id": approval_id},
        )
    if ev_terminal == "cancelled" or status == "cancelled":
        raise MeshError(
            code=c.APPROVAL_CANCELLED,
            exit_code=c.EXIT_APPROVAL_INVALID,
            message=f"Approval '{approval_id}' was cancelled",
            details={"approval_id": approval_id},
        )
    _expire_if_past_ttl(ctx.conn, row, now, dry_run=ctx.dry_run)

    if ev_terminal in ("approved", "rejected") and ev_terminal != decision:
        raise MeshError(
            code=c.DECISION_CONFLICT,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' was already {ev_terminal}; cannot {action}",
            details={"approval_id": approval_id, "status": ev_terminal},
        )
    if (decision == "approved" and status == "rejected") or (decision == "rejected" and status == "approved"):
        raise MeshError(
            code=c.DECISION_CONFLICT,
            exit_code=c.EXIT_CONFLICT,
            message=f"Approval '{approval_id}' was already {status}; cannot {action}",
            details={"approval_id": approval_id, "status": status},
        )

    has_consumed_ev = any(r["type"] == "approval.consumed" for r in ev_rows)
    if ev_terminal == decision and status != decision:
        ev_chan = ("relayed" if ev_dec["via"] == "delegated" else ev_dec["via"]) if ev_dec else None
        is_valid_upgrade = (decision in ("approved", "rejected") and ev_chan == "relayed")
        if not is_valid_upgrade and not is_legacy_consumed_approval(status, ev_terminal, ev_dec, has_consumed_ev):
            raise MeshError(
                code=c.APPROVAL_INVALID,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Database corruption detected: approval row status differs from event chain",
                details={"approval_id": approval_id, "row_status": status, "event_state": ev_terminal},
            )

    # Direct-user card clicks on an already-decided approval support two upgrades:
    # 1. Relayed -> Direct upgrade (for either 'approved' or 'rejected': signs user_mac and
    #    emits approval.decided with upgraded_from="relayed").
    # 2. Upgrading an unconsumed, unexpired approved card to `always_allow` (decision stays approved;
    #    standing grants are created or auto-mode switch is confirmed without duplicating direct approval.decided).
    upgrade_to_direct = (
        status == decision
        and status in ("approved", "rejected")
        and row["decided_via"] == "relayed"
    )
    upgrade_approved_to_always_allow = (status == decision == "approved" and action == "always_allow")

    if status == decision and not upgrade_to_direct and not upgrade_approved_to_always_allow:
        resp_idem: Dict[str, Any] = {
            "approval_id": approval_id,
            "action": action,
            "status": decision,
            "already_decided": True,
            "decided_via": row["decided_via"],
            "trust": "direct_verified" if row["decided_via"] == "direct" else "relayed_unverified",
            "user_mac": row["user_mac"],
        }
        if plan_row is not None:
            resp_idem["plan_id"] = plan_row["id"]
            resp_idem["plan_version"] = plan_row["version"]
            resp_idem["plan_hash"] = plan_row["plan_hash"]
            resp_idem["plan_status"] = plan_row["status"]
        return HandlerResult(data=resp_idem)

    if status == "pending" and is_plan_card and plan_row is not None and plan_row["status"] != "proposed":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Plan '{plan_row['id']}' is in status '{plan_row['status']}', expected 'proposed'",
            details={"plan_id": plan_row["id"], "status": plan_row["status"]},
        )

    # Handle 'always_allow' pre-validation & two-step confirmation for plan_dispatch / step_dispatch
    from mesh.core.grants import (
        create_or_get_standing_grant,
        extract_grant_scopes_from_approval,
        get_project_permission_mode,
        preview_grant_scope_for_card,
    )

    # Reject approving / granting from an inactive project or a dead (terminal/progressed) bound task.
    if decision == "approved":
        _enforce_approval_liveness_for_approve(ctx.conn, row, action)

    switch_to_auto = False
    already_auto_mode = False
    grant_scopes: List[Dict[str, Any]] = []
    if action == "always_allow":
        if row["category"] in ("plan_dispatch", "step_dispatch"):
            if not row["project_id"]:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message=f"Category '{row['category']}' requires a project_id to switch to auto mode",
                    details={"approval_id": approval_id},
                )
            preview = preview_grant_scope_for_card(ctx.conn, row, testing_mode=ctx.testing_mode)
            if not isinstance(preview, dict) or preview.get("kind") != "switch_permission_mode":
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Refusing 'always_allow': card preview cannot describe auto-mode switch for this approval",
                    details={"approval_id": approval_id, "category": row["category"]},
                )
            current_mode = get_project_permission_mode(ctx.conn, row["project_id"])
            if current_mode == "auto":
                # Project is already in 'auto' mode — no mode transition will occur, so skip the
                # switch-confirmation prompt and report truthful already_auto metadata.
                already_auto_mode = True
                switch_to_auto = True
            elif not confirm_auto_mode:
                prow = ctx.conn.execute(
                    "SELECT name FROM projects WHERE id = ?;",
                    (row["project_id"],),
                ).fetchone()
                project_name = (prow["name"] if prow and prow["name"] else None)
                project_label = project_name or row["project_id"]
                return HandlerResult(
                    data={
                        "confirmation_required": True,
                        "action": "always_allow",
                        "effect": "switch_project_to_auto_mode",
                        "approval_id": approval_id,
                        "category": row["category"],
                        "project_id": row["project_id"],
                        "project_name": project_name,
                        "current_permission_mode": current_mode,
                        "target_permission_mode": "auto",
                        "message": (
                            f"选择「始终允许」会将项目「{project_label}」的权限模式切换为「全自动 (auto)」："
                            "后续计划派发与超限重试将自动放行（提权操作 permission_escalation 仍需单独授权）。"
                            "请传入 confirm_auto_mode=true 确认此操作。"
                        ),
                    }
                )
            else:
                switch_to_auto = True
        else:
            grant_scopes = extract_grant_scopes_from_approval(
                ctx.conn, row, testing_mode=ctx.testing_mode
            )
            preview = preview_grant_scope_for_card(ctx.conn, row, testing_mode=ctx.testing_mode)
            preview_keys = (
                {s.get("scope_key") for s in preview.get("scopes", []) if isinstance(s, dict) and s.get("scope_key")}
                if isinstance(preview, dict)
                and preview.get("kind") == "standing_grant"
                and isinstance(preview.get("scopes"), list)
                else set()
            )
            actual_keys = {sc["scope_key"] for sc in grant_scopes}
            if not actual_keys or preview_keys != actual_keys:
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Refusing 'always_allow': card preview cannot describe every standing grant scope",
                    details={
                        "approval_id": approval_id,
                        "category": row["category"],
                        "preview_scopes": sorted(preview_keys),
                        "actual_scopes": sorted(actual_keys),
                    },
                )

    needs_decision_write = (status == "pending") or upgrade_to_direct
    if needs_decision_write:
        # Sign and persist direct user decision (or relayed -> direct upgrade)
        user_mac = compute_user_approval_mac(ctx.k_user, approval_id, row["op_hash"], decision, now)
        if upgrade_to_direct:
            upd_cur = ctx.conn.execute(
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
                WHERE id = ? AND status = ? AND decided_via = 'relayed';""",
                (decision, ctx.principal["id"], note, now, user_mac, approval_id, decision),
            )
        else:
            upd_cur = ctx.conn.execute(
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
                WHERE id = ? AND status = 'pending';""",
                (decision, ctx.principal["id"], note, now, user_mac, approval_id),
            )
        if upd_cur.rowcount != 1:
            raise MeshError(
                code=c.DECISION_CONFLICT,
                exit_code=c.EXIT_CONFLICT,
                message=f"Approval '{approval_id}' changed state concurrently",
                details={"approval_id": approval_id},
            )
    else:
        user_mac = row["user_mac"]

    events_emitted: List[str] = []
    created_grants: List[Dict[str, Any]] = []
    any_grant_created = False
    for sc in grant_scopes:
        gr, newly_created = create_or_get_standing_grant(
            ctx.conn,
            project_id=sc["project_id"],
            category=sc["category"],
            scope_agent_id=sc["scope_agent_id"],
            scope_profile=sc["scope_profile"],
            scope_config_key=sc["scope_config_key"],
            source_approval_id=approval_id,
            actor_id=ctx.principal["id"],
            k_user=ctx.k_user,
            request_id=ctx.request_id,
            now=now,
        )
        created_grants.append(gr)
        if newly_created:
            any_grant_created = True
            if "grant.created" not in events_emitted:
                events_emitted.append("grant.created")

    if needs_decision_write:
        dec_payload: Dict[str, Any] = {
            "decision": decision,
            "action": action,
            "note": note,
            "approval_id": approval_id,
            "category": row["category"],
            "op": row["op_type"],
            "op_hash": row["op_hash"],
        }
        if upgrade_to_direct:
            dec_payload["upgraded_from"] = "relayed"
        if plan_row is not None:
            dec_payload["plan_id"] = plan_row["id"]
            dec_payload["plan_version"] = plan_row["version"]
            dec_payload["plan_hash"] = plan_row["plan_hash"]
        if created_grants:
            dec_payload["grant_ids"] = [g["id"] for g in created_grants]
        if switch_to_auto and not already_auto_mode:
            dec_payload["switched_permission_mode_to"] = "auto"

        record_event(
            conn=ctx.conn,
            type_="approval.decided",
            actor_id=ctx.principal["id"],
            actor_kind="user",
            via="direct",
            credential_id=ctx.credential_id,
            project_id=row["project_id"],
            task_id=row["task_id"],
            approval_id=approval_id,
            reason=note,
            payload=dec_payload,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        events_emitted.insert(0, "approval.decided")

    new_plan_status: Optional[str] = None
    if is_plan_card and plan_row is not None:
        if status == "pending":
            new_plan_status = "approved_pending_dispatch" if decision == "approved" else "rejected"
            ctx.conn.execute(
                "UPDATE plans SET status = ?, updated_at = ? WHERE id = ?;",
                (new_plan_status, now, plan_row["id"]),
            )
            plan_ev_type = "plan.approved" if decision == "approved" else "plan.rejected"
            record_event(
                conn=ctx.conn,
                type_=plan_ev_type,
                actor_id=ctx.principal["id"],
                actor_kind="user",
                via="direct",
                credential_id=ctx.credential_id,
                project_id=row["project_id"],
                approval_id=approval_id,
                reason=note,
                payload={
                    "plan_id": plan_row["id"],
                    "version": plan_row["version"],
                    "plan_hash": plan_row["plan_hash"],
                    "approval_id": approval_id,
                    "action": action,
                    "from_status": "proposed",
                    "to_status": new_plan_status,
                    "reason": note,
                },
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            events_emitted.append(plan_ev_type)
        else:
            new_plan_status = plan_row["status"]

    mode_changed = False
    prev_mode: Optional[str] = None
    cancelled_step_approvals: List[str] = []
    cancelled_plan_approvals: List[str] = []
    if switch_to_auto and row["project_id"]:
        prev_mode = get_project_permission_mode(ctx.conn, row["project_id"])
        if prev_mode != "auto":
            ctx.conn.execute(
                "UPDATE projects SET permission_mode = 'auto', updated_at = ?, version = version + 1 WHERE id = ?;",
                (now, row["project_id"]),
            )
            record_event(
                conn=ctx.conn,
                type_="project.permission_mode_changed",
                actor_id=ctx.principal["id"],
                actor_kind="user",
                via="direct",
                credential_id=ctx.credential_id,
                project_id=row["project_id"],
                approval_id=approval_id,
                payload={
                    "project_id": row["project_id"],
                    "from_mode": prev_mode,
                    "to_mode": "auto",
                    "source": "always_allow",
                    "approval_id": approval_id,
                },
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            mode_changed = True
            events_emitted.append("project.permission_mode_changed")
            from mesh.core.approvals import (
                cancel_pending_plan_dispatch_approvals,
                cancel_pending_step_dispatch_approvals,
            )

            cancelled_step_approvals = cancel_pending_step_dispatch_approvals(
                ctx.conn,
                row["project_id"],
                from_mode=prev_mode,
                to_mode="auto",
                actor_id=ctx.principal["id"],
                actor_kind="user",
                via="direct",
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            cancelled_plan_approvals = cancel_pending_plan_dispatch_approvals(
                ctx.conn,
                row["project_id"],
                from_mode=prev_mode,
                to_mode="auto",
                actor_id=ctx.principal["id"],
                actor_kind="user",
                via="direct",
                request_id=ctx.request_id,
                k_user=ctx.k_user,
            )
            if cancelled_step_approvals or cancelled_plan_approvals:
                events_emitted.append("approval.cancelled")

    resp: Dict[str, Any] = {
        "approval_id": approval_id,
        "category": row["category"],
        "action": action,
        "status": decision,
        "decided_via": "direct",
        "trust": "direct_verified",
        "user_mac": user_mac,
        "confirmation_required": False,
    }
    if upgrade_to_direct:
        resp["upgraded_from"] = "relayed"
    if upgrade_approved_to_always_allow:
        resp["upgraded_to_always_allow"] = True
        if not upgrade_to_direct:
            resp["already_decided"] = True
    if plan_row is not None:
        resp["plan_id"] = plan_row["id"]
        resp["plan_version"] = plan_row["version"]
        resp["plan_hash"] = plan_row["plan_hash"]
        resp["plan_status"] = new_plan_status
    if created_grants:
        resp["grant"] = created_grants[0]
        resp["grants"] = created_grants
        resp["grant_created"] = any_grant_created
    if switch_to_auto:
        resp["permission_mode"] = "auto"
        resp["previous_permission_mode"] = prev_mode
        resp["mode_changed"] = mode_changed
        resp["already_auto"] = already_auto_mode
        resp["cancelled_step_approvals"] = cancelled_step_approvals
        resp["cancelled_plan_approvals"] = cancelled_plan_approvals
        if mode_changed:
            resp["message"] = "已批准当前操作，并将项目权限模式切换为「全自动 (auto)」。"
        elif needs_decision_write:
            resp["message"] = "已批准当前操作；当前项目已处于「全自动 (auto)」模式，无需重复切换。"
        else:
            resp["message"] = "当前操作已批准，且项目已处于「全自动 (auto)」模式，无需重复切换。"

    return HandlerResult(data=resp, events=events_emitted)


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

    # Plan dispatch approvals are strictly direct-user-only and require exact plan_hash so plan & approval never drift.
    if row["category"] == "plan_dispatch" and row["op_type"] == "plan.dispatch":
        if ctx.actor_kind != "user" or ctx.via != "direct":
            raise MeshError(
                code=c.CHANNEL_NOT_ALLOWED,
                exit_code=c.EXIT_APPROVAL_INVALID,
                message="Plan dispatch approvals require direct user approval on the plan card",
                details={"approval_id": approval_id, "category": "plan_dispatch"},
            )
        plan_action = "allow" if decision == "approved" else "reject"
        sub_args = dict(ctx.args)
        sub_args["action"] = plan_action
        sub_ctx = HandlerContext(
            conn=ctx.conn,
            principal=ctx.principal,
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            request_id=ctx.request_id,
            home=ctx.home,
            cmd="approval.decide",
            args=sub_args,
            dry_run=ctx.dry_run,
            k_user=ctx.k_user,
            effects=ctx.effects,
            testing_mode=ctx.testing_mode,
            credential_id=ctx.credential_id,
        )
        return handle_approval_decide(sub_ctx)

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
            decision in ("approved", "rejected")
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

    if status == "expired" or (status in ("pending", "approved") and row["expires_at"] < now):
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
    upgrade_to_direct = (
        status == decision
        and status in ("approved", "rejected")
        and row["decided_via"] == "relayed"
        and not delegated
        and ctx.actor_kind == "user"
        and ctx.via == "direct"
    )
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

    # Reject approving dead cards (terminal project, paused plan/step card, or terminal/progressed
    # bound task) on the legacy RPC surface too. Non-plan/non-step task ops permit staging approvals
    # while a project is paused (matching task.assign/task.reassign/task.retry).
    if decision == "approved":
        _enforce_approval_liveness_for_approve(
            ctx.conn,
            row,
            "approve",
            allow_paused_project=(row["category"] not in ("plan_dispatch", "step_dispatch")),
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
    if status == "approved" and ctx.actor_kind != "user" and (
        row["category"] in ("plan_dispatch", "step_dispatch", "permission_escalation")
        or row["decided_via"] == "direct"
    ):
        raise MeshError(
            code=c.CAPABILITY_MISSING,
            exit_code=c.EXIT_FORBIDDEN,
            message=(
                f"Only the direct user can cancel an approved '{row['category']}' approval; "
                f"agents and leads may only cancel pending requests"
            ),
            details={
                "approval_id": approval_id,
                "status": status,
                "category": row["category"],
                "decided_via": row["decided_via"],
                "required": "user_direct",
            },
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
    sync_plan_on_approval_terminal(
        ctx.conn,
        approval_id,
        cancel_reason or default_reason,
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
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

    # 1. Authority: the requester only (else 4 NOT_REQUESTER, G19).
    # Note on lead-change asymmetry: single-use approvals bind `requested_by`, so a lead change
    # between approval creation and consume invalidates the approval (the new lead must re-request),
    # whereas project-scoped standing grants (`permission_escalation`, `retry_over_limit`) survive
    # lead changes until explicitly revoked by the direct user via `grant.revoke`.
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
    if category in ("plan_dispatch", "step_dispatch"):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Approval category '{category}' is internal-only and cannot be requested via approval.request",
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
    # Note: 'config' writes remain strictly direct-user-only and do not support standing grants.
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
    """List approvals with optional status, project, actionable, include_ids, limit, and card hydration filters."""
    status_filter = _validate_optional_str(ctx.args.get("status"), "status")
    project_filter = _validate_optional_str(ctx.args.get("project"), "project")
    actionable_only = ctx.args.get("actionable_only", False)
    if not isinstance(actionable_only, bool):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'actionable_only' must be a boolean",
            details={"param": "actionable_only"},
        )
    include_card_fields = ctx.args.get("include_card_fields", False)
    if not isinstance(include_card_fields, bool):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Field 'include_card_fields' must be a boolean",
            details={"param": "include_card_fields"},
        )
    raw_include_ids = ctx.args.get("include_ids")
    include_ids: List[str] = []
    if raw_include_ids is not None:
        if not isinstance(raw_include_ids, list) or len(raw_include_ids) > 200:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'include_ids' must be a list of at most 200 approval IDs",
                details={"param": "include_ids"},
            )
        seen_inc: set[str] = set()
        for aid in raw_include_ids:
            if not isinstance(aid, str) or not aid.strip():
                raise MeshError(
                    code=c.INVALID_ARGUMENT,
                    exit_code=c.EXIT_USAGE,
                    message="Every entry in 'include_ids' must be a non-empty string",
                    details={"param": "include_ids"},
                )
            if aid not in seen_inc:
                seen_inc.add(aid)
                include_ids.append(aid)

    raw_limit = ctx.args.get("limit")
    limit: Optional[int] = None
    if raw_limit is not None:
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1 or raw_limit > 500:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="Field 'limit' must be an integer between 1 and 500",
                details={"param": "limit"},
            )
        limit = raw_limit

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

    is_user_or_relayer = (ctx.actor_kind == "user") or ("relay" in ctx.principal.get("capabilities", []))
    agent_id = ctx.principal["id"] if ctx.actor_kind != "user" else None

    def _run_approval_query(extra_where: str, extra_params: List[Any], *, apply_limit: Optional[int]) -> List[sqlite3.Row]:
        q = "SELECT * FROM approvals WHERE 1=1"
        p: List[Any] = []
        if project_filter:
            q += " AND project_id = ?"
            p.append(project_filter)
        if extra_where:
            q += f" AND ({extra_where})"
            p.extend(extra_params)
        if apply_limit is not None and is_user_or_relayer:
            q += " ORDER BY requested_at DESC, id DESC LIMIT ?;"
            p.append(apply_limit)
            fetched = list(ctx.conn.execute(q, p).fetchall())
            fetched.reverse()
            return fetched
        if apply_limit is not None:
            q += " ORDER BY requested_at DESC, id DESC;"
            fetched = list(ctx.conn.execute(q, p).fetchall())
            return fetched
        q += " ORDER BY requested_at ASC, id ASC;"
        return list(ctx.conn.execute(q, p).fetchall())

    where_clauses: List[str] = []
    where_params: List[Any] = []
    if status_filter:
        where_clauses.append("status = ?")
        where_params.append(status_filter)
    elif actionable_only:
        where_clauses.append(
            "(status = 'pending'"
            " OR (decided_via = 'relayed' AND status IN ('approved', 'rejected'))"
            " OR (status = 'approved' AND decided_via = 'direct' AND consumed_at IS NULL))"
        )

    base_rows = _run_approval_query(
        " AND ".join(where_clauses),
        where_params,
        apply_limit=limit,
    )

    if not is_user_or_relayer:
        filtered_base: List[sqlite3.Row] = []
        for r in base_rows:
            if r["requested_by"] == agent_id or (
                r["project_id"] and can_principal_read_project(ctx.conn, agent_id, r["project_id"])
            ):
                filtered_base.append(r)
        if limit is not None:
            filtered_base = filtered_base[:limit]
            filtered_base.reverse()
        rows = filtered_base
    else:
        rows = base_rows

    if include_ids:
        existing_ids = {r["id"] for r in rows}
        missing_ids = [aid for aid in include_ids if aid not in existing_ids]
        if missing_ids:
            placeholders = ",".join("?" for _ in missing_ids)
            inc_rows = _run_approval_query(
                f"id IN ({placeholders})",
                list(missing_ids),
                apply_limit=None,
            )
            for r in inc_rows:
                if not is_user_or_relayer:
                    if r["requested_by"] != agent_id and not (
                        r["project_id"] and can_principal_read_project(ctx.conn, agent_id, r["project_id"])
                    ):
                        continue
                rows.append(r)
            rows.sort(key=lambda r: ((r["requested_at"] or ""), r["id"]))

    prefetched_projects: Dict[str, Any] = {}
    prefetched_tasks: Dict[str, Any] = {}
    prefetched_plans_by_apr: Dict[str, Dict[str, Any]] = {}
    now_ts: Optional[str] = None
    if include_card_fields and rows:
        from mesh.core.grants import compute_approval_card_actions, serialize_approval_card_summary

        now_ts = now_iso_utc()
        pids = sorted({r["project_id"] for r in rows if r["project_id"]})
        if pids:
            ph = ",".join("?" for _ in pids)
            for prow in ctx.conn.execute(
                f"SELECT id, status, permission_mode, plan_gate_mode FROM projects WHERE id IN ({ph});",
                tuple(pids),
            ).fetchall():
                prefetched_projects[prow["id"]] = prow
        tids = sorted({r["task_id"] for r in rows if r["task_id"]})
        if tids:
            ph = ",".join("?" for _ in tids)
            for trow in ctx.conn.execute(
                f"SELECT id, status, kind FROM tasks WHERE id IN ({ph});",
                tuple(tids),
            ).fetchall():
                prefetched_tasks[trow["id"]] = trow
        plan_apr_ids = sorted({r["id"] for r in rows if r["category"] == "plan_dispatch"})
        if plan_apr_ids:
            has_plans = ctx.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plans';"
            ).fetchone()
            if has_plans:
                ph = ",".join("?" for _ in plan_apr_ids)
                for plrow in ctx.conn.execute(
                    f"SELECT id, approval_id, version, status, plan_hash FROM plans WHERE approval_id IN ({ph});",
                    tuple(plan_apr_ids),
                ).fetchall():
                    if plrow["approval_id"]:
                        prefetched_plans_by_apr[plrow["approval_id"]] = dict(plrow)

    items = []
    for r in rows:
        d = _format_approval_dict(ctx.conn, r)
        if include_card_fields:
            _, policy = get_row_approval_policy(ctx.conn, r)
            pl_meta = prefetched_plans_by_apr.get(r["id"])
            card_ui = compute_approval_card_actions(
                ctx.conn,
                r,
                now=now_ts,
                testing_mode=ctx.testing_mode,
                plan_status=pl_meta["status"] if pl_meta is not None else None,
                prefetched_project=prefetched_projects.get(r["project_id"]) if r["project_id"] else None,
                prefetched_tasks=prefetched_tasks,
            )
            card_summary = serialize_approval_card_summary(
                r,
                policy=policy,
                card_ui=card_ui,
                plan_meta=pl_meta,
            )
            d.update(card_summary)
        if ctx.args.get("with_channel"):
            d["decision_channel"] = _decision_channel(ctx.conn, ctx, r)
        items.append(d)

    return HandlerResult(data={"items": items})
