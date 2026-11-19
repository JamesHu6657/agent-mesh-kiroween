"""Task command handlers per plan §6.2, §7.3, §9.1, §9.4, §13.3."""

import asyncio
import json
import os
import shlex
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from mesh.core.redact import redact, redact_value
import mesh.constants as c
from mesh.adapters.base import (
    AdapterNotInstalled,
    AdapterUnsupported,
    EffortNotAllowed,
    EffortUnsupported,
    ProfileUnsupported,
    PromptTooLong,
)
from mesh.adapters.registry import get_adapter
from mesh.core.approvals import (
    check_and_consume_approval,
    create_or_get_pending_approval,
)
from mesh.core.authz import check_authz, get_agent_capabilities, is_project_lead, lead_run_token_scope
from mesh.core.clock import now_iso_utc, parse_duration
from mesh.core.events import last_rejection, record_event
from mesh.core.ids import generate_id
from mesh.core.leases import check_run_session, get_lease_ttl_s, verify_task_lease_and_fence, remember_run_session
from mesh.core.policy import parse_json_str_list, resolve_model_and_effort, validate_resolved_effort
from mesh.core.prompts import (
    PROMPT_LIMIT_BYTES,
    deliverable_summary,
    prompt_strategy,
    read_rework_feedback,
    render_executor_prompt,
)
from mesh.core.states import TASK_STATES, check_task_transition, is_task_terminal
from mesh.core.db import transaction
from mesh.core.repos import submission_git_info
from mesh.core.token_roots import remember_protected_path
from mesh.core.workspaces import (
    WorkspaceError,
    compute_task_workspace_path,
    get_workspaces_root,
    has_task_ws_reservation,
    plan_code_workspace,
    run_code_workspace_plan,
    task_branch_name,
)
from mesh.daemon.handlers.base import Effect, HandlerContext, HandlerResult
from mesh.daemon.handlers.inbox import _recheck_wait_authz
from mesh.daemon.scheduler import ack_window_now, compute_queue_info, fail_queued_task_t6
from mesh.errors import MeshError

PROFILE_RANKS: Dict[str, int] = {
    "readonly": 0,
    "edit": 1,
    "dev": 2,
    "elevated": 3,
    "unrestricted": 4,
}


def heartbeat_every_s(lease_ttl_s: int) -> int:
    """Recommended heartbeat interval: a third of the lease TTL, so two beats can be
    missed before the lease expires."""
    return max(1, int(lease_ttl_s) // 3)


def _lease_timing(lease_ttl_s: int, expires_at: Optional[str], timeout_s: Any,
                  heartbeat_at: Optional[str] = None) -> Dict[str, Any]:
    """Lease/deadline facts an inbox worker needs to keep its run alive.

    An inbox (external) run has no daemon-enforced deadline: it ends when the worker
    submits/fails or when the lease expires. ``timeout_s`` is the task's time budget
    (what a launch run would be killed at), reported for planning only.
    """
    info: Dict[str, Any] = {
        "lease_ttl_s": lease_ttl_s,
        "lease_expires_at": expires_at,
        "heartbeat_every_s": heartbeat_every_s(lease_ttl_s),
        "timeout_s": timeout_s,
        "deadline_at": None,
        "deadline_enforced": False,
    }
    if heartbeat_at:
        info["last_heartbeat_at"] = heartbeat_at
    return info


def _get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    """Fetch task by ID or raise NOT_FOUND."""
    cur = conn.execute(
        """SELECT t.*, p.name as assignee_name
           FROM tasks t
           LEFT JOIN principals p ON t.assignee_agent_id = p.id
           WHERE t.id = ?;""",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Task '{task_id}' not found",
            details={"task_id": task_id},
        )
    return row


def _format_task(
    row: sqlite3.Row,
    conn: Optional[sqlite3.Connection] = None,
    depends_on: Optional[List[str]] = None,
    depended_on_by: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Format task row into API response dictionary."""
    deliverable = None
    submitted_by = None
    submitted_at = None
    if row["deliverable_json"]:
        try:
            d_raw = json.loads(row["deliverable_json"])
            if isinstance(d_raw, dict) and "deliverable" in d_raw and "submitted_by" in d_raw:
                deliverable = d_raw["deliverable"]
                submitted_by = d_raw["submitted_by"]
                submitted_at = d_raw.get("submitted_at")
            else:
                deliverable = d_raw
        except Exception:
            pass

    failure = None
    if row["failure_json"]:
        try:
            failure = json.loads(row["failure_json"])
        except Exception:
            pass

    merge = None
    if row["merge_json"]:
        try:
            merge = json.loads(row["merge_json"])
        except Exception:
            pass

    assignee_name = row["assignee_name"] if "assignee_name" in row.keys() and row["assignee_name"] else None

    queue_position = None
    blocked_reason = None
    if row["status"] == "queued" and conn is not None:
        queue_position, blocked_reason = compute_queue_info(conn, row["id"])

    history: List[Dict[str, Any]] = []
    if conn is not None:
        cur_ev = conn.execute(
            """SELECT seq, ts, type, reason, payload_json
               FROM events
               WHERE task_id = ?
                 AND type IN ('task.failed', 'task.rejected', 'task.retried', 'task.reassigned', 'task.escalated',
                              'task.lease_expired', 'task.closed')
               ORDER BY seq ASC;""",
            (row["id"],),
        )
        for erow in cur_ev.fetchall():
            att = None
            pld = {}
            if erow["payload_json"]:
                try:
                    pld = json.loads(erow["payload_json"])
                    att = pld.get("attempt")
                except Exception:
                    pld = {}
            item = {
                "ts": erow["ts"],
                "type": erow["type"],
                "reason": erow["reason"],
                "attempt": att,
            }
            if erow["type"] == "task.lease_expired":
                item.update({k: pld.get(k) for k in ("run_id", "fence", "holder", "task_status")})
            elif erow["type"] == "task.closed":
                item.update({k: pld.get(k) for k in ("superseded_by", "from_state")})
            history.append(item)

    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "title": row["title"],
        "spec": row["spec"],
        "kind": row["kind"],
        "status": row["status"],
        "priority": row["priority"],
        "assignee": assignee_name or row["assignee_agent_id"],
        "assignee_agent_id": row["assignee_agent_id"],
        "model": row["model"],
        "effort": row["effort"],
        "profile": row["profile"],
        "timeout_s": row["timeout_s"],
        "idle_timeout_s": row["idle_timeout_s"],
        "max_attempts": row["max_attempts"],
        "attempt": row["attempt"],
        "fence_seq": row["fence_seq"],
        "queued_at": row["queued_at"],
        "offered_at": row["offered_at"],
        "queue_position": queue_position,
        "blocked_reason": blocked_reason,
        "workspace_path": row["workspace_path"],
        "branch": row["branch"],
        "base_commit": row["base_commit"],
        "last_session_id": row["last_session_id"],
        "deliverable": deliverable,
        "submitted_by": submitted_by,
        "submitted_at": submitted_at,
        "failure": failure,
        "merge": merge,
        "last_rejection": last_rejection(conn, row["id"]) if conn is not None else None,
        "superseded_by": (superseded_by(conn, row["id"]) if conn is not None and row["status"] == "cancelled"
                          else None),
        "created_by": row["created_by"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "depends_on": depends_on if depends_on is not None else [],
        "depended_on_by": depended_on_by if depended_on_by is not None else [],
        "history": history,
    }


def _created_profile_override(conn: sqlite3.Connection, task: sqlite3.Row) -> Optional[str]:
    """The profile `task assign` keeps for an unassigned task (blind review P2): the one
    given at create time, or None (the assignee's default) when none was given. Tasks
    created before ``profile_explicit`` was recorded keep their stored profile."""
    row = conn.execute(
        "SELECT payload_json FROM events WHERE task_id = ? AND type = 'task.created' ORDER BY seq ASC LIMIT 1;",
        (task["id"],)).fetchone()
    if row is not None:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if payload.get("profile_explicit") is False:
            return None
    return task["profile"]


def _reassigned_profile_override(
    conn: sqlite3.Connection,
    task: sqlite3.Row,
    to_agent: str,
    testing_mode: bool,
) -> Optional[str]:
    """Default profile override on `task reassign` when `--profile` is omitted.

    If the task's current profile came only from a previous agent's default
    (``profile_explicit: false``), let the new agent's default apply. If the
    task's profile was explicit (e.g. ``elevated`` or ``dev``), keep it unless
    the target launch adapter does not support that profile (e.g. ``dev`` ->
    ``agy``/``kiro``), in which case fall back to the target agent's default.
    """
    ev = conn.execute(
        """SELECT payload_json FROM events
           WHERE task_id = ? AND type IN ('task.created', 'task.assigned', 'task.reassigned')
           ORDER BY seq DESC LIMIT 1;""",
        (task["id"],),
    ).fetchone()
    if ev is not None:
        try:
            payload = json.loads(ev["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if payload.get("profile_explicit") is False:
            return None

    ag = conn.execute(
        """SELECT a.adapter, a.delivery FROM agents a JOIN principals p ON a.id = p.id
           WHERE a.id = ? OR p.name = ?;""",
        (to_agent, to_agent),
    ).fetchone()
    if ag is not None and ag["delivery"] == "launch":
        try:
            adapter = get_adapter(ag["adapter"], testing_mode=testing_mode)
            adapter.profile_flags(task["profile"])
        except ProfileUnsupported:
            return None
        except Exception:
            pass
    return task["profile"]


_MAX_INT64 = 9223372036854775807


def _require_str_param(val: Any, param: str, message: Optional[str] = None, *, strip: bool = False) -> str:
    if not isinstance(val, str) or not val.strip():
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=message or f"Parameter '{param}' must be a non-empty string",
            details={"param": param},
        )
    return val.strip() if strip else val


def _optional_str_param(val: Any, param: str) -> Optional[str]:
    if val is not None and not isinstance(val, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Parameter '{param}' must be a string",
            details={"param": param, "got": type(val).__name__},
        )
    return val


def _optional_bool_param(val: Any, param: str, default: bool = False) -> bool:
    if val is None:
        return default
    if not isinstance(val, bool):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Parameter '{param}' must be a boolean",
            details={"param": param, "got": type(val).__name__},
        )
    return val


def _validate_session_param(val: Any) -> Optional[str]:
    if val is None:
        return None
    if not isinstance(val, str) or "\x00" in val or len(val) > 256:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter 'session' must be a string up to 256 characters without NUL bytes",
            details={"param": "session"},
        )
    return val or None


def _validate_fence_param(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (bool, float)) or not isinstance(val, int) or not (0 <= val <= _MAX_INT64):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter 'fence' must be a non-negative 64-bit integer",
            details={"param": "fence"},
        )
    return val


def _parse_strict_int(val: Any, param: str, *, allow_s_suffix: bool = False) -> int:
    if isinstance(val, (bool, float)):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid {param}: {val}",
            details={"param": param},
        )
    if isinstance(val, str):
        if len(val) > 64:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid {param}: {val[:64]}",
                details={"param": param},
            )
        raw = val[:-1] if (allow_s_suffix and val.endswith("s")) else val
    elif isinstance(val, int):
        raw = val
    else:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid {param}: {val}",
            details={"param": param},
        )
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid {param}: {val}",
            details={"param": param},
        )


def _validate_profile_and_effort(profile_override: Optional[str], effort_override: Optional[str]) -> None:
    """Validate profile and effort overrides and check profile/effort swap (§9.1)."""
    valid_profiles = set(PROFILE_RANKS.keys())
    valid_efforts = {"low", "medium", "high", "xhigh", "max", "ultra", "ultracode"}
    if profile_override is not None:
        _optional_str_param(profile_override, "profile")
        if profile_override in valid_efforts:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Effort level '{profile_override}' cannot be used as a permission profile",
                details={"param": "profile", "value": profile_override},
            )
        if profile_override not in valid_profiles:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid profile '{profile_override}': must be one of {sorted(valid_profiles)}",
                details={"param": "profile", "value": profile_override},
            )

    if effort_override is not None:
        _optional_str_param(effort_override, "effort")
        if effort_override in valid_profiles:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Permission profile '{effort_override}' cannot be used as an effort level",
                details={"param": "effort", "value": effort_override},
            )


def _validate_model_and_effort(
    agent: sqlite3.Row,
    model_override: Optional[str],
    effort_override: Optional[str],
    current_model: Optional[str] = None,
    current_effort: Optional[str] = None,
    testing_mode: bool = False,
    check_resolved_model: bool = False,
    check_effort_before_stale_model: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Validate model and effort overrides against agent allowed lists and adapter capabilities (AG Step 5)."""
    allowed_models = parse_json_str_list(agent["allowed_models_json"])
    resolved_model, resolved_effort = resolve_model_and_effort(
        model=model_override,
        current_model=current_model,
        default_model=agent["default_model"],
        effort=effort_override,
        current_effort=current_effort,
        default_effort=agent["default_effort"],
    )

    def _require_allowed_model(model: Optional[str]) -> None:
        if model and model not in allowed_models:
            raise MeshError(
                code=c.MODEL_NOT_ALLOWED,
                exit_code=c.EXIT_USAGE,
                message=f"Model '{model}' is not allowed for agent '{agent['name']}'",
                details={"model": model, "allowed": allowed_models},
            )

    _require_allowed_model(
        resolved_model if (check_resolved_model and not check_effort_before_stale_model) else model_override
    )

    allowed_efforts = parse_json_str_list(agent["allowed_efforts_json"])
    adapter_inst = get_adapter(agent["adapter"], testing_mode=testing_mode) if resolved_effort else None
    validate_resolved_effort(
        adapter_id=agent["adapter"],
        resolved_model=resolved_model,
        resolved_effort=resolved_effort,
        allowed_efforts=allowed_efforts,
        agent_name=agent["name"],
        adapter=adapter_inst,
        testing_mode=testing_mode,
    )

    if check_resolved_model and check_effort_before_stale_model:
        _require_allowed_model(resolved_model)

    return resolved_model, resolved_effort


def _requeue_approval_gates(
    ctx: HandlerContext,
    task: sqlite3.Row,
    ag_row: Optional[sqlite3.Row],
) -> Tuple[str, bool, bool, bool]:
    """Compute (resolved_profile, need_retry, need_perm, will_gate_approval) for retry/reject."""
    resolved_profile = task["profile"] or (ag_row["default_profile"] if ag_row else None) or "edit"
    need_retry = task["attempt"] >= task["max_attempts"]
    max_unapproved = (ag_row["max_unapproved_profile"] if ag_row else None) or "dev"
    need_perm = bool(ag_row) and PROFILE_RANKS.get(resolved_profile, 0) > PROFILE_RANKS.get(max_unapproved, 2)
    will_gate_approval = (need_retry or need_perm) and ctx.actor_kind != "user" and ctx.via != "direct"
    return resolved_profile, need_retry, need_perm, will_gate_approval


def _check_spec_prompt_length(
    conn: sqlite3.Connection,
    project_id: str,
    adapter_id: str,
    profile: str,
    task_doc: Dict[str, Any],
    workspace: str,
    attempt: int,
    max_attempts: int,
    rework: Optional[Dict[str, Any]] = None,
) -> None:
    """Enforce plan §12.5 prompt-length refusal when a caller exists (create/assign/reassign/retry)."""
    if adapter_id == "fake":
        return
    project_row = conn.execute("SELECT id, name FROM projects WHERE id = ?;", (project_id,)).fetchone()
    if not project_row:
        return
    criteria = [
        r["text"]
        for r in conn.execute(
            "SELECT text FROM acceptance_criteria WHERE project_id = ? AND superseded_at IS NULL ORDER BY ordinal ASC;",
            (project_id,),
        ).fetchall()
    ]
    branch = task_doc.get("branch")
    if not branch and task_doc.get("kind") == "code":
        branch = task_branch_name(project_id, task_doc["id"])
    prompt_text = render_executor_prompt(
        task=task_doc,
        project=project_row,
        criteria=criteria,
        workspace=workspace,
        attempt=attempt,
        max_attempts=max_attempts,
        rework=rework,
        branch=branch,
    )
    prompt_bytes = len(prompt_text.encode("utf-8"))
    if prompt_strategy(adapter_id, profile, prompt_bytes, PROMPT_LIMIT_BYTES) == "too_long":
        raise PromptTooLong(prompt_bytes, PROMPT_LIMIT_BYTES, adapter_id, profile)


def _require_task_approval(
    conn: sqlite3.Connection,
    ctx: HandlerContext,
    op: str,
    project_id: str,
    task_id: Optional[str],
    params: Dict[str, Any],
    need_retry: bool,
    need_perm: bool,
    retry_summary: str = "",
    perm_summary: str = "",
    compound_summary: str = "",
    retry_message: str = "",
    perm_message: str = "",
    compound_message: str = "",
) -> None:
    """Create or consume a single/compound approval for a task mutation (§7.4)."""
    if not (need_retry or need_perm):
        return
    if ctx.actor_kind == "user" or ctx.via == "direct":
        return

    gate_params = dict(params)
    if need_retry and need_perm:
        category = "retry_over_limit"
        extra_categories = ["permission_escalation"]
        gate_params["gates"] = ["permission_escalation", "retry_over_limit"]
        summary = compound_summary
        message = compound_message
    elif need_retry:
        category = "retry_over_limit"
        extra_categories = []
        summary = retry_summary
        message = retry_message
    else:
        category = "permission_escalation"
        extra_categories = []
        summary = perm_summary
        message = perm_message

    if not ctx.approval:
        apr_info = create_or_get_pending_approval(
            conn=conn,
            category=category,
            op=op,
            requested_by=ctx.principal["id"],
            params=gate_params,
            summary=summary,
            project_id=project_id,
            target_id=task_id,
            task_id=task_id,
            dry_run=ctx.dry_run,
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            request_id=ctx.request_id,
            k_user=ctx.k_user,
            testing_mode=ctx.testing_mode,
        )
        raise MeshError(
            code=c.APPROVAL_REQUIRED,
            exit_code=c.EXIT_APPROVAL_REQUIRED,
            message=message,
            details=apr_info,
        )

    check_and_consume_approval(
        conn=conn,
        approval_id=ctx.approval,
        op=op,
        category=category,
        caller_id=ctx.principal["id"],
        params=gate_params,
        project_id=project_id,
        target_id=task_id,
        request_id=ctx.request_id,
        extra_categories=extra_categories,
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        k_user=ctx.k_user,
    )


def _validate_assignment_guards(
    conn: sqlite3.Connection,
    ctx: HandlerContext,
    project_id: str,
    assignee_ident: str,
    profile_override: Optional[str] = None,
    model_override: Optional[str] = None,
    effort_override: Optional[str] = None,
    task_title: str = "",
    op: str = "task.create",
    task_id: Optional[str] = None,
    approval_params_extra: Optional[Dict[str, Any]] = None,
    prompt_check: Optional[Dict[str, Any]] = None,
    check_approval: bool = True,
) -> Tuple[sqlite3.Row, str, Optional[str], Optional[str]]:
    """Execute Assignment Guards (AG) 1-6 in order per plan §6.2.

    Returns:
        (agent_row, resolved_profile, resolved_model, resolved_effort)
    """
    # 1. Agent exists and is enabled; not current lead of project
    cur = conn.execute(
        """SELECT a.*, p.name, p.disabled_at
           FROM agents a
           JOIN principals p ON a.id = p.id
           WHERE a.id = ? OR p.name = ?;""",
        (assignee_ident, assignee_ident),
    )
    agent = cur.fetchone()
    if not agent:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Agent '{assignee_ident}' not found",
            details={"agent": assignee_ident},
        )
    if agent["disabled_at"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is disabled",
            details={"agent": agent["name"], "reason": "disabled"},
        )

    # Check if agent is current lead (ACC: E9)
    cur_lead = conn.execute(
        "SELECT agent_id FROM lead_assignments WHERE project_id = ? AND ended_at IS NULL LIMIT 1;",
        (project_id,),
    )
    lead_row = cur_lead.fetchone()
    if lead_row and lead_row["agent_id"] == agent["id"]:
        raise MeshError(
            code=c.LEAD_CANNOT_BE_ASSIGNEE,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is the current lead of project '{project_id}' and cannot be assignee",
            details={"agent": agent["name"], "project_id": project_id},
        )

    # Validate profile and effort overrides and check profile/effort swap (§9.1)
    _validate_profile_and_effort(profile_override, effort_override)

    resolved_profile = profile_override or agent["default_profile"] or "edit"
    delivery = agent["delivery"]

    # Steps 2-4: only for delivery == launch (inbox agents skip 2-4)
    if delivery == "launch":
        adapter = get_adapter(agent["adapter"], testing_mode=ctx.testing_mode)

        # 2. Adapter installed
        if adapter.kind == "test_double" and not ctx.testing_mode:
            raise MeshError(
                code=c.TEST_DOUBLE_DISABLED,
                exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
                message="Test double adapter 'fake' is only available when MESH_TESTING=1",
            )
        if not adapter.is_installed(binary_path=agent["binary_path"]):
            raise AdapterNotInstalled(agent["adapter"])

        # 3. Profile supported by adapter (ACC: N5)
        try:
            adapter.profile_flags(resolved_profile)
        except ProfileUnsupported:
            raise ProfileUnsupported(agent["adapter"], resolved_profile)

        # 4. Effective launch_enabled from probe + smoke evidence (ACC: N4, N19)
        from mesh.adapters.evidence import effective_launch

        ev = effective_launch(ctx.conn, adapter, ctx.testing_mode, agent["binary_path"])
        if not ev.ok:
            raise AdapterUnsupported(
                f"Adapter '{agent['adapter']}' is not enabled for launch"
                + (f" ({ev.reason})" if ev.reason else ""),
                milestone=ev.milestone or adapter.launch_milestone or "M3",
                reason=ev.reason,
            )

    # 5. Model and effort resolve and are allowed (ACC: D3, D7, D17)
    resolved_model, resolved_effort = _validate_model_and_effort(
        agent=agent,
        model_override=model_override,
        effort_override=effort_override,
        testing_mode=ctx.testing_mode,
    )

    # Plan §12.5: check spec-rendered prompt length before requesting/consuming approval
    if delivery == "launch" and prompt_check is not None:
        _check_spec_prompt_length(
            conn=conn,
            project_id=project_id,
            adapter_id=agent["adapter"],
            profile=resolved_profile,
            task_doc=prompt_check["task_doc"],
            workspace=prompt_check["workspace"],
            attempt=prompt_check["attempt"],
            max_attempts=prompt_check["max_attempts"],
            rework=prompt_check.get("rework"),
        )

    # 6. Profile rank vs max_unapproved_profile (ACC: D16)
    if check_approval:
        max_unapproved = agent["max_unapproved_profile"] or "dev"
        need_perm = PROFILE_RANKS.get(resolved_profile, 0) > PROFILE_RANKS.get(max_unapproved, 2)
        if need_perm:
            norm_params = dict(approval_params_extra or {})
            norm_params.update({
                "profile": resolved_profile,
                "model": resolved_model,
                "effort": resolved_effort,
            })
            if op == "task.create":
                norm_params["assignee"] = agent["name"]
            else:
                norm_params["to"] = agent["name"]
            _require_task_approval(
                conn=conn,
                ctx=ctx,
                op=op,
                project_id=project_id,
                task_id=task_id,
                params=norm_params,
                need_retry=False,
                need_perm=True,
                perm_summary=f"Permission escalation to '{resolved_profile}' for task '{task_title}' on agent '{agent['name']}'",
                perm_message=f"Profile '{resolved_profile}' requires user approval (category: permission_escalation)",
            )

    return agent, resolved_profile, resolved_model, resolved_effort


def handle_task_create(ctx: HandlerContext) -> HandlerResult:
    """Create a new task under a project per plan §6.2 (T1/T2)."""
    project_id = _require_str_param(
        ctx.args.get("project"), "project", "Missing required parameter --project"
    )

    # Check project exists
    cur_p = ctx.conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,))
    project = cur_p.fetchone()
    if not project:
        raise MeshError(
            code=c.NOT_FOUND,
            exit_code=c.EXIT_NOT_FOUND,
            message=f"Project '{project_id}' not found",
            details={"project_id": project_id},
        )

    # ACC: E18 - task create on completed or cancelled project returns rc 6 PROJECT_NOT_ACTIVE
    p_status = project["status"]
    if p_status in ("completed", "cancelled"):
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is {p_status}, not active",
            details={"project_id": project_id, "status": p_status},
        )

    check_authz(ctx.conn, "task.create", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    title = _require_str_param(ctx.args.get("title"), "title", "Parameter --title must be non-empty", strip=True)
    spec = _require_str_param(ctx.args.get("spec"), "spec", "Parameter --spec must be non-empty", strip=True)

    # Priority
    priority = 0
    if "priority" in ctx.args and ctx.args["priority"] is not None:
        priority = _parse_strict_int(ctx.args["priority"], "priority")
        if not (-1_000_000 <= priority <= 1_000_000):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"priority must be between -1000000 and 1000000; got {priority}",
                details={"param": "priority", "expected": "-1000000..1000000", "got": priority},
            )

    # Timeout
    min_timeout = 1 if ctx.testing_mode else 60
    timeout_s = None
    if "timeout" in ctx.args and ctx.args["timeout"] is not None:
        timeout_s = _parse_strict_int(ctx.args["timeout"], "timeout", allow_s_suffix=True)
        if not (min_timeout <= timeout_s <= 604800):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"timeout must be between {min_timeout} and 604800 seconds; got {timeout_s}",
                details={"param": "timeout", "expected": f"{min_timeout}..604800", "got": timeout_s},
            )
    else:
        # Default from settings
        cur_to = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_timeout_s';")
        to_row = cur_to.fetchone()
        timeout_s = int(json.loads(to_row["value_json"])) if to_row else 3600
        if not ctx.testing_mode:
            timeout_s = max(60, timeout_s)

    # Idle timeout
    idle_timeout_s = 0
    if "idle_timeout" in ctx.args and ctx.args["idle_timeout"] is not None:
        idle_timeout_s = _parse_strict_int(ctx.args["idle_timeout"], "idle_timeout", allow_s_suffix=True)
        if idle_timeout_s != 0 and not (min_timeout <= idle_timeout_s <= 604800):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"idle_timeout must be 0 or between {min_timeout} and 604800 seconds; got {idle_timeout_s}",
                details={"param": "idle_timeout", "expected": f"0|{min_timeout}..604800", "got": idle_timeout_s},
            )

    # Max attempts (ACC: H9 cap 10)
    cur_cap = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.max_attempts_cap';")
    cap_row = cur_cap.fetchone()
    cap = int(json.loads(cap_row["value_json"])) if cap_row else 10

    max_attempts = None
    if "max_attempts" in ctx.args and ctx.args["max_attempts"] is not None:
        max_attempts = _parse_strict_int(ctx.args["max_attempts"], "max_attempts")
        if not (1 <= max_attempts <= cap):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"max_attempts must be between 1 and {cap}; got {max_attempts}",
                details={"param": "max_attempts", "expected": f"1..{cap}", "got": max_attempts},
            )
    else:
        cur_dma = ctx.conn.execute("SELECT value_json FROM settings WHERE key = 'task.default_max_attempts';")
        dma_row = cur_dma.fetchone()
        max_attempts = int(json.loads(dma_row["value_json"])) if dma_row else 3

    # Kind: code or general
    kind = _optional_str_param(ctx.args.get("kind"), "kind")
    if kind:
        if kind not in ("code", "general"):
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid task kind '{kind}'; must be 'code' or 'general'",
                details={"param": "kind", "value": kind},
            )
    else:
        kind = "code" if project["repo_path"] else "general"
    if kind == "code" and not project["repo_path"]:
        # Q13: a code task needs a repo project (its worktree comes from the repo, §10.3).
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Task kind 'code' needs a repo project; project '{project['id']}' has no repo",
            details={"param": "kind", "value": kind, "reason": "no_repo"},
        )

    # 1. Dependencies check (validated before AG per plan §6.2)
    if "depends_on" in ctx.args and ctx.args["depends_on"] is not None:
        deps = ctx.args["depends_on"]
    else:
        deps = []
    if isinstance(deps, str):
        deps = [deps]
    if not isinstance(deps, list) or any(not isinstance(d, str) or not d.strip() for d in deps):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="depends_on must be a list of task ids",
            details={"param": "depends_on"},
        )
    # Deduplicate preserving order
    clean_deps: List[str] = list(dict.fromkeys(deps))

    for dep_id in clean_deps:
        cur_dep = ctx.conn.execute(
            "SELECT id, project_id FROM tasks WHERE id = ?;",
            (dep_id,),
        )
        dep_row = cur_dep.fetchone()
        if not dep_row:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Prerequisite dependency task '{dep_id}' not found",
                details={"task_id": dep_id},
            )
        if dep_row["project_id"] != project_id:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Dependency task '{dep_id}' belongs to project '{dep_row['project_id']}', not '{project_id}'",
                details={"task_id": dep_id, "project_id": project_id},
            )

    # 2. Assignee & assignment guards
    assignee_ident = _optional_str_param(ctx.args.get("assignee"), "assignee")
    agent_id = None
    agent_name = None
    profile_raw = _optional_str_param(ctx.args.get("profile"), "profile")
    profile = profile_raw or "edit"
    model = _optional_str_param(ctx.args.get("model"), "model")
    effort = _optional_str_param(ctx.args.get("effort"), "effort")

    if assignee_ident:
        preview_task_id = "tsk_" + "0" * 26
        preview_ws = compute_task_workspace_path(
            get_workspaces_root(ctx.conn, ctx.home), project_id, preview_task_id, attempt=1
        )
        agent_row, profile, model, effort = _validate_assignment_guards(
            conn=ctx.conn,
            ctx=ctx,
            project_id=project_id,
            assignee_ident=assignee_ident,
            profile_override=ctx.args.get("profile"),
            model_override=ctx.args.get("model"),
            effort_override=ctx.args.get("effort"),
            task_title=title,
            op="task.create",
            approval_params_extra={
                "project": project_id,
                "title": title,
                "spec": spec,
                "kind": kind,
                "timeout_s": timeout_s,
                "idle_timeout_s": idle_timeout_s,
                "max_attempts": max_attempts,
                "priority": priority,
                "depends_on": clean_deps,
            },
            prompt_check={
                "task_doc": {"id": preview_task_id, "title": title, "spec": spec, "kind": kind, "branch": None},
                "workspace": preview_ws,
                "attempt": 1,
                "max_attempts": max_attempts,
            },
        )
        agent_id = agent_row["id"]
        agent_name = agent_row["name"]
        initial_status = "assigned"
        event_type = "task.assigned"
    else:
        # Validate profile and effort overrides even when unassigned
        profile_override = ctx.args.get("profile")
        effort_override = ctx.args.get("effort")
        _validate_profile_and_effort(profile_override, effort_override)
        initial_status = "created"
        event_type = "task.created"

    now = now_iso_utc()
    task_id = generate_id("tsk")
    # Linear round 5 P2: honour the configured workspaces.root (was always $home/workspaces).
    workspace_path = os.path.join(get_workspaces_root(ctx.conn, ctx.home), project_id, "tasks", task_id)

    ctx.conn.execute(
        """INSERT INTO tasks (
            id, project_id, title, spec, kind, status, priority,
            assignee_agent_id, model, effort, profile, timeout_s,
            idle_timeout_s, max_attempts, attempt, fence_seq,
            queued_at, offered_at, workspace_path, branch, base_commit,
            last_session_id, deliverable_json, failure_json, merge_json,
            created_by, version, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, 0, 0,
            NULL, NULL, ?, NULL, NULL,
            NULL, NULL, NULL, NULL,
            ?, 1, ?, ?
        );""",
        (
            task_id, project_id, title, spec, kind, initial_status, priority,
            agent_id, model, effort, profile, timeout_s,
            idle_timeout_s, max_attempts, workspace_path,
            ctx.principal["id"], now, now,
        ),
    )

    for dep_id in clean_deps:
        ctx.conn.execute(
            "INSERT INTO task_deps (task_id, depends_on_task_id) VALUES (?, ?);",
            (task_id, dep_id),
        )

    record_event(
        conn=ctx.conn,
        type_=event_type,
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=None,
        to_state=initial_status,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "title": title,
            "assignee": agent_name,
            "profile": profile,
            # Blind review P2: whether the profile was asked for, so a later `task assign`
            # of an unassigned task uses the worker's default instead of the edit fallback.
            "profile_explicit": bool(ctx.args.get("profile")),
            "model": model,
            "effort": effort,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    task_row = _get_task(ctx.conn, task_id)
    return HandlerResult(
        data=_format_task(task_row, conn=ctx.conn, depends_on=clean_deps, depended_on_by=[]),
        events=[event_type],
        transitions=[{"entity": "task", "id": task_id, "from": None, "to": initial_status}],
    )


def handle_task_assign(ctx: HandlerContext) -> HandlerResult:
    """Assign an unassigned task to an agent per plan §6.2 (T3)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    raw_to = ctx.args.get("to") if ctx.args.get("to") is not None else ctx.args.get("agent")
    to_agent = _require_str_param(raw_to, "to", "Missing required parameter --to")
    _optional_str_param(ctx.args.get("profile"), "profile")
    _optional_str_param(ctx.args.get("model"), "model")
    _optional_str_param(ctx.args.get("effort"), "effort")

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.assign", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T3 is from created to assigned. Blind review P1: only T3; a
    # failed/escalated/rework/merge_failed task goes back through `task retry` (attempt cap,
    # approval) or `task reassign` (T20), never through assign.
    if task["status"] != "created":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"task assign only assigns a new (created) task; '{task_id}' is {task['status']}. "
                     f"Use `mesh task reassign {task_id} --to <agent>` or `mesh task retry {task_id}`"),
            details={"task_id": task_id, "from": task["status"], "to": "assigned"},
        )
    check_task_transition(task["status"], "assigned")

    created_prof = _created_profile_override(ctx.conn, task)
    effective_prof_override = ctx.args.get("profile") or created_prof
    preview_ws = task["workspace_path"] or compute_task_workspace_path(
        get_workspaces_root(ctx.conn, ctx.home), project_id, task_id, attempt=task["attempt"] + 1
    )

    agent_row, profile, model, effort = _validate_assignment_guards(
        conn=ctx.conn,
        ctx=ctx,
        project_id=project_id,
        assignee_ident=to_agent,
        profile_override=effective_prof_override,
        model_override=ctx.args.get("model") or task["model"],
        effort_override=ctx.args.get("effort") or task["effort"],
        task_title=task["title"],
        op="task.assign",
        task_id=task_id,
        approval_params_extra={"task_id": task_id},
        prompt_check={
            "task_doc": dict(task),
            "workspace": preview_ws,
            "attempt": task["attempt"] + 1,
            "max_attempts": task["max_attempts"],
        },
    )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET assignee_agent_id = ?, status = 'assigned', profile = ?, model = ?, effort = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (agent_row["id"], profile, model, effort, now, task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="task.assigned",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="assigned",
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "assignee": agent_row["name"],
            "profile": profile,
            "profile_explicit": bool(effective_prof_override is not None),
            "model": model,
            "effort": effort,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.assigned"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "assigned"}],
    )


def handle_task_show(ctx: HandlerContext) -> HandlerResult:
    """Show details of a task."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")

    row = _get_task(ctx.conn, task_id)
    project_id = row["project_id"]
    check_authz(ctx.conn, "task.show", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    cur_deps = ctx.conn.execute(
        "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_task_id;",
        (task_id,),
    )
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    cur_dep_by = ctx.conn.execute(
        "SELECT task_id FROM task_deps WHERE depends_on_task_id = ? ORDER BY task_id;",
        (task_id,),
    )
    depended_on_by = [r["task_id"] for r in cur_dep_by.fetchall()]

    data = _format_task(row, conn=ctx.conn, depends_on=depends_on, depended_on_by=depended_on_by)
    live = _live_run_for_viewer(ctx, row)
    if live is not None:
        data["live_run"] = live
    return HandlerResult(data=data)


def _live_run_for_viewer(ctx: HandlerContext, task: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """The live inbox run's lease timing, for the assignee (and the user) only.

    Only an agent-held (inbox) lease is shown: a launch run's lease is renewed by meshd.
    """
    if task["status"] != "running":
        return None
    viewer = (ctx.principal or {}).get("id")
    if ctx.actor_kind != "user" and viewer != task["assignee_agent_id"]:
        return None
    lease = ctx.conn.execute("SELECT * FROM leases WHERE key = ?;", (f"task:{task['id']}",)).fetchone()
    if lease is None or lease["holder_kind"] != "agent":
        return None
    live: Dict[str, Any] = {"run_id": lease["run_id"], "fence": lease["fence"]}
    live.update(_lease_timing(get_lease_ttl_s(ctx.conn, testing_mode=ctx.testing_mode), lease["expires_at"], task["timeout_s"],
                              heartbeat_at=lease["heartbeat_at"]))
    return live


def handle_task_list(ctx: HandlerContext) -> HandlerResult:
    """List tasks with optional filters."""
    raw_proj = ctx.args.get("project") if ctx.args.get("project") is not None else ctx.args.get("project_id")
    project_id = _optional_str_param(raw_proj, "project")
    raw_status = _optional_str_param(ctx.args.get("status"), "status")
    raw_assignee = _optional_str_param(ctx.args.get("assignee"), "assignee")
    if ctx.args.get("verify_merge_auth"):
        if not project_id:
            raise MeshError(
                c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message="--project is required",
                details={"param": "project"},
            )
        if ctx.conn.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
            raise MeshError(
                code=c.NOT_FOUND,
                exit_code=c.EXIT_NOT_FOUND,
                message=f"Project '{project_id}' not found",
                details={"project_id": project_id},
            )
        check_authz(ctx.conn, "task.merge", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)
    check_authz(ctx.conn, "task.list", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id)

    query = """
        SELECT t.*, p.name as assignee_name
        FROM tasks t
        LEFT JOIN principals p ON t.assignee_agent_id = p.id
        WHERE 1=1
    """
    params: List[Any] = []

    project_id = ctx.args.get("project") or ctx.args.get("project_id")
    scope_project = lead_run_token_scope(ctx.principal, ctx.via)
    if scope_project is not None:
        # Review m2-r9 finding 1(a): a lead run token lists only its own project's tasks.
        project_id = scope_project
    if project_id:
        query += " AND t.project_id = ?"
        params.append(project_id)

    status = raw_status
    if status:
        # One status or a comma list (`--status submitted,accepted`).
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in TASK_STATES]
        if unknown or not wanted:
            raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE,
                            message=f"--status: unknown task status {', '.join(unknown) or repr(status)}; "
                                    f"valid: {', '.join(TASK_STATES)} (comma-separate several)",
                            details={"param": "status", "unknown": unknown, "valid": list(TASK_STATES)})
        query += " AND t.status IN (" + ",".join("?" * len(wanted)) + ")"
        params.extend(wanted)

    assignee = raw_assignee
    if assignee:
        query += " AND (t.assignee_agent_id = ? OR p.name = ?)"
        params.extend([assignee, assignee])

    caps = ctx.principal.get("capabilities", [])
    if not caps and ctx.principal.get("id"):
        caps = get_agent_capabilities(ctx.conn, ctx.principal["id"])
    if scope_project is None and ctx.actor_kind != "user" and "relay" not in caps:
        agent_id = ctx.principal.get("id")
        query += """ AND (
            t.assignee_agent_id = ?
            OR t.project_id IN (
                SELECT project_id FROM lead_assignments WHERE agent_id = ?
                UNION
                SELECT id FROM projects WHERE origin_principal_id = ?
            )
        )"""
        params.extend([agent_id, agent_id, agent_id])

    query += " ORDER BY t.priority DESC, t.created_at ASC;"

    cur = ctx.conn.execute(query, params)
    items = []
    for row in cur.fetchall():
        items.append(_format_task(row, conn=ctx.conn))

    return HandlerResult(data={"items": items})


def handle_task_cancel(ctx: HandlerContext) -> HandlerResult:
    """Cancel a task per plan §6.2 (T22)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    reason = _require_str_param(
        ctx.args.get("reason"), "reason", "Parameter --reason is required and non-empty for task cancellation", strip=True
    )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.cancel", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check if task is terminal (e.g. accepted general task, merged, cancelled)
    if is_task_terminal(task["status"], task["kind"]):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' in state '{task['status']}' is terminal and cannot be cancelled",
            details={"task_id": task_id, "status": task["status"], "kind": task["kind"]},
        )

    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot cancel task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )

    lease = ctx.conn.execute(
        "SELECT * FROM leases WHERE key = ?;", (f"task:{task_id}",)
    ).fetchone()
    if lease is not None:
        # T9: a task that holds a lease (running, or submitted in post-submit grace)
        # goes through `cancelling` so the run is always terminated first (§6.2, §11.4).
        check_task_transition(task["status"], "cancelling")
        begin_task_cancel(ctx.conn, task, lease, reason, ctx)
        updated_row = _get_task(ctx.conn, task_id)
        return HandlerResult(
            data=_format_task(updated_row, conn=ctx.conn),
            events=["task.cancelling"] + (["task.cancelled"] if updated_row["status"] == "cancelled" else []),
            transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": updated_row["status"]}],
        )

    # Check transition (T22)
    check_task_transition(task["status"], "cancelled")

    now = now_iso_utc()
    _supersede_offers(ctx.conn, task)

    ctx.conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task_id),
    )

    record_event(
        conn=ctx.conn,
        type_="task.cancelled",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="cancelled",
        reason=reason,
        payload={"task_id": task_id, "project_id": project_id, "reason": reason},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    emit_dependents_blocked(ctx.conn, task_id, ctx.request_id)

    updated_row = _get_task(ctx.conn, task_id)
    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn),
        events=["task.cancelled"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "cancelled"}],
    )


def handle_task_close(ctx: HandlerContext) -> HandlerResult:
    """Close an accepted / merge_failed code task whose work landed through another task.

    `task close TSK --superseded-by TSK2`: TSK goes to the terminal `cancelled` state (no
    new state, so project completion, doctor and deliver keep working) with a
    `task.closed` event that links TSK2; `task show` prints `superseded_by`. The branch
    and worktree are kept. Only for a lead or the user (same rule as `task cancel`).
    """
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    other_id = _require_str_param(
        ctx.args.get("superseded_by"),
        "superseded_by",
        "--superseded-by TASK is required (the task whose merge landed this work)",
    )
    raw_note = _optional_str_param(ctx.args.get("reason"), "reason")
    note = (raw_note or "").strip() or None
    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]
    check_authz(ctx.conn, "task.cancel", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id,
                task_id=task_id)
    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot close task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )
    if other_id == task_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="A task cannot supersede itself",
                        details={"param": "superseded_by", "task_id": task_id})
    allowed_close_states = ("accepted", "merge_failed", "cancelled", "failed")
    if task["status"] not in allowed_close_states or task["kind"] != "code":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"Only an accepted, merge_failed, cancelled, or failed code task can be closed as superseded; "
                     f"task '{task_id}' is a {task['kind']} task in '{task['status']}'. Use `mesh task cancel` otherwise."),
            details={"task_id": task_id, "status": task["status"], "kind": task["kind"],
                     "allowed": list(allowed_close_states)},
        )
    prev_sup = superseded_by(ctx.conn, task_id)
    if prev_sup is not None:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' has already been closed as superseded by '{prev_sup}'",
            details={"task_id": task_id, "status": task["status"], "superseded_by": prev_sup},
        )
    other = ctx.conn.execute("SELECT id, project_id, status, merge_json FROM tasks WHERE id = ?;", (other_id,)).fetchone()
    if other is None or other["project_id"] != project_id:
        raise MeshError(c.NOT_FOUND, exit_code=c.EXIT_NOT_FOUND,
                        message=f"--superseded-by: task '{other_id}' not found in project '{project_id}'",
                        details={"param": "superseded_by", "task_id": other_id, "project_id": project_id})
    if other["status"] != "merged":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"--superseded-by: task '{other_id}' is '{other['status']}', not merged; close '{task_id}' "
                     f"after the other task's merge has landed."),
            details={"param": "superseded_by", "task_id": other_id, "status": other["status"]},
        )
    prow_repo = ctx.conn.execute(
        "SELECT repo_path, integration_branch FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    repo_path = prow_repo["repo_path"] if prow_repo else None
    integration_branch = prow_repo["integration_branch"] if prow_repo else None
    if repo_path and os.path.isdir(repo_path) and task["branch"]:
        from mesh.core.gitops import run_git
        from mesh.core.invariants import _SHA_RE, check_i9_integrated_unmerged_tasks

        other_base_row = ctx.conn.execute("SELECT base_commit FROM tasks WHERE id = ?;", (other_id,)).fetchone()
        other_base = other_base_row["base_commit"] if other_base_row else None
        other_mc = _parse_merge_commit(other["merge_json"])

        prepared = getattr(ctx, "prepared_close", None)
        used_prepared = False
        if isinstance(prepared, dict):
            db_snap = prepared.get("db_snapshot")
            git_snap = prepared.get("git_snapshot")
            if isinstance(db_snap, dict) and isinstance(git_snap, dict):
                cur_db_snap = _task_close_db_snapshot(
                    task,
                    other,
                    other_base,
                    repo_path,
                    integration_branch,
                    _task_close_merged_rows(ctx.conn, project_id),
                )
                if cur_db_snap == db_snap:
                    prep_err = prepared.get("error")
                    if git_snap.get("rev_parse_ok"):
                        tip_ok, cur_tip = _read_branch_sha(run_git, _SHA_RE, repo_path, task["branch"])
                        integ_ok, cur_integ = _read_branch_sha(run_git, _SHA_RE, repo_path, integration_branch)
                        noop_ok = True
                        for n_branch, n_sha in (git_snap.get("noop_branch_shas") or {}).items():
                            nb_ok, cur_nb = _read_branch_sha(run_git, _SHA_RE, repo_path, n_branch)
                            if not nb_ok or cur_nb != n_sha:
                                noop_ok = False
                                break
                        if (
                            tip_ok
                            and integ_ok
                            and noop_ok
                            and cur_tip == git_snap.get("tip_sha")
                            and cur_integ == git_snap.get("integ_sha")
                        ):
                            used_prepared = True
                            if prep_err is not None:
                                raise prep_err
                    elif prep_err is not None:
                        used_prepared = True
                        raise prep_err

        if not used_prepared:
            if task["status"] in ("cancelled", "failed"):
                _check_cancelled_failed_superseded_covered(
                    run_git,
                    _SHA_RE,
                    repo_path,
                    task_id,
                    task["branch"],
                    task["base_commit"],
                    other_id,
                    other_base,
                    other_mc,
                )
            i9_viols = check_i9_integrated_unmerged_tasks(
                ctx.conn,
                project_id,
                task_id=task_id,
                superseding_override=(task_id, other_id),
            )
            if i9_viols:
                raise _task_close_i9_error(task_id, other_id, other_mc, i9_viols)
    open_deps = ctx.conn.execute(
        """SELECT t.id FROM tasks t JOIN task_deps td ON t.id = td.task_id
           WHERE td.depends_on_task_id = ? AND t.status NOT IN ('merged', 'cancelled')
             AND NOT (t.kind = 'general' AND t.status = 'accepted') ORDER BY t.id;""",
        (task_id,),
    ).fetchall()
    if open_deps:
        ids = [r["id"] for r in open_deps]
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"Task '{task_id}' has open dependents ({', '.join(ids)}); a closed task never satisfies a "
                     f"dependency. Point them at '{other_id}' first (`mesh dep remove` / `mesh dep add`)."),
            details={"task_id": task_id, "dependents": ids, "superseded_by": other_id},
        )
    if task["status"] != "cancelled":
        check_task_transition(task["status"], "cancelled")

    now = now_iso_utc()
    _supersede_offers(ctx.conn, task)
    ctx.conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task_id),
    )
    payload = {"task_id": task_id, "project_id": project_id, "superseded_by": other_id,
               "from_state": task["status"], "branch": task["branch"], "note": note}
    record_event(
        conn=ctx.conn,
        type_="task.closed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="cancelled",
        reason="superseded",
        payload=payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    updated_row = _get_task(ctx.conn, task_id)
    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn),
        events=["task.closed"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "cancelled"}],
    )


def superseded_by(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT payload_json FROM events WHERE task_id = ? AND type = 'task.closed' "
                       "ORDER BY seq DESC LIMIT 1;", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"] or "{}").get("superseded_by")
    except ValueError:
        return None


def _parse_merge_commit(merge_json: Optional[str]) -> Optional[str]:
    try:
        mj = json.loads(merge_json or "{}") or {}
    except (TypeError, ValueError):
        mj = {}
    return mj.get("merge_commit") if isinstance(mj, dict) else None


def _task_close_db_snapshot(
    task: Any,
    other: Any,
    other_base: Optional[str],
    repo_path: str,
    integration_branch: Optional[str],
    merged_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "task_id": task["id"],
        "task_status": task["status"],
        "task_branch": task["branch"],
        "task_base_commit": task["base_commit"],
        "other_id": other["id"],
        "other_status": other["status"],
        "other_base_commit": other_base,
        "other_merge_json": other["merge_json"],
        "project_id": task["project_id"],
        "repo_path": repo_path,
        "integration_branch": integration_branch,
        "merged_sig": _task_close_merged_sig(merged_rows),
    }


def _task_close_i9_error(
    task_id: str,
    other_id: str,
    other_mc: Optional[str],
    i9_viols: List[Any],
) -> MeshError:
    return MeshError(
        code=c.INVALID_TRANSITION,
        exit_code=c.EXIT_CONFLICT,
        message=(f"--superseded-by: task '{task_id}' has integrated work not carried by '{other_id}' "
                 f"merge commit '{other_mc}'"),
        details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id,
                 "merge_commit": other_mc, "violations": [v.details for v in i9_viols]},
    )


def _task_close_merged_rows(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT t.id AS task_id, t.project_id, t.branch, t.base_commit, t.merge_json, p.repo_path
               FROM tasks t JOIN projects p ON t.project_id = p.id
               WHERE t.kind = 'code' AND t.status = 'merged' AND t.project_id = ?
               ORDER BY t.id;""",
            (project_id,),
        ).fetchall()
    ]


def _task_close_merged_sig(merged_rows: List[Dict[str, Any]]) -> Tuple[Tuple[Any, ...], ...]:
    return tuple(
        (r["task_id"], r["branch"], r["base_commit"], r["merge_json"], r["repo_path"])
        for r in merged_rows
    )


def _read_branch_sha(
    run_git: Any,
    sha_re: Any,
    repo_path: str,
    branch_name: Optional[str],
) -> Tuple[bool, Optional[str]]:
    if not branch_name:
        return True, None
    try:
        cp = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}^{{commit}}"],
            cwd=repo_path,
        )
    except Exception:
        return False, None
    if cp.returncode == 1:
        return True, None
    if cp.returncode != 0:
        return False, None
    sha = cp.stdout.strip()
    return (True, sha) if sha_re.match(sha) else (False, None)


def _check_cancelled_failed_superseded_covered(
    run_git: Any,
    sha_re: Any,
    repo_path: str,
    task_id: str,
    task_branch: str,
    task_base: Optional[str],
    other_id: str,
    other_base: Optional[str],
    other_mc: Optional[str],
) -> None:
    """Verify `other_mc` carried at least one commit of a cancelled/failed task beyond `task_base`."""
    try:
        tip_cp = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{task_branch}^{{commit}}"],
            cwd=repo_path,
        )
    except Exception as exc:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"--superseded-by: failed to verify branch tip for task '{task_id}': {exc}",
            details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id},
        )
    if tip_cp.returncode == 1:
        return
    if tip_cp.returncode != 0:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"--superseded-by: failed to verify branch tip for task '{task_id}'",
            details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id},
        )
    tip_sha = tip_cp.stdout.strip()
    if not sha_re.match(tip_sha):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"--superseded-by: failed to verify branch tip for task '{task_id}'",
            details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id},
        )

    covered = False
    try:
        if (
            isinstance(other_mc, str) and sha_re.match(other_mc)
            and isinstance(task_base, str) and sha_re.match(task_base)
            and isinstance(other_base, str) and sha_re.match(other_base)
        ):
            p_cp = run_git(["log", "-1", "--format=%P", other_mc], cwd=repo_path)
            if p_cp.returncode != 0:
                raise RuntimeError(f"git log failed (rc {p_cp.returncode})")
            parents = p_cp.stdout.split()
            if len(parents) >= 2:
                other_first = parents[0]

                def _is_anc(a: str, b: str) -> bool:
                    cp = run_git(["merge-base", "--is-ancestor", a, b], cwd=repo_path)
                    if cp.returncode not in (0, 1):
                        raise RuntimeError(f"git merge-base failed (rc {cp.returncode})")
                    return cp.returncode == 0

                for bp in parents[1:]:
                    mb_cp = run_git(["merge-base", "--all", tip_sha, bp], cwd=repo_path)
                    if mb_cp.returncode not in (0, 1):
                        raise RuntimeError(f"git merge-base failed (rc {mb_cp.returncode})")
                    for mb in mb_cp.stdout.split():
                        if (
                            mb != task_base
                            and not _is_anc(mb, task_base)
                            and not _is_anc(mb, other_first)
                            and not _is_anc(mb, other_base)
                        ):
                            covered = True
                            break
                    if covered:
                        break
    except Exception as exc:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"--superseded-by: failed to verify branch coverage for task '{task_id}': {exc}",
            details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id},
        )
    if not covered:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"--superseded-by: task '{task_id}' branch is not covered by '{other_id}' "
                     f"merge commit '{other_mc}'"),
            details={"param": "superseded_by", "task_id": task_id, "superseded_by": other_id,
                     "tip": tip_sha, "merge_commit": other_mc},
        )


def _run_task_close_git_checks(
    row_dict: Dict[str, Any],
    merged_rows: List[Dict[str, Any]],
    other_id: str,
    other_base: Optional[str],
    other_mc: Optional[str],
) -> Dict[str, Any]:
    """Run read-only git checks for `task.close` in a worker thread without DB access."""
    from mesh.core.gitops import run_git
    from mesh.core.invariants import (
        _SHA_RE,
        _check_i9_unmerged_row,
        _merged_task_bases_by_project,
    )

    repo_path = row_dict["repo_path"]
    task_id = row_dict["task_id"]
    task_branch = row_dict["branch"]
    integration_branch = row_dict["integration_branch"]

    tip_ok, tip_sha = _read_branch_sha(run_git, _SHA_RE, repo_path, task_branch)
    integ_ok, integ_sha = _read_branch_sha(run_git, _SHA_RE, repo_path, integration_branch)
    noop_branch_shas: Dict[str, Optional[str]] = {}
    noop_ok = True
    for mr in merged_rows:
        try:
            mj = json.loads(mr["merge_json"] or "{}") or {}
        except (TypeError, ValueError):
            mj = {}
        tip_field = mj.get("tip") if isinstance(mj, dict) else None
        if (
            isinstance(mj, dict)
            and mj.get("noop") is not False
            and not (isinstance(tip_field, str) and _SHA_RE.match(tip_field))
            and mr.get("branch")
            and mr["branch"] not in noop_branch_shas
        ):
            nb_ok, nb_sha = _read_branch_sha(run_git, _SHA_RE, repo_path, mr["branch"])
            if not nb_ok:
                noop_ok = False
                break
            noop_branch_shas[mr["branch"]] = nb_sha

    err: Optional[MeshError] = None
    if row_dict["status"] in ("cancelled", "failed"):
        try:
            _check_cancelled_failed_superseded_covered(
                run_git,
                _SHA_RE,
                repo_path,
                task_id,
                task_branch,
                row_dict["base_commit"],
                other_id,
                other_base,
                other_mc,
            )
        except MeshError as exc:
            err = exc

    if err is None and integration_branch is not None:
        def git(args: List[str], cwd: str, input: Optional[str] = None) -> Any:
            if input is not None:
                return run_git(args, cwd=cwd, input=input)
            return run_git(args, cwd=cwd)

        merged_bases = _merged_task_bases_by_project(git, merged_rows).get(row_dict["project_id"], {})
        sup_mc = (
            other_mc
            if (
                isinstance(other_mc, str)
                and other_mc in merged_bases
                and isinstance(other_base, str)
                and _SHA_RE.match(other_base)
            )
            else None
        )
        i9_viols = _check_i9_unmerged_row(
            git,
            row_dict,
            merged_bases,
            sup_mc,
            other_base if sup_mc else None,
        )
        if i9_viols:
            err = _task_close_i9_error(task_id, other_id, other_mc, i9_viols)

    return {
        "git_snapshot": {
            "rev_parse_ok": tip_ok and integ_ok and noop_ok,
            "tip_sha": tip_sha,
            "integ_sha": integ_sha,
            "noop_branch_shas": noop_branch_shas,
        },
        "error": err,
    }


async def prepare_task_close(ctx: HandlerContext) -> None:
    """Offload read-only git and reverse-I9 checks for `task.close` before BEGIN IMMEDIATE."""
    if ctx.cmd != "task.close":
        return
    task_id = ctx.args.get("task_id")
    other_id = ctx.args.get("superseded_by")
    if not isinstance(task_id, str) or not task_id or not isinstance(other_id, str) or not other_id:
        return
    if other_id == task_id:
        return
    task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if task is None or task["kind"] != "code" or task["status"] not in ("accepted", "merge_failed", "cancelled", "failed"):
        return
    project_id = task["project_id"]
    try:
        check_authz(
            ctx.conn,
            "task.cancel",
            ctx.principal,
            ctx.actor_kind,
            ctx.via,
            project_id=project_id,
            task_id=task_id,
        )
    except MeshError:
        return
    if has_task_ws_reservation(ctx.conn, task_id, project_id):
        return
    if superseded_by(ctx.conn, task_id) is not None:
        return
    other = ctx.conn.execute(
        "SELECT id, project_id, status, base_commit, merge_json FROM tasks WHERE id = ?;",
        (other_id,),
    ).fetchone()
    if other is None or other["project_id"] != project_id or other["status"] != "merged":
        return
    prow = ctx.conn.execute(
        "SELECT repo_path, integration_branch FROM projects WHERE id = ?;",
        (project_id,),
    ).fetchone()
    repo_path = prow["repo_path"] if prow else None
    integration_branch = prow["integration_branch"] if prow else None
    if not (repo_path and os.path.isdir(repo_path) and task["branch"]):
        return

    other_mc = _parse_merge_commit(other["merge_json"])
    merged_rows = _task_close_merged_rows(ctx.conn, project_id)
    row_dict = {
        "task_id": task_id,
        "status": task["status"],
        "branch": task["branch"],
        "base_commit": task["base_commit"],
        "project_id": project_id,
        "repo_path": repo_path,
        "integration_branch": integration_branch,
    }
    db_snapshot = _task_close_db_snapshot(
        task,
        other,
        other["base_commit"],
        repo_path,
        integration_branch,
        merged_rows,
    )
    prep = await asyncio.to_thread(
        _run_task_close_git_checks,
        row_dict,
        merged_rows,
        other_id,
        other["base_commit"],
        other_mc,
    )
    prep["db_snapshot"] = db_snapshot
    ctx.prepared_close = prep  # type: ignore[attr-defined]


def _supersede_offers(conn: Any, task: Any) -> None:
    if task["assignee_agent_id"]:
        conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (task["assignee_agent_id"], task["id"]),
        )


def begin_task_cancel(conn: Any, task: Any, lease: Any, reason: str, ctx: Optional[HandlerContext]) -> None:
    """T9 (and for inbox mode T10 at once) for a task that holds a lease.

    Launch mode: the task becomes `cancelling`; the supervisor terminates the run
    (§11.4) and applies T10 once the release condition holds. Inbox mode: Mesh
    cannot stop the agent, so the fence is invalidated at once: the lease is
    deleted, the external run ends `killed` with end_reason = the cancel reason
    (`cancelled`, or `project_cancelled` from P5; review m1c-run3 #5) and the
    task goes straight on to `cancelled` (T10, "inbox mode: immediately").
    """
    now = now_iso_utc()
    actor = dict(
        actor_id=ctx.principal["id"] if ctx else "system",
        actor_kind=ctx.actor_kind if ctx else "system",
        via=ctx.via if ctx else "system",
        relayed_by=ctx.relayed_by if ctx else None,
    )
    request_id = ctx.request_id if ctx else None
    conn.execute(
        "UPDATE tasks SET status = 'cancelling', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task["id"]),
    )
    record_event(
        conn=conn,
        type_="task.cancelling",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=lease["run_id"],
        from_state=task["status"],
        to_state="cancelling",
        reason=reason,
        payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": lease["run_id"], "reason": reason},
        request_id=request_id,
        k_user=ctx.k_user if ctx else None,
        **actor,
    )
    if lease["holder_kind"] == "meshd":
        return
    conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task['id']}",))
    conn.execute(
        "UPDATE runs SET status = 'killed', end_reason = ?, ended_at = ? "
        "WHERE id = ? AND status IN ('starting', 'running');",
        ("project_cancelled" if reason == "project_cancelled" else "cancelled", now, lease["run_id"]),
    )
    conn.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1 WHERE id = ?;",
        (now, task["id"]),
    )
    record_event(
        conn=conn,
        type_="task.cancelled",
        actor_id="system",
        actor_kind="system",
        via="system",
        project_id=task["project_id"],
        task_id=task["id"],
        run_id=lease["run_id"],
        from_state="cancelling",
        to_state="cancelled",
        reason=reason,
        payload={"task_id": task["id"], "project_id": task["project_id"], "run_id": lease["run_id"], "reason": reason},
        request_id=request_id,
    )
    emit_dependents_blocked(conn, task["id"], request_id)


def emit_dependents_blocked(conn: Any, task_id: str, request_id: Optional[str]) -> None:
    """Emit task.blocked for dependents in 'assigned' of a cancelled task (plan §6.2)."""
    cur_deps = conn.execute(
        """SELECT t.id, t.project_id
           FROM tasks t
           JOIN task_deps td ON t.id = td.task_id
           WHERE td.depends_on_task_id = ? AND t.status = 'assigned';""",
        (task_id,),
    )
    for dep_row in cur_deps.fetchall():
        record_event(
            conn=conn,
            type_="task.blocked",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=dep_row["project_id"],
            task_id=dep_row["id"],
            payload={
                "task_id": dep_row["id"],
                "blocked_by": task_id,
                "reason": f"Prerequisite dependency '{task_id}' was cancelled",
            },
            request_id=request_id,
        )


def handle_task_reassign(ctx: HandlerContext) -> HandlerResult:
    """Reassign a task to a different agent per plan §6.2 (T20)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    raw_to = ctx.args.get("to") if ctx.args.get("to") is not None else ctx.args.get("agent")
    to_agent = _require_str_param(raw_to, "to", "Missing required parameter --to")
    reason = _require_str_param(
        ctx.args.get("reason"), "reason", "Parameter --reason is required and non-empty for task reassignment", strip=True
    )
    profile_arg = _optional_str_param(ctx.args.get("profile"), "profile")
    model_arg = _optional_str_param(ctx.args.get("model"), "model")
    effort_arg = _optional_str_param(ctx.args.get("effort"), "effort")
    reuse_workspace = _optional_bool_param(ctx.args.get("reuse_workspace"), "reuse_workspace", default=False)

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.reassign", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T20 is from assigned, queued, rework, failed, escalated, merge_failed -> assigned
    check_task_transition(task["status"], "assigned")

    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot reassign task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )

    # Compute workspace path (defer DB mutations until after validation and approval)
    cur_prev_run = ctx.conn.execute(
        "SELECT end_reason FROM runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1;",
        (task_id,),
    )
    prev_run = cur_prev_run.fetchone()
    prev_lease_expired = bool(prev_run and prev_run["end_reason"] == "lease_expired")

    workspace_choice = None
    new_ws_path = task["workspace_path"]
    if prev_lease_expired:
        workspace_choice = "reused" if reuse_workspace else "fresh"
        if workspace_choice == "fresh":
            next_attempt = max(1, task["attempt"]) + 1
            ws_root = get_workspaces_root(ctx.conn, ctx.home)
            new_ws_path = compute_task_workspace_path(ws_root, project_id, task_id, attempt=next_attempt)

    preview_ws = new_ws_path or compute_task_workspace_path(
        get_workspaces_root(ctx.conn, ctx.home), project_id, task_id, attempt=task["attempt"] + 1
    )
    effective_profile_override = profile_arg or _reassigned_profile_override(
        ctx.conn, task, to_agent, ctx.testing_mode
    )

    # Validate assignment guards for new assignee BEFORE creating or consuming any approval
    agent_row, profile, model, effort = _validate_assignment_guards(
        conn=ctx.conn,
        ctx=ctx,
        project_id=project_id,
        assignee_ident=to_agent,
        profile_override=effective_profile_override,
        model_override=model_arg,
        effort_override=effort_arg,
        task_title=task["title"],
        op="task.reassign",
        task_id=task_id,
        prompt_check={
            "task_doc": dict(task),
            "workspace": preview_ws,
            "attempt": task["attempt"] + 1,
            "max_attempts": task["max_attempts"],
            "rework": read_rework_feedback(ctx.conn, task_id),
        },
        check_approval=False,
    )

    # Evaluate both retry_over_limit and permission_escalation together in a single gate check
    need_retry = task["status"] in ("failed", "escalated") and task["attempt"] >= task["max_attempts"]
    max_unapproved = agent_row["max_unapproved_profile"] or "dev"
    need_perm = PROFILE_RANKS.get(profile, 0) > PROFILE_RANKS.get(max_unapproved, 2)
    _require_task_approval(
        conn=ctx.conn,
        ctx=ctx,
        op="task.reassign",
        project_id=project_id,
        task_id=task_id,
        params={
            "task_id": task_id,
            "to": agent_row["name"],
            "reason": reason,
            "profile": profile,
            "model": model,
            "effort": effort,
            "reuse_workspace": reuse_workspace,
        },
        need_retry=need_retry,
        need_perm=need_perm,
        retry_summary=f"Reassignment of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
        perm_summary=f"Permission escalation to '{profile}' for task '{task['title']}' on agent '{agent_row['name']}'",
        compound_summary=(
            f"Reassignment of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) "
            f"with permission escalation to '{profile}' on agent '{agent_row['name']}' requires user approval"
        ),
        retry_message=f"Task reassignment exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
        perm_message=f"Profile '{profile}' requires user approval (category: permission_escalation)",
        compound_message=f"Task reassignment exceeds max attempts ({task['max_attempts']}) and escalates profile to '{profile}'; requires direct user approval",
    )

    old_assignee_name = task["assignee_name"]
    now = now_iso_utc()

    if workspace_choice == "fresh":
        remember_protected_path(ctx.conn, task["workspace_path"])
        remember_protected_path(ctx.conn, new_ws_path)

    # On T20 from queued, supersede old assignee's unacked task.offered rows for that task
    if task["status"] == "queued" and task["assignee_agent_id"]:
        ctx.conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (task["assignee_agent_id"], task_id),
        )

    prev_reassign_model = task["model"]
    if task["last_session_id"]:
        srow = ctx.conn.execute(
            "SELECT agent_id, model FROM runs WHERE task_id = ? AND session_id = ? ORDER BY rowid DESC LIMIT 1;",
            (task_id, task["last_session_id"]),
        ).fetchone()
        if srow is not None:
            prev_reassign_model = srow["model"]
    reassign_session_id = (
        None
        if (agent_row["id"] != task["assignee_agent_id"] or model != prev_reassign_model or workspace_choice == "fresh")
        else task["last_session_id"]
    )
    ctx.conn.execute(
        """UPDATE tasks
           SET assignee_agent_id = ?, status = 'assigned', offered_at = NULL, profile = ?, model = ?, effort = ?,
               workspace_path = ?, last_session_id = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (agent_row["id"], profile, model, effort, new_ws_path, reassign_session_id, now, task_id),
    )

    reassign_payload: Dict[str, Any] = {
        "task_id": task_id,
        "project_id": project_id,
        "assignee": agent_row["name"],
        "old_assignee": old_assignee_name,
        "reason": reason,
        "profile": profile,
        "profile_explicit": bool(profile_arg) or (effective_profile_override is not None),
        "model": model,
        "effort": effort,
        "reuse_workspace": reuse_workspace,
    }
    if workspace_choice:
        reassign_payload["workspace"] = workspace_choice
        reassign_payload["workspace_path"] = new_ws_path

    record_event(
        conn=ctx.conn,
        type_="task.reassigned",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="assigned",
        reason=reason,
        payload=reassign_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.reassigned"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "assigned"}],
    )


def _requeue_launch_guard(ctx: HandlerContext, assignee_id: Optional[str]) -> Optional[sqlite3.Row]:
    """Assignee enabled + launch installation/evidence check for paths that requeue a task
    without AG (retry T19, reject T12 -> rework, which the scheduler requeues). Refused
    before any write or approval use."""
    if not assignee_id:
        return None
    agent = ctx.conn.execute(
        "SELECT a.*, p.name, p.disabled_at FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
        (assignee_id,),
    ).fetchone()
    if agent is None:
        return None
    if agent["disabled_at"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is disabled",
            details={"agent": agent["name"], "reason": "disabled"},
        )
    if agent["delivery"] != "launch":
        return agent
    from mesh.adapters.evidence import effective_launch

    adapter = get_adapter(agent["adapter"], testing_mode=ctx.testing_mode)
    if adapter.kind == "test_double" and not ctx.testing_mode:
        raise MeshError(
            code=c.TEST_DOUBLE_DISABLED,
            exit_code=c.EXIT_ADAPTER_UNAVAILABLE,
            message="Test double adapter 'fake' is only available when MESH_TESTING=1",
        )
    if not adapter.is_installed(binary_path=agent["binary_path"]):
        raise AdapterNotInstalled(agent["adapter"])
    ev = effective_launch(ctx.conn, adapter, ctx.testing_mode, agent["binary_path"])
    if not ev.ok:
        raise AdapterUnsupported(
            f"Adapter '{agent['adapter']}' is not enabled for launch"
            + (f" ({ev.reason})" if ev.reason else ""),
            milestone=ev.milestone or adapter.launch_milestone or "M3",
            reason=ev.reason,
        )
    return agent


def handle_task_retry(ctx: HandlerContext) -> HandlerResult:
    """Retry a failed or escalated task per plan §6.2 (T19)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    model_override = _optional_str_param(ctx.args.get("model"), "model")
    effort_override = _optional_str_param(ctx.args.get("effort"), "effort")
    reason = _optional_str_param(ctx.args.get("reason"), "reason")
    reuse_workspace = _optional_bool_param(ctx.args.get("reuse_workspace"), "reuse_workspace", default=False)

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.retry", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T19 is from failed, escalated -> queued
    check_task_transition(task["status"], "queued")

    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot retry task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )

    # Pre-public re-review P2: T19 queues directly, so it must apply the T4 readiness
    # guards itself; otherwise a prerequisite added while the task was failed is skipped.
    unfinished = [r["depends_on_task_id"] for r in ctx.conn.execute(
        """SELECT td.depends_on_task_id FROM task_deps td JOIN tasks d ON d.id = td.depends_on_task_id
           WHERE td.task_id = ? AND d.status NOT IN ('accepted', 'merged') ORDER BY 1;""",
        (task["id"],)).fetchall()]
    unfinished_projects = [r["depends_on_project_id"] for r in ctx.conn.execute(
        """SELECT pd.depends_on_project_id FROM project_deps pd JOIN projects d ON d.id = pd.depends_on_project_id
           WHERE pd.project_id = ? AND d.status != 'completed' ORDER BY 1;""",
        (project_id,)).fetchall()]
    if unfinished or unfinished_projects:
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=(f"Task '{task_id}' cannot be retried while prerequisites are unfinished; "
                     "reassign it (back to assigned) to queue it once they are done"),
            details={"task_id": task_id, "unfinished_tasks": unfinished,
                     "unfinished_projects": unfinished_projects},
        )

    # Disabled check + launch installation/evidence (ACC: N19), before any write or approval use
    assignee_id = task["assignee_agent_id"]
    ag_row = _requeue_launch_guard(ctx, assignee_id)

    # Validate model and effort BEFORE creating or consuming any approval
    _validate_profile_and_effort(profile_override=None, effort_override=effort_override)

    resolved_profile, need_retry, need_perm, will_gate_approval = _requeue_approval_gates(ctx, task, ag_row)

    resolved_model = model_override or task["model"]
    resolved_effort = effort_override or task["effort"]
    if ag_row:
        resolved_model, resolved_effort = _validate_model_and_effort(
            agent=ag_row,
            model_override=model_override,
            effort_override=effort_override,
            current_model=task["model"],
            current_effort=task["effort"],
            testing_mode=ctx.testing_mode,
            check_resolved_model=will_gate_approval,
            check_effort_before_stale_model=True,
        )

    # Fresh workspace calculation after an inbox lease expiry (§6.2 T19); defer DB writes
    cur_prev_run = ctx.conn.execute(
        "SELECT end_reason FROM runs WHERE task_id = ? ORDER BY rowid DESC LIMIT 1;",
        (task_id,),
    )
    prev_run = cur_prev_run.fetchone()
    prev_lease_expired = bool(prev_run and prev_run["end_reason"] == "lease_expired")

    workspace_choice = None
    new_ws_path = task["workspace_path"]
    if prev_lease_expired:
        workspace_choice = "reused" if reuse_workspace else "fresh"
        if workspace_choice == "fresh":
            next_attempt = max(1, task["attempt"]) + 1
            ws_root = get_workspaces_root(ctx.conn, ctx.home)
            new_ws_path = compute_task_workspace_path(ws_root, project_id, task_id, attempt=next_attempt)

    if ag_row and ag_row["delivery"] == "launch":
        preview_ws = new_ws_path or compute_task_workspace_path(
            get_workspaces_root(ctx.conn, ctx.home), project_id, task_id, attempt=task["attempt"] + 1
        )
        _check_spec_prompt_length(
            conn=ctx.conn,
            project_id=project_id,
            adapter_id=ag_row["adapter"],
            profile=resolved_profile,
            task_doc=dict(task),
            workspace=preview_ws,
            attempt=task["attempt"] + 1,
            max_attempts=task["max_attempts"],
            rework=read_rework_feedback(ctx.conn, task_id),
        )

    # Evaluate both retry_over_limit and permission_escalation (§7.4)
    assignee_name = ag_row["name"] if ag_row else task["assignee_agent_id"]
    _require_task_approval(
        conn=ctx.conn,
        ctx=ctx,
        op="task.retry",
        project_id=project_id,
        task_id=task_id,
        params={
            "task_id": task_id,
            "assignee": assignee_name,
            "profile": resolved_profile,
            "model": resolved_model,
            "effort": resolved_effort,
            "reason": reason,
            "reuse_workspace": reuse_workspace,
        },
        need_retry=need_retry,
        need_perm=need_perm,
        retry_summary=f"Retry of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
        perm_summary=f"Permission escalation to '{resolved_profile}' for retry of task '{task['title']}' on agent '{ag_row['name'] if ag_row else ''}'",
        compound_summary=(
            f"Retry of task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) "
            f"with permission escalation to '{resolved_profile}' requires user approval"
        ),
        retry_message=f"Task retry exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
        perm_message=f"Profile '{resolved_profile}' requires user approval (category: permission_escalation)",
        compound_message=f"Task retry exceeds max attempts ({task['max_attempts']}) and escalates profile to '{resolved_profile}'; requires direct user approval",
    )

    is_inbox = bool(ag_row and ag_row["delivery"] == "inbox")

    now = now_iso_utc()
    if workspace_choice == "fresh":
        remember_protected_path(ctx.conn, task["workspace_path"])
        remember_protected_path(ctx.conn, new_ws_path)

    if assignee_id:
        ctx.conn.execute(
            """UPDATE outbox
               SET status = 'superseded'
               WHERE recipient_id = ?
                 AND type = 'task.offered'
                 AND json_extract(payload_json, '$.task_id') = ?
                 AND status IN ('pending', 'delivered');""",
            (assignee_id, task_id),
        )

    offered_at = now if is_inbox else None
    prev_retry_model = task["model"] or (ag_row["default_model"] if ag_row else None)
    if task["last_session_id"]:
        srow = ctx.conn.execute(
            "SELECT model FROM runs WHERE task_id = ? AND session_id = ? ORDER BY rowid DESC LIMIT 1;",
            (task_id, task["last_session_id"]),
        ).fetchone()
        if srow is not None:
            prev_retry_model = srow["model"]
    retry_session_id = (
        None
        if (resolved_model != prev_retry_model or workspace_choice == "fresh")
        else task["last_session_id"]
    )
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'queued', queued_at = ?, offered_at = ?, model = ?, effort = ?, workspace_path = ?,
               last_session_id = ?, updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (now, offered_at, resolved_model, resolved_effort, new_ws_path, retry_session_id, now, task_id),
    )

    retry_payload: Dict[str, Any] = {
        "task_id": task_id,
        "project_id": project_id,
        "assignee": task["assignee_name"],
        "model": resolved_model,
        "effort": resolved_effort,
        "reason": reason,
        "reuse_workspace": reuse_workspace,
    }
    if workspace_choice:
        retry_payload["workspace"] = workspace_choice
        retry_payload["workspace_path"] = new_ws_path

    record_event(
        conn=ctx.conn,
        type_="task.retried",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="queued",
        reason=reason,
        payload=retry_payload,
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    # Check if inbox mode agent: if inbox delivery, record task.offered event for outbox delivery
    if is_inbox:
        record_event(
            conn=ctx.conn,
            type_="task.offered",
            actor_id="system",
            actor_kind="system",
            via="system",
            project_id=project_id,
            task_id=task_id,
            payload={
                "task_id": task_id,
                "project_id": project_id,
                "title": task["title"],
                "spec": task["spec"],
                "fence": task["fence_seq"] + 1,
                "assignee_id": assignee_id,
                "ack_timeout_s": ack_window_now(ctx.conn, assignee_id, testing_mode=ctx.testing_mode)[0],
                "last_rejection": last_rejection(ctx.conn, task_id),
            },
            request_id=ctx.request_id,
        )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.retried"],
        transitions=[{"entity": "task", "id": task_id, "from": task["status"], "to": "queued"}],
    )


def handle_task_escalate(ctx: HandlerContext) -> HandlerResult:
    """Escalate a failed task per plan §6.2 (T21)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")
    reason = _require_str_param(
        ctx.args.get("reason"), "reason", "Parameter --reason is required and non-empty for task escalation", strip=True
    )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.escalate", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # Check transition: T21 is from failed -> escalated
    check_task_transition(task["status"], "escalated")

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'escalated', updated_at = ?, version = version + 1
           WHERE id = ?;""",
        (now, task_id),
    )

    # Fan-out to user and origin agent is performed automatically by record_event -> _fan_out_event
    record_event(
        conn=ctx.conn,
        type_="task.escalated",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state="failed",
        to_state="escalated",
        reason=reason,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": reason,
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    updated_row = _get_task(ctx.conn, task_id)
    cur_deps = ctx.conn.execute("SELECT depends_on_task_id FROM task_deps WHERE task_id = ?;", (task_id,))
    depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]

    return HandlerResult(
        data=_format_task(updated_row, conn=ctx.conn, depends_on=depends_on),
        events=["task.escalated"],
        transitions=[{"entity": "task", "id": task_id, "from": "failed", "to": "escalated"}],
    )


async def handle_task_wait(server: Any, ctx: HandlerContext, reader: Any = None) -> HandlerResult:
    """Block until task reaches a target state or terminal condition per plan §13."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Missing required parameter task_id")

    # Validate task existence and scoping
    row = _get_task(ctx.conn, task_id)
    project_id = row["project_id"]
    check_authz(ctx.conn, "task.wait", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    until = ctx.args.get("until", "submitted")
    if not isinstance(until, str):
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Parameter 'until' must be a string",
            details={"param": "until", "got": type(until).__name__},
        )
    if until == "submitted":
        match_fn = lambda s, k: s == "submitted"
    elif until == "terminal":
        match_fn = lambda s, k: is_task_terminal(s, k)
    elif until.startswith("state:"):
        target_state = until.split(":", 1)[1]
        from mesh.core.states import TASK_STATES
        if target_state not in TASK_STATES:
            raise MeshError(
                code=c.INVALID_ARGUMENT,
                exit_code=c.EXIT_USAGE,
                message=f"Invalid task state in --until: '{target_state}'",
                details={"param": "until", "state": target_state},
            )
        match_fn = lambda s, k: s == target_state
    else:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid --until target: '{until}'. Must be 'submitted', 'terminal', or 'state:<s>'",
            details={"param": "until"},
        )

    timeout_val = ctx.args.get("timeout")
    if not timeout_val:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="--timeout is required for task wait",
            details={"param": "timeout"},
        )
    try:
        timeout_s = parse_duration(timeout_val)
    except ValueError:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Invalid duration format: '{timeout_val}'",
            details={"param": "timeout"},
        )
    if timeout_s <= 0:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Timeout must be greater than zero",
            details={"param": "timeout"},
        )

    start_mono = time.monotonic()
    end_mono = start_mono + timeout_s

    server.active_waits = getattr(server, "active_waits", 0) + 1
    try:
        while True:
            if reader is not None and reader.at_eof():
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message="Client disconnected during wait",
                    data={"items": []},
                )

            # Blind review P2: re-check the credential and access on every pass, so a revoke,
            # disable or lost project access during the wait ends it before data is returned.
            _recheck_wait_authz(ctx, "task.wait", project_id=project_id, task_id=task_id)
            row = _get_task(ctx.conn, task_id)
            if match_fn(row["status"], row["kind"]):
                cur_deps = ctx.conn.execute(
                    "SELECT depends_on_task_id FROM task_deps WHERE task_id = ? ORDER BY depends_on_task_id;",
                    (task_id,),
                )
                depends_on = [r["depends_on_task_id"] for r in cur_deps.fetchall()]
                return HandlerResult(data=_format_task(row, conn=ctx.conn, depends_on=depends_on))

            remaining = end_mono - time.monotonic()
            if remaining <= 0:
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message=f"Task wait timed out after {timeout_s}s",
                    details={"task": task_id, "until": until},
                    data={"items": []},
                )

            async with server.notify_cond:
                try:
                    await asyncio.wait_for(server.notify_cond.wait(), timeout=min(remaining, 0.5))
                except asyncio.TimeoutError:
                    pass

            if reader is not None and reader.at_eof():
                raise MeshError(
                    code=c.WAIT_TIMEOUT,
                    exit_code=c.EXIT_WAIT_TIMEOUT,
                    message="Client disconnected during wait",
                    data={"items": []},
                )
    finally:
        server.active_waits = max(0, getattr(server, "active_waits", 1) - 1)


def _get_current_boot_id(conn: sqlite3.Connection) -> str:
    cur = conn.execute("SELECT id FROM boots WHERE stopped_at IS NULL ORDER BY started_at DESC LIMIT 1;")
    row = cur.fetchone()
    if row:
        return row[0]
    cur2 = conn.execute("SELECT id FROM boots ORDER BY started_at DESC LIMIT 1;")
    row2 = cur2.fetchone()
    if row2:
        return row2[0]
    return generate_id("boot")


def _count_active_task_runs(conn: sqlite3.Connection, agent_id: Optional[str] = None) -> int:
    if agent_id is None:
        return conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('starting', 'running') AND kind IN ('task', 'external');"
        ).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM runs WHERE agent_id = ? AND status IN ('starting', 'running') AND kind IN ('task', 'external');",
        (agent_id,),
    ).fetchone()[0]


def _get_global_run_cap(conn: sqlite3.Connection) -> Optional[int]:
    set_row = conn.execute("SELECT value_json FROM settings WHERE key = 'scheduler.max_total_runs';").fetchone()
    if set_row and set_row["value_json"]:
        try:
            return int(json.loads(set_row["value_json"]))
        except Exception:
            return None
    return None


async def prepare_task_start_workspace(ctx: HandlerContext) -> None:
    """Inbox `task start` of a code task: create/reuse its worktree before T5 (m2-r4).

    Runs before the request transaction, with the git work in a worker thread so the
    event loop is never blocked by git (review m2-r3 finding 5). It only acts when
    the start would get past the handler's own checks for authz, inbox delivery, an
    active project, a queued task, free lease/concurrency slots, and allowed model/effort;
    otherwise it returns and the handler reports the error as before. On success the
    result is put on `ctx.prepared_workspace`.

    A git failure is T6 `workspace_error` (committed here, lead notified by the
    `task.failed` event) and the call returns rc 6 INVALID_TRANSITION with
    `details.reason:"workspace_error"` (Q13).
    """
    if ctx.dry_run or ctx.cmd != "task.start":
        return
    task_id = ctx.args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return
    session = ctx.args.get("session")
    if session is not None and (not isinstance(session, str) or "\x00" in session or len(session) > 256):
        return
    task = ctx.conn.execute(
        "SELECT t.*, a.delivery AS _delivery, pr.disabled_at AS _disabled_at, p.status AS _project_status "
        "FROM tasks t LEFT JOIN agents a ON t.assignee_agent_id = a.id "
        "LEFT JOIN principals pr ON t.assignee_agent_id = pr.id "
        "JOIN projects p ON t.project_id = p.id "
        "WHERE t.id = ?;",
        (task_id,),
    ).fetchone()
    if not task or task["kind"] != "code" or task["status"] != "queued":
        return
    if task["_delivery"] != "inbox" or task["_disabled_at"] is not None or task["_project_status"] != "active":
        return
    try:
        check_authz(ctx.conn, "task.start", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
    except MeshError:
        return
    if has_task_ws_reservation(ctx.conn, task_id, task["project_id"]):
        return
    if ctx.conn.execute("SELECT 1 FROM leases WHERE key = ?;", (f"task:{task_id}",)).fetchone():
        return
    ag = ctx.conn.execute(
        "SELECT a.*, p.name FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
        (task["assignee_agent_id"],),
    ).fetchone()
    if not ag:
        return
    # First start (branch is None) pins base_commit to current integration HEAD; skip preparation
    # while concurrency slots are full so a queued task is not locked to a stale base_commit
    # (retried tasks with an existing branch already fixed base_commit and keep m2-r4 prep reuse).
    if not task["branch"]:
        if _count_active_task_runs(ctx.conn, ag["id"]) >= (ag["max_concurrency"] or 1):
            return
        global_limit = _get_global_run_cap(ctx.conn)
        if global_limit is not None and _count_active_task_runs(ctx.conn) >= global_limit:
            return
    try:
        _validate_model_and_effort(
            agent=ag,
            model_override=None,
            effort_override=None,
            current_model=task["model"],
            current_effort=task["effort"],
            testing_mode=ctx.testing_mode,
            check_resolved_model=True,
        )
    except MeshError:
        return
    plan = plan_code_workspace(ctx.conn, ctx.home, task)
    if plan is None:
        return
    try:
        result = await asyncio.to_thread(run_code_workspace_plan, plan)
    except WorkspaceError as e:
        details = dict(e.details)
        details["message"] = str(e)
        err = {"task_id": task_id, "reason": "workspace_error", "failed": False}
        err.update({k: v for k, v in details.items() if k not in err})
        m_err = MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' cannot start: {e}",
            details=err,
        )
        idem = getattr(ctx, "idempotency", None)
        with transaction(ctx.conn):
            from mesh.daemon.handlers.inbox import recheck_tx_credential_and_authz
            from mesh.daemon.server import _lookup_live_idempotency, find_ws_reserved_idempotency

            recheck_tx_credential_and_authz(ctx, cmd="task.start")
            if idem and idem.get("key"):
                if (
                    _lookup_live_idempotency(ctx.conn, idem["principal_id"], idem["key"]) is not None
                    or find_ws_reserved_idempotency(ctx.conn, idem["principal_id"], idem["key"]) is not None
                ):
                    return
            current = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            if current is None:
                return
            check_authz(
                ctx.conn,
                "task.start",
                ctx.principal,
                ctx.actor_kind,
                ctx.via,
                project_id=current["project_id"],
                task_id=task_id,
            )
            if has_task_ws_reservation(ctx.conn, task_id, current["project_id"]):
                return
            cur_proj = ctx.conn.execute(
                "SELECT status FROM projects WHERE id = ?;", (current["project_id"],)
            ).fetchone()
            cur_ag = (
                ctx.conn.execute(
                    "SELECT a.delivery, p.disabled_at FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
                    (current["assignee_agent_id"],),
                ).fetchone()
                if current["assignee_agent_id"] else None
            )
            if (
                not cur_proj or cur_proj["status"] != "active"
                or not cur_ag or cur_ag["delivery"] != "inbox" or cur_ag["disabled_at"] is not None
            ):
                return
            failed = (
                current["fence_seq"] == task["fence_seq"]
                and current["assignee_agent_id"] == task["assignee_agent_id"]
                and fail_queued_task_t6(
                    ctx.conn, current, "workspace_error", details,
                    assignee_id=current["assignee_agent_id"], request_id=ctx.request_id,
                )
            )
            m_err.details["failed"] = failed
            if failed:
                ctx._committed_side_effect = True  # type: ignore[attr-defined]
                if idem and idem.get("key"):
                    from mesh.daemon.server import _idempotency_record, _store_idempotency_row

                    env_doc = m_err.to_envelope(request_id=ctx.request_id)
                    _store_idempotency_row(
                        ctx.conn,
                        idem["principal_id"],
                        idem["key"],
                        "task.start",
                        idem["request_hash"],
                        json.dumps(_idempotency_record("task.start", env_doc)),
                        m_err.exit_code,
                        ignore_conflict=True,
                    )
        raise m_err
    result = dict(result)
    result["task_id"] = task_id
    result["fence_seq"] = task["fence_seq"]
    ctx.prepared_workspace = result  # type: ignore[attr-defined]


def require_prepared_worktree(task_id: str, ws_path: str) -> None:
    """Full review r3 P1-3: a code task's worktree deleted (`workspace delete`) between
    its preparation and the start transaction fails the start; nothing is committed and
    no attempt is spent (the next start re-attaches the kept branch)."""
    if not os.path.lexists(os.path.join(ws_path, ".git")):
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' worktree disappeared before the start; retry `mesh task start`",
            details={"task_id": task_id, "reason": "workspace_missing"},
        )


def handle_task_start(ctx: HandlerContext) -> HandlerResult:
    """Start execution of an inbox-assigned task per plan §6.2 (T5), §11.1, §11.5."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required for task start")
    _validate_session_param(ctx.args.get("session"))

    # 1. Task exists
    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    # 2. Actor is assignee (or user direct)
    check_authz(ctx.conn, "task.start", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 3. Delivery is inbox (if launch mode -> 6 INVALID_TRANSITION, reason: "launch_mode")
    cur_ag = ctx.conn.execute(
        "SELECT a.*, p.name, p.disabled_at FROM agents a JOIN principals p ON a.id = p.id WHERE a.id = ?;",
        (task["assignee_agent_id"],),
    )
    agent = cur_ag.fetchone()
    if not agent or agent["delivery"] != "inbox":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message="Task start is only valid for inbox-delivery agents",
            details={"reason": "launch_mode"},
        )
    if agent["disabled_at"]:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message=f"Agent '{agent['name']}' is disabled",
            details={"agent": agent["name"], "reason": "disabled"},
        )

    # 4. Project active
    cur_p = ctx.conn.execute("SELECT status FROM projects WHERE id = ?;", (project_id,))
    proj = cur_p.fetchone()
    if not proj or proj["status"] != "active":
        raise MeshError(
            code=c.PROJECT_NOT_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Project '{project_id}' is not active",
            details={"project_id": project_id, "status": proj["status"] if proj else "unknown"},
        )

    # 5. Task status is queued
    if task["status"] != "queued":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' cannot start from status '{task['status']}' (expected 'queued')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "queued"},
        )

    res_key = has_task_ws_reservation(ctx.conn, task_id, project_id)
    if res_key:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Cannot start task '{task_id}': workspace operation '{res_key}' is in progress",
            details={"reservation": res_key, "task_id": task_id, "project_id": project_id},
        )

    # 6. Concurrency limits
    active_slots = _count_active_task_runs(ctx.conn, agent["id"])
    max_concurrency = agent["max_concurrency"] or 1
    if active_slots >= max_concurrency:
        raise MeshError(
            code=c.CONCURRENCY_LIMIT,
            exit_code=c.EXIT_BUSY,
            message=f"Agent '{agent['name']}' has reached concurrency limit ({active_slots}/{max_concurrency})",
            details={"agent": agent["name"], "active": active_slots, "max_concurrency": max_concurrency},
        )

    # Global cap (scheduler.max_total_runs)
    total_active_slots = None
    global_limit = _get_global_run_cap(ctx.conn)
    if global_limit is not None:
        total_active_slots = _count_active_task_runs(ctx.conn)
        if total_active_slots >= global_limit:
            raise MeshError(
                code=c.CONCURRENCY_LIMIT,
                exit_code=c.EXIT_BUSY,
                message=f"Global concurrency cap reached ({total_active_slots}/{global_limit})",
                details={"total_active": total_active_slots, "limit": global_limit},
            )

    # 7. Model & Effort allowed
    task_model, task_effort = _validate_model_and_effort(
        agent=agent,
        model_override=None,
        effort_override=None,
        current_model=task["model"],
        current_effort=task["effort"],
        testing_mode=ctx.testing_mode,
        check_resolved_model=True,
    )

    # 8. Lease is free
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.ALREADY_EXISTS,
            exit_code=c.EXIT_CONFLICT,
            message=f"Task '{task_id}' already holds an active lease",
            details={"task_id": task_id},
        )

    # Transaction allocations
    now = now_iso_utc()
    now_ts = time.time()
    lease_ttl_s = get_lease_ttl_s(ctx.conn, testing_mode=ctx.testing_mode)
    expires_at = now_iso_utc(now_ts + lease_ttl_s)

    fence = task["fence_seq"] + 1
    attempt = task["attempt"] + 1
    run_id = generate_id("run")
    boot_id = _get_current_boot_id(ctx.conn)
    run_dir = os.path.join(ctx.home, "runs", run_id)

    workspaces_root = get_workspaces_root(ctx.conn, ctx.home)
    ws_path = task["workspace_path"]
    if not ws_path:
        ws_path = compute_task_workspace_path(workspaces_root, project_id, task_id, attempt=attempt)

    # Code task (§10.3): the worktree was prepared before this transaction, off the
    # event loop, by prepare_task_start_workspace (m2-r4; review m2-r3 finding 5).
    branch = task["branch"]
    base_commit = task["base_commit"]
    if task["kind"] == "code" and not ctx.dry_run:
        prepared = getattr(ctx, "prepared_workspace", None)
        if not prepared or prepared.get("task_id") != task_id or prepared.get("fence_seq") != task["fence_seq"]:
            raise MeshError(
                code=c.INVALID_TRANSITION,
                exit_code=c.EXIT_CONFLICT,
                message=f"Task '{task_id}' workspace was not prepared; retry the start",
                details={"task_id": task_id, "reason": "workspace_not_prepared"},
            )
        ws_path = prepared["workspace_path"]
        branch = prepared["branch"]
        base_commit = prepared["base_commit"]
        require_prepared_worktree(task_id, ws_path)

    # Insert run
    ctx.conn.execute(
        """INSERT INTO runs (
            id, kind, task_id, project_id, agent_id, attempt, fence,
            adapter, model, effort, profile, status, boot_id, run_dir,
            argv_redacted_json, started_at
        ) VALUES (
            ?, 'external', ?, ?, ?, ?, ?,
            ?, ?, ?, ?, 'running', ?, ?,
            '[]', ?
        );""",
        (
            run_id, task_id, project_id, agent["id"], attempt, fence,
            agent["adapter"], task_model, task_effort, task["profile"],
            boot_id, run_dir, now,
        ),
    )

    # Insert lease
    ctx.conn.execute(
        """INSERT INTO leases (
            key, run_id, holder_kind, holder, agent_id, fence,
            slots_in_use_at_acquire, limit_at_acquire, total_at_acquire, total_limit_at_acquire,
            acquired_at, expires_at, heartbeat_at
        ) VALUES (
            ?, ?, 'agent', ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?
        );""",
        (
            f"task:{task_id}", run_id, agent["id"], agent["id"], fence,
            active_slots, max_concurrency, total_active_slots, global_limit,
            now, expires_at, now,
        ),
    )

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'running', fence_seq = ?, attempt = ?, workspace_path = ?, branch = ?, base_commit = ?,
               version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (fence, attempt, ws_path, branch, base_commit, now, task_id),
    )

    # Event task.started (queued -> running)
    evt = record_event(
        conn=ctx.conn,
        type_="task.started",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        from_state="queued",
        to_state="running",
        payload={"run_id": run_id, "fence": fence, "attempt": attempt,
                 **({"session": ctx.args["session"]} if ctx.args.get("session") else {})},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )
    remember_run_session(ctx.conn, run_id, ctx.args.get("session"))

    # Queue post-commit effect for directories
    ctx.effects.append(Effect("create_run_and_workspace_dirs", {"run_dir": run_dir, "workspace_dir": ws_path,
                                                               "worktree": bool(branch)}))

    data = {
        "run_id": run_id,
        "fence": fence,
        "lease_ttl_s": lease_ttl_s,
        "workspace": ws_path,
        "attempt": attempt,
    }
    if task["kind"] == "code":
        data["branch"] = branch
    data.update(_lease_timing(lease_ttl_s, expires_at, task["timeout_s"]))
    data["keepalive"] = (f"mesh task keepalive {task_id} --run {run_id} --fence {fence} "
                         f"--every {data['heartbeat_every_s']}")
    session = ctx.args.get("session")
    if session:
        data["session"] = session
        data["export"] = f"export MESH_SESSION={shlex.quote(session)}"
        data["keepalive"] = f"MESH_SESSION={shlex.quote(session)} {data['keepalive']}"
    return HandlerResult(
        data=data,
        transitions=[{"from": "queued", "to": "running", "task_id": task_id}],
        events=[evt],
    )


def _apply_run_token_scope(ctx: HandlerContext, task_id: str) -> Tuple[Optional[str], Optional[int]]:
    """Take run id and fence from the run token scope per plan §7.1, §7.2, §11.5.

    For an executor run token the scope's ``run_id`` and ``fence`` are
    authoritative: explicit ``--run``/``--fence`` may only repeat them, and a
    differing value is a stale write (exit 9 ``FENCE_MISMATCH``, no state change).
    """
    arg_run = _optional_str_param(ctx.args.get("run"), "run")
    arg_fence = _validate_fence_param(ctx.args.get("fence"))
    if ctx.via != "run_token":
        return arg_run, arg_fence
    scope = ctx.principal.get("scope") or {}
    if scope.get("task_id") and scope.get("task_id") != task_id:
        raise MeshError(
            code=c.SCOPE_MISMATCH,
            exit_code=c.EXIT_FORBIDDEN,
            message="Run token cannot write to a task outside its run scope",
            details={"scope_task_id": scope.get("task_id"), "task_id": task_id},
        )
    run_id = scope.get("run_id")
    fence = scope.get("fence")
    fence_differs = False
    if arg_fence is not None:
        try:
            fence_differs = int(arg_fence) != int(fence)
        except (TypeError, ValueError):
            fence_differs = True
    if (arg_run and arg_run != run_id) or fence_differs:
        raise MeshError(
            code=c.FENCE_MISMATCH,
            exit_code=c.EXIT_STALE,
            message="Presented run/fence differ from the run token's scope",
            details={"task_id": task_id, "scope_run_id": run_id, "scope_fence": fence,
                     "presented_run_id": arg_run, "presented_fence": arg_fence},
        )
    return run_id, fence


def handle_task_heartbeat(ctx: HandlerContext) -> HandlerResult:
    """Heartbeat an active task lease per plan §11.5."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    session = _validate_session_param(ctx.args.get("session"))
    run_id, fence = _apply_run_token_scope(ctx, task_id)

    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task heartbeat", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task heartbeat", details={"param": "fence"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.heartbeat", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    check_run_session(ctx.conn, task_id, run_id, session)

    # Update lease
    ttl_s = get_lease_ttl_s(ctx.conn, testing_mode=ctx.testing_mode)
    now_ts = time.time()
    now = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_s)

    if lease["holder_kind"] == "meshd":
        # A meshd-held (launch) lease is renewed by the supervisor only (§6.4).
        expires_at = lease["expires_at"]
        ctx.conn.execute("UPDATE leases SET heartbeat_at = ? WHERE key = ?;", (now, lease["key"]))
    else:
        ctx.conn.execute(
            "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE key = ?;",
            (expires_at, now, lease["key"]),
        )

    # Heartbeat writes no event (avoid event spam; recorded in impl notes)
    return HandlerResult(
        data={
            "task_id": task_id,
            "run_id": run_id,
            "fence": fence,
            "expires_at": expires_at,
        }
    )


def handle_task_progress(ctx: HandlerContext) -> HandlerResult:
    """Report task progress and heartbeat lease per plan §11.5."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    message = ctx.args.get("message")
    session = _validate_session_param(ctx.args.get("session"))
    run_id, fence = _apply_run_token_scope(ctx, task_id)

    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task progress", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task progress", details={"param": "fence"})
    if not isinstance(message, str) or not message or len(message.encode("utf-8")) > 4096:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--message is required and must be non-empty and <= 4 KB", details={"param": "message"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.progress", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    check_run_session(ctx.conn, task_id, run_id, session)

    # Update lease expiration
    ttl_s = get_lease_ttl_s(ctx.conn, testing_mode=ctx.testing_mode)
    now_ts = time.time()
    now = now_iso_utc(now_ts)
    expires_at = now_iso_utc(now_ts + ttl_s)

    if lease["holder_kind"] == "meshd":
        # A meshd-held (launch) lease is renewed by the supervisor only (§6.4).
        expires_at = lease["expires_at"]
        ctx.conn.execute("UPDATE leases SET heartbeat_at = ? WHERE key = ?;", (now, lease["key"]))
    else:
        ctx.conn.execute(
            "UPDATE leases SET expires_at = ?, heartbeat_at = ? WHERE key = ?;",
            (expires_at, now, lease["key"]),
        )

    evt = record_event(
        conn=ctx.conn,
        type_="task.progress",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        payload={"message": message, "fence": fence, "run_id": run_id},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "run_id": run_id,
            "fence": fence,
            "message": message,
            "expires_at": expires_at,
        },
        events=[evt],
    )


def _submit_git_info(ctx: HandlerContext, task: sqlite3.Row) -> Dict[str, Any]:
    prepared = getattr(ctx, "submit_git", None)
    if prepared and prepared.get("task_id") == task["id"] and prepared.get("fence_seq") == task["fence_seq"]:
        return prepared["git"]
    # Not collected before the transaction (should not happen for a running task):
    # never run git here on the event loop; record why instead (no submit is blocked).
    return {"branch": task["branch"], "base_commit": task["base_commit"],
            "error": {"step": "collect", "reason": "not_collected"}}


async def prepare_task_submit_git(ctx: HandlerContext) -> None:
    """Fenced `task submit` of a running code task: collect `deliverable.git` (§10.4)
    in a worker thread before the request transaction (m2-r4). Read-only git; the
    handler does all checks and uses the result only for the same task and fence.
    """
    if ctx.dry_run or ctx.cmd != "task.submit":
        return
    task_id = ctx.args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return
    task = ctx.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
    if not task or task["kind"] != "code" or task["status"] != "running":
        return
    # Authz and the fence first (review m2-r4 finding 4): no git process for a caller the
    # handler would refuse. Both checks are read-only; the handler repeats them in its
    # transaction and raises the real error there, so a MeshError here only skips git.
    try:
        run_id, fence = _apply_run_token_scope(ctx, task_id)
        if not run_id or fence is None:
            return
        check_authz(ctx.conn, "task.submit", ctx.principal, ctx.actor_kind, ctx.via,
                    project_id=task["project_id"], task_id=task_id)
        verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    except MeshError:
        return
    prow = ctx.conn.execute("SELECT repo_path FROM projects WHERE id = ?;", (task["project_id"],)).fetchone()
    git = await asyncio.to_thread(submission_git_info, task["workspace_path"], task["branch"], task["base_commit"],
                                  repo_path=(prow["repo_path"] if prow is not None else None))
    ctx.submit_git = {"task_id": task_id, "fence_seq": task["fence_seq"], "git": git}  # type: ignore[attr-defined]


def handle_task_submit(ctx: HandlerContext) -> HandlerResult:
    """Submit completed task deliverable per plan §6.2 (T7), §11.3."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    deliverable = ctx.args.get("deliverable")
    summary = _optional_str_param(ctx.args.get("summary"), "summary")
    session = _validate_session_param(ctx.args.get("session"))
    run_id, fence = _apply_run_token_scope(ctx, task_id)

    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task submit", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task submit", details={"param": "fence"})

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.submit", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence first per plan §11.5
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    check_run_session(ctx.conn, task_id, run_id, session)

    # Verify task state
    if task["status"] != "running":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot submit task '{task_id}' in status '{task['status']}' (expected 'running')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "running"},
        )

    # Deliverable parsing & validation
    if deliverable is None and summary is not None:
        deliverable_data = {"summary": summary}
    elif isinstance(deliverable, dict):
        deliverable_data = deliverable
    elif deliverable is None:
        deliverable_data = {}
    else:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Deliverable must be a JSON object", details={"param": "deliverable"})

    # Plan §15.2: deliverables are redacted before they are stored or echoed.
    deliverable_data = redact_value(deliverable_data)
    deliverable_raw = json.dumps(deliverable_data)
    if len(deliverable_raw.encode("utf-8")) > 200 * 1024:
        raise MeshError(
            c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Deliverable exceeds 200 KB limit",
            details={"param": "deliverable", "bytes": len(deliverable_raw.encode("utf-8")), "limit": 204800},
        )

    if task["kind"] == "code" and not ctx.dry_run:
        # §10.4 (m2-r4): Mesh records the worktree's git state; an agent-sent `git`
        # key is replaced. Collected off the event loop before this transaction.
        deliverable_data = dict(deliverable_data)
        deliverable_data["git"] = _submit_git_info(ctx, task)

    now = now_iso_utc()
    submitted_by = ctx.principal.get("name") or ctx.principal.get("id")

    deliverable_record = {
        "deliverable": deliverable_data,
        "submitted_by": submitted_by,
        "submitted_at": now,
    }

    if lease["holder_kind"] != "meshd":
        # Inbox mode releases at once: delete lease, set run exited
        ctx.conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task_id}",))
        ctx.conn.execute(
            """UPDATE runs
               SET status = 'exited', end_reason = 'submitted', ended_at = ?, result_json = ?
               WHERE id = ?;""",
            (now, json.dumps(deliverable_record), run_id),
        )
    # Launch mode (T7): the lease stays until the run's processes are gone (§11.3/§11.4);
    # the supervisor then closes the run with run.exited_after_submit.

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'submitted', deliverable_json = ?, version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (json.dumps(deliverable_record), now, task_id),
    )

    # Event task.submitted (running -> submitted) with payload fence
    evt = record_event(
        conn=ctx.conn,
        type_="task.submitted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        from_state="running",
        to_state="submitted",
        payload={
            "fence": fence,
            "attempt": task["attempt"],
            "submitted_by": submitted_by,
            "submitted_at": now,
            "deliverable": deliverable_data,
            **({"session": ctx.args["session"]} if ctx.args.get("session") else {}),
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "submitted",
            "run_id": run_id,
            "fence": fence,
            "deliverable": deliverable_data,
            "submitted_by": submitted_by,
            "submitted_at": now,
        },
        transitions=[{"from": "running", "to": "submitted", "task_id": task_id}],
        events=[evt],
    )


def handle_task_fail(ctx: HandlerContext) -> HandlerResult:
    """Report task execution failure per plan §6.2 (T8)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    reason = ctx.args.get("reason")
    session = _validate_session_param(ctx.args.get("session"))
    run_id, fence = _apply_run_token_scope(ctx, task_id)

    if not run_id:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--run is required for task fail", details={"param": "run"})
    if fence is None:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--fence is required for task fail", details={"param": "fence"})
    if not isinstance(reason, str):
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--reason is required and must be a non-empty string <= 4 KB", details={"param": "reason"})
    reason = reason.strip()
    if not reason or len(reason.encode("utf-8")) > 4096:
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="--reason is required and must be non-empty and <= 4 KB", details={"param": "reason"})
    reason = redact(reason)  # §15.1/§15.2: stored in failure_json, result_json and the event

    task = _get_task(ctx.conn, task_id)
    check_authz(ctx.conn, "task.fail", ctx.principal, ctx.actor_kind, ctx.via, project_id=task["project_id"], task_id=task_id)

    # Verify lease and fence first per plan §11.5 (stale writes report 9, not 6)
    lease = verify_task_lease_and_fence(ctx.conn, task_id, presented_run_id=run_id, presented_fence=fence)
    check_run_session(ctx.conn, task_id, run_id, session)

    if task["status"] != "running":
        raise MeshError(
            code=c.INVALID_TRANSITION,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot fail task '{task_id}' in status '{task['status']}' (expected 'running')",
            details={"task_id": task_id, "from_state": task["status"], "expected": "running"},
        )

    now = now_iso_utc()
    failure_record = {
        "reason": "reported",
        "details": {
            "message": reason,
            "run_id": run_id,
        },
    }

    if lease["holder_kind"] == "meshd":
        # Launch mode (T8): the failure is applied once the run's processes are gone
        # (§11.4 release condition); until then the task stays running with the lease.
        pending = {"pending_failure": {
            "message": reason, "reported_at": now,
            "actor_id": ctx.principal["id"], "actor_kind": ctx.actor_kind, "via": ctx.via,
        }}
        ctx.conn.execute("UPDATE runs SET result_json = ? WHERE id = ?;", (json.dumps(redact_value(pending)), run_id))
        evt = record_event(
            conn=ctx.conn,
            type_="task.fail_requested",
            actor_id=ctx.principal["id"],
            actor_kind=ctx.actor_kind,
            via=ctx.via,
            relayed_by=ctx.relayed_by,
            project_id=task["project_id"],
            task_id=task_id,
            run_id=run_id,
            reason="reported",
            payload={"reason": "reported", "message": reason, "fence": fence, "run_id": run_id},
            request_id=ctx.request_id,
            k_user=ctx.k_user,
        )
        return HandlerResult(
            data={"task_id": task_id, "status": "running", "run_id": run_id, "fence": fence, "failure_pending": True},
            events=[evt],
        )

    # Release lease and end run
    ctx.conn.execute("DELETE FROM leases WHERE key = ?;", (f"task:{task_id}",))
    ctx.conn.execute(
        """UPDATE runs
           SET status = 'exited', end_reason = 'failed', ended_at = ?, result_json = ?
           WHERE id = ?;""",
        (now, json.dumps(failure_record), run_id),
    )

    # Update task
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'failed', failure_json = ?, version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (json.dumps(failure_record), now, task_id),
    )

    # Event task.failed (running -> failed)
    evt = record_event(
        conn=ctx.conn,
        type_="task.failed",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=task["project_id"],
        task_id=task_id,
        run_id=run_id,
        from_state="running",
        to_state="failed",
        reason="reported",
        payload={
            "reason": "reported",
            "message": reason,
            "fence": fence,
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "failed",
            "reason": "reported",
            "message": reason,
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        transitions=[{"from": "running", "to": "failed", "task_id": task_id}],
        events=[evt],
    )


async def handle_task_merge(server: Any, ctx: HandlerContext) -> HandlerResult:
    """`task merge` (§10.5, T14–T17): dispatched to the merge pipeline in mesh/daemon/merge.py."""
    from mesh.daemon.merge import run_task_merge

    return await run_task_merge(server, ctx)


def handle_task_accept(ctx: HandlerContext) -> HandlerResult:
    """Accept a submitted task deliverable per plan §6.2 (T11)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    note = _optional_str_param(ctx.args.get("note"), "note")

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.accept", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 1. Transition check first (§6.2 T11): only submitted -> accepted
    check_task_transition(task["status"], "accepted")

    # 2. Check no active lease
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot accept task '{task_id}': active lease still held",
            details={"task_id": task_id},
        )

    # 3. Lead is not assignee (§6.2 T11, E9)
    if ctx.actor_kind != "user" and task["assignee_agent_id"] == ctx.principal["id"]:
        raise MeshError(
            code=c.LEAD_CANNOT_BE_ASSIGNEE,
            exit_code=c.EXIT_FORBIDDEN,
            message="Lead agent cannot accept its own assigned task",
            details={"task_id": task_id, "agent": ctx.principal.get("name")},
        )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'accepted', version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (now, task_id),
    )

    evt = record_event(
        conn=ctx.conn,
        type_="task.accepted",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="accepted",
        payload={"note": note} if note else {},
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    warnings: List[str] = []
    if task["kind"] == "code" and task["deliverable_json"]:
        try:
            drec = json.loads(task["deliverable_json"])
            dgit = ((drec.get("deliverable") or {}).get("git") or {}) if isinstance(drec, dict) else {}
            if isinstance(dgit, dict) and (dgit.get("error") or not dgit.get("head")):
                warnings.append("Code task deliverable.git has an error or missing head; verify branch before merge")
        except (TypeError, ValueError):
            pass

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "accepted",
            "note": note,
        },
        warnings=warnings,
        transitions=[{"from": task["status"], "to": "accepted", "task_id": task_id}],
        events=[evt],
    )


def handle_task_reject(ctx: HandlerContext) -> HandlerResult:
    """Reject a submitted task deliverable and request rework per plan §6.2 (T12)."""
    task_id = _require_str_param(ctx.args.get("task_id"), "task_id", "Task ID is required")
    reason = ctx.args.get("reason")

    if not reason or not isinstance(reason, str) or not reason.strip():
        raise MeshError(c.INVALID_ARGUMENT, exit_code=c.EXIT_USAGE, message="Rejection reason is required", details={"param": "reason"})
    if len(reason.encode("utf-8")) > 4096:
        raise MeshError(
            code=c.INVALID_ARGUMENT,
            exit_code=c.EXIT_USAGE,
            message="Rejection reason exceeds 4096 byte limit",
            details={"param": "reason", "bytes": len(reason.encode("utf-8")), "limit": 4096},
        )

    task = _get_task(ctx.conn, task_id)
    project_id = task["project_id"]

    check_authz(ctx.conn, "task.reject", ctx.principal, ctx.actor_kind, ctx.via, project_id=project_id, task_id=task_id)

    # 1. Transition check first (§6.2 T12): only submitted (or merge_failed) -> rework
    check_task_transition(task["status"], "rework")
    # rework is requeued by the scheduler (T13) without AG: same evidence check (ACC: N19)
    ag_row = _requeue_launch_guard(ctx, task["assignee_agent_id"])

    # 2. Check no active lease
    cur_l = ctx.conn.execute("SELECT key FROM leases WHERE key = ?;", (f"task:{task_id}",))
    if cur_l.fetchone():
        raise MeshError(
            code=c.RUN_STILL_ACTIVE,
            exit_code=c.EXIT_CONFLICT,
            message=f"Cannot reject task '{task_id}': active lease still held",
            details={"task_id": task_id},
        )

    # 3. Recheck assignee effort allowlist (and resolved model when approval-gated, plus launch prompt length)
    # BEFORE approval creation/consumption
    resolved_profile, need_retry, need_perm, will_gate_approval = _requeue_approval_gates(ctx, task, ag_row)
    if ag_row:
        _validate_model_and_effort(
            agent=ag_row,
            model_override=None,
            effort_override=None,
            current_model=task["model"],
            current_effort=task["effort"],
            testing_mode=ctx.testing_mode,
            check_resolved_model=will_gate_approval,
            check_effort_before_stale_model=True,
        )
        if ag_row["delivery"] == "launch":
            preview_ws = task["workspace_path"] or compute_task_workspace_path(
                get_workspaces_root(ctx.conn, ctx.home), project_id, task_id, attempt=task["attempt"] + 1
            )
            _check_spec_prompt_length(
                conn=ctx.conn,
                project_id=project_id,
                adapter_id=ag_row["adapter"],
                profile=resolved_profile,
                task_doc=dict(task),
                workspace=preview_ws,
                attempt=task["attempt"] + 1,
                max_attempts=task["max_attempts"],
                rework={"reason": reason, "previous_summary": deliverable_summary(task["deliverable_json"])},
            )

    # 4. Evaluate both retry_over_limit and permission_escalation (§7.4)
    assignee_name = ag_row["name"] if ag_row else task["assignee_agent_id"]
    _require_task_approval(
        conn=ctx.conn,
        ctx=ctx,
        op="task.reject",
        project_id=project_id,
        task_id=task_id,
        params={
            "task_id": task_id,
            "assignee": assignee_name,
            "profile": resolved_profile,
            "reason": reason,
        },
        need_retry=need_retry,
        need_perm=need_perm,
        retry_summary=f"Reject to rework for task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) requires user approval",
        perm_summary=f"Permission escalation to '{resolved_profile}' for reject to rework of task '{task['title']}' on agent '{assignee_name}'",
        compound_summary=(
            f"Reject to rework for task '{task['title']}' at attempt cap ({task['attempt']}/{task['max_attempts']}) "
            f"with permission escalation to '{resolved_profile}' requires user approval"
        ),
        retry_message=f"Task rejection to rework exceeds max attempts ({task['max_attempts']}) and requires user approval (category: retry_over_limit)",
        perm_message=f"Profile '{resolved_profile}' requires user approval (category: permission_escalation)",
        compound_message=f"Task rejection to rework exceeds max attempts ({task['max_attempts']}) and escalates profile to '{resolved_profile}'; requires direct user approval",
    )

    now = now_iso_utc()
    ctx.conn.execute(
        """UPDATE tasks
           SET status = 'rework', version = version + 1, updated_at = ?
           WHERE id = ?;""",
        (now, task_id),
    )

    evt = record_event(
        conn=ctx.conn,
        type_="task.rejected",
        actor_id=ctx.principal["id"],
        actor_kind=ctx.actor_kind,
        via=ctx.via,
        relayed_by=ctx.relayed_by,
        project_id=project_id,
        task_id=task_id,
        from_state=task["status"],
        to_state="rework",
        reason=reason,
        payload={
            "task_id": task_id,
            "project_id": project_id,
            "reason": reason,
            "rejected_by": "user" if ctx.actor_kind == "user" else ctx.principal.get("name"),
            "assignee_id": task["assignee_agent_id"],
            "attempt": task["attempt"],
            "max_attempts": task["max_attempts"],
        },
        request_id=ctx.request_id,
        k_user=ctx.k_user,
    )

    return HandlerResult(
        data={
            "task_id": task_id,
            "status": "rework",
            "reason": reason,
        },
        transitions=[{"from": task["status"], "to": "rework", "task_id": task_id}],
        events=[evt],
    )
